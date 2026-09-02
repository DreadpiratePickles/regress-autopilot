"""Stage 04's arithmetic: totals from rows, quality copied from stage 03, verdict.

Every figure asserted here is one a person could work out with a calculator from
the rows in the test, which is the property the stage's contract requires.
"""

from conftest import load_test_config, make_row, make_verdict, make_verdicts
from cost_autopilot.ledger.row import STATUS_FAILED, STATUS_OK, STATUS_REFUSED
from cost_autopilot.report.build import (
    NO_RATIO,
    build_quality_facts,
    build_report,
    build_spend_facts,
    cheap_routed_by_tier,
    decide_verdict,
    inconsistent_ids,
)
from cost_autopilot.report.model import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REGRET_TOO_HIGH,
    VERDICT_SAFE,
)
from cost_autopilot.validate.regret import build_report as build_regret

TOP = "gemini-3.1-pro-preview"
CHEAP = "gemini-3.5-flash-lite"
MID = "gemini-3.6-flash"


def regret_for(items, **overrides):
    return build_regret(
        items,
        **{
            "month": "2026-09",
            "sample_percent": 20,
            "cheap_routed_count": 100,
            "shadow_record_count": len(items),
            "generated_utc": "2026-09-02T11:00:00Z",
            **overrides,
        },
    )


class TestSpendFacts:
    def test_totals_match_the_rows_added_up_by_hand(self):
        rows = [
            make_row(
                request_id="a",
                cost_micro_usd=100,
                counterfactual_top_model_cost_micro_usd=500,
            ),
            make_row(
                request_id="b",
                cost_micro_usd=250,
                counterfactual_top_model_cost_micro_usd=900,
            ),
        ]
        facts = build_spend_facts(rows, month="2026-09", top_model_id=TOP)
        assert facts.row_count == 2
        assert facts.spend_micro_usd == 350
        assert facts.counterfactual_micro_usd == 1400
        assert facts.saving_micro_usd == 1050
        assert facts.saving_percent == 75

    def test_spend_is_broken_out_by_tier_as_well_as_team_and_model(self):
        rows = [
            make_row(request_id="a", tier="T1_TRIVIAL", cost_micro_usd=100),
            make_row(request_id="b", tier="T2_STANDARD", cost_micro_usd=200),
            make_row(request_id="c", tier="T2_STANDARD", cost_micro_usd=300),
        ]
        facts = build_spend_facts(rows, month="2026-09", top_model_id=TOP)
        assert facts.spend_by_tier == {"T1_TRIVIAL": 100, "T2_STANDARD": 500}
        assert facts.spend_by_team == {"demo": 600}
        assert facts.spend_by_model == {"model-cheap": 600}

    def test_a_fallback_a_dearer_rung_answered_is_counted_apart_from_one_that_failed(self):
        rows = [
            make_row(
                request_id="recovered",
                status=STATUS_OK,
                chosen_model_id=MID,
                fallback_chain=(CHEAP, MID),
            ),
            make_row(
                request_id="exhausted",
                status=STATUS_FAILED,
                chosen_model_id=None,
                error_type="ProviderTransientError",
                cost_micro_usd=0,
                counterfactual_top_model_cost_micro_usd=0,
                fallback_chain=(CHEAP, MID, TOP),
            ),
        ]
        facts = build_spend_facts(rows, month="2026-09", top_model_id=TOP)
        assert facts.fallback_count == 2
        assert facts.fallbacks_by_tier == {"T1_TRIVIAL": 2}
        assert facts.recovered_by_tier == {"T1_TRIVIAL": 1}
        assert facts.failures_by_tier == {"T1_TRIVIAL": 1}
        assert facts.unreachable_by_tier == {"T1_TRIVIAL": 1}

    def test_a_failure_that_never_reached_the_top_rung_is_not_unreachable(self):
        rows = [
            make_row(
                request_id="stopped",
                status=STATUS_FAILED,
                chosen_model_id=None,
                error_type="ProviderConfigError",
                cost_micro_usd=0,
                counterfactual_top_model_cost_micro_usd=0,
                fallback_chain=(CHEAP,),
            )
        ]
        facts = build_spend_facts(rows, month="2026-09", top_model_id=TOP)
        assert facts.failures_by_tier == {"T1_TRIVIAL": 1}
        assert facts.unreachable_by_tier == {}

    def test_refusals_and_failures_are_counted_never_hidden(self):
        rows = [
            make_row(request_id="ok"),
            make_row(
                request_id="refused",
                status=STATUS_REFUSED,
                chosen_model_id=None,
                error_type="BudgetExceededError",
                cost_micro_usd=0,
                counterfactual_top_model_cost_micro_usd=0,
                fallback_chain=(),
            ),
        ]
        facts = build_spend_facts(rows, month="2026-09", top_model_id=TOP)
        assert facts.refusal_count == 1
        assert facts.requests_by_status == {"ok": 1, "refused": 1}


