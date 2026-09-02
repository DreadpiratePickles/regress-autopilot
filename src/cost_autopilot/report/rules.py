"""Deterministic rules that turn a month's figures into named actions.

No model is called here, and no threshold is invented: every rule fires off a
value in `autopilot.toml` — `[validate] max_regret`, `[validate] min_samples`,
`[validate] sample_percent`, `[report] min_comparisons`, `[report]
max_fallback_rate` — so the same month always produces the same advice, and
disagreeing with a recommendation means disagreeing with a number a human wrote
down.

The rules are one-per-verdict-state, which is what keeps them from contradicting
each other:

  - a tier with **regret actually observed** and an interval above the threshold
    is a routing problem → raise its policy floor;
  - a tier with **no regret observed** but an interval too wide, or with **too
    few samples**, is a *sampling* problem → raise `sample_percent`, and say by
    how much and toward what `n`;
  - a tier that is **safe** while its policy floor sits above what the ladder
    would allow has money on the table → consider lowering the floor, with the
    caveat that the evidence was gathered at the current rung;
  - requests that exhausted the whole ladder are an entitlement problem, not a
    routing one → the action is billing, and no configuration change is proposed;
  - a fallback rate above `max_fallback_rate` means the cheap rung is being paid
    for and then abandoned;
  - unverified prices make every figure above provisional.

Every recommendation carries its evidence — n, the observed rate, the interval —
because a recommendation without evidence is an instruction, and this stage is
not allowed to give instructions.
"""

from ..classify.scorer import Tier
from ..config_file import AutopilotConfig
from ..validate.regret import RegretGroup
from ..validate.report import (
    VERDICT_INSUFFICIENT,
    VERDICT_NO_REGRET_OBSERVED,
    VERDICT_SAFE,
    VERDICT_TOO_HIGH,
    clean_samples_needed,
    tier_verdict_state,
)
from .model import MonthReport, Recommendation

PERCENT = 100

RULE_RAISE_POLICY_FLOOR = "raise_policy_floor"
RULE_RAISE_SAMPLE_PERCENT = "raise_sample_percent"
RULE_LOWER_POLICY_FLOOR = "lower_policy_floor"
RULE_ENABLE_BILLING = "enable_billing"
RULE_HIGH_FALLBACK_RATE = "high_fallback_rate"
RULE_VERIFY_PRICES = "verify_prices"
RULE_LADDER_EXHAUSTED = "ladder_exhausted"

POLICY_SECTION = "policy"
VALIDATE_SECTION = "validate"
SAMPLE_PERCENT_KEY = "sample_percent"
MAX_SAMPLE_PERCENT = 100


def _evidence(group: RegretGroup) -> str:
    rate = (group.regret_count * PERCENT) // group.n if group.n else 0
    return (
        f"n={group.n}, regret {group.regret_count} ({rate}%), "
        f"95% CI [{group.wilson_low:.3f}, {group.wilson_high:.3f}]"
    )


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator) if denominator else 0


def _policy_floor(config: AutopilotConfig, tier: Tier) -> int:
    """Where this tier actually starts: the later of the two floors, as routed."""
    return max(config.ladder.first_capable_index(tier), config.policy.floor_for(tier))


def _raise_policy_floor(
    config: AutopilotConfig, tier: Tier, group: RegretGroup, *, rule_id: str, because: str
) -> Recommendation:
    """One rung up for this tier, or a note that the ladder has nowhere left to go."""
    current = _policy_floor(config, tier)
    top = config.ladder.top_rung.index
    if current >= top:
        return Recommendation(
            rule_id=RULE_LADDER_EXHAUSTED,
            subject=tier.value,
            evidence=_evidence(group),
            action=(
                f"{because} {tier.value} already starts at rung {current}, the top of "
                f"the ladder ({config.ladder.top_rung.model_id}). There is no more "
                "expensive rung to route it to: the next move is a better model on "
                "the ladder, not a policy change."
            ),
        )
    return Recommendation(
        rule_id=rule_id,
        subject=tier.value,
        evidence=_evidence(group),
        action=(
            f"{because} route {tier.value} from rung {current + 1} "
            f"({config.ladder.rung(current + 1).model_id}) instead of rung {current} "
            f"({config.ladder.rung(current).model_id}): set [policy] {tier.value} = "
            f"{current + 1}."
        ),
        config_section=POLICY_SECTION,
        config_key=tier.value,
        current_value=config.policy.floor_for(tier),
        proposed_value=current + 1,
    )


