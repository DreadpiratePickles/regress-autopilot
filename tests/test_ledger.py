"""Ledger rows and the append-only store: schema, atomicity, month rollover."""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from conftest import make_row
from cost_autopilot.ledger.row import (
    SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_REFUSED,
    LedgerRowError,
    row_from_json_dict,
)
from cost_autopilot.ledger.store import (
    LedgerError,
    LedgerStore,
    month_key,
    utc_timestamp,
    validate_month_key,
)
from cost_autopilot.money import MoneyError


class TestRowValidation:
    def test_a_valid_row_builds(self):
        assert make_row().status == STATUS_OK

    def test_currency_is_always_usd(self):
        assert make_row().currency == "USD"

    def test_rejects_an_unknown_status(self):
        with pytest.raises(LedgerRowError, match="status"):
            make_row(status="maybe")

    def test_rejects_an_unknown_tier(self):
        with pytest.raises(LedgerRowError, match="tier"):
            make_row(tier="T9_WILD")

    def test_rejects_a_negative_cost(self):
        with pytest.raises(MoneyError):
            make_row(cost_micro_usd=-1)

    def test_rejects_a_float_cost(self):
        with pytest.raises(MoneyError):
            make_row(cost_micro_usd=1.5)

    def test_an_ok_row_must_name_its_model(self):
        with pytest.raises(LedgerRowError, match="must name the model"):
            make_row(chosen_model_id=None)

    def test_a_refused_row_must_cost_nothing(self):
        with pytest.raises(LedgerRowError, match="must cost nothing"):
            make_row(
                status=STATUS_REFUSED,
                chosen_model_id=None,
                error_type="BudgetExceededError",
                cost_micro_usd=5,
            )

    def test_a_failed_row_must_name_its_error(self):
        with pytest.raises(LedgerRowError, match="error_type"):
            make_row(status=STATUS_FAILED, chosen_model_id=None, error_type=None)

    def test_is_frozen(self):
        row = make_row()
        with pytest.raises(FrozenInstanceError):
            row.cost_micro_usd = 0  # type: ignore[misc]


class TestRoundTrip:
    def test_a_row_survives_a_json_round_trip(self):
        row = make_row()
        restored = row_from_json_dict(json.loads(json.dumps(row.to_json_dict())))
        assert restored == row

    def test_tuples_become_lists_in_json(self):
        payload = make_row().to_json_dict()
        assert isinstance(payload["reasons"], list)
        assert isinstance(payload["fallback_chain"], list)

    def test_an_unknown_field_is_rejected(self):
        payload = make_row().to_json_dict()
        payload["surprise"] = 1
        with pytest.raises(LedgerRowError, match="unknown field"):
            row_from_json_dict(payload)

    def test_a_missing_field_is_rejected(self):
        payload = make_row().to_json_dict()
        del payload["cost_micro_usd"]
        with pytest.raises(LedgerRowError, match="missing field"):
            row_from_json_dict(payload)

    def test_a_different_schema_version_is_refused(self):
        payload = make_row().to_json_dict()
        payload["schema_version"] = SCHEMA_VERSION + 1
        with pytest.raises(LedgerRowError, match="schema_version"):
            row_from_json_dict(payload)

    def test_a_non_object_is_rejected(self):
        with pytest.raises(LedgerRowError, match="JSON object"):
            row_from_json_dict(["not", "an", "object"])


