"""The shape of one ledger row, and its validation in both directions.

A ledger row is the only durable record this system produces. Everything a later
stage can ever say about a routing decision — was it cheap, was it right, did it
fall back, was it refused — has to be reconstructible from these fields, because
nothing else is kept.

Two rules shape the schema:

  - **The request text is not in it.** `log_text` defaults to false and only a
    SHA-256 of the text is stored. A ledger is read by finance and operations
    people, gets copied into spreadsheets, and lives for years; customer text
    does not belong in it. The hash still lets a later stage prove that two rows
    refer to the same request.
  - **Every row carries its own counterfactual.** `counterfactual_top_model_
    cost_micro_usd` is what these token counts would have cost on the top rung.
    Storing it per row rather than recomputing it later means a saving stays
    checkable even after the ladder's prices change, which they will.

`status` is a closed set. A row is `ok` (a model answered), `refused` (the
budget said no, and no model was called) or `failed` (every rung was tried and
none answered). Those are genuinely different facts and none of them is allowed
to decay into another.
"""

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from ..classify.scorer import Tier
from ..money import CURRENCY, validate_micro_usd

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"
STATUSES = frozenset({STATUS_OK, STATUS_REFUSED, STATUS_FAILED})

SCHEMA_VERSION = 1
"""Bumped when a field changes meaning. `ledger summary` refuses a version it
does not understand rather than adding up columns that may not be comparable."""


class LedgerRowError(ValueError):
    """A ledger row is missing a field, or holds a value the schema forbids."""


def text_sha256(text: str) -> str:
    """Stable identity for a request without storing the request."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerRow:
    """One routed request. Frozen: the ledger is append-only, never edited."""

    request_id: str
    ts_utc: str
    team_id: str
    tier: str
    complexity_score: int
    reasons: tuple[str, ...]
    chosen_model_id: str | None
    fallback_chain: tuple[str, ...]
    """Every model id tried, in order. One entry on a clean first-choice call;
    more when a rung failed transiently and the router stepped up. Empty on a
    refusal, because nothing was called."""

    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    counterfactual_top_model_cost_micro_usd: int
    latency_ms: int
    status: str
    error_type: str | None
    request_sha256: str
    schema_version: int = field(default=SCHEMA_VERSION)
    currency: str = field(default=CURRENCY)
    request_text: str | None = field(default=None)
    """Populated only when `log_text` is enabled in `autopilot.toml`. Off by
    default; see the module docstring."""

    def __post_init__(self) -> None:
        for name in ("request_id", "ts_utc", "team_id", "request_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise LedgerRowError(f"{name} must be a non-empty string")
        if self.status not in STATUSES:
            raise LedgerRowError(
                f"status must be one of {sorted(STATUSES)}, got {self.status!r}"
            )
        try:
            Tier(self.tier)
        except ValueError as exc:
            raise LedgerRowError(f"tier must be a known Tier value, got {self.tier!r}") from exc
        for name in ("input_tokens", "output_tokens", "latency_ms", "complexity_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LedgerRowError(f"{name} must be a non-negative integer, got {value!r}")
        validate_micro_usd(self.cost_micro_usd, field="cost_micro_usd")
        validate_micro_usd(
            self.counterfactual_top_model_cost_micro_usd,
            field="counterfactual_top_model_cost_micro_usd",
        )
        if self.currency != CURRENCY:
            raise LedgerRowError(f"currency must be {CURRENCY}, got {self.currency!r}")
        if self.status == STATUS_OK and not self.chosen_model_id:
            raise LedgerRowError("an 'ok' row must name the model that answered")
        if self.status == STATUS_REFUSED and self.cost_micro_usd != 0:
            raise LedgerRowError("a 'refused' row must cost nothing: no model was called")
        if self.status in (STATUS_REFUSED, STATUS_FAILED) and not self.error_type:
            raise LedgerRowError(f"a {self.status!r} row must name its error_type")

    def to_json_dict(self) -> dict[str, Any]:
        """Plain JSON types only: tuples become lists."""
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["fallback_chain"] = list(self.fallback_chain)
        return payload


def row_from_json_dict(payload: object) -> LedgerRow:
    """Rebuild a row read back from disk, validating it at the boundary.

    A ledger file is an external input like any other: it may have been edited,
    truncated, or written by an older version of this code.

    Raises:
        LedgerRowError: the payload is not an object, carries unknown or missing
            keys, or fails the row's own validation.
    """
    if not isinstance(payload, dict):
        raise LedgerRowError(f"ledger row must be a JSON object, got {type(payload).__name__}")

    known = set(LedgerRow.__dataclass_fields__)
    unknown = sorted(set(payload) - known)
    if unknown:
        raise LedgerRowError(f"ledger row has unknown field(s): {', '.join(unknown)}")

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise LedgerRowError(
            f"ledger row has schema_version {version!r}; this build understands "
            f"{SCHEMA_VERSION}. Refusing to total columns that may not be comparable."
        )

    required = known - {"schema_version", "currency", "request_text"}
    missing = sorted(required - set(payload))
    if missing:
        raise LedgerRowError(f"ledger row is missing field(s): {', '.join(missing)}")

    for name in ("reasons", "fallback_chain"):
        value = payload[name]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LedgerRowError(f"{name} must be a list of strings")

    return LedgerRow(
        **{
            **payload,
            "reasons": tuple(payload["reasons"]),
            "fallback_chain": tuple(payload["fallback_chain"]),
        }
    )
