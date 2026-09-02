"""Load a workload file: one JSON request per line, validated before anything runs.

A workload is a batch of requests to route in one go. It is validated in full
*before* the first model call, so a typo on line 28 is found while nothing has
been spent rather than after twenty-seven paid calls.

`criteria` and `expected_tier` are carried but not used by Phase A. They are
what Phase B's validator will judge answers against, in the same style as
project 1's goldens: plain-English, checkable, with at least one negative. They
are validated here so the file cannot rot between phases unnoticed.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .classify.scorer import Tier

REQUIRED_KEYS = frozenset({"id", "text"})
OPTIONAL_KEYS = frozenset({"team_id", "criteria", "expected_tier"})
KNOWN_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
MIN_CRITERIA = 2
MAX_CRITERIA = 4


class WorkloadError(Exception):
    """The workload file is missing, unreadable, or holds an invalid request."""


@dataclass(frozen=True)
class WorkloadRequest:
    """One request from a workload file."""

    id: str
    text: str
    team_id: str | None = None
    criteria: tuple[str, ...] = ()
    expected_tier: Tier | None = None
    """What a human expects this request to classify as. Not an input to
    routing — the classifier must not be able to see the answer — but the
    measurement Phase B needs, and useful now for reporting agreement."""


def _string(payload: dict[str, Any], key: str, *, where: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise WorkloadError(f"{where}: '{key}' must be a non-empty string")
    return value


def _parse_request(payload: object, *, where: str) -> WorkloadRequest:
    if not isinstance(payload, dict):
        raise WorkloadError(
            f"{where}: each line must be a JSON object, got {type(payload).__name__}"
        )

    unknown = sorted(set(payload) - KNOWN_KEYS)
    if unknown:
        raise WorkloadError(f"{where}: unknown key(s): {', '.join(unknown)}")
    missing = sorted(REQUIRED_KEYS - set(payload))
    if missing:
        raise WorkloadError(f"{where}: missing key(s): {', '.join(missing)}")

    criteria = payload.get("criteria", [])
    if not isinstance(criteria, list) or not all(
        isinstance(item, str) and item.strip() for item in criteria
    ):
        raise WorkloadError(f"{where}: 'criteria' must be a list of non-empty strings")
    if criteria and not MIN_CRITERIA <= len(criteria) <= MAX_CRITERIA:
        raise WorkloadError(
            f"{where}: 'criteria' must hold between {MIN_CRITERIA} and {MAX_CRITERIA} "
            f"entries, got {len(criteria)}"
        )

    expected = payload.get("expected_tier")
    if expected is not None:
        try:
            expected = Tier(expected)
        except (ValueError, TypeError) as exc:
            known = ", ".join(tier.value for tier in Tier)
            raise WorkloadError(
                f"{where}: 'expected_tier' must be one of {known}, got {expected!r}"
            ) from exc

    team_id = payload.get("team_id")
    if team_id is not None and (not isinstance(team_id, str) or not team_id.strip()):
        raise WorkloadError(f"{where}: 'team_id' must be a non-empty string when present")

    return WorkloadRequest(
        id=_string(payload, "id", where=where),
        text=_string(payload, "text", where=where),
        team_id=team_id,
        criteria=tuple(criteria),
        expected_tier=expected,
    )


def load_workload(path: Path | str) -> list[WorkloadRequest]:
    """Read and validate a whole workload file.

    Raises:
        WorkloadError: the file is missing or unreadable, a line is not JSON, a
            request is invalid, an id repeats, or the file holds no requests.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkloadError(f"workload file not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkloadError(f"workload file could not be read: {path} ({exc})") from exc

    requests: list[WorkloadRequest] = []
    seen: set[str] = set()
    for number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkloadError(f"{where} is not valid JSON: {exc.msg}") from exc
        request = _parse_request(payload, where=where)
        if request.id in seen:
            raise WorkloadError(f"{where}: duplicate request id {request.id!r}")
        seen.add(request.id)
        requests.append(request)

    if not requests:
        raise WorkloadError(f"workload file holds no requests: {path}")
    return requests
