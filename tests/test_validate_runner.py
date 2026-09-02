"""Stage 03 end to end, without a network: reference answers, judging, regret.

Every provider here answers by *role* — reference answer, criterion judge on the
cheap answer, criterion judge on the reference answer, pairwise forward, pairwise
reverse — rather than by call order, so a test says what it means and does not
break when the runner reorders its calls.
"""

import pytest
from regression_detect.compare import wilson_interval

from cost_autopilot.classify.scorer import Tier
from cost_autopilot.providers.metered import (
    Completion,
    ProviderResponseError,
    ProviderTransientError,
)
from cost_autopilot.route.ladder import Rung
from cost_autopilot.validate.regret import build_report
from cost_autopilot.validate.runner import ValidationContext, validate_records
from cost_autopilot.validate.shadow import ShadowRecord

CHEAP_RUNG = Rung(0, "CHEAP_REF", "model-cheap", 300, 2500, Tier.T1_TRIVIAL)
TOP_RUNG = Rung(2, "TOP_REF", "model-top", 2000, 12000, Tier.T3_COMPLEX)

INPUT_TOKENS, OUTPUT_TOKENS = 100, 50
REFERENCE_CALL_COST = 200 + 600
JUDGE_CALL_COST = 30 + 125

CHEAP_ANSWER = "The cheap answer."
REFERENCE_ANSWER = "The reference answer."

PASS = '{"reason": "it satisfies the criterion", "passed": true}'
FAIL = '{"reason": "it does not satisfy the criterion", "passed": false}'
A_WINS = '{"reason": "A is at least as good", "a_at_least_as_good": true}'
B_WINS = '{"reason": "B is better", "a_at_least_as_good": false}'


def make_record(request_id="req-1", *, criteria=(), rung_index=0, tier="T1_TRIVIAL"):
    return ShadowRecord(
        request_id=request_id,
        ts_utc="2026-09-02T10:00:00Z",
        team_id="demo",
        tier=tier,
        complexity_score=4,
        chosen_model_id="model-cheap" if rung_index == 0 else "model-mid",
        rung_index=rung_index,
        request_text=f"request for {request_id}",
        answer_text=CHEAP_ANSWER,
        criteria=tuple(criteria),
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
    )


class RoleProvider:
    """One metered provider whose reply depends on which role the call plays."""

    def __init__(self, model_id, plan, calls):
        self.model_id = model_id
        self._plan = plan
        self._taken: dict[str, int] = {}
        self.calls = calls

    def _role(self, system: str, user: str) -> str:
        if "a_at_least_as_good" in system:
            return "pairwise_forward" if f"<answer_a>\n{CHEAP_ANSWER}" in user else (
                "pairwise_reverse"
            )
        if "<criterion>" in system or "<criterion>" in user:
            return "criterion_cheap" if f"<summary>\n{CHEAP_ANSWER}" in user else (
                "criterion_reference"
            )
        return "reference"

    def complete(self, *, system, user, temperature):
        role = self._role(system, user)
        self.calls.append(role)
        entry = self._plan.get(role, "")
        if isinstance(entry, list):
            index = self._taken.get(role, 0)
            self._taken[role] = index + 1
            entry = entry[index % len(entry)]
        if isinstance(entry, Exception):
            raise entry
        if role == "reference" and not entry:
            entry = REFERENCE_ANSWER
        return Completion(
            text=entry,
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            model_id=self.model_id,
            latency_ms=5,
        )


def build_context(**plan):
    calls: list[str] = []
    providers: dict[str, RoleProvider] = {}

    def factory(model_id: str) -> RoleProvider:
        if model_id not in providers:
            providers[model_id] = RoleProvider(model_id, plan, calls)
        return providers[model_id]

    context = ValidationContext(
        provider_factory=factory,
        reference_rung=TOP_RUNG,
        judge_rung=CHEAP_RUNG,
        system_prompt="You are a helpful assistant.",
    )
    return context, calls


class TestReferenceAnswer:
    def test_the_reference_is_re_answered_on_the_top_rung(self):
        context, calls = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        verdicts = validate_records([make_record()], context)
        assert calls.count("reference") == 1
        assert verdicts[0].reference_model_id == TOP_RUNG.model_id

    def test_the_reference_call_is_metered_and_priced_at_the_top_rung(self):
        context, _ = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        verdicts = validate_records([make_record()], context)
        assert verdicts[0].reference_cost_micro_usd == REFERENCE_CALL_COST

    def test_the_judge_calls_are_priced_at_the_judge_rung(self):
        context, _ = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        verdicts = validate_records([make_record()], context)
        assert verdicts[0].judge_cost_micro_usd == 2 * JUDGE_CALL_COST

    def test_a_failed_reference_call_is_a_judge_error_and_no_verdict(self):
        context, calls = build_context(reference=ProviderTransientError("rung down"))
        verdicts = validate_records([make_record(criteria=("c1", "c2"))], context)
        assert verdicts[0].regret is None
        assert verdicts[0].judge_errors
        assert "criterion_cheap" not in calls


