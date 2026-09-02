"""Apply an approved proposal to `autopilot.toml`. The only writer of that file.

Everything about this module is deliberately narrow, because it is the one place
in the system where a machine changes what the system is allowed to spend.

  - **It refuses without `--approve` and `--approved-by`.** Both, every time. An
    unattributed change to a budget-bearing file is not an approval, and the
    proposal records the name and the timestamp so the next reader can see who
    signed for it.
  - **It edits two things and nothing else.** `[policy] <TIER>` and `[validate]
    sample_percent`, both integers. A proposal naming anything else is refused
    rather than attempted: a generic TOML writer is a much larger blast radius
    than this stage has any business holding.
  - **It edits lines, not documents.** `tomllib` reads TOML and writes nothing,
    and round-tripping through a writer would strip every comment in a file whose
    comments are half its value. So one scalar on one line is replaced, in place,
    with its trailing comment preserved — and the result is re-loaded through
    `load_config` before it replaces the original, so a botched edit fails
    loudly instead of leaving an unparseable config behind.
  - **It refuses a stale proposal.** If the file no longer holds the
    `current_value` the proposal was built against, somebody has edited it since;
    the refusal names both values rather than overwriting their change.

Applying is idempotent in the only sense that matters: a proposal already marked
`applied` is refused, so re-running the command cannot double-apply a rung.
"""

import os
import re
import tomllib
from pathlib import Path

from ..classify.scorer import Tier
from ..config_file import AutopilotConfig, ConfigFileError, load_config
from .proposal import (
    STATUS_APPLIED,
    STATUS_AWAITING,
    ProposalError,
    ProposedChange,
    TuningProposal,
    write_proposal,
)

POLICY_SECTION = "policy"
VALIDATE_SECTION = "validate"
SAMPLE_PERCENT_KEY = "sample_percent"

APPLICABLE = "[policy] <TIER> and [validate] sample_percent"
"""What this module is allowed to change, quoted verbatim in its refusals."""

_SECTION_HEADER = re.compile(r"^\s*\[\[?(?P<name>[^\]]+)\]\]?\s*$")


