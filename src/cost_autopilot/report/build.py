"""Turn a month's ledger rows and stage 03's regret file into a `MonthReport`.

Pure arithmetic. This module opens no file, calls no model, and reads no clock —
its caller hands it rows, a regret report and a timestamp, which is what makes
every figure in a report testable against numbers written by hand.

Three rules it follows, each of them a decision somebody could have made the
other way:

  - **Regret is read, not recomputed.** Stage 03 owns the definition of regret,
    the Wilson intervals and the denominators; this module copies them across. A
    report that recomputed them would eventually disagree with the `validate`
    run that produced them, and the disagreement would surface as an argument
    about which number is right rather than as a bug.
  - **Spend is recomputed from the rows**, using `ledger.summary.summarise`, the
    same function `ledger summary` prints. Two commands showing different spend
    for the same month is the same failure in the other direction.
  - **A month with no quality measurement gets a verdict of INCONCLUSIVE**, not
    a saving presented as if it were free of consequence. The spend section is
    still rendered — the money was really spent — but nothing calls the routing
    defensible without evidence that it was.
"""

from collections import Counter

from ..config_file import AutopilotConfig
from ..ledger.row import STATUS_FAILED, STATUS_OK, LedgerRow
from ..ledger.summary import summarise
from ..validate.regret import RegretReport
from ..validate.report import clean_samples_needed, tier_verdict_line
from ..validate.verdicts import VerdictRecord
from .model import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REGRET_TOO_HIGH,
    VERDICT_SAFE,
    MonthReport,
    QualityFacts,
    SpendFacts,
)

PERCENT = 100
NO_RATIO = -1
"""`overhead_percent_of_saving` when the month saved nothing. A sentinel rather
than a zero, because "the overhead was 0% of the saving" and "there was no
saving to compare it against" are different facts."""


def _percent(part: int, whole: int) -> int:
    """A floored whole-number percentage, in the house style of `ledger summary`."""
    if whole <= 0:
        return 0
    return (part * PERCENT) // whole


def build_spend_facts(rows: list[LedgerRow], *, month: str, top_model_id: str) -> SpendFacts:
    """Total the month's rows. Delegates the shared totals to `ledger.summary`."""
    summary = summarise(rows, month=month)

    spend_by_tier: Counter[str] = Counter()
    fallbacks_by_tier: Counter[str] = Counter()
    recovered_by_tier: Counter[str] = Counter()
    failures_by_tier: Counter[str] = Counter()
    unreachable_by_tier: Counter[str] = Counter()

    for row in rows:
        spend_by_tier[row.tier] += row.cost_micro_usd
        if len(row.fallback_chain) > 1:
            fallbacks_by_tier[row.tier] += 1
            if row.status == STATUS_OK:
                recovered_by_tier[row.tier] += 1
        if row.status == STATUS_FAILED:
            failures_by_tier[row.tier] += 1
            if top_model_id in row.fallback_chain:
                unreachable_by_tier[row.tier] += 1

    return SpendFacts(
        row_count=summary.row_count,
        spend_micro_usd=summary.spend_micro_usd,
        counterfactual_micro_usd=summary.counterfactual_micro_usd,
        saving_micro_usd=summary.saving_micro_usd,
        saving_percent=summary.saving_percent,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        spend_by_team=summary.spend_by_team,
        spend_by_model=summary.spend_by_model,
        spend_by_tier=dict(sorted(spend_by_tier.items())),
        requests_by_tier=summary.requests_by_tier,
        requests_by_status=summary.requests_by_status,
        fallback_count=summary.fallback_count,
        refusal_count=summary.refusal_count,
        failure_count=summary.failure_count,
        fallbacks_by_tier=dict(sorted(fallbacks_by_tier.items())),
        recovered_by_tier=dict(sorted(recovered_by_tier.items())),
        failures_by_tier=dict(sorted(failures_by_tier.items())),
        unreachable_by_tier=dict(sorted(unreachable_by_tier.items())),
    )


def cheap_routed_by_tier(rows: list[LedgerRow], *, top_model_id: str) -> dict[str, int]:
    """Successful requests answered below the top rung, per tier.

    The population the shadow sample was drawn from, split the way the
    recommendations need it: "raise the sample rate to get 35 T2 comparisons"
    is only actionable next to how many T2 requests there were to sample from.
    """
    counts: Counter[str] = Counter()
    for row in rows:
        if row.status == STATUS_OK and row.chosen_model_id != top_model_id:
            counts[row.tier] += 1
    return dict(sorted(counts.items()))


def build_quality_facts(
    regret: RegretReport | None,
    *,
    rows: list[LedgerRow],
    top_model_id: str,
    saving_micro_usd: int,
    max_regret: float,
    min_samples: int,
) -> QualityFacts:
    """Copy stage 03's figures across, and add the two ratios a report needs."""
    per_tier_population = cheap_routed_by_tier(rows, top_model_id=top_model_id)

    if regret is None:
        return QualityFacts(
            validated=False,
            sample_percent=0,
            cheap_routed_count=sum(per_tier_population.values()),
            cheap_routed_by_tier=per_tier_population,
            shadow_record_count=0,
            validated_count=0,
            inspected_percent=0,
            overall=None,
            by_rung={},
            by_tier={},
            tier_verdicts={},
            position_bias_count=0,
            judge_error_count=0,
            reference_cost_micro_usd=0,
            judge_cost_micro_usd=0,
            validation_cost_micro_usd=0,
            overhead_percent_of_saving=NO_RATIO,
        )

    return QualityFacts(
        validated=True,
        sample_percent=regret.sample_percent,
        cheap_routed_count=regret.cheap_routed_count,
        cheap_routed_by_tier=per_tier_population,
        shadow_record_count=regret.shadow_record_count,
        validated_count=regret.validated_count,
        inspected_percent=_percent(regret.validated_count, regret.cheap_routed_count),
        overall=regret.overall,
        by_rung=dict(regret.by_rung),
        by_tier=dict(regret.by_tier),
        tier_verdicts={
            tier: tier_verdict_line(group, max_regret=max_regret, min_samples=min_samples)
            for tier, group in regret.by_tier.items()
        },
        position_bias_count=regret.position_bias_count,
        judge_error_count=regret.overall.judge_error_count,
        reference_cost_micro_usd=regret.reference_cost_micro_usd,
        judge_cost_micro_usd=regret.judge_cost_micro_usd,
        validation_cost_micro_usd=regret.validation_cost_micro_usd,
        overhead_percent_of_saving=(
            _percent(regret.validation_cost_micro_usd, saving_micro_usd)
            if saving_micro_usd > 0
            else NO_RATIO
        ),
    )


