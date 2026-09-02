"""Workload loading, and the shipped `mixed_v1.jsonl` demo file."""

import json
from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from cost_autopilot.classify.scorer import Tier
from cost_autopilot.workload import WorkloadError, load_workload

WORKLOAD_PATH = "workloads/mixed_v1.jsonl"

NEGATIVE_PREFIXES = ("does not", "must not", "avoids", "never", "do not", "no ")


def write(tmp_path, *objects):
    path = tmp_path / "workload.jsonl"
    path.write_text(
        "\n".join(json.dumps(item) for item in objects) + "\n", encoding="utf-8"
    )
    return path


class TestLoading:
    def test_loads_a_minimal_request(self, tmp_path):
        path = write(tmp_path, {"id": "a", "text": "hello"})
        requests = load_workload(path)
        assert len(requests) == 1
        assert requests[0].id == "a"
        assert requests[0].team_id is None

    def test_loads_optional_fields(self, tmp_path):
        path = write(
            tmp_path,
            {
                "id": "a",
                "text": "hello",
                "team_id": "ops",
                "criteria": ["Says hello.", "Does not shout."],
                "expected_tier": "T1_TRIVIAL",
            },
        )
        request = load_workload(path)[0]
        assert request.team_id == "ops"
        assert request.expected_tier is Tier.T1_TRIVIAL
        assert len(request.criteria) == 2

    def test_blank_lines_are_skipped(self, tmp_path):
        path = tmp_path / "w.jsonl"
        path.write_text('{"id":"a","text":"x"}\n\n{"id":"b","text":"y"}\n', encoding="utf-8")
        assert len(load_workload(path)) == 2

    def test_requests_are_frozen(self, tmp_path):
        request = load_workload(write(tmp_path, {"id": "a", "text": "x"}))[0]
        with pytest.raises(FrozenInstanceError):
            request.id = "b"  # type: ignore[misc]


class TestValidation:
    def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="not found"):
            load_workload(tmp_path / "absent.jsonl")

    def test_an_empty_file_is_an_error(self, tmp_path):
        path = tmp_path / "w.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(WorkloadError, match="no requests"):
            load_workload(path)

    def test_a_bad_json_line_names_the_line_number(self, tmp_path):
        path = tmp_path / "w.jsonl"
        path.write_text('{"id":"a","text":"x"}\n{oops\n', encoding="utf-8")
        with pytest.raises(WorkloadError, match=":2"):
            load_workload(path)

    def test_a_missing_key_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="missing key"):
            load_workload(write(tmp_path, {"id": "a"}))

    def test_an_unknown_key_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="unknown key"):
            load_workload(write(tmp_path, {"id": "a", "text": "x", "surprise": 1}))

    def test_a_blank_text_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="text"):
            load_workload(write(tmp_path, {"id": "a", "text": "   "}))

    def test_a_duplicate_id_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="duplicate"):
            load_workload(write(tmp_path, {"id": "a", "text": "x"}, {"id": "a", "text": "y"}))

    def test_an_unknown_expected_tier_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="expected_tier"):
            load_workload(write(tmp_path, {"id": "a", "text": "x", "expected_tier": "T9"}))

    def test_too_many_criteria_is_an_error(self, tmp_path):
        payload = {"id": "a", "text": "x", "criteria": ["a.", "b.", "c.", "d.", "e."]}
        with pytest.raises(WorkloadError, match="criteria"):
            load_workload(write(tmp_path, payload))

    def test_one_criterion_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="criteria"):
            load_workload(write(tmp_path, {"id": "a", "text": "x", "criteria": ["only one."]}))

    def test_a_non_object_line_is_an_error(self, tmp_path):
        with pytest.raises(WorkloadError, match="JSON object"):
            load_workload(write(tmp_path, ["not", "an", "object"]))


class TestTheShippedWorkload:
    """The demo file is a committed artifact; these are its acceptance criteria."""

    def test_it_loads(self):
        assert len(load_workload(WORKLOAD_PATH)) == 30

    def test_it_spans_the_three_tiers_evenly(self):
        counts = Counter(
            request.expected_tier for request in load_workload(WORKLOAD_PATH)
        )
        assert counts == {Tier.T1_TRIVIAL: 10, Tier.T2_STANDARD: 10, Tier.T3_COMPLEX: 10}

    def test_every_request_carries_two_to_four_criteria(self):
        for request in load_workload(WORKLOAD_PATH):
            assert 2 <= len(request.criteria) <= 4, request.id

    def test_every_request_has_at_least_one_negative_criterion(self):
        # A set of only positive criteria is easy to satisfy by saying more.
        # At least one "must not" is what makes the set discriminating.
        for request in load_workload(WORKLOAD_PATH):
            assert any(
                criterion.lower().startswith(NEGATIVE_PREFIXES)
                for criterion in request.criteria
            ), request.id

    def test_ids_are_unique_and_snake_case(self):
        ids = [request.id for request in load_workload(WORKLOAD_PATH)]
        assert len(set(ids)) == len(ids)
        assert all(item == item.lower() and " " not in item for item in ids)

    def test_trivial_requests_are_short_and_complex_ones_are_not(self):
        requests = load_workload(WORKLOAD_PATH)
        trivial = [r for r in requests if r.expected_tier is Tier.T1_TRIVIAL]
        complex_ = [r for r in requests if r.expected_tier is Tier.T3_COMPLEX]
        assert max(len(r.text) for r in trivial) < min(len(r.text) for r in complex_)
