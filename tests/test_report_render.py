"""The Markdown: what is on the page, and — for the banner — where on the page.

Rendering decides nothing, so these tests are about presence, ordering and the
one property that matters for a document read out of context: a synthetic
artifact says so on its first line.
"""

from dataclasses import replace

from conftest import load_test_config, make_row, make_verdicts
from cost_autopilot.ledger.row import STATUS_FAILED
from cost_autopilot.report.build import build_report
from cost_autopilot.report.proposal import build_proposal
from cost_autopilot.report.render import BANNER, render_proposal, render_report
from cost_autopilot.report.rules import recommend
from cost_autopilot.validate.regret import build_report as build_regret

NOW = "2026-09-02T12:00:00Z"
TOP = "gemini-3.1-pro-preview"
CHEAP = "gemini-3.5-flash-lite"
MID = "gemini-3.6-flash"


def regret_for(items):
    return build_regret(
        items,
        month="2026-09",
        sample_percent=20,
        cheap_routed_count=100,
        shadow_record_count=len(items),
        generated_utc=NOW,
    )


def report_for(tmp_path, *, rows=None, verdicts=None, synthetic=False, substitutions=()):
    config = load_test_config(tmp_path, substitutions)
    report = build_report(
        rows if rows is not None else [make_row(chosen_model_id=CHEAP)],
        regret_for(verdicts) if verdicts else None,
        [],
        config=config,
        month="2026-09",
        generated_utc=NOW,
        synthetic=synthetic,
    )
    return replace(report, recommendations=recommend(report, config)), config


class TestBanner:
    def test_a_synthetic_report_says_so_on_its_very_first_line(self, tmp_path):
        report, _ = report_for(tmp_path, synthetic=True)
        text = render_report(report)
        assert text.splitlines()[0] == BANNER
        assert "not a model judgement" in BANNER

    def test_a_real_report_carries_no_banner(self, tmp_path):
        report, _ = report_for(tmp_path)
        assert render_report(report).splitlines()[0] == "# Cost autopilot report — 2026-09"

    def test_a_synthetic_proposal_says_so_on_its_very_first_line(self, tmp_path):
        report, _ = report_for(
            tmp_path, verdicts=make_verdicts(count=20, regret_count=10), synthetic=True
        )
        proposal = build_proposal(
            report, config_path="autopilot.toml", generated_utc=NOW, synthetic=True
        )
        assert render_proposal(proposal).splitlines()[0] == BANNER


