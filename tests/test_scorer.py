"""The rule-based scorer: features in, a tier plus a score plus reasons out.

Every expectation here is derived from the weights in `scorer.py`, not from
running the scorer and copying what it said.
"""

from dataclasses import FrozenInstanceError

import pytest

from cost_autopilot.classify.features import extract_features
from cost_autopilot.classify.scorer import (
    MAX_SCORE,
    MIN_SCORE,
    WEIGHTS,
    Classification,
    ScorerThresholds,
    Tier,
    classify_text,
    score_features,
)

DEFAULTS = ScorerThresholds(t2_min_score=15, t3_min_score=45)

CLAMPING_TEXT = (
    "Debug, analyze, design and optimize this step by step. "
    "It must not fail, must be at most 3 lines, and must handle errors.\n"
    "```python\nraise ValueError\n```\n"
    'Traceback (most recent call last):\n  File "a.py", line 1\n'
    "| a | b |\n| 1 | 2 |\n"
    "- one\n- two\n- three\n"
    "Why? How? When? " + "word " * 400
)
"""Enough signals firing at once that the raw total runs past `MAX_SCORE`, which
is the only way to see a clamp reason."""


class TestTierEnum:
    def test_has_exactly_three_tiers(self):
        assert [tier.value for tier in Tier] == ["T1_TRIVIAL", "T2_STANDARD", "T3_COMPLEX"]

    def test_tiers_are_ordered(self):
        assert Tier.T1_TRIVIAL < Tier.T2_STANDARD < Tier.T3_COMPLEX

    def test_round_trips_through_its_value(self):
        assert Tier("T3_COMPLEX") is Tier.T3_COMPLEX

    def test_rejects_an_unknown_value(self):
        with pytest.raises(ValueError):
            Tier("T4_IMPOSSIBLE")


class TestTrivialRequests:
    def test_a_bare_fact_question_scores_zero(self):
        result = classify_text("What is the capital of Peru?", DEFAULTS)
        assert result.complexity_score == 0
        assert result.tier is Tier.T1_TRIVIAL

    def test_a_one_line_translation_stays_trivial(self):
        result = classify_text("Translate 'ou est la gare' into English.", DEFAULTS)
        # very short (+0) + one simple verb (+6) = 6
        assert result.complexity_score == WEIGHTS["simple_verb"]
        assert result.tier is Tier.T1_TRIVIAL

    def test_a_unit_conversion_stays_trivial(self):
        result = classify_text("Convert 12 miles to kilometres.", DEFAULTS)
        assert result.tier is Tier.T1_TRIVIAL

    def test_every_classification_carries_at_least_one_reason(self):
        result = classify_text("Hi", DEFAULTS)
        assert len(result.reasons) >= 1


class TestComplexSignals:
    def test_a_stack_trace_and_a_fence_force_complex(self):
        text = (
            "Fix this:\n```python\nprint(row['id'])\n```\n"
            'Traceback (most recent call last):\n  File "a.py", line 3\nKeyError: "id"'
        )
        result = classify_text(text, DEFAULTS)
        assert result.tier is Tier.T3_COMPLEX
        assert result.complexity_score >= WEIGHTS["code_fence"] + WEIGHTS["stack_trace"]

    def test_a_complex_verb_is_worth_more_than_a_simple_one(self):
        assert WEIGHTS["complex_verb"] > WEIGHTS["simple_verb"]

    def test_complex_verbs_are_capped(self):
        text = "Debug, analyze, design, plan, optimize and refactor this."
        features = extract_features(text)
        assert len(features.complex_verbs) >= 5
        score, _ = score_features(features)
        # The verb contribution alone cannot exceed its cap.
        assert score <= MAX_SCORE

    def test_a_multi_step_marker_fires(self):
        result = classify_text("Walk me through the trade-offs step by step.", DEFAULTS)
        assert any("multi-step" in reason for reason in result.reasons)


class TestScoreBounds:
    def test_score_never_below_zero(self):
        result = classify_text("hi", DEFAULTS)
        assert result.complexity_score >= MIN_SCORE

    def test_score_never_above_one_hundred(self):
        result = classify_text(CLAMPING_TEXT, DEFAULTS)
        assert result.complexity_score == MAX_SCORE
        assert result.tier is Tier.T3_COMPLEX

    def test_score_is_always_an_int(self):
        for text in ("hi", "Summarize this paragraph in 2 sentences.", "Debug ```x```"):
            assert isinstance(classify_text(text, DEFAULTS).complexity_score, int)


