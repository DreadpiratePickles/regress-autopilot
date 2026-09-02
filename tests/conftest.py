"""Shared test helpers.

`make_row` lives here rather than in one test module so the ledger tests and the
summary tests build rows the same way. A summary total is only meaningful if the
rows it adds up are the rows the store actually writes.
"""

from cost_autopilot.ledger.row import STATUS_OK, LedgerRow, text_sha256

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
