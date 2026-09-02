"""Summary arithmetic on a synthetic ledger with exactly known totals."""

import pytest

from conftest import make_row
from cost_autopilot.ledger.row import STATUS_FAILED, STATUS_OK, STATUS_REFUSED
from cost_autopilot.ledger.summary import render, summarise


def rows():
    """Six rows whose totals are worked out by hand in the tests below."""
    return [
        make_row(
            request_id="a", team_id="demo", tier="T1_TRIVIAL", chosen_model_id="cheap",
            cost_micro_usd=100, counterfactual_top_model_cost_micro_usd=1000,
            input_tokens=10, output_tokens=5, fallback_chain=("cheap",),
        ),
        make_row(
            request_id="b", team_id="demo", tier="T1_TRIVIAL", chosen_model_id="cheap",
            cost_micro_usd=200, counterfactual_top_model_cost_micro_usd=2000,
            input_tokens=20, output_tokens=10, fallback_chain=("cheap",),
        ),
        make_row(
            request_id="c", team_id="ops", tier="T2_STANDARD", chosen_model_id="mid",
            cost_micro_usd=400, counterfactual_top_model_cost_micro_usd=1600,
            input_tokens=40, output_tokens=20, fallback_chain=("cheap", "mid"),
        ),
        make_row(
            request_id="d", team_id="ops", tier="T3_COMPLEX", chosen_model_id="top",
            cost_micro_usd=800, counterfactual_top_model_cost_micro_usd=800,
            input_tokens=80, output_tokens=40, fallback_chain=("top",),
        ),
        make_row(
            request_id="e", team_id="demo", tier="T1_TRIVIAL", status=STATUS_REFUSED,
            chosen_model_id=None, error_type="BudgetExceededError", cost_micro_usd=0,
            counterfactual_top_model_cost_micro_usd=0, input_tokens=0, output_tokens=0,
            fallback_chain=(),
        ),
        make_row(
            request_id="f", team_id="ops", tier="T2_STANDARD", status=STATUS_FAILED,
            chosen_model_id=None, error_type="ProviderTransientError", cost_micro_usd=0,
            counterfactual_top_model_cost_micro_usd=0, input_tokens=0, output_tokens=0,
            fallback_chain=("cheap", "mid", "top"),
        ),
    ]


@pytest.fixture
def summary():
    return summarise(rows(), month="2026-09")


class TestTotals:
    def test_row_count(self, summary):
        assert summary.row_count == 6

    def test_spend_is_the_sum_of_every_row(self, summary):
        assert summary.spend_micro_usd == 100 + 200 + 400 + 800

    def test_counterfactual_is_the_sum_of_every_row(self, summary):
        assert summary.counterfactual_micro_usd == 1000 + 2000 + 1600 + 800

    def test_saving_is_the_difference(self, summary):
        assert summary.saving_micro_usd == 5400 - 1500

    def test_saving_percent_is_floored(self, summary):
        # 3900 / 5400 = 72.22...% -> 72, never 73.
        assert summary.saving_percent == 72

    def test_tokens_are_summed(self, summary):
        assert summary.input_tokens == 150
        assert summary.output_tokens == 75

    def test_every_total_is_an_int(self, summary):
        for value in (
            summary.spend_micro_usd,
            summary.counterfactual_micro_usd,
            summary.saving_micro_usd,
            summary.saving_percent,
        ):
            assert isinstance(value, int)


class TestBreakdowns:
    def test_spend_by_team(self, summary):
        assert summary.spend_by_team == {"demo": 300, "ops": 1200}

    def test_spend_by_model_excludes_rows_with_no_model(self, summary):
        assert summary.spend_by_model == {"cheap": 300, "mid": 400, "top": 800}

    def test_requests_by_tier_counts_every_row(self, summary):
        assert summary.requests_by_tier == {
            "T1_TRIVIAL": 3,
            "T2_STANDARD": 2,
            "T3_COMPLEX": 1,
        }
        assert sum(summary.requests_by_tier.values()) == summary.row_count

    def test_requests_by_status(self, summary):
        assert summary.requests_by_status == {
            STATUS_FAILED: 1,
            STATUS_OK: 4,
            STATUS_REFUSED: 1,
        }

    def test_fallbacks_counted_only_when_more_than_one_rung_was_tried(self, summary):
        # rows c and f each tried more than one model.
        assert summary.fallback_count == 2

    def test_refusals_and_failures_are_reported_separately(self, summary):
        assert summary.refusal_count == 1
        assert summary.failure_count == 1
        assert summary.ok_count == 4


class TestEdgeCases:
    def test_an_empty_month_totals_to_zero(self):
        summary = summarise([], month="2026-09")
        assert summary.row_count == 0
        assert summary.spend_micro_usd == 0
        assert summary.saving_micro_usd == 0

    def test_saving_percent_of_an_empty_month_is_zero_not_a_division_error(self):
        assert summarise([], month="2026-09").saving_percent == 0

    def test_no_saving_when_everything_went_to_the_top_rung(self):
        row = make_row(cost_micro_usd=800, counterfactual_top_model_cost_micro_usd=800)
        summary = summarise([row], month="2026-09")
        assert summary.saving_micro_usd == 0
        assert summary.saving_percent == 0

    def test_a_month_of_only_refusals_has_no_spend(self):
        row = make_row(
            status=STATUS_REFUSED, chosen_model_id=None, error_type="BudgetExceededError",
            cost_micro_usd=0, counterfactual_top_model_cost_micro_usd=0, fallback_chain=(),
        )
        summary = summarise([row], month="2026-09")
        assert summary.spend_micro_usd == 0
        assert summary.refusal_count == 1


class TestRendering:
    def test_renders_the_headline_numbers(self, summary):
        text = render(summary)
        assert "2026-09" in text
        assert "$0.001500" in text  # spend
        assert "$0.003900" in text  # saving
        assert "72%" in text

    def test_renders_every_breakdown(self, summary):
        text = render(summary)
        for label in ("spend by team", "spend by model", "requests by tier", "requests by status"):
            assert label in text

    def test_an_empty_month_says_so(self):
        assert "No requests recorded" in render(summarise([], month="2026-09"))

    def test_unverified_prices_produce_a_warning_on_every_total(self, summary):
        unverified = summarise(rows(), month="2026-09", prices_verified=False)
        text = render(unverified)
        assert "WARNING" in text
        assert "placeholder prices" in text

    def test_verified_prices_produce_no_warning(self, summary):
        assert "WARNING" not in render(summary)
