"""Stage 02 end to end, without a network: rung choice, budget, fallback, rows."""

import pytest

from cost_autopilot.classify.scorer import ScorerThresholds, Tier
from cost_autopilot.ledger.row import STATUS_FAILED, STATUS_OK, STATUS_REFUSED
from cost_autopilot.providers.fake_metered import FakeProviderFactory
from cost_autopilot.providers.metered import (
    Completion,
    ProviderConfigError,
    ProviderResponseError,
    ProviderTransientError,
)
from cost_autopilot.route.budget import Budgets
from cost_autopilot.route.ladder import Ladder, Rung
from cost_autopilot.route.router import RoutePolicy, Router, RoutingError

CHEAP, MID, TOP = "model-cheap", "model-mid", "model-top"

LADDER = Ladder(
    rungs=(
        Rung(0, "CHEAP_REF", CHEAP, 300, 2500, Tier.T1_TRIVIAL),
        Rung(1, "MID_REF", MID, 750, 3750, Tier.T2_STANDARD),
        Rung(2, "TOP_REF", TOP, 2000, 12000, Tier.T3_COMPLEX),
    ),
    prices_verified=True,
)
THRESHOLDS = ScorerThresholds(t2_min_score=15, t3_min_score=45)
OPEN_POLICY = RoutePolicy(start_rung_by_tier={})
GENEROUS = Budgets(default_monthly_cap_micro_usd=10**9, teams={})

TRIVIAL = "What is the capital of Peru?"
STANDARD = "Summarize this update in 2 sentences: " + "the team shipped a feature. " * 20
COMPLEX = (
    "Debug and analyze this, comparing the trade-offs step by step.\n"
    "```python\nprint(row['id'])\n```\n"
    'Traceback (most recent call last):\n  File "a.py", line 3\nKeyError: "id"\n'
    "It must not crash and must handle at most 3 retries."
)


def build_router(
    *,
    script="answer",
    budgets=GENEROUS,
    policy=OPEN_POLICY,
    spend=0,
    ladder=LADDER,
    log_text=False,
):
    factory = FakeProviderFactory(script)
    router = Router(
        ladder=ladder,
        policy=policy,
        budgets=budgets,
        thresholds=THRESHOLDS,
        provider_factory=factory,
        read_spend_micro_usd=lambda team_id, month: spend,
        system_prompt="You are a helpful assistant.",
        log_text=log_text,
    )
    return router, factory


class TestRungChoiceByTier:
    def test_a_trivial_request_goes_to_the_cheapest_rung(self):
        router, factory = build_router()
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.classification.tier is Tier.T1_TRIVIAL
        assert outcome.row.chosen_model_id == CHEAP
        assert factory.built[CHEAP].call_count == 1
        assert MID not in factory.built

    def test_a_standard_request_skips_the_trivial_only_rung(self):
        router, _ = build_router()
        outcome = router.route(text=STANDARD, team_id="demo")
        assert outcome.classification.tier is Tier.T2_STANDARD
        assert outcome.row.chosen_model_id == MID

    def test_a_complex_request_goes_to_the_top_rung(self):
        router, _ = build_router()
        outcome = router.route(text=COMPLEX, team_id="demo")
        assert outcome.classification.tier is Tier.T3_COMPLEX
        assert outcome.row.chosen_model_id == TOP

    def test_the_fallback_chain_records_only_the_rung_that_answered(self):
        router, _ = build_router()
        assert router.route(text=TRIVIAL, team_id="demo").row.fallback_chain == (CHEAP,)


class TestPolicyFloor:
    def test_policy_can_skip_a_rung_the_ladder_would_allow(self):
        policy = RoutePolicy(start_rung_by_tier={Tier.T1_TRIVIAL: 1})
        router, _ = build_router(policy=policy)
        assert router.route(text=TRIVIAL, team_id="demo").row.chosen_model_id == MID

    def test_policy_cannot_send_a_tier_below_its_capability_floor(self):
        # Asking for rung 0 on a complex request must not defeat the ladder's
        # own rule that only the top rung is trusted with T3.
        policy = RoutePolicy(start_rung_by_tier={Tier.T3_COMPLEX: 0})
        router, _ = build_router(policy=policy)
        assert router.route(text=COMPLEX, team_id="demo").row.chosen_model_id == TOP

    def test_a_policy_past_the_end_of_the_ladder_is_a_configuration_error(self):
        policy = RoutePolicy(start_rung_by_tier={Tier.T1_TRIVIAL: 9})
        router, _ = build_router(policy=policy)
        with pytest.raises(RoutingError, match="rung"):
            router.route(text=TRIVIAL, team_id="demo")