class TestPairwise:
    def test_both_orders_are_judged(self):
        context, calls = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        validate_records([make_record()], context)
        assert calls.count("pairwise_forward") == 1
        assert calls.count("pairwise_reverse") == 1

    def test_the_reference_winning_both_ways_is_regret(self):
        context, _ = build_context(pairwise_forward=B_WINS, pairwise_reverse=A_WINS)
        verdict = validate_records([make_record()], context)[0]
        assert verdict.pairwise_regret is True
        assert verdict.regret is True
        assert verdict.position_bias_detected is False

    def test_a_wins_in_both_orders_is_position_bias_and_counts_as_regret(self):
        context, _ = build_context(pairwise_forward=B_WINS, pairwise_reverse=B_WINS)
        verdict = validate_records([make_record()], context)[0]
        assert verdict.position_bias_detected is True
        assert verdict.pairwise_regret is True

    def test_an_unparseable_pairwise_reply_is_a_judge_error_not_regret(self):
        context, _ = build_context(pairwise_forward="I think A is fine", pairwise_reverse=A_WINS)
        verdict = validate_records([make_record()], context)[0]
        assert verdict.pairwise_regret is None
        assert verdict.regret is None
        assert any("pairwise" in message for message in verdict.judge_errors)

    def test_a_failed_pairwise_call_is_a_judge_error_not_regret(self):
        context, _ = build_context(
            pairwise_forward=ProviderResponseError("judge broke"), pairwise_reverse=A_WINS
        )
        verdict = validate_records([make_record()], context)[0]
        assert verdict.pairwise_regret is None
        assert verdict.judge_errors


class TestCriteria:
    def test_a_record_with_no_criteria_has_no_criteria_verdict(self):
        context, calls = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        verdict = validate_records([make_record()], context)[0]
        assert verdict.criteria_regret is None
        assert verdict.criterion_verdicts == ()
        assert "criterion_cheap" not in calls

    def test_each_criterion_is_judged_on_both_answers(self):
        context, calls = build_context(
            criterion_cheap=PASS,
            criterion_reference=PASS,
            pairwise_forward=A_WINS,
            pairwise_reverse=B_WINS,
        )
        validate_records([make_record(criteria=("c1", "c2"))], context)
        assert calls.count("criterion_cheap") == 2
        assert calls.count("criterion_reference") == 2

    def test_the_cheap_answer_failing_what_the_reference_passed_is_regret(self):
        context, _ = build_context(
            criterion_cheap=FAIL,
            criterion_reference=PASS,
            pairwise_forward=A_WINS,
            pairwise_reverse=B_WINS,
        )
        verdict = validate_records([make_record(criteria=("c1", "c2"))], context)[0]
        assert verdict.criteria_regret is True
        assert verdict.regret is True

    def test_both_answers_failing_the_same_criterion_is_not_regret(self):
        context, _ = build_context(
            criterion_cheap=FAIL,
            criterion_reference=FAIL,
            pairwise_forward=A_WINS,
            pairwise_reverse=B_WINS,
        )
        verdict = validate_records([make_record(criteria=("c1", "c2"))], context)[0]
        assert verdict.criteria_regret is False

    def test_per_criterion_verdicts_are_recorded(self):
        context, _ = build_context(
            criterion_cheap=[PASS, FAIL],
            criterion_reference=PASS,
            pairwise_forward=A_WINS,
            pairwise_reverse=B_WINS,
        )
        verdict = validate_records([make_record(criteria=("c1", "c2"))], context)[0]
        assert [item.criterion for item in verdict.criterion_verdicts] == ["c1", "c2"]
        assert [item.cheap_passed for item in verdict.criterion_verdicts] == [True, False]
        assert all(item.reference_passed for item in verdict.criterion_verdicts)

    def test_an_unparseable_criterion_reply_is_a_judge_error_not_a_failed_criterion(self):
        context, _ = build_context(
            criterion_cheap="probably fine",
            criterion_reference=PASS,
            pairwise_forward=A_WINS,
            pairwise_reverse=B_WINS,
        )
        verdict = validate_records([make_record(criteria=("c1", "c2"))], context)[0]
        assert verdict.criteria_regret is None
        assert verdict.criterion_verdicts[0].cheap_passed is None
        assert len(verdict.judge_errors) == 2


