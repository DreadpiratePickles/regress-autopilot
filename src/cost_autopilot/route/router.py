"""Route one request: choose a rung, enforce the budget, call, fall back, record.

The order of operations is the design. Classification and the budget check both
happen before any model is called, so a refused request costs nothing and a
misconfigured ladder fails before it can spend. Only then does the router call,
and only a `ProviderTransientError` — a failure the provider already retried
internally and still could not get past — moves it up a rung.

Fallback is bounded by the ladder itself: at most one attempt per remaining
rung, no retries beyond the provider's own, no wrapping around to the bottom.
The bound is structural rather than a counter, so it cannot drift out of step
with the ladder's length. Repeating the same failure is not progress; when the
rungs run out the router stops, records `status: failed` with the last error
type, and returns.

Every path — success, refusal, exhaustion — produces exactly one ledger row.
That is what makes the ledger totals mean something: the row count is the
request count, not the success count.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..classify.scorer import Classification, ScorerThresholds, Tier, classify_text
from ..ledger.row import STATUS_FAILED, STATUS_OK, STATUS_REFUSED, LedgerRow, text_sha256
from ..ledger.store import month_key, utc_timestamp
from ..providers.metered import Completion, MeteredProvider, ProviderError, ProviderTransientError
from ..validate.sampling import is_sampled
from ..validate.shadow import ShadowRecord
from .budget import BudgetExceededError, Budgets, check_budget
from .ladder import Ladder, LadderError

ProviderFactory = Callable[[str], MeteredProvider]
"""`model_id -> MeteredProvider`. The router never constructs a provider itself,
which is what lets `--dry-run` swap in fakes without the routing logic knowing."""

SpendReader = Callable[[str, str], int]
"""`(team_id, month) -> micro-USD spent`. Injected rather than reached for so the
router stays testable without a ledger directory on disk."""

DEFAULT_TEMPERATURE = 0.0
"""Zero so a re-run of the same workload is as comparable as the provider
allows. Phase B compares answers across runs; sampling noise would blur it."""


class RoutingError(Exception):
    """The router could not be built or run. Distinct from a routed failure."""


@dataclass(frozen=True)
class RoutePolicy:
    """Per-tier minimum starting rung, on top of the ladder's own capability rule.

    The ladder answers "which rungs *can* serve this tier". The policy answers
    "which rungs should we bother trying". The effective start is the later of
    the two, so a policy can skip a cheap rung it does not trust for a tier, but
    can never send a tier to a rung whose ceiling is below it.
    """

    start_rung_by_tier: dict[Tier, int]

    def floor_for(self, tier: Tier) -> int:
        return self.start_rung_by_tier.get(tier, 0)


@dataclass(frozen=True)
class RouteOutcome:
    """What happened to one request, and the row that records it."""

    row: LedgerRow
    completion: Completion | None
    classification: Classification
    shadow_record: ShadowRecord | None = None
    """Present only when this request was drawn into the validation sample.

    Returned rather than written here for the same reason the ledger row is: the
    router decides, the caller persists. A shadow write that failed inside
    `route` would take a routing decision down with it, and the ledger row is the
    record that must survive."""

    @property
    def status(self) -> str:
        return self.row.status


@dataclass(frozen=True)
class Router:
    """Stage 02, assembled. Holds no mutable state between requests."""

    ladder: Ladder
    policy: RoutePolicy
    budgets: Budgets
    thresholds: ScorerThresholds
    provider_factory: ProviderFactory
    read_spend_micro_usd: SpendReader
    system_prompt: str
    log_text: bool = False
    temperature: float = DEFAULT_TEMPERATURE
    shadow_enabled: bool = False
    """Whether cheap answers may be kept for stage 03. Off here by default so a
    router built without `[validate]` in mind stores no request text at all."""

    shadow_sample_percent: int = 0

    def start_index_for(self, tier: Tier) -> int:
        """The first rung to try: the later of the capability floor and the policy floor.

        Raises:
            RoutingError: the policy points past the end of the ladder. That is
                a configuration mistake and fails loudly rather than silently
                collapsing onto the top rung.
        """
        try:
            capable = self.ladder.first_capable_index(tier)
        except LadderError as exc:
            raise RoutingError(str(exc)) from exc
        start = max(capable, self.policy.floor_for(tier))
        if start >= len(self.ladder.rungs):
            raise RoutingError(
                f"policy starts tier {tier.value} at rung {start}, but the ladder has "
                f"{len(self.ladder.rungs)} rung(s)"
            )
        return start

    def route(
        self,
        *,
        text: str,
        team_id: str,
        moment: datetime | None = None,
        request_id: str | None = None,
        criteria: tuple[str, ...] = (),
    ) -> RouteOutcome:
        """Route one request and return the outcome, always with a ledger row.

        Never raises for an ordinary failure: a budget refusal and an exhausted
        ladder are both recorded outcomes, because the ledger has to see them.
        `RoutingError` and classification errors still propagate — those are
        broken configuration or unusable input, not results.

        `criteria` are the workload's pass criteria for this request. They play
        no part in routing — the classifier must not be able to see the answer —
        and are only copied onto a shadow record if one is made.
        """
        classification = classify_text(text, self.thresholds)
        identity = _RequestIdentity(
            request_id=request_id or str(uuid.uuid4()),
            ts_utc=utc_timestamp(moment),
            team_id=team_id,
            text=text,
            criteria=criteria,
        )

        try:
            check_budget(
                team_id=team_id,
                month=month_key(moment),
                spend_micro_usd=self.read_spend_micro_usd(team_id, month_key(moment)),
                budgets=self.budgets,
            )
        except BudgetExceededError as exc:
            return self._refusal(identity, classification, exc)

        return self._attempt_rungs(identity, classification)

    def _attempt_rungs(
        self, identity: "_RequestIdentity", classification: Classification
    ) -> RouteOutcome:
        start = self.start_index_for(classification.tier)
        tried: list[str] = []
        last_error: ProviderError | None = None

        for rung in self.ladder.rungs[start:]:
            tried.append(rung.model_id)
            provider = self.provider_factory(rung.model_id)
            try:
                completion = provider.complete(
                    system=self.system_prompt,
                    user=identity.text,
                    temperature=self.temperature,
                )
            except ProviderTransientError as exc:
                # The provider has already exhausted its own bounded retries.
                # Another attempt on the same rung would just repeat it, so the
                # only useful move left is a different model.
                last_error = exc
                continue
            except ProviderError as exc:
                # Config errors and malformed replies are not transient. Trying a
                # more expensive model would spend money on the same fault.
                last_error = exc
                break
            return self._success(identity, classification, rung.index, completion, tuple(tried))

        return self._exhausted(identity, classification, tuple(tried), last_error)

    def _success(
        self,
        identity: "_RequestIdentity",
        classification: Classification,
        rung_index: int,
        completion: Completion,
        tried: tuple[str, ...],
    ) -> RouteOutcome:
        cost = self.ladder.cost_micro_usd(
            rung_index=rung_index,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        counterfactual = self.ladder.top_rung_cost_micro_usd(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        record = self._shadow_record(identity, classification, rung_index, completion)
        row = self._row(
            identity,
            classification,
            chosen_model_id=completion.model_id,
            fallback_chain=tried,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_micro_usd=cost,
            counterfactual_micro_usd=counterfactual,
            latency_ms=completion.latency_ms,
            status=STATUS_OK,
            error_type=None,
            shadow_sampled=record is not None,
        )
        return RouteOutcome(
            row=row,
            completion=completion,
            classification=classification,
            shadow_record=record,
        )

    def _shadow_record(
        self,
        identity: "_RequestIdentity",
        classification: Classification,
        rung_index: int,
        completion: Completion,
    ) -> ShadowRecord | None:
        """Keep this answer for stage 03, or decide not to. The only text this
        system persists passes through here.

        Three gates, all of which must open: validation is enabled at all, the
        answer did not come from the top rung, and the request id falls in the
        sample. The top-rung exclusion is not an optimisation — a reference
        answer for a top-rung request would come from the rung that already
        answered it, so there would be nothing to compare.
        """
        if not self.shadow_enabled or self.shadow_sample_percent <= 0:
            return None
        if rung_index >= self.ladder.top_rung.index:
            return None
        if not is_sampled(identity.request_id, self.shadow_sample_percent):
            return None
        return ShadowRecord(
            request_id=identity.request_id,
            ts_utc=identity.ts_utc,
            team_id=identity.team_id,
            tier=classification.tier.value,
            complexity_score=classification.complexity_score,
            chosen_model_id=completion.model_id,
            rung_index=rung_index,
            request_text=identity.text,
            answer_text=completion.text,
            criteria=identity.criteria,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )

    def _refusal(
        self,
        identity: "_RequestIdentity",
        classification: Classification,
        error: BudgetExceededError,
    ) -> RouteOutcome:
        row = self._row(
            identity,
            classification,
            chosen_model_id=None,
            fallback_chain=(),
            input_tokens=0,
            output_tokens=0,
            cost_micro_usd=0,
            counterfactual_micro_usd=0,
            latency_ms=0,
            status=STATUS_REFUSED,
            error_type=type(error).__name__,
        )
        return RouteOutcome(row=row, completion=None, classification=classification)

    def _exhausted(
        self,
        identity: "_RequestIdentity",
        classification: Classification,
        tried: tuple[str, ...],
        last_error: ProviderError | None,
    ) -> RouteOutcome:
        row = self._row(
            identity,
            classification,
            chosen_model_id=None,
            fallback_chain=tried,
            input_tokens=0,
            output_tokens=0,
            cost_micro_usd=0,
            counterfactual_micro_usd=0,
            latency_ms=0,
            status=STATUS_FAILED,
            error_type=type(last_error).__name__ if last_error else "NoRungAvailable",
        )
        return RouteOutcome(row=row, completion=None, classification=classification)

    def _row(
        self,
        identity: "_RequestIdentity",
        classification: Classification,
        *,
        chosen_model_id: str | None,
        fallback_chain: tuple[str, ...],
        input_tokens: int,
        output_tokens: int,
        cost_micro_usd: int,
        counterfactual_micro_usd: int,
        latency_ms: int,
        status: str,
        error_type: str | None,
        shadow_sampled: bool = False,
    ) -> LedgerRow:
        return LedgerRow(
            request_id=identity.request_id,
            ts_utc=identity.ts_utc,
            team_id=identity.team_id,
            tier=classification.tier.value,
            complexity_score=classification.complexity_score,
            reasons=classification.reasons,
            chosen_model_id=chosen_model_id,
            fallback_chain=fallback_chain,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micro_usd=cost_micro_usd,
            counterfactual_top_model_cost_micro_usd=counterfactual_micro_usd,
            latency_ms=latency_ms,
            status=status,
            error_type=error_type,
            request_sha256=text_sha256(identity.text),
            request_text=identity.text if self.log_text else None,
            shadow_sampled=shadow_sampled,
        )


@dataclass(frozen=True)
class _RequestIdentity:
    """The per-request facts every row needs, gathered once."""

    request_id: str
    ts_utc: str
    team_id: str
    text: str
    criteria: tuple[str, ...] = ()