def _key_pattern(key: str) -> re.Pattern[str]:
    """`key = <value>` with any spacing, capturing the value and any comment."""
    return re.compile(
        rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)(?P<value>[^#\n]*?)(?P<suffix>\s*(?:#.*)?)$"
    )


def current_value(config: AutopilotConfig, change: ProposedChange) -> int:
    """What the loaded configuration says today for the key this change names.

    Raises:
        ProposalError: the change names a key this module may not apply.
    """
    if change.section == POLICY_SECTION:
        try:
            tier = Tier(change.key)
        except ValueError as exc:
            raise ProposalError(
                f"[policy] {change.key!r} is not a known tier, so it cannot be applied"
            ) from exc
        return config.policy.floor_for(tier)
    if change.section == VALIDATE_SECTION and change.key == SAMPLE_PERCENT_KEY:
        return config.validate.sample_percent
    raise ProposalError(
        f"this build can only apply {APPLICABLE}; the proposal names "
        f"[{change.section}] {change.key}. Make that change by hand, in a reviewed diff."
    )


def replace_scalar(text: str, *, section: str, key: str, value: int) -> str:
    """Replace one `key = <int>` inside one section, keeping comments and layout.

    Raises:
        ProposalError: the section is absent, the key is absent from it, or the
            key appears more than once. Every one of those is a file this module
            declines to guess about.
    """
    lines = text.splitlines(keepends=True)
    pattern = _key_pattern(key)
    here = ""
    hits: list[int] = []

    for index, line in enumerate(lines):
        header = _SECTION_HEADER.match(line)
        if header:
            here = header.group("name").strip()
            continue
        if here == section and pattern.match(line):
            hits.append(index)

    if not hits:
        raise ProposalError(
            f"could not find '{key}' inside [{section}]: the configuration file does "
            "not hold the key this proposal was built against"
        )
    if len(hits) > 1:
        raise ProposalError(
            f"'{key}' appears {len(hits)} times inside [{section}]; refusing to guess "
            "which one to change"
        )

    index = hits[0]
    match = pattern.match(lines[index])
    if match is None:  # pragma: no cover - the index came from the same pattern
        raise ProposalError(f"could not rewrite '{key}' inside [{section}]")
    newline = "\n" if lines[index].endswith("\n") else ""
    lines[index] = (
        f"{match.group('prefix')}{value}{match.group('suffix').rstrip()}{newline}"
    )
    return "".join(lines)


def _rewritten(text: str, changes: tuple[ProposedChange, ...]) -> str:
    for change in changes:
        text = replace_scalar(
            text, section=change.section, key=change.key, value=change.proposed_value
        )
    return text


def _verify_written(path: Path, changes: tuple[ProposedChange, ...]) -> None:
    """Re-read the edited file and check each key really holds its new value."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    for change in changes:
        table = document
        for part in change.section.split("."):
            table = table.get(part, {}) if isinstance(table, dict) else {}
        written = table.get(change.key) if isinstance(table, dict) else None
        if written != change.proposed_value:
            raise ProposalError(
                f"after rewriting, [{change.section}] {change.key} reads {written!r} "
                f"rather than {change.proposed_value}. The file was not changed."
            )


def apply_proposal(
    proposal: TuningProposal,
    *,
    config_path: Path | str,
    approve: bool,
    approved_by: str,
    now_utc: str,
) -> TuningProposal:
    """Apply a proposal to `config_path` and return it, marked applied.

    The caller writes the returned proposal back to disk; this function does not,
    so a caller can show the result before persisting it.

    Raises:
        ProposalError: `--approve` is absent, the approver is unnamed, the
            proposal is empty or already applied, it was built against a
            different configuration file, the file has moved on since, or the
            rewritten file will not load.
    """
    if not approve:
        raise ProposalError(
            "refusing to change autopilot.toml without --approve. This file decides "
            "what the system is allowed to spend; a proposal is a suggestion until a "
            "person says otherwise."
        )
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ProposalError(
            "--approved-by must name the person approving this change. An "
            "unattributed change to what a system spends is not an approval."
        )
    if proposal.status == STATUS_APPLIED:
        raise ProposalError(
            f"this proposal was already applied by {proposal.approved_by!r} at "
            f"{proposal.applied_utc}. Generate a fresh report rather than applying "
            "the same change twice."
        )
    if proposal.status != STATUS_AWAITING:
        raise ProposalError(f"unexpected proposal status {proposal.status!r}")
    if proposal.is_empty:
        raise ProposalError(
            "this proposal holds no changes: the month's evidence recommended nothing "
            "that a configuration file can express."
        )

    config_path = Path(config_path)
    if Path(proposal.config_path).resolve() != config_path.resolve():
        raise ProposalError(
            f"this proposal was built against {proposal.config_path}, not "
            f"{config_path}. Applying it to a different configuration would change a "
            "file the evidence never described."
        )

    config = load_config(config_path)
    for change in proposal.changes:
        live = current_value(config, change)
        if live != change.current_value:
            raise ProposalError(
                f"{change.label} is {live} in {config_path}, but this proposal was "
                f"built when it was {change.current_value}. Somebody has changed the "
                "file since; re-run `autopilot report` and read the new proposal."
            )

    original = config_path.read_text(encoding="utf-8")
    candidate = config_path.with_name(config_path.name + ".proposed")
    candidate.write_text(_rewritten(original, proposal.changes), encoding="utf-8")
    try:
        _verify_written(candidate, proposal.changes)
        load_config(candidate)
    except (ConfigFileError, tomllib.TOMLDecodeError, OSError) as exc:
        candidate.unlink(missing_ok=True)
        raise ProposalError(
            f"the rewritten configuration would not load ({exc}). {config_path} is "
            "unchanged."
        ) from exc
    except ProposalError:
        candidate.unlink(missing_ok=True)
        raise
    os.replace(candidate, config_path)

    return TuningProposal(
        month=proposal.month,
        generated_utc=proposal.generated_utc,
        config_path=proposal.config_path,
        changes=proposal.changes,
        synthetic=proposal.synthetic,
        status=STATUS_APPLIED,
        approved_by=approved_by.strip(),
        approved_utc=now_utc,
        applied_utc=now_utc,
    )


def apply_and_record(
    proposal_path: Path | str,
    proposal: TuningProposal,
    *,
    config_path: Path | str,
    approve: bool,
    approved_by: str,
    now_utc: str,
) -> TuningProposal:
    """Apply, then write the approval back into the proposal file itself.

    The record of who approved a change lives next to the change, not in a log
    somewhere else, so the two cannot be separated by a copy.
    """
    applied = apply_proposal(
        proposal,
        config_path=config_path,
        approve=approve,
        approved_by=approved_by,
        now_utc=now_utc,
    )
    write_proposal(proposal_path, applied)
    return applied
