"""Markdown for `report.md` and `proposal.md`. Presentation only.

This module computes nothing and decides nothing. Every number it prints was
already on the `MonthReport` or the `TuningProposal` it was handed, and every
verdict sentence was written by `validate.report.tier_verdict_line` — the same
function `ledger summary` calls. If a figure looks wrong here, the bug is in
`build.py` or in stage 03, never in this file. That separation is what makes the
report reviewable: a renderer that also did arithmetic could disagree with the
JSON beside it.

The synthetic banner is the first line of the file when it is the first line at
all, rather than a footnote. These documents get committed under
`docs/examples/`, pasted into chat threads, and skimmed from the top; a caveat at
the bottom of such a document is a caveat nobody reads.
"""

from ..money import format_micro_usd
from .model import MonthReport, Recommendation
from .proposal import STATUS_AWAITING, TuningProposal

REPORT_MD_FILENAME = "report.md"
REPORT_JSON_FILENAME = "report.json"

BANNER = (
    "> **SYNTHETIC — produced by a dry run.** Every verdict below is a canned "
    "constant from an in-memory fake, not a model judgement, and every cost comes "
    "from fixed synthetic token counts. These numbers exercise the arithmetic and "
    "the pipeline; they measure nothing."
)

NO_RATIO = -1


def _money(amount: int) -> str:
    return format_micro_usd(amount)


def _counts_table(title: str, pairs: dict[str, int], *, unit: str) -> list[str]:
    if not pairs:
        return [f"_{title}: none._", ""]
    lines = [f"| {title} | {unit} |", "|---|---:|"]
    lines += [f"| `{key}` | {value} |" for key, value in pairs.items()]
    return [*lines, ""]


def _money_table(title: str, pairs: dict[str, int]) -> list[str]:
    if not pairs:
        return [f"_{title}: none._", ""]
    lines = [f"| {title} | spend |", "|---|---:|"]
    lines += [f"| `{key}` | {_money(value)} |" for key, value in pairs.items()]
    return [*lines, ""]


def _spend_section(report: MonthReport) -> list[str]:
    spend = report.spend
    lines = [
        "## Spend",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Requests recorded | {spend.row_count} |",
        f"| Spend | {_money(spend.spend_micro_usd)} |",
        f"| If every request had gone to the top rung | "
        f"{_money(spend.counterfactual_micro_usd)} |",
        f"| Saving | {_money(spend.saving_micro_usd)} ({spend.saving_percent}%) |",
        f"| Tokens | {spend.input_tokens} in / {spend.output_tokens} out |",
        "",
        "The saving is a subtraction, not a claim: every row carries what its own "
        "token counts would have cost on the top rung, so the figure is checkable "
        "row by row. It is an approximation in one direction only — the top model "
        "would have produced a different number of output tokens for the same "
        "prompt — and it says nothing at all about quality. That is the Quality "
        "section below.",
        "",
    ]
    lines += _money_table("Spend by team", spend.spend_by_team)
    lines += _money_table("Spend by model", spend.spend_by_model)
    lines += _money_table("Spend by tier", spend.spend_by_tier)
    lines += _counts_table("Requests by tier", spend.requests_by_tier, unit="requests")
    lines += _counts_table("Requests by status", spend.requests_by_status, unit="requests")
    lines += [
        "### Outcomes",
        "",
        "| Outcome | Count |",
        "|---|---:|",
        f"| Tried more than one rung | {spend.fallback_count} |",
        f"| …of which a dearer rung answered | {sum(spend.recovered_by_tier.values())} |",
        f"| Refused by a budget | {spend.refusal_count} |",
        f"| Failed on every rung | {spend.failure_count} |",
        f"| …of which reached the top rung and still got nothing | "
        f"{sum(spend.unreachable_by_tier.values())} |",
        "",
        "The two split lines matter for what to do next. A fallback a dearer rung "
        "answered is evidence about routing — the cheap rung was tried, paid for, "
        "and not enough. A request that climbed the whole ladder and still got "
        "nothing is evidence about *reachability*: on a free-tier key a paid-only "
        "model answers `429` with `limit: 0`, which is indistinguishable from a "
        "rate limit and is honestly recorded as a transient failure.",
        "",
    ]
    return lines


