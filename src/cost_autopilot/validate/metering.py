"""Charge stage 03 for its own calls, at the same tariff stage 02 is charged at.

Validation costs money. A reference answer on the top rung is exactly the call
the router avoided, and every judgement is another call on top. Reporting a
saving while leaving that out would be the same accounting mistake this system
exists to catch, one level up — so the overhead is measured, not estimated, and
printed next to the saving it qualifies.

`MeteringTextProvider` exists because project 1's judging seam takes a `Provider`
(text in, text out) while this package's providers are `MeteredProvider` (text
plus usage). Rather than fork the judge, the metered provider is narrowed to the
protocol the judge expects and the usage it would have dropped is kept here.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..money import completion_cost_micro_usd
from ..providers.metered import Completion, MeteredProvider


@dataclass
class CostMeter:
    """Running totals for one leg of a validation: reference, or judging.

    Mutable on purpose — it is an accumulator, and the immutable record it feeds
    is the `VerdictRecord`. Each call is ceiled on its own, exactly as the ledger
    prices a routed call, so a total here is comparable with a total there.
    """

    input_price_micro_usd_per_1k_tokens: int
    output_price_micro_usd_per_1k_tokens: int
    calls: int = field(default=0)
    input_tokens: int = field(default=0)
    output_tokens: int = field(default=0)
    cost_micro_usd: int = field(default=0)
    latency_ms: int = field(default=0)

    def record(self, completion: Completion) -> None:
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        self.latency_ms += completion.latency_ms
        self.cost_micro_usd += completion_cost_micro_usd(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            input_price_micro_usd_per_1k=self.input_price_micro_usd_per_1k_tokens,
            output_price_micro_usd_per_1k=self.output_price_micro_usd_per_1k_tokens,
        )


class MeteringTextProvider:
    """A `MeteredProvider` narrowed to project 1's text-only `Provider` protocol.

    Every call is metered on the way through and, if a pacer was supplied, paced
    before it leaves. Putting both here means every model call stage 03 makes —
    criterion judging included, which happens inside project 1's code — is
    counted and paced, with no call site able to forget.
    """

    def __init__(
        self,
        provider: MeteredProvider,
        meter: CostMeter,
        *,
        before_call: Callable[[], None] | None = None,
    ) -> None:
        self._provider = provider
        self._meter = meter
        self._before_call = before_call
        self.model_id = provider.model_id

    def complete(self, *, system: str, user: str, temperature: float) -> str:
        completion = self.call(system=system, user=user, temperature=temperature)
        return completion.text

    def call(self, *, system: str, user: str, temperature: float) -> Completion:
        """The metered call itself, for the one caller that needs the token counts."""
        if self._before_call is not None:
            self._before_call()
        completion = self._provider.complete(system=system, user=user, temperature=temperature)
        self._meter.record(completion)
        return completion
