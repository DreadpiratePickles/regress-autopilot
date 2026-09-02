"""The model ladder: ordered rungs, a capability ceiling, and integer prices."""

from dataclasses import FrozenInstanceError

import pytest

from cost_autopilot.classify.scorer import Tier
from cost_autopilot.route.ladder import Ladder, LadderError, Rung


def rung(index: int, max_tier: Tier, *, in_price: int = 300, out_price: int = 2500) -> Rung:
    return Rung(
        index=index,
        model_ref=f"REF_{index}",
        model_id=f"model-{index}",
        input_price_micro_usd_per_1k_tokens=in_price,
        output_price_micro_usd_per_1k_tokens=out_price,
        max_tier=max_tier,
    )


THREE_RUNGS = (
    rung(0, Tier.T1_TRIVIAL, in_price=300, out_price=2500),
    rung(1, Tier.T2_STANDARD, in_price=750, out_price=3750),
    rung(2, Tier.T3_COMPLEX, in_price=2000, out_price=12000),
)


class TestRungValidation:
    def test_builds_with_valid_values(self):
        assert rung(0, Tier.T1_TRIVIAL).model_id == "model-0"

    @pytest.mark.parametrize("price", [-1, 1.5, "300", None, True])
    def test_rejects_a_bad_input_price(self, price):
        with pytest.raises(LadderError):
            rung(0, Tier.T1_TRIVIAL, in_price=price)

    @pytest.mark.parametrize("price", [-1, 0.5, "2500", None])
    def test_rejects_a_bad_output_price(self, price):
        with pytest.raises(LadderError):
            rung(0, Tier.T1_TRIVIAL, out_price=price)

    def test_a_zero_price_is_allowed(self):
        # A free tier or a self-hosted rung is legitimate; a negative one is not.
        free = rung(0, Tier.T1_TRIVIAL, in_price=0, out_price=0)
        assert free.input_price_micro_usd_per_1k_tokens == 0

    def test_rejects_a_non_tier_ceiling(self):
        with pytest.raises(LadderError):
            Rung(
                index=0,
                model_ref="R",
                model_id="m",
                input_price_micro_usd_per_1k_tokens=1,
                output_price_micro_usd_per_1k_tokens=1,
                max_tier="T1_TRIVIAL",  # type: ignore[arg-type]
            )

    def test_is_frozen(self):
        item = rung(0, Tier.T1_TRIVIAL)
        with pytest.raises(FrozenInstanceError):
            item.model_id = "other"  # type: ignore[misc]


class TestLadderValidation:
    def test_builds_from_ordered_rungs(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert len(ladder.rungs) == 3

    def test_rejects_an_empty_ladder(self):
        with pytest.raises(LadderError, match="at least one rung"):
            Ladder(rungs=(), prices_verified=True)

    def test_rejects_out_of_order_indices(self):
        with pytest.raises(LadderError, match="index"):
            Ladder(rungs=(rung(1, Tier.T1_TRIVIAL), rung(0, Tier.T3_COMPLEX)), prices_verified=True)

    def test_rejects_a_ceiling_that_goes_backwards(self):
        # A ladder must be non-decreasing in capability, or "the first rung that
        # can handle this tier" stops meaning "the cheapest one that can".
        rungs = (rung(0, Tier.T3_COMPLEX), rung(1, Tier.T1_TRIVIAL))
        with pytest.raises(LadderError, match="non-decreasing"):
            Ladder(rungs=rungs, prices_verified=True)

    def test_rejects_a_duplicate_model_id(self):
        rungs = (
            Rung(0, "A", "same-model", 1, 1, Tier.T1_TRIVIAL),
            Rung(1, "B", "same-model", 2, 2, Tier.T3_COMPLEX),
        )
        with pytest.raises(LadderError, match="duplicate"):
            Ladder(rungs=rungs, prices_verified=True)

    def test_allows_equal_consecutive_ceilings(self):
        rungs = (rung(0, Tier.T2_STANDARD), rung(1, Tier.T2_STANDARD))
        assert len(Ladder(rungs=rungs, prices_verified=False).rungs) == 2


class TestCapabilityFloor:
    def test_trivial_starts_at_the_cheapest_rung(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert ladder.first_capable_index(Tier.T1_TRIVIAL) == 0

    def test_standard_skips_the_trivial_only_rung(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert ladder.first_capable_index(Tier.T2_STANDARD) == 1

    def test_complex_needs_the_top_rung(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert ladder.first_capable_index(Tier.T3_COMPLEX) == 2

    def test_raises_when_no_rung_can_serve_the_tier(self):
        ladder = Ladder(rungs=(rung(0, Tier.T1_TRIVIAL),), prices_verified=True)
        with pytest.raises(LadderError, match="no rung"):
            ladder.first_capable_index(Tier.T3_COMPLEX)


class TestTopRung:
    def test_top_rung_is_the_last_one(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert ladder.top_rung.index == 2
        assert ladder.top_rung.model_id == "model-2"

    def test_top_rung_of_a_single_rung_ladder_is_that_rung(self):
        ladder = Ladder(rungs=(rung(0, Tier.T3_COMPLEX),), prices_verified=True)
        assert ladder.top_rung.index == 0


class TestPricing:
    def test_prices_a_completion_with_integer_arithmetic(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        # rung 0: 1000 in at 300/1k -> 300; 1000 out at 2500/1k -> 2500.
        cost = ladder.cost_micro_usd(rung_index=0, input_tokens=1000, output_tokens=1000)
        assert cost == 2800
        assert isinstance(cost, int)

    def test_counterfactual_prices_the_same_tokens_on_the_top_rung(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        # rung 2: 1000 in at 2000/1k -> 2000; 1000 out at 12000/1k -> 12000.
        cost = ladder.top_rung_cost_micro_usd(input_tokens=1000, output_tokens=1000)
        assert cost == 14000

    def test_the_counterfactual_is_never_cheaper_on_a_sane_ladder(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        actual = ladder.cost_micro_usd(rung_index=0, input_tokens=500, output_tokens=200)
        counterfactual = ladder.top_rung_cost_micro_usd(input_tokens=500, output_tokens=200)
        assert counterfactual >= actual

    def test_zero_tokens_cost_nothing(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        assert ladder.cost_micro_usd(rung_index=1, input_tokens=0, output_tokens=0) == 0

    def test_rejects_an_unknown_rung_index(self):
        ladder = Ladder(rungs=THREE_RUNGS, prices_verified=True)
        with pytest.raises(LadderError, match="rung index"):
            ladder.cost_micro_usd(rung_index=9, input_tokens=1, output_tokens=1)