def _quality_section(report: MonthReport) -> list[str]:
    quality = report.quality
    if not quality.validated or quality.overall is None:
        return [
            "## Quality",
            "",
            "This month has no `regret.json`, so nothing here knows whether the "
            "cheap answers were good enough. The saving above is a saving in "
            "spend with the quality question open. Run `autopilot validate` "
            f"--month {report.month}`.",
            "",
            f"Cheap-routed requests available to sample: {quality.cheap_routed_count}.",
            "",
        ]

    group = quality.overall
    overhead = (
        "no saving to compare against"
        if quality.overhead_percent_of_saving == NO_RATIO
        else f"{quality.overhead_percent_of_saving}% of the saving"
    )
    lines = [
        "## Quality",
        "",
        "Read from stage 03's `regret.json`, never recomputed here.",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Sample rate | {quality.sample_percent}% |",
        f"| Cheap-routed requests | {quality.cheap_routed_count} |",
        f"| Shadow records kept | {quality.shadow_record_count} |",
        f"| Validated | {quality.validated_count} |",
        f"| Fraction of cheap-routed requests inspected | {quality.inspected_percent}% |",
        f"| Comparisons with a readable verdict | {group.n} |",
        f"| Regret | {group.regret_count} |",
        f"| 95% Wilson interval | [{group.wilson_low:.3f}, {group.wilson_high:.3f}] |",
        f"| Position bias detected | {quality.position_bias_count} |",
        f"| Judge errors | {quality.judge_error_count} |",
        "",
        "### What the measurement cost",
        "",
        "| Leg | Cost |",
        "|---|---:|",
        f"| Reference answers on the top rung | "
        f"{_money(quality.reference_cost_micro_usd)} |",
        f"| Judge calls | {_money(quality.judge_cost_micro_usd)} |",
        f"| **Total validation overhead** | "
        f"**{_money(quality.validation_cost_micro_usd)}** ({overhead}) |",
        "",
        "Overhead is reported next to the saving rather than netted out of it, so a "
        "reader can do the subtraction themselves.",
        "",
        "### Regret by rung",
        "",
        "| Rung | n | Regret | 95% CI | Judge errors |",
        "|---|---:|---:|---|---:|",
    ]
    for key, item in quality.by_rung.items():
        lines.append(
            f"| rung {key} | {item.n} | {item.regret_count} | "
            f"[{item.wilson_low:.3f}, {item.wilson_high:.3f}] | {item.judge_error_count} |"
        )
    lines += [
        "",
        "### Regret by tier",
        "",
        "| Tier | n | Regret | 95% CI | Verdict |",
        "|---|---:|---:|---|---|",
    ]
    for key, item in quality.by_tier.items():
        verdict = quality.tier_verdicts.get(key, "")
        lines.append(
            f"| `{key}` | {item.n} | {item.regret_count} | "
            f"[{item.wilson_low:.3f}, {item.wilson_high:.3f}] | {verdict} |"
        )
    lines.append("")
    return lines


def _recommendation_block(item: Recommendation) -> list[str]:
    lines = [
        f"#### `{item.rule_id}` — {item.subject}",
        "",
        f"- **Evidence:** {item.evidence}",
        f"- **Action:** {item.action}",
    ]
    if item.is_config_change:
        lines.append(
            f"- **Config:** `[{item.config_section}] {item.config_key}` "
            f"{item.current_value} → {item.proposed_value}"
        )
    else:
        lines.append("- **Config:** none — this is not a change a config file can make.")
    lines.append("")
    return lines


def _recommendations_section(report: MonthReport) -> list[str]:
    lines = ["## Recommendations", ""]
    if not report.recommendations:
        lines += [
            "None. Every tier's verdict is `safe` at the configured thresholds and "
            "nothing about the month's outcomes crossed a rule.",
            "",
        ]
        return lines
    lines += [
        "Produced by deterministic rules over the figures above and the thresholds "
        "in `autopilot.toml`. Each one names its evidence and one specific action; "
        "none of them has been applied.",
        "",
    ]
    for item in report.recommendations:
        lines += _recommendation_block(item)
    return lines


