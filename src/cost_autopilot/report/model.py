"""The shape of one month's report, and of one recommendation.

Nothing in this module computes anything. It exists so that `build.py`,
`rules.py` and `render.py` agree on what a report holds, and so that the JSON on
disk is a mechanical projection of the same fields rather than a second,
hand-maintained schema that can drift from the Markdown.

Two conventions carried over from the rest of the package:

  - **Money is integer micro-USD**, validated at construction. Rates that are
    genuinely real numbers — a regret rate, a Wilson bound — stay floats,
    because rounding a confidence bound to hide that it is one would be worse.
  - **Percentages are floored integers.** A floored saving can never overstate
    the saving, and a floored inspected-fraction can never overstate how much of
    the month was actually looked at.

A `Recommendation` is deliberately not free text. It carries the evidence and
the action as separate fields, and — where the action is a configuration change
— the section, key, current value and proposed value, so `proposal.py` can turn
it into a diff without parsing English.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from ..money import CURRENCY, validate_micro_usd
from ..validate.regret import RegretGroup

REPORT_SCHEMA_VERSION = 1

VERDICT_SAFE = "SAFE"
VERDICT_REGRET_TOO_HIGH = "REGRET_TOO_HIGH"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICTS = (VERDICT_SAFE, VERDICT_REGRET_TOO_HIGH, VERDICT_INCONCLUSIVE)

EXIT_SAFE = 0
EXIT_REGRET_TOO_HIGH = 1
EXIT_INCONCLUSIVE = 2
EXIT_COULD_NOT_RUN = 3
"""Project 1's convention, and the one stage 04's contract declares: 0 the
routing is defensible, 1 it is not, 2 the evidence cannot say, 3 the report could
not be produced at all. 3 is separate from 2 on purpose — "we could not tell" and
"the tool is broken" are different messages to send a person at 3am."""

EXIT_BY_VERDICT = {
    VERDICT_SAFE: EXIT_SAFE,
    VERDICT_REGRET_TOO_HIGH: EXIT_REGRET_TOO_HIGH,
    VERDICT_INCONCLUSIVE: EXIT_INCONCLUSIVE,
}

SYNTHETIC_BANNER = (
    "SYNTHETIC — produced by `--dry-run`. Every verdict below is a canned "
    "constant from an in-memory fake, not a model judgement, and every cost is a "
    "fixed synthetic token count. Nothing here measures anything."
)
"""The first line of any artifact produced from a dry run.

