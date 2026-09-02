"""Budget caps: the integer edges, and what an unlisted team is held to."""

import pytest

from cost_autopilot.money import MoneyError
from cost_autopilot.route.budget import (
    BudgetError,
    BudgetExceededError,
    Budgets,
    check_budget,
)

BUDGETS = Budgets(default_monthly_cap_micro_usd=1000, teams={"vip": 5000, "frozen": 0})


class TestCapLookup:
    def test_a_listed_team_gets_its_own_cap(self):
        assert BUDGETS.cap_for("vip") == 5000

    def test_an_unlisted_team_gets_the_default(self):
        assert BUDGETS.cap_for("nobody") == 1000

    def test_a_zero_cap_is_honoured_not_treated_as_missing(self):
        assert BUDGETS.cap_for("frozen") == 0


class TestValidation:
    def test_rejects_a_negative_default(self):
        with pytest.raises(MoneyError):
            Budgets(default_monthly_cap_micro_usd=-1, teams={})

    def test_rejects_a_decimal_cap(self):
        with pytest.raises(MoneyError):
            Budgets(default_monthly_cap_micro_usd=1.5, teams={})

    def test_rejects_a_negative_team_cap(self):
        with pytest.raises(MoneyError):
            Budgets(default_monthly_cap_micro_usd=100, teams={"a": -1})

    def test_rejects_a_non_table_teams_value(self):
        with pytest.raises(BudgetError):
            Budgets(default_monthly_cap_micro_usd=100, teams=["a"])  # type: ignore[arg-type]

    def test_rejects_an_empty_team_id(self):
        with pytest.raises(BudgetError):
            Budgets(default_monthly_cap_micro_usd=100, teams={"  ": 5})


class TestCheckBudget:
    def test_below_the_cap_passes_and_returns_the_cap(self):
        assert check_budget(
            team_id="nobody", month="2026-09", spend_micro_usd=999, budgets=BUDGETS
        ) == 1000

    def test_exactly_at_the_cap_is_refused(self):
        # The documented edge: the cap is the most a team may spend.
        with pytest.raises(BudgetExceededError):
            check_budget(team_id="nobody", month="2026-09", spend_micro_usd=1000, budgets=BUDGETS)

    def test_one_micro_usd_below_the_cap_passes(self):
        assert check_budget(
            team_id="vip", month="2026-09", spend_micro_usd=4999, budgets=BUDGETS
        ) == 5000

    def test_over_the_cap_is_refused(self):
        with pytest.raises(BudgetExceededError):
            check_budget(team_id="vip", month="2026-09", spend_micro_usd=5001, budgets=BUDGETS)

    def test_a_zero_cap_refuses_at_zero_spend(self):
        with pytest.raises(BudgetExceededError):
            check_budget(team_id="frozen", month="2026-09", spend_micro_usd=0, budgets=BUDGETS)

    def test_zero_spend_against_a_positive_cap_passes(self):
        assert check_budget(
            team_id="nobody", month="2026-09", spend_micro_usd=0, budgets=BUDGETS
        ) == 1000

    def test_rejects_a_non_integer_spend(self):
        with pytest.raises(MoneyError):
            check_budget(team_id="vip", month="2026-09", spend_micro_usd=1.5, budgets=BUDGETS)


class TestTheError:
    def test_carries_the_numbers_for_the_ledger_row(self):
        with pytest.raises(BudgetExceededError) as caught:
            check_budget(team_id="vip", month="2026-09", spend_micro_usd=6000, budgets=BUDGETS)
        error = caught.value
        assert error.team_id == "vip"
        assert error.month == "2026-09"
        assert error.spend_micro_usd == 6000
        assert error.cap_micro_usd == 5000

    def test_the_message_names_the_team_and_the_month(self):
        with pytest.raises(BudgetExceededError, match="2026-09"):
            check_budget(team_id="vip", month="2026-09", spend_micro_usd=6000, budgets=BUDGETS)

    def test_the_message_renders_money_as_dollars(self):
        with pytest.raises(BudgetExceededError, match=r"\$0\.005000"):
            check_budget(team_id="vip", month="2026-09", spend_micro_usd=6000, budgets=BUDGETS)
