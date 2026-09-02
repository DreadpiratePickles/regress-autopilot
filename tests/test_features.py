"""Feature extraction is pure and deterministic: text in, counts out.

Every test here is hand-built text with an exactly known answer. Nothing in this
file calls a model, reads a file, or touches a clock.
"""

from dataclasses import FrozenInstanceError

import pytest

from cost_autopilot.classify.features import (
    COMPLEX_VERBS,
    SIMPLE_VERBS,
    FeatureExtractionError,
    RequestFeatures,
    extract_features,
)


class TestValidation:
    @pytest.mark.parametrize("bad", [None, 42, b"bytes", ["a"], 3.5])
    def test_rejects_non_string(self, bad):
        with pytest.raises(FeatureExtractionError):
            extract_features(bad)

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
    def test_rejects_blank(self, blank):
        with pytest.raises(FeatureExtractionError):
            extract_features(blank)

    def test_features_are_frozen(self):
        features = extract_features("hello")
        with pytest.raises(FrozenInstanceError):
            features.word_count = 99  # type: ignore[misc]

    def test_is_a_request_features(self):
        assert isinstance(extract_features("hello"), RequestFeatures)


class TestCounts:
    def test_char_and_word_counts(self):
        features = extract_features("What is the capital of Peru?")
        assert features.char_count == 28
        assert features.word_count == 6

    def test_word_count_ignores_repeated_whitespace(self):
        assert extract_features("one   two\n\nthree\tfour").word_count == 4

    def test_question_count(self):
        assert extract_features("Is it raining?").question_count == 1
        assert extract_features("Why? How? When?").question_count == 3
        assert extract_features("No question here.").question_count == 0

    def test_number_count(self):
        # Three numbers: 12, 3.50, 7781. A decimal point does not split one in two.
        features = extract_features("Order 12 units at 3.50 each, invoice 7781.")
        assert features.number_count == 3

    def test_number_count_is_zero_without_digits(self):
        assert extract_features("no digits at all").number_count == 0


class TestCodeAndTraces:
    def test_detects_a_fenced_code_block(self):
        text = "Fix this:\n```python\nprint(x)\n```"
        assert extract_features(text).has_code_fence is True

    def test_plain_prose_has_no_fence(self):
        assert extract_features("Just some prose about code.").has_code_fence is False

    def test_detects_a_python_traceback(self):
        text = 'Traceback (most recent call last):\n  File "a.py", line 3\nKeyError: "id"'
        assert extract_features(text).has_stack_trace is True

    def test_detects_a_python_file_frame_without_the_header(self):
        assert extract_features('  File "run.py", line 12, in main').has_stack_trace is True

    def test_detects_a_javascript_frame(self):
        text = "TypeError: undefined is not a function\n    at handler (app.js:14:9)"
        assert extract_features(text).has_stack_trace is True

    def test_the_word_error_alone_is_not_a_trace(self):
        assert extract_features("I got an error yesterday.").has_stack_trace is False


class TestTables:
    def test_detects_a_pipe_table(self):
        text = "Convert this:\n| a | b |\n| 1 | 2 |\n| 3 | 4 |"
        assert extract_features(text).has_table is True

    def test_detects_a_tab_separated_table(self):
        text = "name\tqty\ttotal\nbolt\t4\t8\nnut\t2\t3"
        assert extract_features(text).has_table is True

    def test_one_pipe_line_is_not_a_table(self):
        assert extract_features("a | b and nothing else").has_table is False

    def test_prose_with_commas_is_not_a_table(self):
        text = "We sell bolts, nuts, and washers.\nThey cost 4, 2, and 1."
        assert extract_features(text).has_table is False


class TestVerbs:
    def test_finds_a_simple_verb(self):
        assert extract_features("Translate this to French.").simple_verbs == ("translate",)

    def test_finds_a_complex_verb(self):
        assert extract_features("Debug the failing job.").complex_verbs == ("debug",)

    def test_verbs_are_deduplicated_and_sorted(self):
        features = extract_features("Summarize it, then translate it, then summarize again.")
        assert features.simple_verbs == ("summarize", "translate")

    def test_verb_matching_is_case_insensitive(self):
        assert extract_features("ANALYZE the logs.").complex_verbs == ("analyze",)

    def test_verb_matching_respects_word_boundaries(self):
        # "listen" contains "list"; "designer" contains "design".
        features = extract_features("Listen to the designers.")
        assert features.simple_verbs == ()
        assert features.complex_verbs == ()

    def test_both_lists_can_fire(self):
        features = extract_features("Extract the fields and then design a schema.")
        assert features.simple_verbs == ("extract",)
        assert features.complex_verbs == ("design",)

    def test_the_two_vocabularies_do_not_overlap(self):
        assert set(SIMPLE_VERBS).isdisjoint(set(COMPLEX_VERBS))


class TestConstraintsAndBullets:
    def test_counts_constraint_markers(self):
        text = "It must be short, at most 3 lines, and must not mention pricing."
        features = extract_features(text)
        assert features.constraint_count == 3
        assert "at most" in features.constraint_markers

    def test_no_constraints_in_plain_prose(self):
        features = extract_features("Tell me about the weather.")
        assert features.constraint_count == 0
        assert features.constraint_markers == ()

    def test_counts_dash_bullets(self):
        text = "Consider:\n- one\n- two\n- three"
        assert extract_features(text).bullet_count == 3

    def test_counts_numbered_bullets(self):
        text = "Steps:\n1. do this\n2. do that\n3) and this"
        assert extract_features(text).bullet_count == 3

    def test_a_dash_inside_a_sentence_is_not_a_bullet(self):
        assert extract_features("A well - placed dash is not a bullet.").bullet_count == 0


class TestOutputLengthHint:
    @pytest.mark.parametrize(
        "text",
        [
            "Summarize it in 2 sentences.",
            "Answer in under 100 words.",
            "Keep it to at most 50 words.",
            "Give me 3-4 sentences.",
            "No more than 80 words please.",
        ],
    )
    def test_finds_a_hint(self, text):
        assert extract_features(text).output_length_hint is not None

    def test_no_hint_when_none_is_given(self):
        assert extract_features("Summarize this ticket.").output_length_hint is None

    def test_a_bare_number_is_not_a_hint(self):
        assert extract_features("There were 12 incidents.").output_length_hint is None


class TestMultiStepMarkers:
    def test_finds_a_marker(self):
        assert extract_features("Walk through it step by step.").multi_step_markers != ()

    def test_finds_trade_off_spelled_either_way(self):
        assert extract_features("Compare the trade-offs.").multi_step_markers == ("trade-off",)
        assert extract_features("Compare the tradeoffs.").multi_step_markers == ("tradeoff",)

    def test_none_in_a_plain_request(self):
        assert extract_features("What time is it?").multi_step_markers == ()


class TestNonAscii:
    def test_pure_ascii_is_zero_permille(self):
        assert extract_features("plain ascii text").non_ascii_permille == 0

    def test_counts_non_ascii_characters(self):
        # 4 of 8 characters are non-ASCII -> 500 permille.
        assert extract_features("abéèêëcd").non_ascii_permille == 500

    def test_permille_is_an_integer_not_a_ratio(self):
        value = extract_features("café").non_ascii_permille
        assert isinstance(value, int)
        assert value == 250

    def test_all_non_ascii(self):
        assert extract_features("你好世界").non_ascii_permille == 1000


class TestDeterminism:
    def test_same_text_gives_identical_features(self):
        text = "Debug this:\n```py\nx = 1/0\n```\nIt must not crash."
        assert extract_features(text) == extract_features(text)
