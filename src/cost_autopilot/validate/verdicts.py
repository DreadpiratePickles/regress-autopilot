"""What stage 03 concluded about one sampled request, and where those lines live.

One record per validated request, not one per judged criterion. The unit that
matters is "was routing *this request* to a cheaper model a mistake", and that is
a single fact assembled from several judgements: the per-criterion comparisons,
the two pairwise orderings, and whatever failed to produce a verdict at all.

Three states, kept apart everywhere:

  - `True` — the cheap answer was insufficient. This is regret.
  - `False` — the cheap answer was good enough.
  - `None` — no readable judgement. Not a pass. A record with no verdict is
    excluded from the denominator and counted as a judge error, because counting
    it as "no regret" would let a broken judge quietly improve the numbers.

The costs of validating are on the record too. Validation is not free, and a tool
that reported a saving while hiding the price of proving it would be making the
same mistake it exists to correct.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..classify.scorer import Tier
from ..ledger.store import validate_month_key
from ..money import CURRENCY, validate_micro_usd

VERDICTS_FILENAME = "verdicts.jsonl"
DONE_FILENAME = "done.jsonl"
REGRET_FILENAME = "regret.json"

VERDICT_SCHEMA_VERSION = 1


class VerdictError(Exception):
    """A verdict record is invalid, or its file could not be read or written."""


def _optional_bool(value: object, *, field_name: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise VerdictError(f"{field_name} must be true, false or null, got {value!r}")
    return value


@dataclass(frozen=True)
class CriterionVerdict:
    """One criterion, judged on the cheap answer and on the reference answer.

    Both sides are recorded even when they agree. "Both models missed it" is a
    fact about the criterion, not about the routing, and only having both makes
    that distinguishable from "the cheap model missed it".
    """

    criterion: str
    cheap_passed: bool | None
    reference_passed: bool | None
    cheap_reason: str | None = None
    reference_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.criterion, str) or not self.criterion.strip():
            raise VerdictError("criterion must be a non-empty string")
        _optional_bool(self.cheap_passed, field_name="cheap_passed")
        _optional_bool(self.reference_passed, field_name="reference_passed")

    @property
    def is_regret(self) -> bool:
        """The cheap answer failed something the reference answer delivered."""
        return self.cheap_passed is False and self.reference_passed is True

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerdictRecord:
    """Everything stage 03 concluded about one sampled request."""

    request_id: str
    ts_utc: str
    team_id: str
    tier: str
    rung_index: int
    chosen_model_id: str
    reference_model_id: str
    judge_model_id: str
    criteria_regret: bool | None
    criterion_verdicts: tuple[CriterionVerdict, ...]
    pairwise_forward: bool | None
    pairwise_reverse: bool | None
    pairwise_regret: bool | None
    position_bias_detected: bool
    judge_errors: tuple[str, ...]
    reference_cost_micro_usd: int
    judge_cost_micro_usd: int
    reference_latency_ms: int
    judge_latency_ms: int
    currency: str = field(default=CURRENCY)
    schema_version: int = field(default=VERDICT_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        for name in ("request_id", "ts_utc", "team_id", "chosen_model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise VerdictError(f"{name} must be a non-empty string")
        try:
            Tier(self.tier)
        except ValueError as exc:
            raise VerdictError(f"tier must be a known Tier value, got {self.tier!r}") from exc
        for name in ("criteria_regret", "pairwise_regret", "pairwise_forward", "pairwise_reverse"):
            _optional_bool(getattr(self, name), field_name=name)
        if not isinstance(self.position_bias_detected, bool):
            raise VerdictError("position_bias_detected must be a boolean")
        for name in ("reference_cost_micro_usd", "judge_cost_micro_usd"):
            validate_micro_usd(getattr(self, name), field=name)
        for name in ("rung_index", "reference_latency_ms", "judge_latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VerdictError(f"{name} must be a non-negative integer, got {value!r}")
        if self.currency != CURRENCY:
            raise VerdictError(f"currency must be {CURRENCY}, got {self.currency!r}")

    @property
    def regret(self) -> bool | None:
        """The one fact stage 04 counts. `None` when nothing readable was produced."""
        signals = [
            signal
            for signal in (self.criteria_regret, self.pairwise_regret)
            if signal is not None
        ]
        if not signals:
            return None
        return any(signals)

    @property
    def validation_cost_micro_usd(self) -> int:
        return self.reference_cost_micro_usd + self.judge_cost_micro_usd

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["criterion_verdicts"] = [item.to_json_dict() for item in self.criterion_verdicts]
        payload["judge_errors"] = list(self.judge_errors)
        payload["regret"] = self.regret
        return payload


def verdict_from_json_dict(payload: object) -> VerdictRecord:
    """Rebuild a verdict read back from disk, validating it at the boundary.

    Raises:
        VerdictError: not an object, unknown or missing fields, or a schema
            version this build does not understand.
    """
    if not isinstance(payload, dict):
        raise VerdictError(f"verdict must be a JSON object, got {type(payload).__name__}")

    known = set(VerdictRecord.__dataclass_fields__)
    unknown = sorted(set(payload) - known - {"regret"})
    if unknown:
        raise VerdictError(f"verdict has unknown field(s): {', '.join(unknown)}")

    version = payload.get("schema_version")
    if version != VERDICT_SCHEMA_VERSION:
        raise VerdictError(
            f"verdict has schema_version {version!r}; this build understands "
            f"{VERDICT_SCHEMA_VERSION}"
        )

    missing = sorted((known - {"schema_version", "currency"}) - set(payload))
    if missing:
        raise VerdictError(f"verdict is missing field(s): {', '.join(missing)}")

    criteria = payload["criterion_verdicts"]
    if not isinstance(criteria, list):
        raise VerdictError("criterion_verdicts must be a list")
    errors = payload["judge_errors"]
    if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
        raise VerdictError("judge_errors must be a list of strings")

    fields = {key: value for key, value in payload.items() if key != "regret"}
    return VerdictRecord(
        **{
            **fields,
            "criterion_verdicts": tuple(CriterionVerdict(**item) for item in criteria),
            "judge_errors": tuple(errors),
        }
    )


class VerdictStore:
    """The three files one validated month produces, under `<dir>/<YYYY-MM>/`.

    `done.jsonl` is what makes a re-run idempotent. It is written *after* the
    verdict line, so a crash between the two re-validates one request rather than
    losing it — a duplicated judgement costs one extra call, a lost one silently
    shrinks the sample.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def month_directory(self, month: str) -> Path:
        return self.directory / validate_month_key(month)

    def verdicts_path(self, month: str) -> Path:
        return self.month_directory(month) / VERDICTS_FILENAME

    def done_path(self, month: str) -> Path:
        return self.month_directory(month) / DONE_FILENAME

    def regret_path(self, month: str) -> Path:
        return self.month_directory(month) / REGRET_FILENAME

    def append(self, record: VerdictRecord, *, month: str) -> Path:
        payload = json.dumps(record.to_json_dict(), ensure_ascii=False, sort_keys=True)
        return self._append_line(self.verdicts_path(month), payload)

    def mark_done(self, request_id: str, *, month: str) -> Path:
        payload = json.dumps({"request_id": request_id}, sort_keys=True)
        return self._append_line(self.done_path(month), payload)

    def done_ids(self, month: str) -> frozenset[str]:
        """Request ids already validated for this month. Empty when nothing has run."""
        path = self.done_path(month)
        if not path.exists():
            return frozenset()
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VerdictError(f"could not read {path}: {exc}") from exc

        seen: set[str] = set()
        for number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerdictError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            if not isinstance(request_id, str) or not request_id.strip():
                raise VerdictError(f"{path}:{number} does not name a request_id")
            seen.add(request_id)
        return frozenset(seen)

    def read_month(self, month: str) -> list[VerdictRecord]:
        return list(self.iter_month(month))

    def iter_month(self, month: str) -> Iterator[VerdictRecord]:
        path = self.verdicts_path(month)
        if not path.exists():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VerdictError(f"could not read {path}: {exc}") from exc
        for number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VerdictError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
            yield verdict_from_json_dict(payload)

    def _append_line(self, path: Path, payload: str) -> Path:
        line = (payload + "\n").encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise VerdictError(f"could not open {path}: {exc}") from exc
        try:
            written = os.write(descriptor, line)
            if written != len(line):
                raise VerdictError(
                    f"short write to {path}: {written} of {len(line)} bytes. "
                    "The line may be truncated; do not treat this file as complete."
                )
        finally:
            os.close(descriptor)
        return path
