"""The model ladder: which models exist, in what order, at what price.

A ladder is an ordered list of rungs, cheapest first, each carrying a capability
ceiling — the hardest tier that rung is trusted with. Two invariants make the
routing rule "take the first rung that can handle this tier" mean "take the
cheapest rung that can":

  1. Indices are consecutive from zero, so "first" is well defined.
  2. Ceilings never decrease going up the ladder, so a capable rung is never
     hidden behind an incapable one further up.

Both are checked when the ladder is built rather than assumed, because a ladder
comes from a configuration file a human edits.

Prices are integer micro-USD per 1,000 tokens and are validated here, at the
boundary. `prices_verified` travels with the ladder rather than being dropped
after loading: a spend figure computed from prices nobody checked has to be
labelled as such everywhere it surfaces, and it cannot be if the flag stopped at
the parser.
"""

from dataclasses import dataclass

from ..classify.scorer import Tier
from ..money import completion_cost_micro_usd


class LadderError(ValueError):
    """A rung or a ladder is not usable: bad price, bad order, bad ceiling."""


def _price(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LadderError(
            f"{field} must be a non-negative integer of micro-USD per 1k tokens, "
            f"got {type(value).__name__}: {value!r}"
        )
    if value < 0:
        raise LadderError(f"{field} must be a non-negative integer, got {value}")
    return value


@dataclass(frozen=True)
class Rung:
    """One model the router may call, with its price and its ceiling."""

    index: int
    model_ref: str
    """The name of the environment variable in `config.py` that resolves to the
    model id. Configuration names rungs this way so no vendor string ever needs
    to appear in `autopilot.toml`."""

    model_id: str
    input_price_micro_usd_per_1k_tokens: int
    output_price_micro_usd_per_1k_tokens: int
    max_tier: Tier
    """The hardest tier this rung is trusted with. A ceiling, not a preference:
    the router will never send a harder request here as a first choice, though a
    fallback may still land above it."""

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise LadderError(f"rung index must be a non-negative integer, got {self.index!r}")
        for name in ("model_ref", "model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise LadderError(f"rung {self.index}: {name} must be a non-empty string")
        if not isinstance(self.max_tier, Tier):
            raise LadderError(
                f"rung {self.index}: max_tier must be a Tier, got {type(self.max_tier).__name__}"
            )
        _price(
            self.input_price_micro_usd_per_1k_tokens,
            field=f"rung {self.index} input_price_micro_usd_per_1k_tokens",
        )
        _price(
            self.output_price_micro_usd_per_1k_tokens,
            field=f"rung {self.index} output_price_micro_usd_per_1k_tokens",
        )


@dataclass(frozen=True)
class Ladder:
    """The rungs in cheapest-first order, plus whether their prices were checked."""

    rungs: tuple[Rung, ...]
    prices_verified: bool

    def __post_init__(self) -> None:
        if not self.rungs:
            raise LadderError("a ladder needs at least one rung")
        for position, item in enumerate(self.rungs):
            if not isinstance(item, Rung):
                raise LadderError(f"ladder entry {position} is not a Rung")
            if item.index != position:
                raise LadderError(
                    f"ladder rung at position {position} declares index {item.index}; "
                    "rung index must equal its position, counting from zero"
                )
        ceilings = [item.max_tier.rank for item in self.rungs]
        if any(later < earlier for earlier, later in zip(ceilings, ceilings[1:], strict=False)):
            raise LadderError(
                "ladder capability ceilings must be non-decreasing from cheapest to most "
                "expensive, otherwise 'the first capable rung' is not the cheapest capable rung"
            )
        model_ids = [item.model_id for item in self.rungs]
        if len(set(model_ids)) != len(model_ids):
            raise LadderError(f"ladder has duplicate model id(s): {sorted(model_ids)}")
        if not isinstance(self.prices_verified, bool):
            raise LadderError("prices_verified must be a boolean")

    @property
    def top_rung(self) -> Rung:
        """The most expensive rung: the fallback of last resort, and the
        baseline every ledger row's counterfactual cost is measured against."""
        return self.rungs[-1]

    def rung(self, index: int) -> Rung:
        out_of_range = not isinstance(index, int) or not 0 <= index < len(self.rungs)
        if isinstance(index, bool) or out_of_range:
            raise LadderError(
                f"rung index {index!r} is outside the ladder (0..{len(self.rungs) - 1})"
            )
        return self.rungs[index]

    def first_capable_index(self, tier: Tier) -> int:
        """Index of the cheapest rung whose ceiling reaches `tier`.

        Raises:
            LadderError: no rung is trusted with this tier. That is a
                configuration mistake, not a routing outcome, so it fails loudly
                rather than quietly sending the request to the top rung.
        """
        for item in self.rungs:
            if item.max_tier >= tier:
                return item.index
        raise LadderError(
            f"no rung on the ladder is trusted with tier {tier.value}; "
            f"the top rung's ceiling is {self.top_rung.max_tier.value}"
        )

    def cost_micro_usd(self, *, rung_index: int, input_tokens: int, output_tokens: int) -> int:
        """What a completion of this size cost on this rung. Integer micro-USD."""
        item = self.rung(rung_index)
        return completion_cost_micro_usd(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_price_micro_usd_per_1k=item.input_price_micro_usd_per_1k_tokens,
            output_price_micro_usd_per_1k=item.output_price_micro_usd_per_1k_tokens,
        )

    def top_rung_cost_micro_usd(self, *, input_tokens: int, output_tokens: int) -> int:
        """What the same token counts would have cost on the top rung.

        This is the counterfactual recorded on every ledger row. It is an honest
        approximation, not a measurement: the top model would have produced a
        different number of output tokens for the same prompt. What it does
        support exactly is the claim the ledger actually makes — "these tokens,
        priced at the top rung's tariff, would have cost this much" — which is
        the comparison a finance reader wants and the only one available without
        running every request twice.
        """
        return self.cost_micro_usd(
            rung_index=self.top_rung.index,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
