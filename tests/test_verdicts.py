"""Verdict records and the files they live in, including the idempotency ledger."""

import pytest

from cost_autopilot.money import MoneyError
from cost_autopilot.validate.verdicts import (
    CriterionVerdict,
    VerdictError,
    VerdictRecord,
    VerdictStore,
    verdict_from_json_dict,
)

FIELDS = {
    "request_id": "req-1",
    "ts_utc": "2026-09-02T11:00:00Z",
    "team_id": "demo",
    "tier": "T1_TRIVIAL",
    "rung_index": 0,
    "chosen_model_id": "model-cheap",
    "reference_model_id": "model-top",
    "judge_model_id": "model-cheap",
    "criteria_regret": False,
    "criterion_verdicts": (),
    "pairwise_forward": True,
    "pairwise_reverse": False,
    "pairwise_regret": False,
    "position_bias_detected": False,
    "judge_errors": (),
    "reference_cost_micro_usd": 800,
    "judge_cost_micro_usd": 310,
    "reference_latency_ms": 900,
    "judge_latency_ms": 40,
}


def make_verdict(**overrides) -> VerdictRecord:
    return VerdictRecord(**{**FIELDS, **overrides})


class TestRegretProperty:
    def test_either_signal_being_true_is_regret(self):
        assert make_verdict(criteria_regret=True).regret is True
        assert make_verdict(pairwise_regret=True).regret is True

    def test_both_signals_false_is_no_regret(self):
        assert make_verdict().regret is False

    def test_no_usable_signal_is_no_verdict_at_all(self):
        assert make_verdict(criteria_regret=None, pairwise_regret=None).regret is None

    def test_one_missing_signal_does_not_erase_the_other(self):
        assert make_verdict(criteria_regret=None, pairwise_regret=True).regret is True
        assert make_verdict(criteria_regret=None, pairwise_regret=False).regret is False


class TestValidation:
    def test_a_record_round_trips_through_json(self):
        record = make_verdict(
            criterion_verdicts=(
                CriterionVerdict(
                    criterion="States that the capital is Lima.",
                    cheap_passed=True,
                    reference_passed=True,
                    cheap_reason="it does",
                    reference_reason="it does",
                ),
            ),
            judge_errors=("pairwise forward: JudgeParseError",),
        )
        assert verdict_from_json_dict(record.to_json_dict()) == record

    def test_an_unknown_tier_is_refused(self):
        with pytest.raises(VerdictError):
            make_verdict(tier="T9_NONSENSE")

    def test_a_negative_cost_is_refused(self):
        with pytest.raises(MoneyError):
            make_verdict(judge_cost_micro_usd=-1)

    def test_an_unknown_schema_version_is_refused(self):
        payload = make_verdict().to_json_dict()
        payload["schema_version"] = 42
        with pytest.raises(VerdictError):
            verdict_from_json_dict(payload)

    def test_an_unknown_field_is_refused(self):
        payload = make_verdict().to_json_dict()
        payload["surprise"] = True
        with pytest.raises(VerdictError):
            verdict_from_json_dict(payload)


