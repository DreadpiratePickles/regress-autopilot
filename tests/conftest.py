"""Shared test helpers.

`make_row` lives here rather than in one test module so the ledger tests and the
summary tests build rows the same way. A summary total is only meaningful if the
rows it adds up are the rows the store actually writes.

`write_config` and `load_test_config` copy the *committed* `autopilot.toml` into a
temporary directory rather than inventing a config in code. Stage 04's tests
assert on the recommendations that file's thresholds produce, so a test fixture
that drifted from the real file would be testing a configuration nobody runs.
"""

from collections.abc import Sequence
from pathlib import Path

from cost_autopilot.config_file import AutopilotConfig, load_config
from cost_autopilot.ledger.row import STATUS_OK, LedgerRow, text_sha256
from cost_autopilot.validate.verdicts import VerdictRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_CONFIG = REPO_ROOT / "autopilot.toml"

DEFAULT_ROW_FIELDS = {
    "request_id": "req-1",
    "ts_utc": "2026-09-02T10:00:00Z",
    "team_id": "demo",
    "tier": "T1_TRIVIAL",
    "complexity_score": 4,
    "reasons": ("very short request: 5 words (+0)",),
    "chosen_model_id": "model-cheap",
    "fallback_chain": ("model-cheap",),
    "input_tokens": 100,
    "output_tokens": 50,
    "cost_micro_usd": 155,
    "counterfactual_top_model_cost_micro_usd": 800,
    "latency_ms": 120,
    "status": STATUS_OK,
    "error_type": None,
    "request_sha256": text_sha256("hello"),
}


def make_row(**overrides) -> LedgerRow:
    """A valid ledger row, with any field overridden."""
    return LedgerRow(**{**DEFAULT_ROW_FIELDS, **overrides})


DEFAULT_VERDICT_FIELDS = {
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


def make_verdict(**overrides) -> VerdictRecord:
    """A valid verdict record, with any field overridden."""
    return VerdictRecord(**{**DEFAULT_VERDICT_FIELDS, **overrides})


def make_verdicts(
    *, count: int, regret_count: int, tier: str = "T1_TRIVIAL", rung_index: int = 0
) -> list[VerdictRecord]:
    """`count` verdicts of one tier, the first `regret_count` of them regretful."""
    return [
        make_verdict(
            request_id=f"{tier}-{rung_index}-{index}",
            tier=tier,
            rung_index=rung_index,
            pairwise_regret=index < regret_count,
            pairwise_forward=index >= regret_count,
            pairwise_reverse=index < regret_count,
        )
        for index in range(count)
    ]


def write_config(directory: Path, substitutions: Sequence[tuple[str, str]] = ()) -> Path:
    """Copy the committed `autopilot.toml` into `directory`, applying substitutions.

    Every substitution is asserted to have matched, so a test cannot silently
    keep testing the default after the committed file's wording moves.
    """
    text = COMMITTED_CONFIG.read_text(encoding="utf-8")
    for old, new in substitutions:
        if old not in text:
            raise AssertionError(f"substitution target not present in autopilot.toml: {old!r}")
        text = text.replace(old, new)
    path = directory / "autopilot.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load_test_config(
    directory: Path, substitutions: Sequence[tuple[str, str]] = ()
) -> AutopilotConfig:
    """The committed configuration, rooted in a throwaway directory."""
    return load_config(write_config(directory, substitutions))
