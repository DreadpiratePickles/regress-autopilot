"""The tuning proposal: the `autopilot.toml` diff this month's evidence implies.

A proposal is not an edit. It is a file that says what edit the rules would make,
what evidence they made it from, and that a named human has not yet approved it.
`status` starts at `awaiting_human_approval` and only `apply.py` — behind an
explicit `--approve` and an explicit `--approved-by` — ever moves it.

**Why a proposal at all, rather than tuning automatically.** The rulebook's rule
is that humans approve anything that spends, and every change in here changes
what the next month costs: raising a policy floor sends traffic to a more
expensive rung, raising `sample_percent` buys more validation calls. A tool that
adjusted its own routing in response to its own judge would also be grading its
own work and then acting on the grade, which is the one arrangement stage 03's
contract forbids outright.

**Why the changes are integers with a current value.** A proposal is applied by
`apply.py` only if the config still holds the `current_value` the proposal was
built against. Somebody who edits `autopilot.toml` between generating a proposal
and approving it gets a refusal naming both values, rather than a silent
overwrite of the change they just made.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .model import MonthReport

PROPOSAL_SCHEMA_VERSION = 1

STATUS_AWAITING = "awaiting_human_approval"
STATUS_APPLIED = "applied"
STATUSES = (STATUS_AWAITING, STATUS_APPLIED)

PROPOSAL_JSON_FILENAME = "proposal.json"
PROPOSAL_MD_FILENAME = "proposal.md"


class ProposalError(Exception):
    """A proposal file is malformed, already applied, or no longer matches the config."""


@dataclass(frozen=True)
class ProposedChange:
    """One key in `autopilot.toml`, its current value, and what to move it to."""

    section: str
    key: str
    current_value: int
    proposed_value: int
    evidence: str
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("section", "key", "evidence"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProposalError(f"proposed change {name} must be a non-empty string")
        for name in ("current_value", "proposed_value"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProposalError(f"proposed change {name} must be an integer")
        if self.current_value == self.proposed_value:
            raise ProposalError(
                f"[{self.section}] {self.key} is already {self.current_value}; a "
                "proposal that changes nothing is not a proposal"
            )
        if not self.rule_ids:
            raise ProposalError(
                f"[{self.section}] {self.key} names no rule; every change must say "
                "which rule produced it"
            )

    @property
    def label(self) -> str:
        return f"[{self.section}] {self.key}"

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rule_ids"] = list(self.rule_ids)
        return payload


@dataclass(frozen=True)
class TuningProposal:
    """Every change one month's report recommends, and who has signed for them."""

    month: str
    generated_utc: str
    config_path: str
    changes: tuple[ProposedChange, ...]
    synthetic: bool = False
    status: str = field(default=STATUS_AWAITING)
    approved_by: str | None = None
    approved_utc: str | None = None
    applied_utc: str | None = None
    schema_version: int = field(default=PROPOSAL_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ProposalError(
                f"status must be one of {list(STATUSES)}, got {self.status!r}"
            )
        for name in ("month", "generated_utc", "config_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProposalError(f"{name} must be a non-empty string")
        if self.status == STATUS_APPLIED and not (self.approved_by and self.applied_utc):
            raise ProposalError(
                "an applied proposal must record who approved it and when it was "
                "applied; an unattributed change to what a system spends is not an "
                "approval"
            )

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "month": self.month,
            "generated_utc": self.generated_utc,
            "config_path": self.config_path,
            "synthetic": self.synthetic,
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_utc": self.approved_utc,
            "applied_utc": self.applied_utc,
            "changes": [item.to_json_dict() for item in self.changes],
        }


def build_proposal(
    report: MonthReport,
    *,
    config_path: str,
    generated_utc: str,
    synthetic: bool = False,
) -> TuningProposal:
    """Fold a report's recommendations into one change per configuration key.

    Two rules fire on the same key when, for example, a tier both regrets and
    falls back. They are merged rather than emitted twice, and the merge takes
    the **higher** proposed value: raising a policy floor is the conservative
    direction — it spends more and risks less — so a recommendation to raise
    always wins over one to lower.
    """
    merged: dict[tuple[str, str], ProposedChange] = {}
    for item in report.config_changes:
        # `config_changes` filters on `is_config_change`, which is only true when
        # all four fields are set — `Recommendation` refuses a half-specified one.
        key = (str(item.config_section), str(item.config_key))
        candidate = ProposedChange(
            section=str(item.config_section),
            key=str(item.config_key),
            current_value=int(item.current_value or 0),
            proposed_value=int(item.proposed_value or 0),
            evidence=f"{item.rule_id} ({item.subject}): {item.evidence}",
            rule_ids=(item.rule_id,),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        merged[key] = ProposedChange(
            section=existing.section,
            key=existing.key,
            current_value=existing.current_value,
            proposed_value=max(existing.proposed_value, candidate.proposed_value),
            evidence=f"{existing.evidence}; {candidate.evidence}",
            rule_ids=tuple(sorted(set(existing.rule_ids + candidate.rule_ids))),
        )

    return TuningProposal(
        month=report.month,
        generated_utc=generated_utc,
        config_path=config_path,
        synthetic=synthetic,
        changes=tuple(merged[key] for key in sorted(merged)),
    )


def write_proposal(path: Path | str, proposal: TuningProposal) -> Path:
    """Write `proposal.json`, creating the month directory if it does not exist."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(proposal.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def proposal_from_json_dict(payload: object) -> TuningProposal:
    """Rebuild a proposal read back from disk, validating it at the boundary.

    Raises:
        ProposalError: not an object, unknown or missing fields, or a schema
            version this build does not understand. A proposal is a file a human
            may have edited, so it is checked as strictly as a ledger row.
    """
    if not isinstance(payload, dict):
        raise ProposalError(f"proposal must be a JSON object, got {type(payload).__name__}")

    known = {
        "schema_version",
        "month",
        "generated_utc",
        "config_path",
        "synthetic",
        "status",
        "approved_by",
        "approved_utc",
        "applied_utc",
        "changes",
    }
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ProposalError(f"proposal has unknown field(s): {', '.join(unknown)}")

    version = payload.get("schema_version")
    if version != PROPOSAL_SCHEMA_VERSION:
        raise ProposalError(
            f"proposal has schema_version {version!r}; this build understands "
            f"{PROPOSAL_SCHEMA_VERSION}"
        )

    missing = sorted(known - {"schema_version"} - set(payload))
    if missing:
        raise ProposalError(f"proposal is missing field(s): {', '.join(missing)}")

    entries = payload["changes"]
    if not isinstance(entries, list):
        raise ProposalError("proposal 'changes' must be a list")

    changes: list[ProposedChange] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ProposalError(f"proposal change #{index} must be a JSON object")
        change_keys = set(ProposedChange.__dataclass_fields__)
        entry_unknown = sorted(set(entry) - change_keys)
        if entry_unknown:
            raise ProposalError(
                f"proposal change #{index} has unknown field(s): {', '.join(entry_unknown)}"
            )
        entry_missing = sorted(change_keys - set(entry))
        if entry_missing:
            raise ProposalError(
                f"proposal change #{index} is missing field(s): {', '.join(entry_missing)}"
            )
        rule_ids = entry["rule_ids"]
        if not isinstance(rule_ids, list) or not all(isinstance(item, str) for item in rule_ids):
            raise ProposalError(f"proposal change #{index} 'rule_ids' must be a list of strings")
        changes.append(ProposedChange(**{**entry, "rule_ids": tuple(rule_ids)}))

    return TuningProposal(
        **{
            key: value
            for key, value in payload.items()
            if key not in {"changes", "schema_version"}
        },
        changes=tuple(changes),
    )


def load_proposal(path: Path | str) -> TuningProposal:
    """Read and validate a proposal file.

    Raises:
        ProposalError: the file is missing, is not JSON, or is not a proposal.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProposalError(f"proposal file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalError(f"proposal file could not be read: {path} ({exc})") from exc
    return proposal_from_json_dict(payload)
