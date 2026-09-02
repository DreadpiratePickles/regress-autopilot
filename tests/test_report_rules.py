"""The recommendation rules: one per verdict state, plus the operational three.

Every assertion is against the *committed* `autopilot.toml`, with the one value
under test substituted. A rule that only fires under a configuration nobody runs
is a rule nobody will ever see.
"""

from conftest import load_test_config, make_row, make_verdicts
from cost_autopilot.ledger.row import STATUS_FAILED, STATUS_OK
from cost_autopilot.report.build import build_report
from cost_autopilot.report.rules import (
    RULE_ENABLE_BILLING,
    RULE_HIGH_FALLBACK_RATE,
    RULE_LADDER_EXHAUSTED,
    RULE_LOWER_POLICY_FLOOR,
    RULE_RAISE_POLICY_FLOOR,
    RULE_RAISE_SAMPLE_PERCENT,
    RULE_VERIFY_PRICES,
    recommend,
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


def report_for(tmp_path, *, rows=None, verdicts=None, substitutions=()):
    config = load_test_config(tmp_path, substitutions)
    regret = regret_for(verdicts) if verdicts else None
    report = build_report(
        rows if rows is not None else [make_row()],
        regret,
        [],
        config=config,
        month="2026-09",
        generated_utc="2026-09-02T12:00:00Z",
    )
    return report, config


def by_rule(items, rule_id):
    return [item for item in items if item.rule_id == rule_id]


def failed_row(request_id, tier, chain):
    return make_row(
        request_id=request_id,
        tier=tier,
        status=STATUS_FAILED,
        chosen_model_id=None,
        error_type="ProviderTransientError",
        cost_micro_usd=0,
        counterfactual_top_model_cost_micro_usd=0,
        fallback_chain=chain,
    )


class TestRegretTooHigh:
    def test_a_regretful_tier_is_told_to_start_one_rung_higher(self, tmp_path):
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=20, regret_count=10)
        )
        made = by_rule(recommend(report, config), RULE_RAISE_POLICY_FLOOR)
        assert len(made) == 1
        item = made[0]
        assert item.subject == "T1_TRIVIAL"
        assert "n=20, regret 10 (50%)" in item.evidence
        assert "95% CI [" in item.evidence
        assert "route T1_TRIVIAL from rung 1" in item.action
        assert "instead of rung 0" in item.action
        assert (item.config_section, item.config_key) == ("policy", "T1_TRIVIAL")
        assert (item.current_value, item.proposed_value) == (0, 1)

    def test_a_tier_already_at_the_top_gets_advice_not_a_config_change(self, tmp_path):
        report, config = report_for(
            tmp_path,
            verdicts=make_verdicts(
                count=20, regret_count=10, tier="T3_COMPLEX", rung_index=2
            ),
            substitutions=[("T3_COMPLEX = 0", "T3_COMPLEX = 2")],
        )
        made = recommend(report, config)
        assert by_rule(made, RULE_RAISE_POLICY_FLOOR) == []
        exhausted = by_rule(made, RULE_LADDER_EXHAUSTED)
        assert len(exhausted) == 1
        assert exhausted[0].is_config_change is False
        assert "top of the ladder" in exhausted[0].action


class TestNotEnoughEvidence:
    def test_too_few_samples_asks_for_more_and_quantifies_it(self, tmp_path):
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=4, regret_count=1)
        )
        made = by_rule(recommend(report, config), RULE_RAISE_SAMPLE_PERCENT)
        assert len(made) == 1
        action = made[0].action
        assert "fewer than min_samples 10" in action
        assert "n~35" in action
        assert "about 175 cheap-routed T1_TRIVIAL request(s)" in action
        assert (made[0].config_section, made[0].config_key) == ("validate", "sample_percent")
        assert made[0].current_value == 20

    def test_zero_regret_on_a_wide_interval_asks_for_more_samples_not_a_reroute(
        self, tmp_path
    ):
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=12, regret_count=0)
        )
        made = recommend(report, config)
        assert by_rule(made, RULE_RAISE_POLICY_FLOOR) == []
        sampling = by_rule(made, RULE_RAISE_SAMPLE_PERCENT)
        assert len(sampling) == 1
        assert "observed no regret at all" in sampling[0].action
        assert "~23 more comparison(s)" in sampling[0].action

    def test_the_proposed_rate_scales_with_this_month_s_own_traffic(self, tmp_path):
        """35 comparisons from 70 cheap-routed T1 requests needs 50%, not 100%."""
        rows = [
            make_row(request_id=f"r{index}", chosen_model_id=CHEAP, tier="T1_TRIVIAL")
            for index in range(70)
        ]
        report, config = report_for(
            tmp_path, rows=rows, verdicts=make_verdicts(count=12, regret_count=0)
        )
        made = by_rule(recommend(report, config), RULE_RAISE_SAMPLE_PERCENT)
        assert made[0].proposed_value == 50

    def test_an_unreachable_threshold_is_named_rather_than_sampled_harder(self, tmp_path):
        report, config = report_for(
            tmp_path,
            verdicts=make_verdicts(count=12, regret_count=0),
            substitutions=[("max_regret = 0.10", "max_regret = 0.0")],
        )
        made = by_rule(recommend(report, config), RULE_RAISE_SAMPLE_PERCENT)
        assert made[0].is_config_change is False
        assert "no sample size can clear" in made[0].action


