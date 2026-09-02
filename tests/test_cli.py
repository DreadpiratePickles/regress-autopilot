"""The command line, exercised in-process. No test here touches the network."""

import json
from pathlib import Path

import pytest

from cost_autopilot.cli import EXIT_BAD_CONFIG, EXIT_OK, EXIT_PARTIAL_FAILURE, main
from cost_autopilot.ledger.store import month_key
from cost_autopilot.report.model import EXIT_COULD_NOT_RUN, EXIT_INCONCLUSIVE

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = REPO_ROOT / "workloads" / "mixed_v1.jsonl"


@pytest.fixture
def workspace(tmp_path):
    """A throwaway copy of the real config, pointed at a throwaway ledger."""
    config = (REPO_ROOT / "autopilot.toml").read_text(encoding="utf-8")
    (tmp_path / "autopilot.toml").write_text(config, encoding="utf-8")
    return tmp_path


class Recorder:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        self.lines.append(str(line))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def run(workspace, *argv) -> tuple[int, Recorder]:
    echo = Recorder()
    code = main(["--config", str(workspace / "autopilot.toml"), *argv], echo=echo)
    return code, echo


def ledger_rows(workspace) -> list[dict]:
    files = sorted((workspace / "ledger").glob("*.jsonl"))
    return [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestClassify:
    def test_prints_a_tier_and_reasons(self, workspace):
        code, echo = run(workspace, "classify", "--text", "What is the capital of Peru?")
        assert code == EXIT_OK
        assert "T1_TRIVIAL" in echo.text
        assert "reasons:" in echo.text

    def test_calls_no_model_and_writes_no_ledger(self, workspace):
        run(workspace, "classify", "--text", "Debug this ```x``` now.")
        assert not (workspace / "ledger").exists()

    def test_a_complex_request_classifies_as_complex(self, workspace):
        text = (
            "Debug and analyze this, comparing the trade-offs step by step.\n"
            "```python\nprint(x)\n```\n"
            'Traceback (most recent call last):\n  File "a.py", line 3\nKeyError: "x"'
        )
        _, echo = run(workspace, "classify", "--text", text)
        assert "T3_COMPLEX" in echo.text

    def test_blank_text_is_a_usage_error(self, workspace):
        code, echo = run(workspace, "classify", "--text", "   ")
        assert code == EXIT_BAD_CONFIG
        assert "error:" in echo.text


class TestRouteSingle:
    def test_dry_run_routes_and_writes_one_row(self, workspace):
        code, echo = run(
            workspace, "route", "--team", "demo", "--text", "What is 2 + 2?", "--dry-run"
        )
        assert code == EXIT_OK
        assert "Dry run" in echo.text
        rows = ledger_rows(workspace)
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["team_id"] == "demo"

    def test_the_row_records_a_cost_and_a_counterfactual(self, workspace):
        run(workspace, "route", "--team", "demo", "--text", "What is 2 + 2?", "--dry-run")
        row = ledger_rows(workspace)[0]
        assert isinstance(row["cost_micro_usd"], int)
        assert row["counterfactual_top_model_cost_micro_usd"] >= row["cost_micro_usd"]
        assert row["currency"] == "USD"

    def test_the_request_text_is_not_written_to_the_ledger(self, workspace):
        secret = "What is 2 + 2? my-secret-phrase"
        run(workspace, "route", "--team", "demo", "--text", secret, "--dry-run")
        ledger_file = next(iter((workspace / "ledger").glob("*.jsonl")))
        assert "my-secret-phrase" not in ledger_file.read_text(encoding="utf-8")
        assert ledger_rows(workspace)[0]["request_text"] is None

    def test_reading_the_request_from_a_file(self, workspace):
        source = workspace / "request.txt"
        source.write_text("Translate 'bonjour' to English.", encoding="utf-8")
        code, _ = run(
            workspace, "route", "--team", "demo", "--file", str(source), "--dry-run"
        )
        assert code == EXIT_OK
        assert len(ledger_rows(workspace)) == 1

    def test_a_missing_file_is_a_setup_error(self, workspace):
        code, echo = run(
            workspace, "route", "--team", "demo", "--file", str(workspace / "no.txt"), "--dry-run"
        )
        assert code == EXIT_BAD_CONFIG
        assert "error:" in echo.text

    def test_a_zero_budget_refuses_and_still_records_a_row(self, workspace):
        config = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        config = config.replace("demo = 5000000", "demo = 0")
        (workspace / "autopilot.toml").write_text(config, encoding="utf-8")

        code, _ = run(workspace, "route", "--team", "demo", "--text", "Hi there", "--dry-run")
        assert code == EXIT_PARTIAL_FAILURE
        row = ledger_rows(workspace)[0]
        assert row["status"] == "refused"
        assert row["cost_micro_usd"] == 0
        assert row["error_type"] == "BudgetExceededError"


class TestRouteWorkload:
    def test_a_dry_run_of_the_demo_workload_writes_one_row_per_request(self, workspace):
        code, echo = run(
            workspace,
            "route",
            "--team",
            "demo",
            "--workload",
            str(WORKLOAD),
            "--dry-run",
            "--min-interval-ms",
            "0",
        )
        assert code == EXIT_OK
        rows = ledger_rows(workspace)
        assert len(rows) == 30
        assert all(row["status"] == "ok" for row in rows)
        assert "30 ledger row(s): 30 ok" in echo.text

    def test_the_dry_run_spans_more_than_one_rung(self, workspace):
        run(
            workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
            "--dry-run", "--min-interval-ms", "0",
        )
        models = {row["chosen_model_id"] for row in ledger_rows(workspace)}
        assert len(models) > 1, "a mixed workload should not all land on one rung"

    def test_every_row_carries_the_reasons_that_chose_its_rung(self, workspace):
        run(
            workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
            "--dry-run", "--min-interval-ms", "0",
        )
        assert all(row["reasons"] for row in ledger_rows(workspace))

    def test_a_malformed_workload_is_rejected_before_anything_is_written(self, workspace):
        bad = workspace / "bad.jsonl"
        bad.write_text('{"id": "a", "text": "x"}\n{not json\n', encoding="utf-8")
        code, echo = run(
            workspace, "route", "--team", "demo", "--workload", str(bad), "--dry-run"
        )
        assert code == EXIT_BAD_CONFIG
        assert not (workspace / "ledger").exists(), "nothing may be written after a bad load"

    def test_a_negative_interval_is_rejected(self, workspace):
        code, _ = run(
            workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
            "--dry-run", "--min-interval-ms", "-5",
        )
        assert code == EXIT_BAD_CONFIG


class TestLedgerSummary:
    def test_an_empty_ledger_summarises_to_nothing(self, workspace):
        code, echo = run(workspace, "ledger", "summary")
        assert code == EXIT_OK
        assert "No requests recorded" in echo.text

    def test_summary_totals_the_dry_run(self, workspace):
        run(
            workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
            "--dry-run", "--min-interval-ms", "0",
        )
        code, echo = run(workspace, "ledger", "summary")
        assert code == EXIT_OK
        assert "requests           30" in echo.text
        assert "saving" in echo.text
        assert "spend by team" in echo.text

    def test_the_summary_spend_equals_the_sum_of_the_rows(self, workspace):
        run(
            workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
            "--dry-run", "--min-interval-ms", "0",
        )
        expected = sum(row["cost_micro_usd"] for row in ledger_rows(workspace))
        _, echo = run(workspace, "ledger", "summary")
        dollars, micros = divmod(expected, 1_000_000)
        assert f"${dollars}.{micros:06d}" in echo.text

    def test_a_bad_month_is_rejected(self, workspace):
        code, echo = run(workspace, "ledger", "summary", "--month", "sept")
        assert code == EXIT_BAD_CONFIG
        assert "YYYY-MM" in echo.text

    def test_a_month_with_no_rows_is_not_an_error(self, workspace):
        code, echo = run(workspace, "ledger", "summary", "--month", "1999-01")
        assert code == EXIT_OK
        assert "No requests recorded" in echo.text


class TestConfigErrors:
    def test_a_missing_config_is_exit_two(self, tmp_path):
        echo = Recorder()
        code = main(["--config", str(tmp_path / "absent.toml"), "classify", "--text", "x"], echo)
        assert code == EXIT_BAD_CONFIG
        assert "not found" in echo.text

    def test_an_invalid_config_is_exit_two(self, tmp_path):
        (tmp_path / "autopilot.toml").write_text("[ladder\n", encoding="utf-8")
        code, echo = run(tmp_path, "classify", "--text", "x")
        assert code == EXIT_BAD_CONFIG

    def test_unverified_prices_warn_on_every_route(self, workspace):
        config = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        config = config.replace("prices_verified = true", "prices_verified = false")
        (workspace / "autopilot.toml").write_text(config, encoding="utf-8")
        _, echo = run(workspace, "route", "--team", "demo", "--text", "Hi", "--dry-run")
        assert "prices_verified is false" in echo.text


class TestDryRunIsolation:
    def test_dry_run_never_imports_the_vendor_sdk_path(self, workspace, monkeypatch):
        """A dry run must not be able to reach a provider even with no key set."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        code, _ = run(workspace, "route", "--team", "demo", "--text", "Hi", "--dry-run")
        assert code == EXIT_OK

    def test_a_real_run_without_a_key_fails_before_writing_a_row(self, workspace, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        # The name has to be patched where it is *used*: `gemini_metered` did
        # `from dotenv import load_dotenv`, so patching `dotenv.load_dotenv`
        # rebinds a name nothing reads, and a real .env in the repository root
        # would then supply the key this test is asserting the absence of.
        monkeypatch.setattr(
            "cost_autopilot.providers.gemini_metered.load_dotenv", lambda *a, **k: False
        )
        code, echo = run(workspace, "route", "--team", "demo", "--text", "Hi")
        assert code == EXIT_BAD_CONFIG
        assert "GEMINI_API_KEY" in echo.text
        assert "error:" in echo.text


def set_config(workspace, old, new):
    path = workspace / "autopilot.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def route_everything(workspace):
    """Route the demo workload with every cheap answer shadow-sampled."""
    set_config(workspace, "sample_percent = 20", "sample_percent = 100")
    return run(
        workspace, "route", "--team", "demo", "--workload", str(WORKLOAD),
        "--dry-run", "--min-interval-ms", "0",
    )


def shadow_records(workspace) -> list[dict]:
    files = sorted((workspace / "shadow").glob("*.jsonl"))
    return [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def verdict_records(workspace) -> list[dict]:
    files = sorted((workspace / "validate").glob("*/verdicts.jsonl"))
    return [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestShadowSamplingThroughTheCli:
    def test_routing_writes_a_shadow_record_for_every_cheap_answer(self, workspace):
        route_everything(workspace)
        rows = ledger_rows(workspace)
        top = {row["chosen_model_id"] for row in rows} - {
            record["chosen_model_id"] for record in shadow_records(workspace)
        }
        assert len(shadow_records(workspace)) == sum(
            1 for row in rows if row["shadow_sampled"]
        )
        assert top, "the top rung must never be shadow-sampled"

    def test_the_shadow_file_holds_the_text_and_the_ledger_does_not(self, workspace):
        route_everything(workspace)
        ledger_text = next(iter((workspace / "ledger").glob("*.jsonl"))).read_text("utf-8")
        shadow_text = next(iter((workspace / "shadow").glob("*.jsonl"))).read_text("utf-8")
        assert "capital of Peru" in shadow_text
        assert "capital of Peru" not in ledger_text

    def test_the_criteria_travel_from_the_workload_onto_the_record(self, workspace):
        route_everything(workspace)
        assert any(record["criteria"] for record in shadow_records(workspace))

    def test_disabling_validation_writes_no_shadow_file_at_all(self, workspace):
        set_config(workspace, "enabled = true", "enabled = false")
        run(workspace, "route", "--team", "demo", "--text", "What is 2 + 2?", "--dry-run")
        assert not (workspace / "shadow").exists()
        assert ledger_rows(workspace)[0]["shadow_sampled"] is False


class TestValidateCommand:
    def test_a_dry_run_validates_every_shadow_record(self, workspace):
        route_everything(workspace)
        code, echo = run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        assert code == EXIT_OK
        assert "Dry run" in echo.text
        assert len(verdict_records(workspace)) == len(shadow_records(workspace))

    def test_it_writes_a_regret_file_carrying_the_sampling_rule(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        payload = json.loads(
            (workspace / "validate" / month_key() / "regret.json").read_text(encoding="utf-8")
        )
        assert payload["sample_percent"] == 100
        assert payload["cheap_routed_count"] == len(shadow_records(workspace))
        assert isinstance(payload["validation_cost_micro_usd"], int)

    def test_re_running_adds_no_duplicates(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        first = len(verdict_records(workspace))
        code, echo = run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        assert code == EXIT_OK
        assert len(verdict_records(workspace)) == first
        assert "0 newly validated" in echo.text

    def test_limit_validates_only_the_first_n_records(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0", "--limit", "3")
        assert len(verdict_records(workspace)) == 3

    def test_limit_then_a_full_run_completes_the_rest_without_duplicating(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0", "--limit", "3")
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        ids = [record["request_id"] for record in verdict_records(workspace)]
        assert len(ids) == len(set(ids)) == len(shadow_records(workspace))

    def test_a_month_with_no_shadow_records_is_not_an_error(self, workspace):
        code, echo = run(workspace, "validate", "--dry-run", "--month", "1999-01")
        assert code == EXIT_OK
        assert "No shadow records" in echo.text

    def test_a_bad_month_is_rejected(self, workspace):
        code, echo = run(workspace, "validate", "--dry-run", "--month", "sept")
        assert code == EXIT_BAD_CONFIG
        assert "YYYY-MM" in echo.text

    def test_a_negative_limit_is_rejected(self, workspace):
        route_everything(workspace)
        code, _ = run(workspace, "validate", "--dry-run", "--limit", "-1")
        assert code == EXIT_BAD_CONFIG


class TestSummaryQualitySection:
    def test_the_section_is_absent_until_a_month_has_been_validated(self, workspace):
        route_everything(workspace)
        _, echo = run(workspace, "ledger", "summary")
        assert "Quality (from shadow validation)" not in echo.text

    def test_the_section_appears_once_a_regret_file_exists(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        code, echo = run(workspace, "ledger", "summary")
        assert code == EXIT_OK
        assert "Quality (from shadow validation)" in echo.text
        assert "validation cost" in echo.text

    def test_the_section_gives_a_verdict_line_per_tier(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        _, echo = run(workspace, "ledger", "summary")
        verdicts = (
            "safe:",
            "insufficient evidence:",
            "no regret observed",
            "regret too high —",
        )
        tiers = [line for line in echo.lines if "T1_TRIVIAL" in line or "T2_STANDARD" in line]
        assert tiers, "the quality section must break regret down by tier"
        assert sum(echo.text.count(phrase) for phrase in verdicts) >= 1

    def test_the_summary_never_rewrites_configuration(self, workspace):
        before = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        run(workspace, "ledger", "summary")
        after = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        assert after == before.replace("sample_percent = 20", "sample_percent = 100")


class TestReportCommand:
    """Stage 04 through the command line. Nothing here calls a model."""

    def test_a_month_with_no_data_is_inconclusive_and_still_writes_the_artifacts(
        self, workspace
    ):
        code, echo = run(workspace, "report", "--month", "2026-09")
        assert code == EXIT_INCONCLUSIVE
        assert "Verdict: INCONCLUSIVE" in echo.text
        directory = workspace / "report" / "2026-09"
        for name in ("report.md", "report.json", "proposal.md", "proposal.json"):
            assert (directory / name).is_file(), name

    def test_it_reports_the_month_a_dry_run_produced(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        code, echo = run(workspace, "report", "--dry-run")
        assert code in (EXIT_OK, EXIT_INCONCLUSIVE)
        assert "Dry run" in echo.text
        assert "spend" in echo.text
        assert "saving" in echo.text

    def test_a_dry_run_marks_every_artifact_synthetic_on_its_first_line(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        run(workspace, "report", "--dry-run")
        directory = workspace / "report" / month_key()
        for name in ("report.md", "proposal.md"):
            first = (directory / name).read_text(encoding="utf-8").splitlines()[0]
            assert "SYNTHETIC" in first, name
            assert "not a model judgement" in first, name

    def test_a_real_run_does_not_claim_to_be_synthetic(self, workspace):
        route_everything(workspace)
        run(workspace, "report")
        text = (workspace / "report" / month_key() / "report.md").read_text(encoding="utf-8")
        assert "SYNTHETIC" not in text

    def test_out_overrides_the_configured_directory(self, workspace):
        run(workspace, "report", "--month", "2026-09", "--out", str(workspace / "elsewhere"))
        assert (workspace / "elsewhere" / "2026-09" / "report.md").is_file()
        assert not (workspace / "report").exists()

    def test_the_report_never_writes_to_the_configuration(self, workspace):
        route_everything(workspace)
        before = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        run(workspace, "report")
        assert (workspace / "autopilot.toml").read_text(encoding="utf-8") == before

    def test_the_json_and_the_markdown_agree_on_the_verdict(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        run(workspace, "report")
        directory = workspace / "report" / month_key()
        payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        text = (directory / "report.md").read_text(encoding="utf-8")
        assert f"## Verdict: {payload['verdict']}" in text

    def test_a_bad_month_could_not_run_rather_than_inconclusive(self, workspace):
        code, echo = run(workspace, "report", "--month", "not-a-month")
        assert code == EXIT_COULD_NOT_RUN
        assert "error:" in echo.text

    def test_a_missing_config_could_not_run(self, tmp_path):
        echo = Recorder()
        code = main(
            ["--config", str(tmp_path / "absent.toml"), "report", "--month", "2026-09"],
            echo=echo,
        )
        assert code == EXIT_COULD_NOT_RUN

    def test_the_regret_figures_match_what_ledger_summary_printed(self, workspace):
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        _, summary = run(workspace, "ledger", "summary")
        run(workspace, "report")
        text = (workspace / "report" / month_key() / "report.md").read_text(encoding="utf-8")
        for line in summary.lines:
            stripped = line.strip()
            if stripped.startswith(("safe:", "insufficient evidence:", "no regret observed")):
                assert stripped in text


class TestApplyProposalCommand:
    def _proposal(self, workspace):
        """A month whose evidence really does recommend a change.

        Routing samples everything so there is something to validate, then the
        rate is put back to the committed 20% before the report runs — which is
        what makes the sampling rule fire with a value different from the one in
        the file, and so gives `apply-proposal` a real diff to refuse or apply.
        """
        route_everything(workspace)
        run(workspace, "validate", "--dry-run", "--min-interval-ms", "0")
        set_config(workspace, "sample_percent = 100", "sample_percent = 20")
        run(workspace, "report")
        path = workspace / "report" / month_key() / "proposal.json"
        assert json.loads(path.read_text(encoding="utf-8"))["changes"], (
            "this fixture must produce a non-empty proposal"
        )
        return path

    def test_approve_is_required(self, workspace):
        path = self._proposal(workspace)
        before = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        code, echo = run(
            workspace, "apply-proposal", "--file", str(path), "--approved-by", "Bobby"
        )
        assert code == EXIT_BAD_CONFIG
        assert "without --approve" in echo.text
        assert (workspace / "autopilot.toml").read_text(encoding="utf-8") == before

    def test_an_approver_is_required_by_the_parser(self, workspace):
        path = self._proposal(workspace)
        with pytest.raises(SystemExit):
            run(workspace, "apply-proposal", "--file", str(path), "--approve")

    def test_approving_changes_the_file_and_records_who(self, workspace):
        path = self._proposal(workspace)
        code, echo = run(
            workspace,
            "apply-proposal",
            "--file",
            str(path),
            "--approve",
            "--approved-by",
            "Bobby Meher",
        )
        assert code == EXIT_OK
        assert "Applied" in echo.text
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["status"] == "applied"
        assert after["approved_by"] == "Bobby Meher"
        config = (workspace / "autopilot.toml").read_text(encoding="utf-8")
        for change in after["changes"]:
            assert f"{change['key']} = {change['proposed_value']}" in config

    def test_applying_twice_is_refused(self, workspace):
        path = self._proposal(workspace)
        args = ("apply-proposal", "--file", str(path), "--approve", "--approved-by", "B")
        assert run(workspace, *args)[0] == EXIT_OK
        code, echo = run(workspace, *args)
        assert code == EXIT_BAD_CONFIG
        assert "already applied" in echo.text

    def test_a_missing_proposal_file_is_a_named_refusal(self, workspace):
        code, echo = run(
            workspace,
            "apply-proposal",
            "--file",
            str(workspace / "nope.json"),
            "--approve",
            "--approved-by",
            "B",
        )
        assert code == EXIT_BAD_CONFIG
        assert "not found" in echo.text