class TestStoreAppend:
    def test_append_creates_the_file_and_the_directory(self, tmp_path):
        store = LedgerStore(tmp_path / "nested" / "ledger")
        path = store.append(make_row())
        assert path.exists()
        assert path.name == "2026-09.jsonl"

    def test_each_append_adds_exactly_one_line(self, tmp_path):
        store = LedgerStore(tmp_path)
        for index in range(5):
            store.append(make_row(request_id=f"req-{index}"))
        text = (tmp_path / "2026-09.jsonl").read_text(encoding="utf-8")
        assert len(text.splitlines()) == 5

    def test_every_line_is_independently_valid_json(self, tmp_path):
        store = LedgerStore(tmp_path)
        for index in range(3):
            store.append(make_row(request_id=f"req-{index}"))
        for line in (tmp_path / "2026-09.jsonl").read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["schema_version"] == SCHEMA_VERSION

    def test_appending_never_rewrites_an_earlier_row(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row(request_id="first"))
        before = (tmp_path / "2026-09.jsonl").read_text(encoding="utf-8")
        store.append(make_row(request_id="second"))
        after = (tmp_path / "2026-09.jsonl").read_text(encoding="utf-8")
        assert after.startswith(before)

    def test_a_row_is_filed_under_the_month_in_its_own_timestamp(self, tmp_path):
        store = LedgerStore(tmp_path)
        path = store.append(make_row(ts_utc="2027-01-15T00:00:00Z"))
        assert path.name == "2027-01.jsonl"

    def test_month_rollover_writes_two_files(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row(request_id="a", ts_utc="2026-09-30T23:59:59Z"))
        store.append(make_row(request_id="b", ts_utc="2026-10-01T00:00:00Z"))
        assert store.months() == ["2026-09", "2026-10"]
        assert len(store.read_month("2026-09")) == 1
        assert len(store.read_month("2026-10")) == 1


class TestStoreRead:
    def test_a_month_with_no_file_reads_as_empty(self, tmp_path):
        assert LedgerStore(tmp_path).read_month("2026-09") == []

    def test_blank_lines_are_skipped(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row())
        (tmp_path / "2026-09.jsonl").write_text(
            json.dumps(make_row().to_json_dict()) + "\n\n\n", encoding="utf-8"
        )
        assert len(store.read_month("2026-09")) == 1

    def test_a_corrupt_line_is_an_error_not_a_silent_skip(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row())
        with (tmp_path / "2026-09.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        with pytest.raises(LedgerError, match="not valid JSON"):
            store.read_month("2026-09")

    def test_an_invalid_row_is_an_error(self, tmp_path):
        (tmp_path / "2026-09.jsonl").write_text('{"request_id": "x"}\n', encoding="utf-8")
        with pytest.raises(LedgerError, match="not a valid ledger row"):
            LedgerStore(tmp_path).read_month("2026-09")

    def test_months_ignores_files_that_are_not_month_keys(self, tmp_path):
        (tmp_path / "notes.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "2026-09.jsonl").write_text("", encoding="utf-8")
        assert LedgerStore(tmp_path).months() == ["2026-09"]

    def test_months_on_a_missing_directory_is_empty(self, tmp_path):
        assert LedgerStore(tmp_path / "absent").months() == []


class TestSpend:
    def test_spend_sums_only_the_named_team(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row(request_id="a", team_id="demo", cost_micro_usd=100))
        store.append(make_row(request_id="b", team_id="demo", cost_micro_usd=250))
        store.append(make_row(request_id="c", team_id="other", cost_micro_usd=9999))
        assert store.spend_micro_usd(team_id="demo", month="2026-09") == 350

    def test_spend_is_zero_for_a_month_with_no_rows(self, tmp_path):
        assert LedgerStore(tmp_path).spend_micro_usd(team_id="demo", month="2026-09") == 0

    def test_spend_is_an_int(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(make_row())
        assert isinstance(store.spend_micro_usd(team_id="demo", month="2026-09"), int)

    def test_a_refusal_adds_nothing_to_spend(self, tmp_path):
        store = LedgerStore(tmp_path)
        store.append(
            make_row(
                status=STATUS_REFUSED,
                chosen_model_id=None,
                error_type="BudgetExceededError",
                cost_micro_usd=0,
                fallback_chain=(),
            )
        )
        assert store.spend_micro_usd(team_id="demo", month="2026-09") == 0


class TestTimeHelpers:
    def test_timestamps_are_utc_with_a_trailing_z(self):
        stamp = utc_timestamp(datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC))
        assert stamp == "2026-09-02T10:00:00Z"

    def test_month_key_is_derived_in_utc(self):
        assert month_key(datetime(2026, 12, 31, 23, 30, tzinfo=UTC)) == "2026-12"

    def test_validate_month_key_accepts_a_real_month(self):
        assert validate_month_key("2026-09") == "2026-09"

    @pytest.mark.parametrize("bad", ["2026", "2026-13", "sept", "2026-09-02", 202609])
    def test_validate_month_key_rejects_anything_else(self, bad):
        with pytest.raises(LedgerError):
            validate_month_key(bad)