class TestThresholdBoundaries:
    """A tier boundary is inclusive at the bottom: score >= t2_min_score is T2."""

    def test_exactly_at_t2_minimum_is_t2(self):
        thresholds = ScorerThresholds(t2_min_score=6, t3_min_score=45)
        result = classify_text("Translate this.", thresholds)
        assert result.complexity_score == 6
        assert result.tier is Tier.T2_STANDARD

    def test_one_below_t2_minimum_is_t1(self):
        thresholds = ScorerThresholds(t2_min_score=7, t3_min_score=45)
        result = classify_text("Translate this.", thresholds)
        assert result.complexity_score == 6
        assert result.tier is Tier.T1_TRIVIAL

    def test_exactly_at_t3_minimum_is_t3(self):
        thresholds = ScorerThresholds(t2_min_score=1, t3_min_score=6)
        result = classify_text("Translate this.", thresholds)
        assert result.complexity_score == 6
        assert result.tier is Tier.T3_COMPLEX

    def test_one_below_t3_minimum_is_t2(self):
        thresholds = ScorerThresholds(t2_min_score=1, t3_min_score=7)
        result = classify_text("Translate this.", thresholds)
        assert result.tier is Tier.T2_STANDARD

    def test_zero_score_with_a_zero_t2_minimum_is_t2(self):
        thresholds = ScorerThresholds(t2_min_score=0, t3_min_score=45)
        assert classify_text("Hello", thresholds).tier is Tier.T2_STANDARD


class TestThresholdValidation:
    def test_rejects_t3_below_t2(self):
        with pytest.raises(ValueError, match="t3_min_score"):
            ScorerThresholds(t2_min_score=50, t3_min_score=20)

    def test_allows_equal_thresholds(self):
        # Equal thresholds mean the T2 band is empty. Odd, but coherent.
        thresholds = ScorerThresholds(t2_min_score=30, t3_min_score=30)
        assert thresholds.t2_min_score == 30

    @pytest.mark.parametrize("bad", [-1, 101, 1.5, "20", None, True])
    def test_rejects_out_of_range_or_non_integer(self, bad):
        with pytest.raises(ValueError):
            ScorerThresholds(t2_min_score=bad, t3_min_score=45)


class TestReasons:
    def test_reasons_name_the_feature_that_fired(self):
        result = classify_text("Debug this failing job.", DEFAULTS)
        assert any("debug" in reason for reason in result.reasons)

    def test_reasons_carry_their_weight(self):
        result = classify_text("Debug this failing job.", DEFAULTS)
        assert any("+14" in reason for reason in result.reasons)

    def test_reasons_are_a_tuple_of_strings(self):
        result = classify_text("Summarize this.", DEFAULTS)
        assert isinstance(result.reasons, tuple)
        assert all(isinstance(reason, str) for reason in result.reasons)

    def test_a_clamp_reason_carries_exactly_one_sign(self):
        """A clamp's delta is negative by construction, so it already has a sign.
        Prefixing another produced `(+-14)`, which reads as a typo."""
        result = classify_text(CLAMPING_TEXT, DEFAULTS)
        clamps = [reason for reason in result.reasons if reason.startswith("clamped from ")]
        assert len(clamps) == 1
        assert "+-" not in clamps[0]

        raw_total = sum(
            int(reason.rsplit("+", 1)[1].rstrip(")"))
            for reason in result.reasons
            if reason is not clamps[0]
        )
        assert clamps[0] == f"clamped from {raw_total} to {MAX_SCORE} ({MAX_SCORE - raw_total})"

    def test_the_weights_in_the_reasons_sum_to_the_score(self):
        text = "Debug this table:\n| a | b |\n| 1 | 2 |\nIt must not crash."
        result = classify_text(text, DEFAULTS)
        total = sum(int(reason.rsplit("+", 1)[1].rstrip(")")) for reason in result.reasons)
        assert total == result.complexity_score


class TestClassificationRecord:
    def test_is_frozen(self):
        result = classify_text("Hello", DEFAULTS)
        with pytest.raises(FrozenInstanceError):
            result.tier = Tier.T3_COMPLEX  # type: ignore[misc]

    def test_carries_the_features_it_decided_from(self):
        result = classify_text("What is 2 + 2?", DEFAULTS)
        assert isinstance(result, Classification)
        assert result.features.question_count == 1

    def test_records_that_the_rules_decided_it(self):
        assert classify_text("Hello", DEFAULTS).decided_by == "rules"

    def test_same_text_and_thresholds_give_the_same_answer(self):
        text = "Compare the trade-offs, at most 3 bullets."
        assert classify_text(text, DEFAULTS) == classify_text(text, DEFAULTS)
