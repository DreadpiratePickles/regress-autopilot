"""Regret: the share of sampled cheap answers that were not good enough.

This is the number the whole system is judged on, and the number nobody else
reports. A saving with unmeasured regret is not a saving; it is a cost that has
been moved somewhere no one is looking.

Three rules decide what the arithmetic is allowed to say:

  - **A verdict that could not be read is not a pass.** Records with no readable
    judgement are excluded from `n` and counted in `judge_error_count`. Counting
    them as "no regret" would let a broken judge improve the numbers.
  - **Every rate travels with an interval.** Six samples with one regret is a
    rate of 17% and an interval from 3% to 56%; quoting the 17% alone would be
    presenting a coin toss as a measurement. The Wilson interval is imported
    from project 1 rather than reimplemented.
  - **The denominator is stated.** `sample_percent` and the month's total
    cheap-routed count travel in the file, so a reader can see what fraction of
    the traffic was actually inspected instead of assuming it was all of it.

Money stays integer micro-USD. Rates and interval bounds are floats, because a
Wilson bound is a real number and rounding it to hide that would be worse.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from regression_detect.compare import wilson_interval

from ..money import CURRENCY, validate_micro_usd
from .verdicts import VerdictRecord

REGRET_SCHEMA_VERSION = 1


class RegretError(ValueError):
    """A regret report is malformed or holds a version this build cannot read."""


@dataclass(frozen=True)
class RegretGroup:
    """One slice — everything, one rung, or one tier — counted and bounded."""

    label: str
    n: int
    """Records with a readable verdict. The denominator, and never inflated by
    records the judge could not decide."""

    regret_count: int
    regret_rate: float
    wilson_low: float
    wilson_high: float
    judge_error_count: int
    validation_cost_micro_usd: int

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def _group(label: str, verdicts: list[VerdictRecord]) -> RegretGroup:
    decided = [item for item in verdicts if item.regret is not None]
    regret_count = sum(1 for item in decided if item.regret)
    n = len(decided)
    low, high = wilson_interval(regret_count, n)
    return RegretGroup(
        label=label,
        n=n,
        regret_count=regret_count,
        regret_rate=(regret_count / n) if n else 0.0,
        wilson_low=low,
        wilson_high=high,
        judge_error_count=sum(len(item.judge_errors) for item in verdicts),
        validation_cost_micro_usd=sum(item.validation_cost_micro_usd for item in verdicts),
    )


@dataclass(frozen=True)
class RegretReport:
    """One month's regret, overall and broken down, with what it cost to find out."""

    month: str
    generated_utc: str
    sample_percent: int
    cheap_routed_count: int
    """Successful non-top-rung requests in the month's ledger: the population the
    shadow sample was drawn from."""

    shadow_record_count: int
    validated_count: int
    overall: RegretGroup
    by_rung: dict[str, RegretGroup]
    by_tier: dict[str, RegretGroup]
    reference_cost_micro_usd: int
    judge_cost_micro_usd: int
    validation_cost_micro_usd: int
    position_bias_count: int
    currency: str = field(default=CURRENCY)
    schema_version: int = field(default=REGRET_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        for name in (
            "reference_cost_micro_usd",
            "judge_cost_micro_usd",
            "validation_cost_micro_usd",
        ):
            validate_micro_usd(getattr(self, name), field=name)

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["overall"] = self.overall.to_json_dict()
        payload["by_rung"] = {key: value.to_json_dict() for key, value in self.by_rung.items()}
        payload["by_tier"] = {key: value.to_json_dict() for key, value in self.by_tier.items()}
        return payload


def build_report(
    verdicts: list[VerdictRecord],
    *,
    month: str,
    sample_percent: int,
    cheap_routed_count: int,
    shadow_record_count: int,
    generated_utc: str,
) -> RegretReport:
    """Aggregate verdicts into the month's regret. Pure arithmetic; opens no file."""
    by_rung: dict[str, list[VerdictRecord]] = defaultdict(list)
    by_tier: dict[str, list[VerdictRecord]] = defaultdict(list)
    for verdict in verdicts:
        by_rung[str(verdict.rung_index)].append(verdict)
        by_tier[verdict.tier].append(verdict)

    reference_cost = sum(item.reference_cost_micro_usd for item in verdicts)
    judge_cost = sum(item.judge_cost_micro_usd for item in verdicts)

    return RegretReport(
        month=month,
        generated_utc=generated_utc,
        sample_percent=sample_percent,
        cheap_routed_count=cheap_routed_count,
        shadow_record_count=shadow_record_count,
        validated_count=len(verdicts),
        overall=_group("overall", list(verdicts)),
        by_rung={key: _group(f"rung {key}", value) for key, value in sorted(by_rung.items())},
        by_tier={key: _group(key, value) for key, value in sorted(by_tier.items())},
        reference_cost_micro_usd=reference_cost,
        judge_cost_micro_usd=judge_cost,
        validation_cost_micro_usd=reference_cost + judge_cost,
        position_bias_count=sum(1 for item in verdicts if item.position_bias_detected),
    )


def _group_from_json(payload: object, *, where: str) -> RegretGroup:
    if not isinstance(payload, dict):
        raise RegretError(f"{where} must be a JSON object")
    known = set(RegretGroup.__dataclass_fields__)
    unknown = sorted(set(payload) - known)
    if unknown:
        raise RegretError(f"{where} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(known - set(payload))
    if missing:
        raise RegretError(f"{where} is missing field(s): {', '.join(missing)}")
    return RegretGroup(**payload)


def report_from_json_dict(payload: object) -> RegretReport:
    """Rebuild a report read back from disk, validating it at the boundary.

    Raises:
        RegretError: not an object, unknown or missing fields, or a schema
            version this build does not understand.
    """
    if not isinstance(payload, dict):
        raise RegretError(f"regret report must be a JSON object, got {type(payload).__name__}")

    known = set(RegretReport.__dataclass_fields__)
    unknown = sorted(set(payload) - known)
    if unknown:
        raise RegretError(f"regret report has unknown field(s): {', '.join(unknown)}")

    version = payload.get("schema_version")
    if version != REGRET_SCHEMA_VERSION:
        raise RegretError(
            f"regret report has schema_version {version!r}; this build understands "
            f"{REGRET_SCHEMA_VERSION}"
        )

    missing = sorted((known - {"schema_version", "currency"}) - set(payload))
    if missing:
        raise RegretError(f"regret report is missing field(s): {', '.join(missing)}")

    for name in ("by_rung", "by_tier"):
        if not isinstance(payload[name], dict):
            raise RegretError(f"{name} must be a JSON object")

    return RegretReport(
        **{
            **payload,
            "overall": _group_from_json(payload["overall"], where="overall"),
            "by_rung": {
                key: _group_from_json(value, where=f"by_rung.{key}")
                for key, value in payload["by_rung"].items()
            },
            "by_tier": {
                key: _group_from_json(value, where=f"by_tier.{key}")
                for key, value in payload["by_tier"].items()
            },
        }
    )