def _raise_sample_percent(
    config: AutopilotConfig, report: MonthReport, tier: Tier, group: RegretGroup, *, state: str
) -> Recommendation:
    """More evidence, quantified: how many clean comparisons, and at what rate."""
    current_percent = config.validate.sample_percent
    clean = clean_samples_needed(config.validate.max_regret)
    target = max(config.validate.min_samples, clean or config.validate.min_samples)
    population = report.quality.cheap_routed_by_tier.get(tier.value, 0)

    if clean is None:
        return Recommendation(
            rule_id=RULE_RAISE_SAMPLE_PERCENT,
            subject=tier.value,
            evidence=_evidence(group),
            action=(
                f"max_regret is {config.validate.max_regret:.3f}, which no sample "
                "size can clear at zero observed regret. Raise [validate] max_regret "
                "to a reachable threshold before sampling harder."
            ),
        )

    # Requests needed at the *current* rate to yield `target` comparisons, and the
    # rate that would have yielded them from this month's traffic. Both are quoted:
    # the first says how long to wait, the second says what to change.
    requests_at_current = _ceil_div(target * PERCENT, current_percent) if current_percent else 0
    proposed_percent = (
        min(MAX_SAMPLE_PERCENT, _ceil_div(target * PERCENT, population))
        if population
        else MAX_SAMPLE_PERCENT
    )
    shortfall = max(1, target - group.n)

    reason = (
        f"{tier.value} has {group.n} comparison(s), fewer than min_samples "
        f"{config.validate.min_samples}"
        if state == VERDICT_INSUFFICIENT
        else (
            f"{tier.value} observed no regret at all, but {group.n} comparison(s) "
            f"leave the 95% upper bound at {group.wilson_high:.3f}"
        )
    )
    return Recommendation(
        rule_id=RULE_RAISE_SAMPLE_PERCENT,
        subject=tier.value,
        evidence=_evidence(group),
        action=(
            f"{reason}. Reaching n~{target} — the smallest sample whose interval "
            f"clears max_regret {config.validate.max_regret:.3f} at zero regret — "
            f"needs ~{shortfall} more comparison(s), which at the current "
            f"{current_percent}% means about {requests_at_current} cheap-routed "
            f"{tier.value} request(s); this month had {population}. Raise [validate] "
            f"{SAMPLE_PERCENT_KEY} from {current_percent} to {proposed_percent}."
        ),
        config_section=VALIDATE_SECTION,
        config_key=SAMPLE_PERCENT_KEY,
        current_value=current_percent,
        proposed_value=proposed_percent,
    )


def _lower_policy_floor(
    config: AutopilotConfig, tier: Tier, group: RegretGroup
) -> Recommendation | None:
    """A safe tier whose policy floor skips a rung the ladder would have allowed."""
    capable = config.ladder.first_capable_index(tier)
    configured = config.policy.floor_for(tier)
    if configured <= capable:
        return None
    return Recommendation(
        rule_id=RULE_LOWER_POLICY_FLOOR,
        subject=tier.value,
        evidence=_evidence(group),
        action=(
            f"{tier.value} is safe at rung {configured} while the ladder would "
            f"allow rung {capable} ({config.ladder.rung(capable).model_id}). Set "
            f"[policy] {tier.value} = {configured - 1} to capture the difference — "
            "but read the caveat: this regret was measured at rung "
            f"{configured}, so it is evidence about that rung and not about the "
            "cheaper one. Expect the next month's report to re-measure it."
        ),
        config_section=POLICY_SECTION,
        config_key=tier.value,
        current_value=configured,
        proposed_value=configured - 1,
    )


def _billing(config: AutopilotConfig, report: MonthReport) -> list[Recommendation]:
    """Requests that ran out of ladder. An entitlement problem, not a routing one."""
    made: list[Recommendation] = []
    top = config.ladder.top_rung
    for tier, count in report.spend.unreachable_by_tier.items():
        made.append(
            Recommendation(
                rule_id=RULE_ENABLE_BILLING,
                subject=tier,
                evidence=(
                    f"{count} {tier} request(s) recorded status=failed after trying "
                    f"every rung up to and including {top.model_id}"
                ),
                action=(
                    f"enable billing for rung {top.index} ({top.model_id}) — {count} "
                    f"{tier} request(s) failed with no reachable model. Read the "
                    "vendor's error body before acting: a 429 carrying `limit: 0` "
                    "means the model is paid-only and no amount of waiting will help, "
                    "whereas a non-zero limit means a daily quota that resets. This "
                    "system records both as a transient failure because the status "
                    "code is genuinely the same. No configuration change is proposed "
                    "either way: the fix is at the provider account, not in this file."
                ),
            )
        )
    return made


