"""The bodies of `autopilot report` and `autopilot apply-proposal`.

Kept out of `cli.py` so the command line stays a wiring layer, and kept out of
`build.py` so the arithmetic stays pure and file-free.

`report` reads three inputs and writes four artifacts:

    ledger/<month>.jsonl          →  report/<month>/report.md
    validate/<month>/regret.json  →  report/<month>/report.json
    validate/<month>/verdicts.jsonl  →  report/<month>/proposal.md
                                     →  report/<month>/proposal.json

It calls no model — not with `--dry-run`, not without it. `--dry-run` on this
command means something different from the other three: there is nothing to
stub, so the flag says *the inputs came from a dry run*, and every artifact it
writes carries the synthetic banner. It is a labelling flag, and mislabelling is
the failure it prevents.

`apply-proposal` is the only command in this package that writes to
`autopilot.toml`, and it refuses to do so without `--approve` and a named
approver.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..config_file import AutopilotConfig
from ..ledger.store import LedgerStore, utc_timestamp
from ..money import format_micro_usd
from ..validate.report import load_report
from ..validate.verdicts import VerdictStore
from .apply import apply_and_record
from .build import build_report
from .model import EXIT_SAFE, MonthReport
from .proposal import (
    PROPOSAL_JSON_FILENAME,
    PROPOSAL_MD_FILENAME,
    TuningProposal,
    build_proposal,
    load_proposal,
    write_proposal,
)
from .render import (
    REPORT_JSON_FILENAME,
    REPORT_MD_FILENAME,
    render_proposal,
    render_report,
)
from .rules import recommend


def month_directory(config: AutopilotConfig, *, month: str, out_dir: str | None) -> Path:
    """Where this month's four artifacts land."""
    root = Path(out_dir) if out_dir else config.report.directory
    return root / month


def write_artifacts(
    directory: Path, report: MonthReport, proposal: TuningProposal
) -> dict[str, Path]:
    """Write `report.md`, `report.json`, `proposal.md` and `proposal.json`."""
    directory.mkdir(parents=True, exist_ok=True)
    report_md = directory / REPORT_MD_FILENAME
    report_json = directory / REPORT_JSON_FILENAME
    proposal_md = directory / PROPOSAL_MD_FILENAME
    proposal_json = directory / PROPOSAL_JSON_FILENAME

    report_md.write_text(render_report(report), encoding="utf-8")
    report_json.write_text(
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    proposal_md.write_text(render_proposal(proposal), encoding="utf-8")
    write_proposal(proposal_json, proposal)
    return {
        "report_md": report_md,
        "report_json": report_json,
        "proposal_md": proposal_md,
        "proposal_json": proposal_json,
    }


def run_report(
    config: AutopilotConfig,
    *,
    config_path: str,
    month: str,
    out_dir: str | None,
    dry_run: bool,
    echo: Callable[[str], None],
    generated_utc: str | None = None,
) -> int:
    """Render one month. Returns the verdict's exit code."""
    generated = generated_utc or utc_timestamp()

    ledger = LedgerStore(config.ledger.directory)
    verdicts_store = VerdictStore(config.validate.directory)
    rows = ledger.read_month(month)
    regret = load_report(verdicts_store.regret_path(month))
    verdicts = verdicts_store.read_month(month)

    report = build_report(
        rows,
        regret,
        verdicts,
        config=config,
        month=month,
        generated_utc=generated,
        synthetic=dry_run,
        config_path=config_path,
    )
    report = replace(report, recommendations=recommend(report, config))
    proposal = build_proposal(
        report, config_path=config_path, generated_utc=generated, synthetic=dry_run
    )

    written = write_artifacts(
        month_directory(config, month=month, out_dir=out_dir), report, proposal
    )

    if dry_run:
        echo(
            "Dry run: the inputs came from fakes, so every verdict below is a "
            "constant and every cost is synthetic. The artifacts say so too."
        )
    if not config.ladder.prices_verified:
        echo("WARNING: prices_verified is false; every amount below is a placeholder.")

    echo(f"Report for {month}")
    echo(f"  requests           {report.spend.row_count}")
    echo(f"  spend              {format_micro_usd(report.spend.spend_micro_usd)}")
    echo(
        f"  saving             {format_micro_usd(report.spend.saving_micro_usd)} "
        f"({report.spend.saving_percent}%)"
    )
    echo(
        f"  validation cost    "
        f"{format_micro_usd(report.quality.validation_cost_micro_usd)}"
    )
    echo(
        f"  inspected          {report.quality.inspected_percent}% of "
        f"{report.quality.cheap_routed_count} cheap-routed request(s)"
    )
    echo("")
    echo(f"Verdict: {report.verdict}")
    echo("")
    echo(report.verdict_reason)
    echo("")
    for item in report.recommendations:
        echo(f"  [{item.rule_id}] {item.subject}")
        echo(f"      evidence: {item.evidence}")
        echo(f"      action:   {item.action}")
    if not report.recommendations:
        echo("  No recommendations: nothing crossed a rule this month.")
    echo("")
    for label in ("report_md", "report_json", "proposal_md", "proposal_json"):
        echo(f"  wrote {written[label]}")
    if proposal.is_empty:
        echo("  The proposal holds no changes; nothing to approve.")
    else:
        echo(
            f"  The proposal holds {len(proposal.changes)} change(s), status "
            f"{proposal.status}. This tool will not apply them."
        )
    return report.exit_code


def run_apply_proposal(
    *,
    config_path: str,
    proposal_path: str,
    approve: bool,
    approved_by: str,
    echo: Callable[[str], None],
    now_utc: str | None = None,
) -> int:
    """Apply an approved proposal. Returns 0, or raises `ProposalError`."""
    proposal = load_proposal(proposal_path)
    applied = apply_and_record(
        proposal_path,
        proposal,
        config_path=config_path,
        approve=approve,
        approved_by=approved_by,
        now_utc=now_utc or utc_timestamp(),
    )
    echo(f"Applied {len(applied.changes)} change(s) to {config_path}:")
    for change in applied.changes:
        echo(f"  {change.label}  {change.current_value} -> {change.proposed_value}")
    echo(
        f"Approved by {applied.approved_by!r} at {applied.applied_utc}; recorded in "
        f"{proposal_path}."
    )
    echo(
        "This changes what future requests cost. Re-run `autopilot report` next "
        "month to see whether the evidence agreed."
    )
    return EXIT_SAFE
