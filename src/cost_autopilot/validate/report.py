"""Write the regret file, read it back, and render it under `ledger summary`.

Presentation only: nothing here computes a rate or an interval, and nothing here
changes configuration. The strongest thing this module does is print the sentence
"consider routing this tier up" — a recommendation for a person, next to the
evidence for it. Applying it is a reviewed diff to `autopilot.toml`, because a
tool that retuned its own routing in response to its own judge would be grading
its own work and then acting on the grade.

The three verdicts, in the order they are checked:

  - **safe** — the 95% *upper* bound on regret is below `max_regret`. The upper
    bound rather than the rate, so a low rate on a handful of samples cannot pass.
  - **insufficient evidence** — fewer than `min_samples` validated records. Say
    so; absence of evidence is not evidence of no regret.
  - **regret too high** — enough samples, and the interval does not fit under the
    threshold. Name the action.
"""

import json
from pathlib import Path

from ..money import format_micro_usd
from .regret import RegretError, RegretGroup, RegretReport, report_from_json_dict

QUALITY_HEADING = "Quality (from shadow validation)"

VERDICT_SAFE = "safe"
VERDICT_INSUFFICIENT = "insufficient evidence"
VERDICT_TOO_HIGH = "regret too high"

PERCENT = 100


def write_report(path: Path | str, report: RegretReport) -> Path:
    """Write `regret.json`, creating the month directory if it does not exist."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_report(path: Path | str) -> RegretReport | None:
    """Read `regret.json`, or `None` when the month has not been validated.

    Raises:
        RegretError: the file exists but is not a readable report. A month whose
            quality figures cannot be parsed is not a month with no regret.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegretError(f"could not read the regret report {path}: {exc}") from exc
    return report_from_json_dict(payload)


def _rate_percent(group: RegretGroup) -> int:
    """A whole-number percentage, floored, in the house style of `ledger summary`."""
    if group.n == 0:
        return 0
    return (group.regret_count * PERCENT) // group.n


def _group_line(group: RegretGroup, *, name: str, width: int) -> str:
    return (
        f"    {name.ljust(width)}  n={group.n:<4} regret {group.regret_count} "
        f"({_rate_percent(group)}%)  95% CI "
        f"[{group.wilson_low:.3f}, {group.wilson_high:.3f}]"
    )


def tier_verdict_line(group: RegretGroup, *, max_regret: float, min_samples: int) -> str:
    """The one sentence a human is meant to act on for this tier."""
    if group.wilson_high < max_regret:
        return (
            f"{VERDICT_SAFE}: the 95% upper bound {group.wilson_high:.3f} "
            f"is below max_regret {max_regret:.3f}"
        )
    if group.n < min_samples:
        return (
            f"{VERDICT_INSUFFICIENT}: {group.n} validated sample(s), "
            f"fewer than min_samples {min_samples}"
        )
    return (
        f"{VERDICT_TOO_HIGH} — consider routing this tier up "
        f"(95% upper bound {group.wilson_high:.3f}, max_regret {max_regret:.3f})"
    )


def render_quality(report: RegretReport, *, max_regret: float, min_samples: int) -> str:
    """Render the quality section `ledger summary` prints after its totals."""
    lines = [
        "",
        f"  {QUALITY_HEADING}",
        "",
        f"    sample_percent     {report.sample_percent}",
        f"    cheap-routed       {report.cheap_routed_count} successful request(s) below "
        "the top rung",
        f"    shadow-sampled     {report.shadow_record_count}",
        f"    validated          {report.validated_count}",
    ]

    if report.overall.n == 0:
        lines += [
            "",
            "    No validated samples produced a readable verdict, so this month has",
            "    no regret figure. Absence of evidence is not evidence of no regret.",
            f"    judge errors       {report.overall.judge_error_count}",
            f"    validation cost    {format_micro_usd(report.validation_cost_micro_usd)}",
        ]
        return "\n".join(lines)

    lines += [
        f"    overall regret     {report.overall.regret_count} of {report.overall.n} "
        f"({_rate_percent(report.overall)}%)  95% CI "
        f"[{report.overall.wilson_low:.3f}, {report.overall.wilson_high:.3f}]",
        f"    position bias      {report.position_bias_count}",
        f"    judge errors       {report.overall.judge_error_count}",
        f"    validation cost    {format_micro_usd(report.validation_cost_micro_usd)} "
        f"(reference {format_micro_usd(report.reference_cost_micro_usd)} + judge "
        f"{format_micro_usd(report.judge_cost_micro_usd)})",
        "",
        "    regret by rung:",
    ]
    rung_width = max((len(key) + 5 for key in report.by_rung), default=6)
    for key, group in report.by_rung.items():
        lines.append(_group_line(group, name=f"rung {key}", width=rung_width))

    lines += ["", "    regret by tier:"]
    tier_width = max((len(key) for key in report.by_tier), default=6)
    for key, group in report.by_tier.items():
        lines.append(_group_line(group, name=key, width=tier_width))
        lines.append(
            f"      {tier_verdict_line(group, max_regret=max_regret, min_samples=min_samples)}"
        )
    return "\n".join(lines)