class TestSafeTier:
    def test_a_safe_tier_at_the_ladder_s_own_floor_recommends_nothing(self, tmp_path):
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=200, regret_count=0)
        )
        assert recommend(report, config) == ()

    def test_a_safe_tier_above_the_ladder_s_floor_is_offered_the_cheaper_rung(
        self, tmp_path
    ):
        report, config = report_for(
            tmp_path,
            verdicts=make_verdicts(count=200, regret_count=0),
            substitutions=[("T1_TRIVIAL = 0", "T1_TRIVIAL = 1")],
        )
        made = by_rule(recommend(report, config), RULE_LOWER_POLICY_FLOOR)
        assert len(made) == 1
        assert (made[0].current_value, made[0].proposed_value) == (1, 0)
        assert "read the caveat" in made[0].action
        assert "measured at rung 1" in made[0].action


class TestOperationalRules:
    def test_requests_that_exhausted_the_ladder_ask_for_billing_not_a_setting(
        self, tmp_path
    ):
        rows = [
            failed_row("a", "T3_COMPLEX", (CHEAP, MID, TOP)),
            failed_row("b", "T3_COMPLEX", (CHEAP, MID, TOP)),
        ]
        report, config = report_for(tmp_path, rows=rows)
        made = by_rule(recommend(report, config), RULE_ENABLE_BILLING)
        assert len(made) == 1
        assert "2 T3_COMPLEX request(s) failed with no reachable model" in made[0].action
        assert "enable billing for rung 2" in made[0].action
        assert made[0].is_config_change is False

    def test_fallbacks_a_dearer_rung_answered_recommend_starting_higher(self, tmp_path):
        rows = [
            make_row(
                request_id=f"r{index}",
                tier="T1_TRIVIAL",
                status=STATUS_OK,
                chosen_model_id=MID,
                fallback_chain=(CHEAP, MID),
            )
            for index in range(8)
        ] + [
            make_row(request_id="clean", tier="T1_TRIVIAL", chosen_model_id=CHEAP),
        ]
        report, config = report_for(tmp_path, rows=rows)
        made = by_rule(recommend(report, config), RULE_HIGH_FALLBACK_RATE)
        assert len(made) == 1
        assert "8 of 9 T1_TRIVIAL request(s) were answered by a dearer rung" in (
            made[0].evidence
        )
        assert (made[0].current_value, made[0].proposed_value) == (0, 1)

    def test_the_proposed_rung_is_the_effective_start_plus_one_not_the_file_value(
        self, tmp_path
    ):
        """T2 starts at rung 1 because the ladder says rung 0 cannot serve it, even
        though `[policy] T2_STANDARD` reads 0. The proposal has to move the file to
        2 to move the routing at all — moving it to 1 would change nothing."""
        rows = [
            make_row(
                request_id=f"r{index}",
                tier="T2_STANDARD",
                status=STATUS_OK,
                chosen_model_id=TOP,
                fallback_chain=(MID, TOP),
            )
            for index in range(8)
        ] + [make_row(request_id="clean", tier="T2_STANDARD", chosen_model_id=MID)]
        report, config = report_for(tmp_path, rows=rows)
        made = by_rule(recommend(report, config), RULE_HIGH_FALLBACK_RATE)
        assert (made[0].current_value, made[0].proposed_value) == (0, 2)

    def test_fallbacks_that_never_recovered_do_not_recommend_spending_more(self, tmp_path):
        """Ten requests that climbed the whole ladder and got nothing say the
        ladder is unreachable, not that the cheap rung was inadequate."""
        rows = [failed_row(f"r{index}", "T2_STANDARD", (CHEAP, MID, TOP)) for index in range(10)]
        report, config = report_for(tmp_path, rows=rows)
        made = recommend(report, config)
        assert by_rule(made, RULE_HIGH_FALLBACK_RATE) == []
        assert len(by_rule(made, RULE_ENABLE_BILLING)) == 1

    def test_a_fallback_rate_under_the_threshold_says_nothing(self, tmp_path):
        rows = [
            make_row(
                request_id="fell-back",
                tier="T2_STANDARD",
                status=STATUS_OK,
                chosen_model_id=MID,
                fallback_chain=(CHEAP, MID),
            )
        ] + [
            make_row(request_id=f"clean{index}", tier="T2_STANDARD", chosen_model_id=CHEAP)
            for index in range(9)
        ]
        report, config = report_for(tmp_path, rows=rows)
        assert by_rule(recommend(report, config), RULE_HIGH_FALLBACK_RATE) == []

    def test_unverified_prices_make_every_figure_provisional(self, tmp_path):
        report, config = report_for(
            tmp_path, substitutions=[("prices_verified = true", "prices_verified = false")]
        )
        made = by_rule(recommend(report, config), RULE_VERIFY_PRICES)
        assert len(made) == 1
        assert made[0].is_config_change is False
        assert "none of them should be quoted" in made[0].action


class TestEveryRecommendation:
    def test_each_one_names_evidence_and_an_action(self, tmp_path):
        rows = [failed_row("a", "T3_COMPLEX", (CHEAP, MID, TOP))]
        report, config = report_for(
            tmp_path,
            rows=rows,
            verdicts=make_verdicts(count=20, regret_count=10),
            substitutions=[("prices_verified = true", "prices_verified = false")],
        )
        made = recommend(report, config)
        assert len(made) >= 3
        for item in made:
            assert item.evidence.strip()
            assert item.action.strip()
            assert "consider tuning" not in item.action.lower()

    def test_the_rules_are_deterministic(self, tmp_path):
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=20, regret_count=10)
        )
        assert recommend(report, config) == recommend(report, config)

    def test_nothing_in_the_rules_touches_the_configuration_file(self, tmp_path):
        config_path = tmp_path / "autopilot.toml"
        report, config = report_for(
            tmp_path, verdicts=make_verdicts(count=20, regret_count=10)
        )
        before = config_path.read_text(encoding="utf-8")
        recommend(report, config)
        assert config_path.read_text(encoding="utf-8") == before
