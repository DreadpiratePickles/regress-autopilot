"""The regret file, and the quality section `ledger summary` prints from it."""

import json

import pytest
from regression_detect.compare import wilson_interval

from cost_autopilot.validate.regret import RegretReport, build_report, report_from_json_dict
from cost_autopilot.validate.report import (
    QUALITY_HEADING,
    load_report,
    render_quality,
    write_report,
)
from cost_autopilot.validate.verdicts import VerdictRecord

BASE = {
    "request_id": "req-1",
    "ts_utc": "2026-09-02T11:00:00Z",
    "team_id": "demo",
    "tier": "T1_TRIVIAL",
    "rung_index": 0,
    "chosen_model_id": "model-cheap",
    "reference_model_id": "model-top",
    "judge_model_id": "model-cheap",
    "criteria_regret": None,
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


def verdicts(*, count, regret_count, tier="T1_TRIVIAL", rung_index=0, start=0):
    made = []
    for index in range(count):
        regretful = index < regret_count
        made.append(
            VerdictRecord(
                **{
                    **BASE,
                    "request_id": f"{tier}-{rung_index}-{start + index}",
                    "tier": tier,
                    "rung_index": rung_index,
                    "pairwise_regret": regretful,
                    "pairwise_forward": not regretful,
                    "pairwise_reverse": regretful,
                }
            )
        )
    return made


def report_for(items, **overrides) -> RegretReport:
    return build_report(
        items,
        **{
            "month": "2026-09",
            "sample_percent": 20,
            "cheap_routed_count": 23,
            "shadow_record_count": len(items),
            "generated_utc": "2026-09-02T11:00:00Z",
            **overrides,
        },
    )


class TestReportSerialisation:
    def test_a_report_round_trips_through_json(self, tmp_path):
        report = report_for(verdicts(count=4, regret_count=1))
        assert report_from_json_dict(report.to_json_dict()) == report

    def test_write_then_load_returns_the_same_report(self, tmp_path):
        report = report_for(verdicts(count=4, regret_count=1))
        path = write_report(tmp_path / "2026-09" / "regret.json", report)
        assert load_report(path) == report

    def test_loading_an_absent_report_is_none_not_an_error(self, tmp_path):
        assert load_report(tmp_path / "missing.json") is None

    def test_money_stays_an_integer_in_the_written_file(self, tmp_path):
        report = report_for(verdicts(count=4, regret_count=1))
        path = write_report(tmp_path / "regret.json", report)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload["validation_cost_micro_usd"], int)

    def test_a_corrupt_report_file_is_an_error(self, tmp_path):
        path = tmp_path / "regret.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_report(path)


class TestQualityRendering:
    def test_the_section_is_headed_so_a_reader_knows_where_it_came_from(self):
        text = render_quality(
            report_for(verdicts(count=4, regret_count=1)), max_regret=0.10, min_samples=10
        )
        assert QUALITY_HEADING in text
        assert QUALITY_HEADING == "Quality (from shadow validation)"

    def test_it_says_what_fraction_of_cheap_routed_traffic_was_inspected(self):
        text = render_quality(
            report_for(verdicts(count=4, regret_count=1)), max_regret=0.10, min_samples=10
        )
        assert "sample_percent     20" in text
        assert "cheap-routed       23" in text

    def test_the_validation_overhead_cost_is_reported(self):
        text = render_quality(
            report_for(verdicts(count=4, regret_count=1)), max_regret=0.10, min_samples=10
        )
        assert "validation cost" in text
        assert "$0.004440" in text  # 4 * (800 + 310) micro-USD

    def test_an_empty_report_says_so_rather_than_printing_zeroes(self):
        text = render_quality(report_for([]), max_regret=0.10, min_samples=10)
        assert "No validated samples" in text


class TestTierVerdictLines:
    """The three sentences a human is meant to act on, spelled out exactly."""

    def test_a_tight_interval_below_the_threshold_reads_as_safe(self):
        report = report_for(verdicts(count=200, regret_count=0))
        low, high = wilson_interval(0, 200)
        text = render_quality(report, max_regret=0.10, min_samples=10)
        assert f"safe: the 95% upper bound {high:.3f} is below max_regret 0.100" in text
        assert low == 0.0

    def test_too_few_samples_reads_as_insufficient_evidence(self):
        report = report_for(verdicts(count=4, regret_count=1))
        text = render_quality(report, max_regret=0.10, min_samples=10)
        assert (
            "insufficient evidence: 4 validated sample(s), fewer than min_samples 10" in text
        )

    def test_enough_samples_with_too_much_regret_names_the_action(self):
        report = report_for(verdicts(count=20, regret_count=10))
        _, high = wilson_interval(10, 20)
        text = render_quality(report, max_regret=0.10, min_samples=10)
        assert (
            "regret too high — consider routing this tier up "
            f"(95% upper bound {high:.3f}, max_regret 0.100)" in text
        )

    def test_the_verdict_is_decided_per_tier_not_overall(self):
        items = verdicts(count=200, regret_count=0) + verdicts(
            count=20, regret_count=10, tier="T3_COMPLEX", rung_index=1
        )
        text = render_quality(report_for(items), max_regret=0.10, min_samples=10)
        assert "safe: the 95% upper bound" in text
        assert "regret too high — consider routing this tier up" in text

    def test_nothing_in_the_rendering_changes_configuration(self):
        report = report_for(verdicts(count=20, regret_count=10))
        text = render_quality(report, max_regret=0.10, min_samples=10)
        assert "consider" in text
        assert "autopilot.toml has been updated" not in text


class TestReportBoundaries:
    def test_a_non_object_is_refused(self):
        with pytest.raises(ValueError):
            report_from_json_dict(["not", "an", "object"])

    def test_an_unknown_top_level_field_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["surprise"] = 1
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_missing_top_level_field_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        del payload["overall"]
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_an_unknown_schema_version_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["schema_version"] = 77
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_breakdown_that_is_not_a_table_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["by_tier"] = []
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_malformed_group_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["overall"] = {"label": "overall"}
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_group_with_an_unknown_field_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["overall"]["surprise"] = 1
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_group_that_is_not_an_object_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["overall"] = "overall"
        with pytest.raises(ValueError):
            report_from_json_dict(payload)

    def test_a_negative_cost_is_refused(self):
        payload = report_for(verdicts(count=2, regret_count=0)).to_json_dict()
        payload["validation_cost_micro_usd"] = -1
        with pytest.raises(ValueError):
            report_from_json_dict(payload)