class TestCheapRoutedPopulation:
    def test_only_successful_rows_below_the_top_rung_are_counted(self):
        rows = [
            make_row(request_id="a", chosen_model_id=CHEAP, tier="T1_TRIVIAL"),
            make_row(request_id="b", chosen_model_id=MID, tier="T2_STANDARD"),
            make_row(request_id="c", chosen_model_id=TOP, tier="T3_COMPLEX"),
            make_row(
                request_id="d",
                tier="T1_TRIVIAL",
                status=STATUS_FAILED,
                chosen_model_id=None,
                error_type="ProviderTransientError",
                cost_micro_usd=0,
                counterfactual_top_model_cost_micro_usd=0,
                fallback_chain=(CHEAP,),
            ),
        ]
        assert cheap_routed_by_tier(rows, top_model_id=TOP) == {
            "T1_TRIVIAL": 1,
            "T2_STANDARD": 1,
        }


class TestQualityFacts:
    def test_a_month_with_no_regret_file_reports_no_measurement(self, tmp_path):
        facts = build_quality_facts(
            None,
            rows=[make_row(request_id="a", chosen_model_id=CHEAP)],
            top_model_id=TOP,
            saving_micro_usd=1000,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.validated is False
        assert facts.overall is None
        assert facts.validated_count == 0
        assert facts.cheap_routed_count == 1
        assert facts.overhead_percent_of_saving == NO_RATIO

    def test_regret_is_copied_across_rather_than_recomputed(self):
        regret = regret_for(make_verdicts(count=20, regret_count=2))
        facts = build_quality_facts(
            regret,
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=100_000,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.overall == regret.overall
        assert facts.by_tier == dict(regret.by_tier)
        assert facts.validated_count == 20

    def test_the_inspected_fraction_is_validated_over_cheap_routed(self):
        regret = regret_for(make_verdicts(count=20, regret_count=0), cheap_routed_count=80)
        facts = build_quality_facts(
            regret,
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.inspected_percent == 25

    def test_overhead_is_a_floored_percentage_of_the_saving(self):
        regret = regret_for(make_verdicts(count=4, regret_count=0))
        # 4 records x (800 reference + 310 judge) = 4440 micro-USD of overhead.
        assert regret.validation_cost_micro_usd == 4440
        facts = build_quality_facts(
            regret,
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=10_000,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.overhead_percent_of_saving == 44

    def test_no_saving_means_no_ratio_rather_than_zero(self):
        facts = build_quality_facts(
            regret_for(make_verdicts(count=4, regret_count=0)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=0,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.overhead_percent_of_saving == NO_RATIO

    def test_every_tier_gets_the_shared_verdict_sentence(self):
        regret = regret_for(
            make_verdicts(count=20, regret_count=10)
            + make_verdicts(count=200, regret_count=0, tier="T2_STANDARD", rung_index=1)
        )
        facts = build_quality_facts(
            regret,
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        assert facts.tier_verdicts["T1_TRIVIAL"].startswith("regret too high")
        assert facts.tier_verdicts["T2_STANDARD"].startswith("safe")


class TestVerdict:
    def test_no_regret_file_is_inconclusive_and_says_to_validate(self):
        facts = build_quality_facts(
            None, rows=[], top_model_id=TOP, saving_micro_usd=0, max_regret=0.10, min_samples=10
        )
        verdict, reason = decide_verdict(
            facts, month="2026-09", max_regret=0.10, min_comparisons=10
        )
        assert verdict == VERDICT_INCONCLUSIVE
        assert "no regret.json" in reason
        assert "autopilot validate" in reason

    def test_too_few_comparisons_is_inconclusive(self):
        facts = build_quality_facts(
            regret_for(make_verdicts(count=3, regret_count=0)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        verdict, reason = decide_verdict(
            facts, month="2026-09", max_regret=0.10, min_comparisons=10
        )
        assert verdict == VERDICT_INCONCLUSIVE
        assert "fewer than [report] min_comparisons 10" in reason

    def test_a_tight_interval_under_the_threshold_is_safe(self):
        facts = build_quality_facts(
            regret_for(make_verdicts(count=200, regret_count=0)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        verdict, reason = decide_verdict(
            facts, month="2026-09", max_regret=0.10, min_comparisons=10
        )
        assert verdict == VERDICT_SAFE
        assert "is below max_regret 0.100" in reason

    def test_zero_regret_on_a_wide_interval_is_inconclusive_not_an_accusation(self):
        """The fourth verdict, at month scale. The report's job is to say whether
        the routing is defensible; "the sample is too small" is not a finding
        about the routing, so it must not read as REGRET_TOO_HIGH."""
        facts = build_quality_facts(
            regret_for(make_verdicts(count=12, regret_count=0)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        verdict, reason = decide_verdict(
            facts, month="2026-09", max_regret=0.10, min_comparisons=10
        )
        assert verdict == VERDICT_INCONCLUSIVE
        assert "no regret observed in 12 comparison(s)" in reason
        assert "need ~23 more clean comparison(s)" in reason

    def test_an_unreachable_threshold_is_named_rather_than_quoting_a_shortfall(self):
        facts = build_quality_facts(
            regret_for(make_verdicts(count=12, regret_count=0)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.0,
            min_samples=10,
        )
        _, reason = decide_verdict(facts, month="2026-09", max_regret=0.0, min_comparisons=10)
        assert "no sample size clears max_regret 0.000" in reason

    def test_observed_regret_above_the_threshold_is_the_finding(self):
        facts = build_quality_facts(
            regret_for(make_verdicts(count=20, regret_count=10)),
            rows=[],
            top_model_id=TOP,
            saving_micro_usd=1,
            max_regret=0.10,
            min_samples=10,
        )
        verdict, reason = decide_verdict(
            facts, month="2026-09", max_regret=0.10, min_comparisons=10
        )
        assert verdict == VERDICT_REGRET_TOO_HIGH
        assert "regret 10 of 20 (50%)" in reason


class TestInconsistencies:
    def test_a_verdict_with_no_ledger_row_is_named(self):
        rows = [make_row(request_id="known")]
        verdicts = [make_verdict(request_id="known"), make_verdict(request_id="orphan")]
        assert inconsistent_ids(rows, verdicts) == ("orphan",)

    def test_a_consistent_month_names_nothing(self):
        rows = [make_row(request_id="known")]
        assert inconsistent_ids(rows, [make_verdict(request_id="known")]) == ()


class TestBuildReport:
    def test_the_whole_report_assembles_from_the_committed_configuration(self, tmp_path):
        config = load_test_config(tmp_path)
        rows = [make_row(request_id="a", chosen_model_id=CHEAP)]
        report = build_report(
            rows,
            regret_for(make_verdicts(count=200, regret_count=0)),
            [],
            config=config,
            month="2026-09",
            generated_utc="2026-09-02T12:00:00Z",
        )
        assert report.verdict == VERDICT_SAFE
        assert report.exit_code == 0
        assert report.synthetic is False
        assert report.prices_verified is True
        assert report.thresholds["max_regret"] == 0.10
        assert report.thresholds["min_comparisons"] == 10

    def test_a_dry_run_report_is_marked_synthetic(self, tmp_path):
        report = build_report(
            [make_row()],
            None,
            [],
            config=load_test_config(tmp_path),
            month="2026-09",
            generated_utc="2026-09-02T12:00:00Z",
            synthetic=True,
        )
        assert report.synthetic is True

    def test_the_json_projection_carries_the_verdict_and_its_exit_code(self, tmp_path):
        report = build_report(
            [make_row()],
            None,
            [],
            config=load_test_config(tmp_path),
            month="2026-09",
            generated_utc="2026-09-02T12:00:00Z",
        )
        payload = report.to_json_dict()
        assert payload["verdict"] == VERDICT_INCONCLUSIVE
        assert payload["exit_code"] == 2
        assert payload["schema_version"] == 1
        assert payload["currency"] == "USD"
        assert "spend" in payload and "quality" in payload