class TestVerdictStore:
    def test_files_land_under_the_month_directory(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        month = tmp_path / "validate" / "2026-09"
        assert store.verdicts_path("2026-09") == month / "verdicts.jsonl"
        assert store.done_path("2026-09") == month / "done.jsonl"
        assert store.regret_path("2026-09") == month / "regret.json"

    def test_append_then_read_returns_the_record(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.append(make_verdict(), month="2026-09")
        assert [item.request_id for item in store.read_month("2026-09")] == ["req-1"]

    def test_done_ids_start_empty_and_grow(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        assert store.done_ids("2026-09") == frozenset()
        store.mark_done("req-1", month="2026-09")
        store.mark_done("req-2", month="2026-09")
        assert store.done_ids("2026-09") == frozenset({"req-1", "req-2"})

    def test_marking_the_same_id_twice_does_not_duplicate_the_set(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.mark_done("req-1", month="2026-09")
        store.mark_done("req-1", month="2026-09")
        assert store.done_ids("2026-09") == frozenset({"req-1"})

    def test_a_corrupt_verdict_line_is_an_error_not_a_skipped_record(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.append(make_verdict(), month="2026-09")
        store.verdicts_path("2026-09").write_text("{oops\n", encoding="utf-8")
        with pytest.raises(VerdictError):
            store.read_month("2026-09")


class TestFieldValidation:
    @pytest.mark.parametrize("field", ["criteria_regret", "pairwise_regret", "pairwise_forward"])
    def test_a_three_state_field_must_be_true_false_or_null(self, field):
        with pytest.raises(VerdictError):
            make_verdict(**{field: "maybe"})

    def test_position_bias_must_be_a_boolean(self):
        with pytest.raises(VerdictError):
            make_verdict(position_bias_detected=None)

    def test_a_blank_request_id_is_refused(self):
        with pytest.raises(VerdictError):
            make_verdict(request_id="  ")

    def test_a_negative_latency_is_refused(self):
        with pytest.raises(VerdictError):
            make_verdict(judge_latency_ms=-1)

    def test_a_currency_other_than_usd_is_refused(self):
        with pytest.raises(VerdictError):
            make_verdict(currency="GBP")

    def test_a_blank_criterion_is_refused(self):
        with pytest.raises(VerdictError):
            CriterionVerdict(criterion="  ", cheap_passed=True, reference_passed=True)

    def test_a_criterion_verdict_is_regret_only_when_the_reference_delivered(self):
        lost = CriterionVerdict(criterion="c", cheap_passed=False, reference_passed=True)
        shared = CriterionVerdict(criterion="c", cheap_passed=False, reference_passed=False)
        unknown = CriterionVerdict(criterion="c", cheap_passed=None, reference_passed=True)
        assert lost.is_regret is True
        assert shared.is_regret is False
        assert unknown.is_regret is False

    def test_the_validation_cost_is_the_two_legs_added_up(self):
        assert make_verdict().validation_cost_micro_usd == 800 + 310


class TestVerdictFromJson:
    def test_a_non_object_is_refused(self):
        with pytest.raises(VerdictError):
            verdict_from_json_dict("not an object")

    def test_a_missing_field_is_refused(self):
        payload = make_verdict().to_json_dict()
        del payload["pairwise_regret"]
        with pytest.raises(VerdictError):
            verdict_from_json_dict(payload)

    def test_criterion_verdicts_must_be_a_list(self):
        payload = make_verdict().to_json_dict()
        payload["criterion_verdicts"] = {}
        with pytest.raises(VerdictError):
            verdict_from_json_dict(payload)

    def test_judge_errors_must_be_a_list_of_strings(self):
        payload = make_verdict().to_json_dict()
        payload["judge_errors"] = [1]
        with pytest.raises(VerdictError):
            verdict_from_json_dict(payload)

    def test_the_derived_regret_field_is_written_but_not_read_back_as_input(self):
        payload = make_verdict(pairwise_regret=True).to_json_dict()
        assert payload["regret"] is True
        assert verdict_from_json_dict(payload).regret is True


class TestDoneFile:
    def test_a_corrupt_done_line_is_an_error(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.mark_done("req-1", month="2026-09")
        store.done_path("2026-09").write_text("{oops\n", encoding="utf-8")
        with pytest.raises(VerdictError):
            store.done_ids("2026-09")

    def test_a_done_line_that_names_no_request_is_an_error(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.mark_done("req-1", month="2026-09")
        store.done_path("2026-09").write_text('{"nothing": 1}\n', encoding="utf-8")
        with pytest.raises(VerdictError):
            store.done_ids("2026-09")

    def test_blank_lines_are_skipped_in_both_files(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.append(make_verdict(), month="2026-09")
        store.mark_done("req-1", month="2026-09")
        for path in (store.verdicts_path("2026-09"), store.done_path("2026-09")):
            path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        assert len(store.read_month("2026-09")) == 1
        assert store.done_ids("2026-09") == frozenset({"req-1"})

    def test_a_directory_where_the_file_should_be_is_an_error(self, tmp_path):
        store = VerdictStore(tmp_path / "validate")
        store.verdicts_path("2026-09").mkdir(parents=True)
        with pytest.raises(VerdictError):
            store.append(make_verdict(), month="2026-09")

    def test_reading_a_month_that_was_never_validated_is_empty(self, tmp_path):
        assert VerdictStore(tmp_path / "validate").read_month("2026-09") == []