def _fallbacks(config: AutopilotConfig, report: MonthReport) -> list[Recommendation]:
    """Tiers paying for a cheap call and then paying again a rung up.

    Counts only the fallbacks a dearer rung *answered*. A request that climbed
    the whole ladder and still failed says the ladder is unreachable, which is
    the billing rule's business; treating it as evidence that the cheap rung was
    inadequate would recommend spending more money on a call nobody could serve.
    """
    made: list[Recommendation] = []
    threshold = config.report.max_fallback_rate
    for tier_name, count in report.spend.recovered_by_tier.items():
        attempted = report.spend.requests_by_tier.get(tier_name, 0)
        if attempted <= 0:
            continue
        rate = count / attempted
        if rate <= threshold:
            continue
        tier = Tier(tier_name)
        current = _policy_floor(config, tier)
        top = config.ladder.top_rung.index
        evidence = (
            f"{count} of {attempted} {tier_name} request(s) were answered by a dearer "
            f"rung after a cheaper one failed ({(count * PERCENT) // attempted}%), "
            f"above [report] max_fallback_rate {threshold:.2f}"
        )
        if current >= top:
            made.append(
                Recommendation(
                    rule_id=RULE_HIGH_FALLBACK_RATE,
                    subject=tier_name,
                    evidence=evidence,
                    action=(
                        "a fallback is a paid-for call that produced nothing, but "
                        f"{tier_name} already starts at the top rung: these are "
                        "provider health, not routing, and no policy change would "
                        "help."
                    ),
                )
            )
            continue
        made.append(
            Recommendation(
                rule_id=RULE_HIGH_FALLBACK_RATE,
                subject=tier_name,
                evidence=evidence,
                action=(
                    "a fallback is a paid-for call that produced nothing: set "
                    f"[policy] {tier_name} = {current + 1} so the request starts on "
                    f"rung {current + 1} ({config.ladder.rung(current + 1).model_id}), "
                    "the rung it keeps ending up on."
                ),
                config_section=POLICY_SECTION,
                config_key=tier_name,
                current_value=config.policy.floor_for(tier),
                proposed_value=current + 1,
            )
        )
    return made


def recommend(report: MonthReport, config: AutopilotConfig) -> tuple[Recommendation, ...]:
    """Every rule, in a fixed order, over one built report."""
    made: list[Recommendation] = []

    for tier_name, group in sorted(report.quality.by_tier.items()):
        try:
            tier = Tier(tier_name)
        except ValueError:  # pragma: no cover - a verdict file always names a real tier
            continue
        state = tier_verdict_state(
            group,
            max_regret=config.validate.max_regret,
            min_samples=config.validate.min_samples,
        )
        if state == VERDICT_TOO_HIGH:
            made.append(
                _raise_policy_floor(
                    config,
                    tier,
                    group,
                    rule_id=RULE_RAISE_POLICY_FLOOR,
                    because="regret is above the threshold:",
                )
            )
        elif state in (VERDICT_INSUFFICIENT, VERDICT_NO_REGRET_OBSERVED):
            made.append(_raise_sample_percent(config, report, tier, group, state=state))
        elif state == VERDICT_SAFE:
            lowered = _lower_policy_floor(config, tier, group)
            if lowered is not None:
                made.append(lowered)

    made += _billing(config, report)
    made += _fallbacks(config, report)

    if not report.prices_verified:
        made.append(
            Recommendation(
                rule_id=RULE_VERIFY_PRICES,
                subject="ladder",
                evidence="[ladder] prices_verified = false in autopilot.toml",
                action=(
                    "re-read the vendor's published pricing page, update every "
                    "[[ladder.rung]] price, and set prices_verified = true with the "
                    "date. Until then every amount in this report is computed from "
                    "prices nobody checked and none of them should be quoted."
                ),
            )
        )

    return tuple(made)
