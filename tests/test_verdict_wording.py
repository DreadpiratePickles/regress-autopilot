"""The four verdict wordings, and the sample size the fourth one quotes.

`tier_verdict_line` is the only place those sentences are written. `ledger
summary` and stage 04's report both render it, so the assertions here are exact
strings: a wording change should be a deliberate, visible diff rather than
something two commands quietly disagree about.
"""

import pytest
from regression_detect.compare import wilson_interval

from cost_autopilot.validate.regret import RegretGroup
from cost_autopilot.validate.report import (
    VERDICT_INSUFFICIENT,
    VERDICT_NO_REGRET_OBSERVED,
    VERDICT_SAFE,
    VERDICT_TOO_HIGH,
    clean_samples_needed,
    tier_verdict_line,
    tier_verdict_state,
)


def group(*, n: int, regret_count: int, label: str = "T1_TRIVIAL") -> RegretGroup:
    low, high = wilson_interval(regret_count, n)
    return RegretGroup(
        label=label,
        n=n,
        regret_count=regret_count,
        regret_rate=(regret_count / n) if n else 0.0,
        wilson_low=low,
        wilson_high=high,
        judge_error_count=0,
        validation_cost_micro_usd=0,
    )


class TestCleanSamplesNeeded:
    def test_the_shipped_threshold_needs_thirty_five(self):
        """The number `autopilot.toml` quotes at min_samples, computed rather than
        copied. If this ever moves, the config comment is wrong."""
        assert clean_samples_needed(0.10) == 35

    def test_the_answer_really_is_the_smallest_such_n(self):
        needed = clean_samples_needed(0.10)
        assert wilson_interval(0, needed)[1] < 0.10
        assert wilson_interval(0, needed - 1)[1] >= 0.10

    @pytest.mark.parametrize("max_regret", [0.05, 0.10, 0.20, 0.33, 0.50])
    def test_it_is_the_boundary_for_every_reachable_threshold(self, max_regret):
        needed = clean_samples_needed(max_regret)
        assert wilson_interval(0, needed)[1] < max_regret
        assert wilson_interval(0, needed - 1)[1] >= max_regret

    def test_a_loose_threshold_is_satisfied_by_one_sample(self):
        assert clean_samples_needed(0.95) == 1

    def test_a_looser_threshold_never_needs_more_samples(self):
        assert clean_samples_needed(0.20) < clean_samples_needed(0.10)

    def test_zero_regret_tolerance_is_unreachable_and_says_so(self):
        """`max_regret = 0.0` is a legal setting no sample size satisfies. The
        answer is None, not an enormous number and not an infinite loop."""
        assert clean_samples_needed(0.0) is None

    def test_the_search_respects_its_limit(self):
        assert clean_samples_needed(0.001, limit=10) is None
        assert clean_samples_needed(0.001) == 3838


class TestVerdictState:
    def test_a_tight_interval_is_safe(self):
        state = tier_verdict_state(
            group(n=200, regret_count=0), max_regret=0.10, min_samples=10
        )
        assert state == VERDICT_SAFE

    def test_too_few_samples_is_insufficient_evidence(self):
        state = tier_verdict_state(
            group(n=4, regret_count=1), max_regret=0.10, min_samples=10
        )
        assert state == VERDICT_INSUFFICIENT

    def test_zero_regret_on_a_wide_interval_is_the_fourth_state(self):
        state = tier_verdict_state(
            group(n=12, regret_count=0), max_regret=0.10, min_samples=10
        )
        assert state == VERDICT_NO_REGRET_OBSERVED

    def test_observed_regret_on_a_wide_interval_is_too_high(self):
        state = tier_verdict_state(
            group(n=20, regret_count=10), max_regret=0.10, min_samples=10
        )
        assert state == VERDICT_TOO_HIGH

    def test_safe_is_checked_before_the_sample_size(self):
        """A tiny sample can still be safe under a loose threshold, and saying
        "insufficient evidence" there would be false: the interval does fit."""
        state = tier_verdict_state(
            group(n=2, regret_count=0), max_regret=0.95, min_samples=10
        )
        assert state == VERDICT_SAFE


class TestVerdictLine:
    def test_safe_names_the_bound_and_the_threshold(self):
        item = group(n=200, regret_count=0)
        assert tier_verdict_line(item, max_regret=0.10, min_samples=10) == (
            f"safe: the 95% upper bound {item.wilson_high:.3f} is below max_regret 0.100"
        )

    def test_insufficient_evidence_names_both_counts(self):
        item = group(n=4, regret_count=1)
        assert tier_verdict_line(item, max_regret=0.10, min_samples=10) == (
            "insufficient evidence: 4 validated sample(s), fewer than min_samples 10"
        )

    def test_the_fourth_verdict_says_zero_regret_and_counts_the_shortfall(self):
        item = group(n=12, regret_count=0)
        line = tier_verdict_line(item, max_regret=0.10, min_samples=10)
        assert line == (
            "no regret observed (n=12); interval too wide — need ~23 more clean "
            f"samples (95% upper bound {item.wilson_high:.3f}, max_regret 0.100)"
        )

    def test_the_fourth_verdict_never_accuses_the_routing(self):
        line = tier_verdict_line(group(n=12, regret_count=0), max_regret=0.10, min_samples=10)
        assert "regret too high" not in line
        assert "routing this tier up" not in line

    def test_the_shortfall_is_needed_minus_n(self):
        for n in (10, 20, 33):
            line = tier_verdict_line(
                group(n=n, regret_count=0), max_regret=0.10, min_samples=10
            )
            assert f"need ~{35 - n} more clean samples" in line

    def test_a_shortfall_of_one_says_sample_not_samples(self):
        """n = 34 is one short of the 35 a zero rate needs at max_regret = 0.10,
        and "1 more clean samples" is the kind of wording that makes a reader
        distrust the arithmetic next to it."""
        line = tier_verdict_line(group(n=34, regret_count=0), max_regret=0.10, min_samples=10)
        assert "need ~1 more clean sample (" in line
        assert "clean samples" not in line

    def test_an_unreachable_threshold_says_so_rather_than_quoting_a_number(self):
        line = tier_verdict_line(group(n=12, regret_count=0), max_regret=0.0, min_samples=10)
        assert "no sample size brings the 95% upper bound below max_regret 0.000" in line

    def test_regret_too_high_still_names_the_action(self):
        item = group(n=20, regret_count=10)
        assert tier_verdict_line(item, max_regret=0.10, min_samples=10) == (
            "regret too high — consider routing this tier up "
            f"(95% upper bound {item.wilson_high:.3f}, max_regret 0.100)"
        )

    def test_the_line_always_matches_the_state(self):
        cases = [(200, 0), (4, 1), (12, 0), (20, 10)]
        for n, regret_count in cases:
            item = group(n=n, regret_count=regret_count)
            state = tier_verdict_state(item, max_regret=0.10, min_samples=10)
            assert tier_verdict_line(item, max_regret=0.10, min_samples=10).startswith(state)
