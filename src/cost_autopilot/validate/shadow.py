"""The shadow sample: the only place this system stores a request and its answer.

Stage 03 cannot judge an answer it does not have, and it cannot ask "would a
better model have done better?" without the question that was asked. So the
sampled requests are written here, in full, and the design consequence is stated
rather than hidden:

  - **This file holds customer text.** The ledger deliberately does not, and that
    stays true. `shadow/` is gitignored, written `0o600`, and the whole mechanism
    is opt-in through `[validate] enabled`. Turning it off costs the quality
    measurement and nothing else.
  - **Only a sample is stored**, decided by `sampling.is_sampled`, so the volume
    of retained text is a configured percentage rather than everything.
  - **Only cheap-routed answers are stored.** A request that already went to the
    top rung has nothing to compare against — the reference answer would come
    from the same rung — so it is never sampled.

The file format and the append discipline are the ledger's, for the same reason:
one encoded line, one `O_APPEND` write, nothing ever updated.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..classify.scorer import Tier
from ..ledger.store import LedgerError, validate_month_key

SHADOW_FILE_SUFFIX = ".jsonl"
MONTH_KEY_LENGTH = len("YYYY-MM")

SHADOW_SCHEMA_VERSION = 1
"""Bumped when a field changes meaning. A reader refuses a version it does not
understand rather than judging answers it may be misreading."""


class ShadowError(Exception):
    """A shadow record is invalid, or its file could not be read or written."""


def _non_empty(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowError(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _non_negative(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShadowError(f"{field_name} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class ShadowRecord:
    """One cheap-routed request kept for validation, with everything stage 03 needs."""

    request_id: str
    ts_utc: str
    team_id: str
    tier: str
    complexity_score: int
    chosen_model_id: str
    rung_index: int
    request_text: str
    answer_text: str
    criteria: tuple[str, ...]
    """The workload's plain-English pass criteria, empty when the request came
    from `--text` rather than a workload file. Copied onto the record rather than
    looked up later: a workload file edited between routing and validating would
    otherwise change the standard an already-recorded answer is judged against."""

    input_tokens: int
    output_tokens: int
    schema_version: int = field(default=SHADOW_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        for name in ("request_id", "ts_utc", "team_id", "chosen_model_id"):
            _non_empty(getattr(self, name), field_name=name)
        _non_empty(self.request_text, field_name="request_text")
        _non_empty(self.answer_text, field_name="answer_text")
        try:
            Tier(self.tier)
        except ValueError as exc:
            raise ShadowError(f"tier must be a known Tier value, got {self.tier!r}") from exc
        for name in ("complexity_score", "rung_index", "input_tokens", "output_tokens"):
            _non_negative(getattr(self, name), field_name=name)
        if not isinstance(self.criteria, tuple):
            raise ShadowError("criteria must be a tuple of strings")
        for item in self.criteria:
            _non_empty(item, field_name="criterion")

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["criteria"] = list(self.criteria)
        return payload


def record_from_json_dict(payload: object) -> ShadowRecord:
    """Rebuild a record read back from disk, validating it at the boundary.

    Raises:
        ShadowError: not an object, unknown or missing fields, an unreadable
            schema version, or a value the record itself rejects.
    """
    if not isinstance(payload, dict):
        raise ShadowError(f"shadow record must be a JSON object, got {type(payload).__name__}")

    known = set(ShadowRecord.__dataclass_fields__)
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ShadowError(f"shadow record has unknown field(s): {', '.join(unknown)}")

    version = payload.get("schema_version")
    if version != SHADOW_SCHEMA_VERSION:
        raise ShadowError(
            f"shadow record has schema_version {version!r}; this build understands "
            f"{SHADOW_SCHEMA_VERSION}"
        )

    missing = sorted((known - {"schema_version"}) - set(payload))
    if missing:
        raise ShadowError(f"shadow record is missing field(s): {', '.join(missing)}")

    criteria = payload["criteria"]
    if not isinstance(criteria, list):
        raise ShadowError("criteria must be a list of strings")

    return ShadowRecord(**{**payload, "criteria": tuple(criteria)})


class ShadowStore:
    """Append-only access to `<dir>/<YYYY-MM>.jsonl`, filed by UTC month."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for_month(self, month: str) -> Path:
        return self.directory / f"{validate_month_key(month)}{SHADOW_FILE_SUFFIX}"

    def append(self, record: ShadowRecord, *, month: str | None = None) -> Path:
        """Append one record, filed under the month in its own timestamp."""
        path = self.path_for_month(month or record.ts_utc[:MONTH_KEY_LENGTH])
        line = (
            json.dumps(record.to_json_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise ShadowError(f"could not open the shadow file {path}: {exc}") from exc
        try:
            written = os.write(descriptor, line)
            if written != len(line):
                raise ShadowError(
                    f"short write to {path}: {written} of {len(line)} bytes. "
                    "The record may be truncated; do not validate from this file."
                )
        finally:
            os.close(descriptor)
        return path

    def read_month(self, month: str) -> list[ShadowRecord]:
        """Every record for one month, validated. A missing file is an empty list."""
        return list(self.iter_month(month))

    def iter_month(self, month: str) -> Iterator[ShadowRecord]:
        path = self.path_for_month(month)
        if not path.exists():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ShadowError(f"could not read the shadow file {path}: {exc}") from exc

        for number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ShadowError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
            yield record_from_json_dict(payload)

    def months(self) -> list[str]:
        """Every month the shadow directory holds a file for, oldest first."""
        if not self.directory.is_dir():
            return []
        found = []
        for path in self.directory.glob(f"*{SHADOW_FILE_SUFFIX}"):
            try:
                found.append(validate_month_key(path.stem))
            except LedgerError:
                continue
        return sorted(found)