class TestRunControls:
    def test_limit_stops_after_n_records(self):
        context, _ = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        records = [make_record(f"req-{index}") for index in range(5)]
        assert len(validate_records(records, context, limit=2)) == 2

    def test_a_limit_of_zero_validates_nothing(self):
        context, calls = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        assert validate_records([make_record()], context, limit=0) == []
        assert calls == []

    def test_already_validated_ids_are_skipped(self):
        context, calls = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        records = [make_record("req-1"), make_record("req-2")]
        verdicts = validate_records(records, context, skip_ids=frozenset({"req-1"}))
        assert [item.request_id for item in verdicts] == ["req-2"]
        assert calls.count("reference") == 1

    def test_every_verdict_is_handed_to_the_callback_as_it_is_produced(self):
        context, _ = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        seen = []
        records = [make_record("req-1"), make_record("req-2")]
        validate_records(records, context, on_verdict=seen.append)
        assert [item.request_id for item in seen] == ["req-1", "req-2"]

    def test_a_negative_limit_is_refused(self):
        context, _ = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        with pytest.raises(ValueError):
            validate_records([make_record()], context, limit=-1)


class TestRegretAggregation:
    """Exact counts, and Wilson bounds recomputed in the test with the same function."""

    def build_verdicts(self):
        # Six records: four on rung 0 (one regret), two on rung 1 (one regret).
        regretful = build_context(pairwise_forward=B_WINS, pairwise_reverse=A_WINS)
        content = build_context(pairwise_forward=A_WINS, pairwise_reverse=B_WINS)
        verdicts = []
        for index in range(4):
            context = regretful[0] if index == 0 else content[0]
            verdicts += validate_records([make_record(f"r0-{index}", rung_index=0)], context)
        for index in range(2):
            context = regretful[0] if index == 0 else content[0]
            verdicts += validate_records(
                [make_record(f"r1-{index}", rung_index=1, tier="T2_STANDARD")], context
            )
        return verdicts

    def test_overall_counts_and_interval(self):
        report = build_report(
            self.build_verdicts(),
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=23,
            shadow_record_count=6,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.overall.n == 6
        assert report.overall.regret_count == 2
        low, high = wilson_interval(2, 6)
        assert report.overall.wilson_low == pytest.approx(low)
        assert report.overall.wilson_high == pytest.approx(high)

    def test_the_breakdown_by_rung_is_exact(self):
        report = build_report(
            self.build_verdicts(),
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=23,
            shadow_record_count=6,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.by_rung["0"].n == 4
        assert report.by_rung["0"].regret_count == 1
        assert report.by_rung["1"].n == 2
        assert report.by_rung["1"].regret_count == 1
        assert report.by_rung["1"].wilson_high == pytest.approx(wilson_interval(1, 2)[1])

    def test_the_breakdown_by_tier_is_exact(self):
        report = build_report(
            self.build_verdicts(),
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=23,
            shadow_record_count=6,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.by_tier["T1_TRIVIAL"].n == 4
        assert report.by_tier["T2_STANDARD"].regret_count == 1

    def test_validation_cost_is_the_sum_of_reference_and_judge_costs(self):
        verdicts = self.build_verdicts()
        report = build_report(
            verdicts,
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=23,
            shadow_record_count=6,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.reference_cost_micro_usd == 6 * REFERENCE_CALL_COST
        assert report.judge_cost_micro_usd == 6 * 2 * JUDGE_CALL_COST
        assert report.validation_cost_micro_usd == (
            report.reference_cost_micro_usd + report.judge_cost_micro_usd
        )

    def test_the_sample_percent_and_cheap_routed_count_travel_with_the_report(self):
        report = build_report(
            self.build_verdicts(),
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=23,
            shadow_record_count=6,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.sample_percent == 20
        assert report.cheap_routed_count == 23
        assert report.shadow_record_count == 6

    def test_unjudgeable_records_are_counted_as_judge_errors_not_as_no_regret(self):
        context, _ = build_context(reference=ProviderTransientError("down"))
        verdicts = validate_records([make_record("req-x")], context)
        report = build_report(
            verdicts,
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=1,
            shadow_record_count=1,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.overall.n == 0
        assert report.overall.judge_error_count >= 1
        assert report.overall.wilson_low == 0.0
        assert report.overall.wilson_high == 1.0

    def test_an_empty_report_is_all_zeroes_and_a_full_width_interval(self):
        report = build_report(
            [],
            month="2026-09",
            sample_percent=20,
            cheap_routed_count=0,
            shadow_record_count=0,
            generated_utc="2026-09-02T11:00:00Z",
        )
        assert report.overall.n == 0
        assert report.overall.regret_rate == 0.0
        assert report.by_rung == {}
        assert report.by_tier == {}
