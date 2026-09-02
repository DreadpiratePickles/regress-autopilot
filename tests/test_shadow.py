"""The shadow store: the only place a request's text and answer are persisted."""

import json

import pytest

from cost_autopilot.ledger.store import LedgerError
from cost_autopilot.validate.shadow import (
    ShadowError,
    ShadowRecord,
    ShadowStore,
    record_from_json_dict,
)

FIELDS = {
    "request_id": "req-1",
    "ts_utc": "2026-09-02T10:00:00Z",
    "team_id": "demo",
    "tier": "T1_TRIVIAL",
    "complexity_score": 4,
    "chosen_model_id": "model-cheap",
    "rung_index": 0,
    "request_text": "What is the capital of Peru?",
    "answer_text": "Lima.",
    "criteria": ("States that the capital of Peru is Lima.", "Is a single sentence."),
    "input_tokens": 100,
    "output_tokens": 50,
}


def make_record(**overrides) -> ShadowRecord:
    return ShadowRecord(**{**FIELDS, **overrides})


class TestRecordValidation:
    def test_a_valid_record_round_trips_through_json(self):
        record = make_record()
        assert record_from_json_dict(record.to_json_dict()) == record

    def test_criteria_become_a_list_in_json(self):
        assert isinstance(make_record().to_json_dict()["criteria"], list)

    def test_a_record_may_carry_no_criteria(self):
        assert make_record(criteria=()).criteria == ()

    @pytest.mark.parametrize("field", ["request_id", "ts_utc", "team_id", "chosen_model_id"])
    def test_blank_identity_fields_are_refused(self, field):
        with pytest.raises(ShadowError):
            make_record(**{field: "  "})

    def test_blank_request_text_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(request_text="   ")

    def test_blank_answer_text_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(answer_text="")

    def test_an_unknown_tier_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(tier="T4_IMPOSSIBLE")

    def test_a_negative_rung_index_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(rung_index=-1)

    def test_a_non_string_criterion_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(criteria=("fine", 7))


class TestRecordFromJson:
    def test_a_non_object_is_refused(self):
        with pytest.raises(ShadowError):
            record_from_json_dict(["not", "an", "object"])

    def test_an_unknown_field_is_refused(self):
        payload = make_record().to_json_dict()
        payload["surprise"] = 1
        with pytest.raises(ShadowError):
            record_from_json_dict(payload)

    def test_a_missing_field_is_refused(self):
        payload = make_record().to_json_dict()
        del payload["answer_text"]
        with pytest.raises(ShadowError):
            record_from_json_dict(payload)

    def test_an_unknown_schema_version_is_refused(self):
        payload = make_record().to_json_dict()
        payload["schema_version"] = 99
        with pytest.raises(ShadowError):
            record_from_json_dict(payload)


class TestShadowStore:
    def test_append_files_the_record_under_its_own_month(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        path = store.append(make_record())
        assert path.name == "2026-09.jsonl"
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    def test_appending_twice_keeps_both_records(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        store.append(make_record(request_id="req-1"))
        store.append(make_record(request_id="req-2"))
        assert [r.request_id for r in store.read_month("2026-09")] == ["req-1", "req-2"]

    def test_a_month_with_no_file_reads_as_empty(self, tmp_path):
        assert ShadowStore(tmp_path / "shadow").read_month("2026-09") == []

    def test_a_corrupt_line_is_an_error_not_a_skipped_record(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        store.append(make_record())
        store.path_for_month("2026-09").write_text("{not json\n", encoding="utf-8")
        with pytest.raises(ShadowError):
            store.read_month("2026-09")

    def test_a_bad_month_key_is_refused(self, tmp_path):
        with pytest.raises(LedgerError):
            ShadowStore(tmp_path / "shadow").path_for_month("2026-13-01")

    def test_the_written_line_is_json_carrying_the_request_text(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        path = store.append(make_record())
        payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert payload["request_text"] == FIELDS["request_text"]
        assert payload["answer_text"] == FIELDS["answer_text"]


class TestStoreEdges:
    def test_months_lists_only_real_month_files(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        store.append(make_record())
        store.append(make_record(ts_utc="2026-10-01T00:00:00Z"))
        (tmp_path / "shadow" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
        assert store.months() == ["2026-09", "2026-10"]

    def test_months_is_empty_when_nothing_has_been_written(self, tmp_path):
        assert ShadowStore(tmp_path / "shadow").months() == []

    def test_blank_lines_are_skipped_rather_than_failing_the_read(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        path = store.append(make_record())
        path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        assert len(store.read_month("2026-09")) == 1

    def test_a_directory_where_the_file_should_be_is_an_error(self, tmp_path):
        store = ShadowStore(tmp_path / "shadow")
        store.path_for_month("2026-09").mkdir(parents=True)
        with pytest.raises(ShadowError):
            store.append(make_record())

    def test_criteria_read_back_as_something_other_than_a_list_is_refused(self, tmp_path):
        payload = make_record().to_json_dict()
        payload["criteria"] = "one criterion"
        with pytest.raises(ShadowError):
            record_from_json_dict(payload)

    def test_criteria_built_as_a_list_rather_than_a_tuple_is_refused(self):
        with pytest.raises(ShadowError):
            make_record(criteria=["a", "b"])
