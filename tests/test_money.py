"""Money is integer micro-USD. These tests exist to keep a float out."""

import pytest

from cost_autopilot.money import (
    CURRENCY,
    MICRO_USD_PER_USD,
    MoneyError,
    completion_cost_micro_usd,
    format_micro_usd,
    token_cost_micro_usd,
    validate_micro_usd,
)


class TestTokenCost:
    def test_zero_tokens_cost_nothing(self):
        assert token_cost_micro_usd(tokens=0, price_micro_usd_per_1k=2500) == 0

    def test_zero_price_costs_nothing(self):
        assert token_cost_micro_usd(tokens=1_000_000, price_micro_usd_per_1k=0) == 0

    def test_exact_thousand_is_exactly_the_price(self):
        assert token_cost_micro_usd(tokens=1000, price_micro_usd_per_1k=300) == 300

    def test_exact_multiple_does_not_round_up(self):
        # 3000 tokens at 300 micro-USD/1k is exactly 900. Ceil must not add one.
        assert token_cost_micro_usd(tokens=3000, price_micro_usd_per_1k=300) == 900

    def test_partial_thousand_rounds_up(self):
        # 1 token at 300/1k is 0.3 micro-USD. Rounding down would make a
        # million one-token calls free; the house never loses the fraction.
        assert token_cost_micro_usd(tokens=1, price_micro_usd_per_1k=300) == 1

    def test_rounds_up_just_below_a_boundary(self):
        assert token_cost_micro_usd(tokens=999, price_micro_usd_per_1k=1000) == 999
        assert token_cost_micro_usd(tokens=1001, price_micro_usd_per_1k=1000) == 1001

    def test_ceil_on_an_awkward_ratio(self):
        # 1500 * 333 = 499500; /1000 = 499.5 -> 500
        assert token_cost_micro_usd(tokens=1500, price_micro_usd_per_1k=333) == 500

    def test_huge_token_counts_stay_exact(self):
        # Python ints do not overflow; a float would have lost precision here.
        tokens = 10**15 + 1
        result = token_cost_micro_usd(tokens=tokens, price_micro_usd_per_1k=12_000)
        assert result == (tokens * 12_000 + 999) // 1000
        assert isinstance(result, int)

    def test_result_is_always_an_int(self):
        for tokens, price in ((0, 0), (1, 1), (7, 13), (123_456, 2500)):
            assert isinstance(
                token_cost_micro_usd(tokens=tokens, price_micro_usd_per_1k=price), int
            )

    @pytest.mark.parametrize("tokens", [-1, 1.5, "10", None, True])
    def test_rejects_non_integer_or_negative_tokens(self, tokens):
        with pytest.raises(MoneyError):
            token_cost_micro_usd(tokens=tokens, price_micro_usd_per_1k=100)

    @pytest.mark.parametrize("price", [-1, 0.3, "100", None, True])
    def test_rejects_non_integer_or_negative_price(self, price):
        with pytest.raises(MoneyError):
            token_cost_micro_usd(tokens=10, price_micro_usd_per_1k=price)


class TestCompletionCost:
    def test_sums_input_and_output_legs_after_rounding_each(self):
        # Each leg ceils on its own, and the legs have different prices:
        # 1 input token at 300/1k -> ceil(0.3) = 1;
        # 1 output token at 2500/1k -> ceil(2.5) = 3.
        cost = completion_cost_micro_usd(
            input_tokens=1,
            output_tokens=1,
            input_price_micro_usd_per_1k=300,
            output_price_micro_usd_per_1k=2500,
        )
        assert cost == 4

    def test_realistic_call(self):
        # 1200 in at 300/1k -> 360; 800 out at 2500/1k -> 2000.
        cost = completion_cost_micro_usd(
            input_tokens=1200,
            output_tokens=800,
            input_price_micro_usd_per_1k=300,
            output_price_micro_usd_per_1k=2500,
        )
        assert cost == 2360
        assert isinstance(cost, int)

    def test_zero_tokens_both_legs(self):
        assert (
            completion_cost_micro_usd(
                input_tokens=0,
                output_tokens=0,
                input_price_micro_usd_per_1k=300,
                output_price_micro_usd_per_1k=2500,
            )
            == 0
        )

    def test_rejects_a_float_anywhere(self):
        with pytest.raises(MoneyError):
            completion_cost_micro_usd(
                input_tokens=10.0,
                output_tokens=1,
                input_price_micro_usd_per_1k=300,
                output_price_micro_usd_per_1k=2500,
            )


class TestValidateMicroUsd:
    def test_accepts_zero_and_positive(self):
        assert validate_micro_usd(0, field="cap") == 0
        assert validate_micro_usd(5_000_000, field="cap") == 5_000_000

    @pytest.mark.parametrize("value", [-1, 1.0, "5", None, True])
    def test_rejects_everything_else(self, value):
        with pytest.raises(MoneyError):
            validate_micro_usd(value, field="cap")

    def test_error_names_the_field(self):
        with pytest.raises(MoneyError, match="monthly_cap_micro_usd"):
            validate_micro_usd(-1, field="monthly_cap_micro_usd")


class TestFormatting:
    def test_currency_is_always_usd(self):
        assert CURRENCY == "USD"

    def test_one_dollar_is_a_million_micro_usd(self):
        assert MICRO_USD_PER_USD == 1_000_000

    def test_formats_with_six_decimal_places(self):
        assert format_micro_usd(2_360) == "$0.002360"
        assert format_micro_usd(0) == "$0.000000"
        assert format_micro_usd(1_000_000) == "$1.000000"
        assert format_micro_usd(1_234_567) == "$1.234567"

    def test_formatting_never_goes_through_a_float(self):
        # 0.1 + 0.2 style drift would show up in the last digits here.
        huge = 10**15 + 7
        assert format_micro_usd(huge) == "$1000000000.000007"

    def test_rejects_a_non_integer(self):
        with pytest.raises(MoneyError):
            format_micro_usd(1.5)