def _footer(report: MonthReport) -> list[str]:
    thresholds = ", ".join(
        f"`{key}` = {value}" for key, value in sorted(report.thresholds.items())
    )
    lines = ["## Provenance", ""]
    if not report.prices_verified:
        lines += [
            "> **`prices_verified` is false in `autopilot.toml`.** Every amount in "
            "this report is computed from prices nobody has checked against the "
            "vendor's published page. Do not quote any of them.",
            "",
        ]
    if report.inconsistent_request_ids:
        lines += [
            "> **Inconsistency.** "
            f"{len(report.inconsistent_request_ids)} verdict(s) name a request this "
            "month's ledger does not hold, so the denominators above cannot be "
            "checked against the ledger alone: "
            + ", ".join(f"`{item}`" for item in report.inconsistent_request_ids[:10])
            + ".",
            "",
        ]
    lines += [
        f"- Month: `{report.month}`",
        f"- Generated: `{report.generated_utc}`",
        f"- Verdict: `{report.verdict}` (exit code {report.exit_code})",
        f"- Configuration: `{report.config_path or 'not recorded'}`",
        "- Ladder, cheapest first: "
        + (
            ", ".join(f"`{item}`" for item in report.ladder)
            if report.ladder
            else "not recorded"
        )
        + ". The saving above is measured against the last of these, so a figure "
        "from one ladder is not comparable to a figure from another.",
        f"- Thresholds: {thresholds}",
        f"- Prices verified: `{str(report.prices_verified).lower()}`",
        f"- Report schema version: `{report.schema_version}`",
        "",
    ]
    return lines


def render_report(report: MonthReport) -> str:
    """The whole of `report.md`."""
    lines: list[str] = []
    if report.synthetic:
        lines += [BANNER, ""]
    lines += [
        f"# Cost autopilot report — {report.month}",
        "",
        f"## Verdict: {report.verdict}",
        "",
        report.verdict_reason,
        "",
    ]
    lines += _spend_section(report)
    lines += _quality_section(report)
    lines += _recommendations_section(report)
    lines += _footer(report)
    return "\n".join(lines).rstrip() + "\n"


def render_proposal(proposal: TuningProposal) -> str:
    """The whole of `proposal.md`: the diff, its evidence, and how to approve it."""
    lines: list[str] = []
    if proposal.synthetic:
        lines += [BANNER, ""]
    lines += [
        f"# Tuning proposal — {proposal.month}",
        "",
        f"- **Status:** `{proposal.status}`",
        f"- **Config:** `{proposal.config_path}`",
        f"- **Generated:** `{proposal.generated_utc}`",
        f"- **Approved by:** {proposal.approved_by or '_nobody yet_'}",
        f"- **Applied:** {proposal.applied_utc or '_not applied_'}",
        "",
    ]

    if proposal.is_empty:
        lines += [
            "This month's evidence recommends no change that `autopilot.toml` can "
            "express. That is a real answer: some months the routing is already the "
            "routing the numbers support, and some recommendations — enabling "
            "billing, re-reading a price list — are not settings.",
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    lines += ["## The diff", "", "```diff"]
    for change in proposal.changes:
        lines += [
            f" [{change.section}]",
            f"-{change.key} = {change.current_value}",
            f"+{change.key} = {change.proposed_value}",
        ]
    lines += ["```", "", "## Why", ""]
    for change in proposal.changes:
        lines += [
            f"### `{change.label}`: {change.current_value} → {change.proposed_value}",
            "",
            f"- **Rules:** {', '.join(f'`{item}`' for item in change.rule_ids)}",
            f"- **Evidence:** {change.evidence}",
            "",
        ]

    if proposal.status == STATUS_AWAITING:
        lines += [
            "## Approving this",
            "",
            "This tool will not apply the diff above. Read the evidence, decide, and "
            "then either edit `autopilot.toml` by hand or run:",
            "",
            "```bash",
            "uv run python scripts/autopilot.py apply-proposal \\",
            f"  --file report/{proposal.month}/proposal.json \\",
            '  --approve --approved-by "your name"',
            "```",
            "",
            "Both flags are required. The command refuses if `autopilot.toml` has "
            "moved on since this proposal was generated, refuses a proposal that has "
            "already been applied, and writes your name and the timestamp back into "
            "`proposal.json` so the approval travels with the change.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
