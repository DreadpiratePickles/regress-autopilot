"""Stage 03's loop: re-answer on the top rung, judge both answers, record one verdict.

The order is the argument. First the *reference* answer — the same request, on
the rung the router chose not to use — because without it there is nothing to
compare against and "the cheap answer was fine" is an opinion. Then two
independent judgements of the pair:

  - **Criteria**, where the workload supplied them. Each criterion is judged on
    the cheap answer and on the reference answer separately, and regret means the
    cheap answer failed something the reference answer delivered. Judging only
    the cheap answer would count every criterion no model could satisfy as a
    routing mistake.
  - **Pairwise**, always. Most traffic has no criteria, and the general question
    — did the user lose anything — does not need them.

A request is regretted if either signal says so. Neither can silently become the
other, and a judgement that could not be read becomes a judge error rather than a
verdict: `None` is a third state everywhere, not a synonym for "passed".

Nothing here writes a file. The caller decides where verdicts land, which is what
lets the CLI append each one as it is produced and stay resumable after a crash.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from regression_detect.judge.criterion import JudgeError, judge_criterion

from ..ledger.store import utc_timestamp
from ..providers.metered import ProviderError
from ..route.ladder import Rung
from ..route.router import ProviderFactory
from .metering import CostMeter, MeteringTextProvider
from .pairwise import PairwiseError, combine_orders, judge_pairwise
from .shadow import ShadowRecord
from .verdicts import CriterionVerdict, VerdictRecord

JUDGE_TEMPERATURE = 0.0
"""Judging is not creative work. Zero so a re-judged pair gives the same answer."""


@dataclass(frozen=True)
class ValidationContext:
    """Everything one validation run needs that is not the records themselves."""

    provider_factory: ProviderFactory
    reference_rung: Rung
    """The top rung. What the router would have spent, spent deliberately."""

    judge_rung: Rung
    """The rung whose tariff prices the judge. Configuration guarantees the judge
    model is on the ladder, because a judge call this file could not price would
    make the reported overhead a guess."""

    system_prompt: str
    reference_temperature: float = field(default=0.0)
    pacer: Callable[[], None] | None = field(default=None)
    """Called before every model call. Provider quotas are per minute and one
    record costs up to five calls, so a batch has to be spread out."""


def _meters(context: ValidationContext) -> tuple[CostMeter, CostMeter]:
    reference = CostMeter(
        input_price_micro_usd_per_1k_tokens=(
            context.reference_rung.input_price_micro_usd_per_1k_tokens
        ),
        output_price_micro_usd_per_1k_tokens=(
            context.reference_rung.output_price_micro_usd_per_1k_tokens
        ),
    )
    judge = CostMeter(
        input_price_micro_usd_per_1k_tokens=(
            context.judge_rung.input_price_micro_usd_per_1k_tokens
        ),
        output_price_micro_usd_per_1k_tokens=(
            context.judge_rung.output_price_micro_usd_per_1k_tokens
        ),
    )
    return reference, judge


def _judge_one_criterion(
    *, request_text: str, answer: str, criterion: str, provider: MeteringTextProvider
) -> tuple[bool | None, str | None, str | None]:
    """Judge one answer against one criterion. Returns (passed, reason, error)."""
    try:
        verdict = judge_criterion(
            ticket=request_text,
            summary=answer,
            criterion=criterion,
            provider=provider,
            temperature=JUDGE_TEMPERATURE,
        )
    except (JudgeError, ProviderError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return verdict.passed, verdict.reason, None


def _judge_criteria(
    record: ShadowRecord, reference_answer: str, provider: MeteringTextProvider
) -> tuple[tuple[CriterionVerdict, ...], bool | None, list[str]]:
    """Judge every criterion on both answers and decide whether the cheap one lost."""
    verdicts: list[CriterionVerdict] = []
    errors: list[str] = []

    for criterion in record.criteria:
        cheap_passed, cheap_reason, cheap_error = _judge_one_criterion(
            request_text=record.request_text,
            answer=record.answer_text,
            criterion=criterion,
            provider=provider,
        )
        reference_passed, reference_reason, reference_error = _judge_one_criterion(
            request_text=record.request_text,
            answer=reference_answer,
            criterion=criterion,
            provider=provider,
        )
        if cheap_error:
            errors.append(f"criterion (cheap answer) {criterion!r}: {cheap_error}")
        if reference_error:
            errors.append(f"criterion (reference answer) {criterion!r}: {reference_error}")
        verdicts.append(
            CriterionVerdict(
                criterion=criterion,
                cheap_passed=cheap_passed,
                reference_passed=reference_passed,
                cheap_reason=cheap_reason,
                reference_reason=reference_reason,
            )
        )

    comparable = [
        item
        for item in verdicts
        if item.cheap_passed is not None and item.reference_passed is not None
    ]
    regret = any(item.is_regret for item in comparable) if comparable else None
    return tuple(verdicts), regret, errors


def _judge_pair(
    record: ShadowRecord, reference_answer: str, provider: MeteringTextProvider
) -> tuple[bool | None, bool | None, list[str]]:
    """Run the comparison in both orders. A failure in either loses both."""
    orders = (
        ("pairwise forward", record.answer_text, reference_answer),
        ("pairwise reverse", reference_answer, record.answer_text),
    )
    results: list[bool | None] = []
    errors: list[str] = []
    for label, answer_a, answer_b in orders:
        try:
            verdict = judge_pairwise(
                request=record.request_text,
                answer_a=answer_a,
                answer_b=answer_b,
                provider=provider,
            )
        except (PairwiseError, ProviderError) as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            results.append(None)
        else:
            results.append(verdict.a_at_least_as_good)
    return results[0], results[1], errors


def validate_record(
    record: ShadowRecord, context: ValidationContext, *, moment: datetime | None = None
) -> VerdictRecord:
    """Validate one shadow record. Never raises for a judge failure — it records it."""
    reference_meter, judge_meter = _meters(context)
    reference_provider = MeteringTextProvider(
        context.provider_factory(context.reference_rung.model_id),
        reference_meter,
        before_call=context.pacer,
    )
    judge_provider = MeteringTextProvider(
        context.provider_factory(context.judge_rung.model_id),
        judge_meter,
        before_call=context.pacer,
    )

    errors: list[str] = []
    reference_answer: str | None = None
    try:
        reference_answer = reference_provider.call(
            system=context.system_prompt,
            user=record.request_text,
            temperature=context.reference_temperature,
        ).text
    except ProviderError as exc:
        # Without a reference answer there is nothing to compare against, so no
        # judgement is attempted and none is invented.
        errors.append(f"reference answer: {type(exc).__name__}: {exc}")

    criterion_verdicts: tuple[CriterionVerdict, ...] = ()
    criteria_regret: bool | None = None
    forward: bool | None = None
    reverse: bool | None = None

    if reference_answer is not None:
        criterion_verdicts, criteria_regret, criteria_errors = _judge_criteria(
            record, reference_answer, judge_provider
        )
        errors += criteria_errors
        forward, reverse, pair_errors = _judge_pair(record, reference_answer, judge_provider)
        errors += pair_errors

    pairwise = combine_orders(forward=forward, reverse=reverse)

    return VerdictRecord(
        request_id=record.request_id,
        ts_utc=utc_timestamp(moment),
        team_id=record.team_id,
        tier=record.tier,
        rung_index=record.rung_index,
        chosen_model_id=record.chosen_model_id,
        reference_model_id=context.reference_rung.model_id,
        judge_model_id=context.judge_rung.model_id,
        criteria_regret=criteria_regret,
        criterion_verdicts=criterion_verdicts,
        pairwise_forward=pairwise.forward,
        pairwise_reverse=pairwise.reverse,
        pairwise_regret=pairwise.regret,
        position_bias_detected=pairwise.position_bias_detected,
        judge_errors=tuple(errors),
        reference_cost_micro_usd=reference_meter.cost_micro_usd,
        judge_cost_micro_usd=judge_meter.cost_micro_usd,
        reference_latency_ms=reference_meter.latency_ms,
        judge_latency_ms=judge_meter.latency_ms,
    )


def validate_records(
    records: Sequence[ShadowRecord],
    context: ValidationContext,
    *,
    limit: int | None = None,
    skip_ids: Iterable[str] = (),
    on_verdict: Callable[[VerdictRecord], None] | None = None,
    moment: datetime | None = None,
) -> list[VerdictRecord]:
    """Validate a batch, skipping what has already been done and stopping at `limit`.

    `on_verdict` is called as each verdict is produced rather than at the end, so
    a caller can persist progress and a run interrupted halfway is resumable.

    Raises:
        ValueError: `limit` is negative.
    """
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    ):
        raise ValueError(f"limit must be a non-negative integer, got {limit!r}")

    already = frozenset(skip_ids)
    produced: list[VerdictRecord] = []
    for record in records:
        if limit is not None and len(produced) >= limit:
            break
        if record.request_id in already:
            continue
        verdict = validate_record(record, context, moment=moment)
        produced.append(verdict)
        if on_verdict is not None:
            on_verdict(verdict)
    return produced