class TestBudgetEnforcement:
    def test_a_team_under_its_cap_is_served(self):
        budgets = Budgets(default_monthly_cap_micro_usd=1000, teams={})
        router, _ = build_router(budgets=budgets, spend=999)
        assert router.route(text=TRIVIAL, team_id="demo").status == STATUS_OK

    def test_a_team_exactly_at_its_cap_is_refused(self):
        # The integer edge: spend == cap refuses. The cap is the most a team
        # may spend, so the request that would pass it is the one stopped.
        budgets = Budgets(default_monthly_cap_micro_usd=1000, teams={})
        router, factory = build_router(budgets=budgets, spend=1000)
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.status == STATUS_REFUSED
        assert factory.built == {}, "a refused request must not call any model"

    def test_a_team_over_its_cap_is_refused(self):
        budgets = Budgets(default_monthly_cap_micro_usd=1000, teams={})
        router, _ = build_router(budgets=budgets, spend=1001)
        assert router.route(text=TRIVIAL, team_id="demo").status == STATUS_REFUSED

    def test_a_zero_cap_refuses_the_very_first_request(self):
        budgets = Budgets(default_monthly_cap_micro_usd=0, teams={})
        router, _ = build_router(budgets=budgets, spend=0)
        assert router.route(text=TRIVIAL, team_id="demo").status == STATUS_REFUSED

    def test_a_per_team_cap_overrides_the_default(self):
        budgets = Budgets(default_monthly_cap_micro_usd=0, teams={"vip": 10**6})
        router, _ = build_router(budgets=budgets, spend=0)
        assert router.route(text=TRIVIAL, team_id="vip").status == STATUS_OK
        assert router.route(text=TRIVIAL, team_id="other").status == STATUS_REFUSED

    def test_a_refusal_row_costs_nothing_and_names_its_error(self):
        budgets = Budgets(default_monthly_cap_micro_usd=0, teams={})
        router, _ = build_router(budgets=budgets)
        row = router.route(text=TRIVIAL, team_id="demo").row
        assert row.cost_micro_usd == 0
        assert row.counterfactual_top_model_cost_micro_usd == 0
        assert row.error_type == "BudgetExceededError"
        assert row.fallback_chain == ()
        assert row.chosen_model_id is None


class TestFallback:
    def test_a_transient_failure_steps_up_one_rung_and_succeeds(self):
        script = [ProviderTransientError("rate limited"), "answer"]
        router, factory = build_router(script=script)
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.status == STATUS_OK
        assert outcome.row.chosen_model_id == MID
        assert outcome.row.fallback_chain == (CHEAP, MID)

    def test_fallback_is_bounded_by_the_ladder_length(self):
        router, factory = build_router(script=ProviderTransientError("always down"))
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.status == STATUS_FAILED
        assert outcome.row.fallback_chain == (CHEAP, MID, TOP)
        # Exactly one attempt per rung; no rung is retried by the router.
        for model_id in (CHEAP, MID, TOP):
            assert factory.built[model_id].call_count == 1

    def test_exhaustion_records_the_last_error_type(self):
        router, _ = build_router(script=ProviderTransientError("always down"))
        row = router.route(text=TRIVIAL, team_id="demo").row
        assert row.status == STATUS_FAILED
        assert row.error_type == "ProviderTransientError"
        assert row.cost_micro_usd == 0

    def test_a_complex_request_that_fails_has_nowhere_to_fall_back_to(self):
        router, factory = build_router(script=ProviderTransientError("down"))
        outcome = router.route(text=COMPLEX, team_id="demo")
        assert outcome.row.fallback_chain == (TOP,)
        assert outcome.status == STATUS_FAILED

    def test_a_config_error_does_not_fall_back(self):
        # A rejected credential will be rejected by the next rung too. Falling
        # back would spend money on a fault that is not about the model.
        router, factory = build_router(script=ProviderConfigError("bad key"))
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.status == STATUS_FAILED
        assert outcome.row.fallback_chain == (CHEAP,)
        assert outcome.row.error_type == "ProviderConfigError"
        assert MID not in factory.built

    def test_a_malformed_reply_does_not_fall_back(self):
        router, _ = build_router(script=ProviderResponseError("garbage"))
        outcome = router.route(text=TRIVIAL, team_id="demo")
        assert outcome.row.fallback_chain == (CHEAP,)
        assert outcome.row.error_type == "ProviderResponseError"