class TestReportContents:
    def test_the_verdict_comes_before_the_numbers(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=200, regret_count=0))
        text = render_report(report)
        assert text.index("## Verdict: SAFE") < text.index("## Spend")

    def test_spend_is_broken_down_three_ways(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(report)
        for heading in ("Spend by team", "Spend by model", "Spend by tier"):
            assert heading in text

    def test_the_saving_appears_in_money_and_in_percent(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(report)
        assert "| Saving |" in text
        assert f"({report.spend.saving_percent}%)" in text

    def test_outcomes_separate_a_recovered_fallback_from_an_exhausted_ladder(self, tmp_path):
        rows = [
            make_row(
                request_id="exhausted",
                status=STATUS_FAILED,
                chosen_model_id=None,
                error_type="ProviderTransientError",
                cost_micro_usd=0,
                counterfactual_top_model_cost_micro_usd=0,
                fallback_chain=(CHEAP, MID, TOP),
            )
        ]
        text = render_report(report_for(tmp_path, rows=rows)[0])
        assert "of which a dearer rung answered" in text
        assert "of which reached the top rung and still got nothing" in text

    def test_the_quality_section_states_the_inspected_fraction_and_the_overhead(
        self, tmp_path
    ):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=20, regret_count=1))
        text = render_report(report)
        assert "Fraction of cheap-routed requests inspected" in text
        assert "Total validation overhead" in text
        assert "Reference answers on the top rung" in text

    def test_each_tier_carries_its_verdict_sentence(self, tmp_path):
        report, _ = report_for(
            tmp_path,
            verdicts=make_verdicts(count=20, regret_count=10)
            + make_verdicts(count=200, regret_count=0, tier="T2_STANDARD", rung_index=1),
        )
        text = render_report(report)
        assert "regret too high — consider routing this tier up" in text
        assert "safe: the 95% upper bound" in text

    def test_a_month_with_no_validation_says_the_quality_question_is_open(self, tmp_path):
        text = render_report(report_for(tmp_path)[0])
        assert "This month has no `regret.json`" in text
        assert "autopilot validate" in text

    def test_every_recommendation_prints_its_evidence_and_action(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=20, regret_count=10))
        text = render_report(report)
        for item in report.recommendations:
            assert f"`{item.rule_id}`" in text
            assert item.evidence in text
            assert item.action in text

    def test_no_recommendations_is_stated_rather_than_left_blank(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=200, regret_count=0))
        assert "None. Every tier's verdict is `safe`" in render_report(report)

    def test_unverified_prices_are_flagged_in_the_document_too(self, tmp_path):
        report, _ = report_for(
            tmp_path, substitutions=[("prices_verified = true", "prices_verified = false")]
        )
        assert "Do not quote any of them." in render_report(report)

    def test_an_inconsistency_is_named_in_the_provenance_block(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(replace(report, inconsistent_request_ids=("orphan-1",)))
        assert "**Inconsistency.**" in text
        assert "`orphan-1`" in text

    def test_the_footer_carries_the_thresholds_the_verdict_rests_on(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(report)
        assert "`max_regret` = 0.1" in text
        assert "`min_comparisons` = 10" in text
        assert "Report schema version" in text

    def test_the_document_ends_with_exactly_one_newline(self, tmp_path):
        text = render_report(report_for(tmp_path)[0])
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


class TestProposalContents:
    def test_the_diff_shows_both_values(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=20, regret_count=10))
        proposal = build_proposal(report, config_path="autopilot.toml", generated_utc=NOW)
        text = render_proposal(proposal)
        assert "```diff" in text
        assert "-T1_TRIVIAL = 0" in text
        assert "+T1_TRIVIAL = 1" in text

    def test_it_says_it_will_not_apply_itself_and_shows_the_command(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=20, regret_count=10))
        proposal = build_proposal(report, config_path="autopilot.toml", generated_utc=NOW)
        text = render_proposal(proposal)
        assert "This tool will not apply the diff above." in text
        assert "--approve --approved-by" in text
        assert "awaiting_human_approval" in text

    def test_every_change_carries_its_rules_and_evidence(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=20, regret_count=10))
        proposal = build_proposal(report, config_path="autopilot.toml", generated_utc=NOW)
        text = render_proposal(proposal)
        for change in proposal.changes:
            assert change.evidence in text
            for rule_id in change.rule_ids:
                assert f"`{rule_id}`" in text

    def test_an_empty_proposal_explains_itself(self, tmp_path):
        report, _ = report_for(tmp_path, verdicts=make_verdicts(count=200, regret_count=0))
        proposal = build_proposal(report, config_path="autopilot.toml", generated_utc=NOW)
        text = render_proposal(proposal)
        assert "recommends no change" in text
        assert "```diff" not in text

    def test_the_footer_names_the_configuration_and_the_ladder(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(replace(report, config_path="autopilot.demo.toml"))
        assert "- Configuration: `autopilot.demo.toml`" in text
        assert "`gemini-3.5-flash-lite`" in text
        assert "not comparable to a figure from another" in text

    def test_an_unrecorded_configuration_says_so_rather_than_guessing(self, tmp_path):
        report, _ = report_for(tmp_path)
        text = render_report(replace(report, config_path="", ladder=()))
        assert "- Configuration: `not recorded`" in text
        assert "Ladder, cheapest first: not recorded" in text
