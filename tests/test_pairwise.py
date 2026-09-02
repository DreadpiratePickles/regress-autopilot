"""The pairwise judge: strict parsing, and the swapped-order truth table."""

import pytest

from cost_autopilot.validate.pairwise import (
    PAIRWISE_PROMPT_PATH,
    PairwiseParseError,
    PairwiseVerdict,
    build_pairwise_user_message,
    combine_orders,
    judge_pairwise,
    load_pairwise_prompt,
    parse_pairwise_verdict,
)

FORWARD_TRUE = '{"reason": "A answers the question.", "a_at_least_as_good": true}'
FORWARD_FALSE = '{"reason": "B is more complete.", "a_at_least_as_good": false}'


class RecordingProvider:
    """A text `Provider` that replays scripted replies and remembers the calls."""

    def __init__(self, replies):
        self.model_id = "fake-judge"
        self._replies = list(replies)
        self.calls = []

    def complete(self, *, system, user, temperature):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        return self._replies[(len(self.calls) - 1) % len(self._replies)]


class TestPrompt:
    def test_the_prompt_file_ships_with_the_package(self):
        assert PAIRWISE_PROMPT_PATH.is_file()

    def test_the_prompt_names_the_key_it_demands(self):
        assert "a_at_least_as_good" in load_pairwise_prompt()

    def test_a_missing_prompt_is_an_error_not_an_unguided_judge(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_pairwise_prompt(tmp_path / "absent.md")


class TestUserMessage:
    def test_all_three_inputs_travel_inside_their_own_delimiters(self):
        message = build_pairwise_user_message("the request", "answer one", "answer two")
        assert "<request>\nthe request\n</request>" in message
        assert "<answer_a>\nanswer one\n</answer_a>" in message
        assert "<answer_b>\nanswer two\n</answer_b>" in message

    def test_the_answers_are_not_formatted_into_the_system_prompt(self):
        provider = RecordingProvider([FORWARD_TRUE])
        judge_pairwise(
            request="q", answer_a="cheap answer", answer_b="reference", provider=provider
        )
        assert "cheap answer" not in provider.calls[0]["system"]
        assert "cheap answer" in provider.calls[0]["user"]


class TestParsing:
    def test_a_true_verdict_parses(self):
        verdict = parse_pairwise_verdict(FORWARD_TRUE)
        assert verdict == PairwiseVerdict(a_at_least_as_good=True, reason="A answers the question.")

    def test_a_false_verdict_parses(self):
        assert parse_pairwise_verdict(FORWARD_FALSE).a_at_least_as_good is False

    def test_a_single_markdown_fence_is_tolerated(self):
        raw = f"```json\n{FORWARD_TRUE}\n```"
        assert parse_pairwise_verdict(raw).a_at_least_as_good is True

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "not json at all",
            "[1, 2, 3]",
            '{"a_at_least_as_good": true}',
            '{"reason": "ok", "a_at_least_as_good": true, "extra": 1}',
            '{"reason": "ok", "a_at_least_as_good": "true"}',
            '{"reason": "ok", "a_at_least_as_good": 1}',
            '{"reason": "", "a_at_least_as_good": true}',
            '{"reason": 5, "a_at_least_as_good": true}',
        ],
    )
    def test_anything_else_is_a_parse_error(self, raw):
        with pytest.raises(PairwiseParseError):
            parse_pairwise_verdict(raw)

    def test_a_non_string_reply_is_a_parse_error(self):
        with pytest.raises(PairwiseParseError):
            parse_pairwise_verdict(None)


class TestCombineOrders:
    """The truth table that turns two swapped judgements into one verdict."""

    def test_both_orders_naming_the_slot_a_answer_reads_as_a_tie(self):
        outcome = combine_orders(forward=True, reverse=True)
        assert outcome.regret is False
        assert outcome.position_bias_detected is False

    def test_the_cheap_answer_judged_strictly_better_is_not_regret(self):
        outcome = combine_orders(forward=True, reverse=False)
        assert outcome.regret is False
        assert outcome.position_bias_detected is False

    def test_the_reference_judged_strictly_better_is_regret(self):
        outcome = combine_orders(forward=False, reverse=True)
        assert outcome.regret is True
        assert outcome.position_bias_detected is False

    def test_each_order_naming_the_other_answer_is_position_bias_and_counts_as_regret(self):
        outcome = combine_orders(forward=False, reverse=False)
        assert outcome.position_bias_detected is True
        assert outcome.regret is True

    @pytest.mark.parametrize(
        ("forward", "reverse"), [(None, True), (True, None), (None, None)]
    )
    def test_a_missing_order_yields_no_verdict_and_no_bias_claim(self, forward, reverse):
        outcome = combine_orders(forward=forward, reverse=reverse)
        assert outcome.regret is None
        assert outcome.position_bias_detected is False


class TestJudgePairwise:
    def test_it_returns_the_parsed_verdict(self):
        provider = RecordingProvider([FORWARD_FALSE])
        verdict = judge_pairwise(request="q", answer_a="a", answer_b="b", provider=provider)
        assert verdict.a_at_least_as_good is False

    def test_it_judges_at_temperature_zero(self):
        provider = RecordingProvider([FORWARD_TRUE])
        judge_pairwise(request="q", answer_a="a", answer_b="b", provider=provider)
        assert provider.calls[0]["temperature"] == 0.0

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_input_is_refused_before_the_model_is_called(self, blank):
        provider = RecordingProvider([FORWARD_TRUE])
        with pytest.raises(ValueError):
            judge_pairwise(request=blank, answer_a="a", answer_b="b", provider=provider)
        assert provider.calls == []


class TestFenceTolerance:
    """One tolerance, shared by every strict parser in this package."""

    def test_an_unclosed_fence_is_not_silently_repaired(self):
        with pytest.raises(PairwiseParseError):
            parse_pairwise_verdict(f"```json\n{FORWARD_TRUE}")

    def test_a_fence_naming_another_language_is_not_stripped(self):
        with pytest.raises(PairwiseParseError):
            parse_pairwise_verdict(f"```python\n{FORWARD_TRUE}\n```")

    def test_an_unlabelled_fence_is_stripped(self):
        assert parse_pairwise_verdict(f"```\n{FORWARD_TRUE}\n```").a_at_least_as_good is True