class TestCostRecording:
    def make_completion(self, model_id, *, in_tokens=1000, out_tokens=1000):
        return Completion(
            text="answer",
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            model_id=model_id,
            latency_ms=42,
        )

    def test_cost_is_computed_from_the_rung_that_answered(self):
        router, _ = build_router(script=self.make_completion(CHEAP))
        row = router.route(text=TRIVIAL, team_id="demo").row
        # cheap rung: 1000 in at 300/1k -> 300; 1000 out at 2500/1k -> 2500.
        assert row.cost_micro_usd == 2800

    def test_counterfactual_is_the_same_tokens_at_top_rung_prices(self):
        router, _ = build_router(script=self.make_completion(CHEAP))
        row = router.route(text=TRIVIAL, team_id="demo").row
        # top rung: 1000 in at 2000/1k -> 2000; 1000 out at 12000/1k -> 12000.
        assert row.counterfactual_top_model_cost_micro_usd == 14000

    def test_cost_is_an_int_not_a_float(self):
        router, _ = build_router(script=self.make_completion(CHEAP))
        row = router.route(text=TRIVIAL, team_id="demo").row
        assert isinstance(row.cost_micro_usd, int)
        assert isinstance(row.counterfactual_top_model_cost_micro_usd, int)

    def test_the_cost_of_a_fallback_is_the_rung_that_actually_answered(self):
        script = [ProviderTransientError("down"), self.make_completion(MID)]
        router, _ = build_router(script=script)
        row = router.route(text=TRIVIAL, team_id="demo").row
        # mid rung: 1000 in at 750/1k -> 750; 1000 out at 3750/1k -> 3750.
        assert row.cost_micro_usd == 4500

    def test_zero_token_completion_costs_nothing(self):
        completion = self.make_completion(CHEAP, in_tokens=0, out_tokens=0)
        router, _ = build_router(script=completion)
        assert router.route(text=TRIVIAL, team_id="demo").row.cost_micro_usd == 0

    def test_latency_comes_from_the_completion(self):
        router, _ = build_router(script=self.make_completion(CHEAP))
        assert router.route(text=TRIVIAL, team_id="demo").row.latency_ms == 42


class TestRowContents:
    def test_every_path_produces_exactly_one_row(self):
        for script, budgets in (
            ("answer", GENEROUS),
            (ProviderTransientError("x"), GENEROUS),
            ("answer", Budgets(default_monthly_cap_micro_usd=0, teams={})),
        ):
            router, _ = build_router(script=script, budgets=budgets)
            outcome = router.route(text=TRIVIAL, team_id="demo")
            assert outcome.row is not None

    def test_the_request_text_is_not_stored_by_default(self):
        router, _ = build_router()
        row = router.route(text=TRIVIAL, team_id="demo").row
        assert row.request_text is None
        assert row.request_sha256 and len(row.request_sha256) == 64

    def test_the_text_is_stored_only_when_log_text_is_on(self):
        router, _ = build_router(log_text=True)
        assert router.route(text=TRIVIAL, team_id="demo").row.request_text == TRIVIAL

    def test_the_hash_is_stable_for_the_same_text(self):
        router, _ = build_router()
        first = router.route(text=TRIVIAL, team_id="demo").row
        second = router.route(text=TRIVIAL, team_id="demo").row
        assert first.request_sha256 == second.request_sha256
        assert first.request_id != second.request_id

    def test_the_row_carries_the_classification_reasons(self):
        router, _ = build_router()
        row = router.route(text=COMPLEX, team_id="demo").row
        assert row.tier == "T3_COMPLEX"
        assert any("stack trace" in reason for reason in row.reasons)

    def test_request_id_is_a_uuid_unless_supplied(self):
        router, _ = build_router()
        supplied = router.route(text=TRIVIAL, team_id="demo", request_id="abc-1").row
        assert supplied.request_id == "abc-1"
