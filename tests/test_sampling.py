"""Shadow sampling: the same request id must always decide the same way."""

import pytest

from cost_autopilot.validate.sampling import (
    SAMPLE_BUCKETS,
    SamplingError,
    is_sampled,
    sample_bucket,
    validate_sample_percent,
)

SYNTHETIC_IDS = [f"req-{index}" for index in range(1000)]


class TestBucket:
    def test_a_bucket_is_inside_the_range(self):
        for request_id in SYNTHETIC_IDS[:50]:
            assert 0 <= sample_bucket(request_id) < SAMPLE_BUCKETS

    def test_the_same_id_always_lands_in_the_same_bucket(self):
        assert sample_bucket("req-1") == sample_bucket("req-1")
        assert sample_bucket("req-1") != sample_bucket("req-2")

    def test_a_blank_id_is_refused(self):
        with pytest.raises(SamplingError):
            sample_bucket("   ")

    def test_a_non_string_id_is_refused(self):
        with pytest.raises(SamplingError):
            sample_bucket(7)


class TestIsSampled:
    def test_the_same_id_is_always_sampled_or_never_sampled(self):
        first = [is_sampled(request_id, 20) for request_id in SYNTHETIC_IDS[:100]]
        second = [is_sampled(request_id, 20) for request_id in SYNTHETIC_IDS[:100]]
        assert first == second

    def test_zero_percent_samples_nothing(self):
        assert not any(is_sampled(request_id, 0) for request_id in SYNTHETIC_IDS)

    def test_one_hundred_percent_samples_everything(self):
        assert all(is_sampled(request_id, 100) for request_id in SYNTHETIC_IDS)

    def test_the_rate_over_a_thousand_ids_is_close_to_the_configured_percent(self):
        sampled = sum(1 for request_id in SYNTHETIC_IDS if is_sampled(request_id, 20))
        assert 150 <= sampled <= 250

    def test_a_higher_percent_never_samples_fewer_ids(self):
        low = {rid for rid in SYNTHETIC_IDS if is_sampled(rid, 20)}
        high = {rid for rid in SYNTHETIC_IDS if is_sampled(rid, 50)}
        assert low <= high


class TestValidateSamplePercent:
    @pytest.mark.parametrize("value", [0, 1, 20, 99, 100])
    def test_accepts_the_whole_inclusive_range(self, value):
        assert validate_sample_percent(value) == value

    @pytest.mark.parametrize("value", [-1, 101, 1000])
    def test_rejects_values_outside_zero_to_one_hundred(self, value):
        with pytest.raises(SamplingError):
            validate_sample_percent(value)

    @pytest.mark.parametrize("value", [True, 20.0, "20", None])
    def test_rejects_anything_that_is_not_an_integer(self, value):
        with pytest.raises(SamplingError):
            validate_sample_percent(value)