It is a banner rather than a footnote because these files are meant to be read
out of context — committed under `docs/examples/`, pasted into a chat — and a
caveat at the bottom of a document that gets skimmed from the top is a caveat
nobody reads."""


class ReportError(ValueError):
    """A report or a recommendation is malformed, or names a verdict that does
    not exist."""


@dataclass(frozen=True)
class Recommendation:
    """One thing to do about this month, the evidence for it, and its config diff."""

    rule_id: str
    """Which deterministic rule fired. Stable, so a reader can find the rule in
    `rules.py` and argue with it rather than with the sentence it produced."""

    subject: str
    """What the recommendation is about: a tier, a rung, or the whole month."""

    evidence: str
    """The numbers behind it — n, the observed rate, the interval — written out.
    A recommendation without its evidence is an instruction, not an argument."""

    action: str
    """One specific thing to do, naming a key and a value where there is one."""

    config_section: str | None = None
    config_key: str | None = None
    current_value: int | None = None
    proposed_value: int | None = None
    """Set together or not at all. When all four are present this recommendation
    becomes one line of the tuning proposal; when they are absent the action is
    something no configuration file can express — enabling billing, re-reading a
    price list — and it stays advice."""

    def __post_init__(self) -> None:
        for name in ("rule_id", "subject", "evidence", "action"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ReportError(f"recommendation {name} must be a non-empty string")
        provided = [
            self.config_section is not None,
            self.config_key is not None,
            self.current_value is not None,
            self.proposed_value is not None,
        ]
        if any(provided) and not all(provided):
            raise ReportError(
                f"recommendation {self.rule_id!r} sets some but not all of "
                "config_section, config_key, current_value and proposed_value; a "
                "half-specified change cannot become a reviewable diff"
            )
        for name in ("current_value", "proposed_value"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ReportError(f"recommendation {name} must be an integer or None")

    @property
    def is_config_change(self) -> bool:
        return self.config_key is not None and self.current_value != self.proposed_value

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpendFacts:
    """What the month cost, what it would have cost, and what did not go well."""

    row_count: int
    spend_micro_usd: int
    counterfactual_micro_usd: int
    saving_micro_usd: int
    saving_percent: int
    input_tokens: int
    output_tokens: int
    spend_by_team: dict[str, int]
    spend_by_model: dict[str, int]
    spend_by_tier: dict[str, int]
    requests_by_tier: dict[str, int]
    requests_by_status: dict[str, int]
    fallback_count: int
    refusal_count: int
    failure_count: int
    fallbacks_by_tier: dict[str, int]
    """Requests of this tier that tried more than one rung, whatever happened next.
    Sums to `fallback_count`, which is what `ledger summary` prints."""

    recovered_by_tier: dict[str, int]
    """Requests of this tier where a cheaper rung failed and a dearer one answered.

    The subset the fallback rule reasons about, and a strict subset of
    `fallbacks_by_tier`. A request that climbed the whole ladder and still got
    nothing says the ladder is unreachable, not that the cheap rung was wrong for
    it, so counting it as evidence for routing the tier higher would recommend
    spending more money on a call that was never going to be answered."""

    failures_by_tier: dict[str, int]
    unreachable_by_tier: dict[str, int]
    """Failures whose fallback chain reached the top rung and still got nothing.

    Counted apart from ordinary failures because the fix is different: an
    ordinary failure is a bad minute at one provider, whereas a request that
    exhausted the whole ladder means the ladder has no model this deployment can
    actually reach — an entitlement or billing problem, not a routing one."""

    def __post_init__(self) -> None:
        for name in (
            "spend_micro_usd",
            "counterfactual_micro_usd",
            "saving_micro_usd",
        ):
            validate_micro_usd(getattr(self, name), field=name)

    @property
    def ok_count(self) -> int:
        return self.requests_by_status.get("ok", 0)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityFacts:
    """What stage 03 found, read back rather than recomputed."""

    validated: bool
    """False when the month has no `regret.json` at all. Every field below is
    then zero or empty, and the report's verdict is INCONCLUSIVE."""

    sample_percent: int
    cheap_routed_count: int
    cheap_routed_by_tier: dict[str, int]
    shadow_record_count: int
    validated_count: int
    inspected_percent: int
    """Validated records as a floored percentage of the month's cheap-routed
    requests. The denominator of every regret figure in this report, stated so
    nobody reads a rate as if it covered the whole month."""

    overall: RegretGroup | None
    by_rung: dict[str, RegretGroup]
    by_tier: dict[str, RegretGroup]
    tier_verdicts: dict[str, str]
    """One sentence per tier, from `validate.report.tier_verdict_line`. The same
    function `ledger summary` calls, so the two can never word it differently."""

    position_bias_count: int
    judge_error_count: int
    reference_cost_micro_usd: int
    judge_cost_micro_usd: int
    validation_cost_micro_usd: int
    overhead_percent_of_saving: int
    """Validation cost as a floored percentage of the month's saving, or -1 when
    the month saved nothing and the ratio has no meaning. Reported next to the
    saving rather than netted out of it: hiding the price of proving a saving is
    the accounting mistake this whole system exists to catch, one level up."""

    def __post_init__(self) -> None:
        for name in (
            "reference_cost_micro_usd",
            "judge_cost_micro_usd",
            "validation_cost_micro_usd",
        ):
            validate_micro_usd(getattr(self, name), field=name)

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["overall"] = self.overall.to_json_dict() if self.overall else None
        payload["by_rung"] = {key: value.to_json_dict() for key, value in self.by_rung.items()}
        payload["by_tier"] = {key: value.to_json_dict() for key, value in self.by_tier.items()}
        return payload


@dataclass(frozen=True)
class MonthReport:
    """One month, judged. The artifact stage 04 exists to produce."""

    month: str
    generated_utc: str
    synthetic: bool
    prices_verified: bool
    verdict: str
    verdict_reason: str
    spend: SpendFacts
    quality: QualityFacts
    thresholds: dict[str, float]
    """The numbers the verdict rests on, copied in so the report is readable
    without the config file that produced it."""

    config_path: str = ""
    ladder: tuple[str, ...] = ()
    """Which configuration produced this month, and its rungs cheapest first.

    Both are provenance rather than data. A saving is measured against whatever
    the top rung happens to be, so a report from a two-rung demo ladder and one
    from the three-rung production ladder are answering different questions —
    and a document that gets read out of context has to say which."""

    recommendations: tuple[Recommendation, ...] = ()
    inconsistent_request_ids: tuple[str, ...] = ()
    """Verdicts naming a request the month's ledger does not hold. Named rather
    than quietly dropped: a figure computed around an inconsistency is a figure
    whose denominator nobody can check."""

    currency: str = field(default=CURRENCY)
    schema_version: int = field(default=REPORT_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ReportError(
                f"verdict must be one of {list(VERDICTS)}, got {self.verdict!r}"
            )
        for name in ("month", "generated_utc", "verdict_reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ReportError(f"{name} must be a non-empty string")
        if self.currency != CURRENCY:
            raise ReportError(f"currency must be {CURRENCY}, got {self.currency!r}")

    @property
    def exit_code(self) -> int:
        return EXIT_BY_VERDICT[self.verdict]

    @property
    def config_changes(self) -> tuple[Recommendation, ...]:
        return tuple(item for item in self.recommendations if item.is_config_change)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "month": self.month,
            "generated_utc": self.generated_utc,
            "synthetic": self.synthetic,
            "prices_verified": self.prices_verified,
            "currency": self.currency,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "exit_code": self.exit_code,
            "thresholds": dict(self.thresholds),
            "config_path": self.config_path,
            "ladder": list(self.ladder),
            "spend": self.spend.to_json_dict(),
            "quality": self.quality.to_json_dict(),
            "recommendations": [item.to_json_dict() for item in self.recommendations],
            "inconsistent_request_ids": list(self.inconsistent_request_ids),
        }