def decide_verdict(
    quality: QualityFacts, *, month: str, max_regret: float, min_comparisons: int
) -> tuple[str, str]:
    """The month's one-word answer, and the sentence that justifies it.

    The same four-state logic `tier_verdict_line` applies to a tier, applied to
    the month: "no regret observed but the interval is too wide" is INCONCLUSIVE
    here rather than a fourth verdict, because a report's job is to say whether
    the routing is defensible and "the sample is too small to tell" is not an
    accusation about the routing.
    """
    if not quality.validated or quality.overall is None:
        return (
            VERDICT_INCONCLUSIVE,
            f"no regret.json for {month}: the month has spend but no quality "
            "measurement, and a saving whose regret was never measured is not a "
            "saving anyone should quote. Run `autopilot validate`.",
        )

    group = quality.overall
    if group.n < min_comparisons:
        return (
            VERDICT_INCONCLUSIVE,
            f"{group.n} comparison(s) produced a readable verdict, fewer than "
            f"[report] min_comparisons {min_comparisons}. Absence of evidence is "
            "not evidence of no regret.",
        )

    rate_percent = _percent(group.regret_count, group.n)
    interval = f"95% CI [{group.wilson_low:.3f}, {group.wilson_high:.3f}]"

    if group.wilson_high < max_regret:
        return (
            VERDICT_SAFE,
            f"regret {group.regret_count} of {group.n} ({rate_percent}%), {interval}; "
            f"the upper bound {group.wilson_high:.3f} is below max_regret "
            f"{max_regret:.3f}.",
        )

    if group.regret_count == 0:
        needed = clean_samples_needed(max_regret)
        more = (
            f"need ~{max(1, needed - group.n)} more clean comparison(s)"
            if needed is not None
            else f"no sample size clears max_regret {max_regret:.3f}"
        )
        return (
            VERDICT_INCONCLUSIVE,
            f"no regret observed in {group.n} comparison(s), {interval}; the upper "
            f"bound {group.wilson_high:.3f} is still above max_regret "
            f"{max_regret:.3f}, so this is a small sample rather than a good "
            f"result — {more}.",
        )

    return (
        VERDICT_REGRET_TOO_HIGH,
        f"regret {group.regret_count} of {group.n} ({rate_percent}%), {interval}; "
        f"the upper bound {group.wilson_high:.3f} exceeds max_regret "
        f"{max_regret:.3f}.",
    )


def inconsistent_ids(
    rows: list[LedgerRow], verdicts: list[VerdictRecord]
) -> tuple[str, ...]:
    """Verdicts naming a request this month's ledger does not hold.

    Named in the report rather than silently dropped. A verdict with no ledger
    row means the two files describe different months, or one has been edited,
    and every rate below is divided by a denominator the reader cannot check.
    """
    known = {row.request_id for row in rows}
    return tuple(sorted({item.request_id for item in verdicts if item.request_id not in known}))


def build_report(
    rows: list[LedgerRow],
    regret: RegretReport | None,
    verdicts: list[VerdictRecord],
    *,
    config: AutopilotConfig,
    month: str,
    generated_utc: str,
    synthetic: bool = False,
    config_path: str = "",
) -> MonthReport:
    """Assemble the whole report. Recommendations are added afterwards, by `rules`."""
    top_model_id = config.ladder.top_rung.model_id
    spend = build_spend_facts(rows, month=month, top_model_id=top_model_id)
    quality = build_quality_facts(
        regret,
        rows=rows,
        top_model_id=top_model_id,
        saving_micro_usd=spend.saving_micro_usd,
        max_regret=config.validate.max_regret,
        min_samples=config.validate.min_samples,
    )
    verdict, reason = decide_verdict(
        quality,
        month=month,
        max_regret=config.validate.max_regret,
        min_comparisons=config.report.min_comparisons,
    )
    return MonthReport(
        month=month,
        generated_utc=generated_utc,
        synthetic=synthetic,
        prices_verified=config.ladder.prices_verified,
        verdict=verdict,
        verdict_reason=reason,
        spend=spend,
        quality=quality,
        thresholds={
            "max_regret": config.validate.max_regret,
            "min_samples": config.validate.min_samples,
            "min_comparisons": config.report.min_comparisons,
            "max_fallback_rate": config.report.max_fallback_rate,
            "sample_percent": config.validate.sample_percent,
        },
        config_path=config_path,
        ladder=tuple(rung.model_id for rung in config.ladder.rungs),
        inconsistent_request_ids=inconsistent_ids(rows, verdicts),
    )
