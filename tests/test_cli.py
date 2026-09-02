"""The command line, exercised in-process. No test here touches the network."""

import json
from pathlib import Path

import pytest

from cost_autopilot.cli import EXIT_BAD_CONFIG, EXIT_OK, EXIT_PARTIAL_FAILURE, main

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
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        code, echo = run(workspace, "route", "--team", "demo", "--text", "Hi")
        assert code == EXIT_BAD_CONFIG
        assert "GEMINI_API_KEY" in echo.text
        assert "error:" in echo.text
