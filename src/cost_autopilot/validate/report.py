"""Write the regret file, read it back, and render it under `ledger summary`.

Presentation only: nothing here computes a rate or an interval, and nothing here
changes configuration. The strongest thing this module does is print the sentence
"consider routing this tier up" — a recommendation for a person, next to the
evidence for it. Applying it is a reviewed diff to `autopilot.toml`, because a
tool that retuned its own routing in response to its own judge would be grading
its own work and then acting on the grade.

The four verdicts, in the order they are checked:

  - **safe** — the 95% *upper* bound on regret is below `max_regret`. The upper
    bound rather than the rate, so a low rate on a handful of samples cannot pass.
  - **insufficient evidence** — fewer than `min_samples` validated records. Say
    so; absence of evidence is not evidence of no regret.
  - **no regret observed … interval too wide** — enough samples to escape
    "insufficient evidence", zero regret actually observed, and an interval that
    still does not fit under the threshold. Phase B shipped without this state
    and said so in `docs/design.md` §13: such a tier reported "regret too high"
    while its observed rate was zero, which is true of the bound and reads as an
    accusation the data does not support. The fourth state says what is actually
    wrong — the sample, not the routing — and computes how much more of it is
    needed.
  - **regret too high** — enough samples, regret actually observed, and the
    interval does not fit under the threshold. Name the action.

`tier_verdict_line` is the single place those four sentences are written.
`ledger summary` and stage 04's report both call it, so a reader can never be
shown two different wordings for the same evidence.
"""

import json
from pathlib import Path

from regression_detect.compare import wilson_interval

from ..money import format_micro_usd
from .regret import RegretError, RegretGroup, RegretReport, report_from_json_dict

QUALITY_HEADING = "Quality (from shadow validation)"

VERDICT_SAFE = "safe"
VERDICT_INSUFFICIENT = "insufficient evidence"
VERDICT_NO_REGRET_OBSERVED = "no regret observed"
VERDICT_TOO_HIGH = "regret too high"

PERCENT = 100

CLEAN_SAMPLE_SEARCH_LIMIT = 100_000
"""Largest `n` `clean_samples_needed` will look at before giving up.

A bound rather than an unbounded loop: `max_regret = 0.0` is a legal setting and
no sample size satisfies it, so the search has to be able to say "never" instead
of running forever."""


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


def _upper_bound_at_zero_regret(n: int) -> float:
    """The 95% Wilson upper bound on a rate observed as zero out of `n`."""
    return wilson_interval(0, n)[1]


def clean_samples_needed(
    max_regret: float, *, limit: int = CLEAN_SAMPLE_SEARCH_LIMIT
) -> int | None:
    """Smallest `n` whose upper bound at *zero observed regret* clears `max_regret`.

    This is the number the fourth verdict quotes. It is computed rather than
    written down because it moves with `max_regret`: at the shipped 0.10 it is
    35, and a deployment that raises the threshold to 0.20 needs only 16. A
    constant in the docstring would be wrong for every deployment but one.

    The bound `wilson_interval(0, n)[1]` decreases monotonically in `n`, so a
    doubling search followed by a bisection finds the boundary in about thirty
    evaluations. The same `wilson_interval` the verdicts use is called, rather
    than its closed form, so the two can never disagree.

    Returns:
        The smallest such `n`, or `None` when no sample size within `limit`
        reaches it — which is the true answer for `max_regret = 0.0`.
    """
    high = 1
    while high <= limit and _upper_bound_at_zero_regret(high) >= max_regret:
        high *= 2
    if high > limit:
        return None
    low = high // 2
    while low + 1 < high:
        middle = (low + high) // 2
        if _upper_bound_at_zero_regret(middle) < max_regret:
            high = middle
        else:
            low = middle
    return high


def tier_verdict_state(
    group: RegretGroup, *, max_regret: float, min_samples: int
) -> str:
    """Which of the four verdicts this group is in, as a bare constant.

    Split out from the sentence so stage 04's recommendation rules can branch on
    the state without matching English against `tier_verdict_line`'s output. One
    function decides; the other writes it down.
    """
    if group.wilson_high < max_regret:
        return VERDICT_SAFE
    if group.n < min_samples:
        return VERDICT_INSUFFICIENT
    if group.regret_count == 0:
        return VERDICT_NO_REGRET_OBSERVED
    return VERDICT_TOO_HIGH


def tier_verdict_line(group: RegretGroup, *, max_regret: float, min_samples: int) -> str:
    """The one sentence a human is meant to act on for this tier.

    Four states, checked in the order the module docstring lists them. Both
    `ledger summary` and stage 04's Markdown report render this string verbatim.
    """
    state = tier_verdict_state(group, max_regret=max_regret, min_samples=min_samples)
    if state == VERDICT_SAFE:
        return (
            f"{VERDICT_SAFE}: the 95% upper bound {group.wilson_high:.3f} "
            f"is below max_regret {max_regret:.3f}"
        )
    if state == VERDICT_INSUFFICIENT:
        return (
            f"{VERDICT_INSUFFICIENT}: {group.n} validated sample(s), "
            f"fewer than min_samples {min_samples}"
        )
    if state == VERDICT_NO_REGRET_OBSERVED:
        needed = clean_samples_needed(max_regret)
        if needed is None:
            return (
                f"{VERDICT_NO_REGRET_OBSERVED} (n={group.n}); interval too wide — "
                f"no sample size brings the 95% upper bound below max_regret "
                f"{max_regret:.3f}"
            )
        shortfall = max(1, needed - group.n)
        return (
            f"{VERDICT_NO_REGRET_OBSERVED} (n={group.n}); interval too wide — "
            f"need ~{shortfall} more clean sample{'' if shortfall == 1 else 's'} "
            f"(95% upper bound {group.wilson_high:.3f}, max_regret {max_regret:.3f})"
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
