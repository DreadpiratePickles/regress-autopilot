"""Reading and appending the ledger files, one per calendar month (UTC).

The ledger is a directory of JSONL files named `YYYY-MM.jsonl`. Splitting by
month is not a filing convenience: budgets are monthly, so "what has this team
spent this month" has to be answerable by reading exactly one file, and the file
a budget check reads must be the file the resulting row is appended to.

Appends are atomic in the sense that matters here. Each row is serialised to a
single line, encoded once, and written with one `os.write` call to a file opened
`O_APPEND`. POSIX guarantees an `O_APPEND` write smaller than `PIPE_BUF` will
not interleave with another process's, so two routers sharing a ledger directory
produce interleaved *rows*, never interleaved bytes within a row. That is enough
for an append-only log; it is deliberately not enough for a queue, and the
rulebook's point that a JSON file is not a transactional database still stands —
what makes this safe is that nothing is ever updated, only added.

A month boundary is UTC. A local-time boundary would put two different days'
work in one budget period depending on where the caller happens to be sitting.
"""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .row import LedgerRow, LedgerRowError, row_from_json_dict

LEDGER_FILE_SUFFIX = ".jsonl"
MONTH_FORMAT = "%Y-%m"
MONTH_KEY_LENGTH = len("YYYY-MM")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LedgerError(Exception):
    """The ledger directory or one of its files could not be read or written."""


def now_utc() -> datetime:
    """The clock, behind one function so tests can hand in their own instant."""
    return datetime.now(UTC)


def utc_timestamp(moment: datetime | None = None) -> str:
    """An ISO-8601 UTC timestamp, second resolution, always with a trailing Z."""
    return (moment or now_utc()).astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def month_key(moment: datetime | None = None) -> str:
    """The `YYYY-MM` a moment belongs to, in UTC."""
    return (moment or now_utc()).astimezone(UTC).strftime(MONTH_FORMAT)


def validate_month_key(value: str) -> str:
    """Check a `--month` argument at the boundary.

    Raises:
        LedgerError: the value is not a `YYYY-MM` calendar month.
    """
    if not isinstance(value, str):
        raise LedgerError(f"month must be a string like 2026-09, got {type(value).__name__}")
    try:
        datetime.strptime(value, MONTH_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise LedgerError(f"month must look like YYYY-MM, got {value!r}") from exc
    return value


class LedgerStore:
    """Append-only access to `<dir>/<YYYY-MM>.jsonl`."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for_month(self, month: str) -> Path:
        return self.directory / f"{validate_month_key(month)}{LEDGER_FILE_SUFFIX}"

    def append(self, row: LedgerRow, *, month: str | None = None) -> Path:
        """Append one row and return the file it landed in.

        The month is taken from the row's own `ts_utc` unless overridden, so a
        row can never be filed under a month other than the one it happened in.
        """
        target_month = month or row.ts_utc[:MONTH_KEY_LENGTH]
        path = self.path_for_month(target_month)
        line = (json.dumps(row.to_json_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise LedgerError(f"could not open the ledger file {path}: {exc}") from exc
        try:
            written = os.write(descriptor, line)
            if written != len(line):
                raise LedgerError(
                    f"short write to {path}: {written} of {len(line)} bytes. "
                    "The row may be truncated; do not treat this ledger as complete."
                )
        finally:
            os.close(descriptor)
        return path

    def months(self) -> list[str]:
        """Every month the ledger holds a file for, oldest first."""
        if not self.directory.is_dir():
            return []
        names = (
            path.stem
            for path in self.directory.glob(f"*{LEDGER_FILE_SUFFIX}")
            if path.is_file()
        )
        return sorted(name for name in names if _is_month_key(name))

    def read_month(self, month: str) -> list[LedgerRow]:
        """Every row for one month, validated.

        A month with no file is an empty list — that is a real answer, not a
        failure. A file that exists but holds a bad line *is* a failure: a
        summary that silently skipped rows would under-report spend.

        Raises:
            LedgerError: the file cannot be read, or a line is not a valid row.
        """
        return list(self.iter_month(month))

    def iter_month(self, month: str) -> Iterator[LedgerRow]:
        path = self.path_for_month(month)
        if not path.exists():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError(f"could not read the ledger file {path}: {exc}") from exc

        for number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc
            try:
                yield row_from_json_dict(payload)
            except LedgerRowError as exc:
                raise LedgerError(f"{path}:{number} is not a valid ledger row: {exc}") from exc

    def spend_micro_usd(self, *, team_id: str, month: str) -> int:
        """What one team has already spent in one month, in integer micro-USD.

        This is the number the budget check reads, so it counts only rows that
        actually cost money. A refusal costs nothing by construction, and a
        failed row carries whatever the failed attempts consumed.
        """
        return sum(row.cost_micro_usd for row in self.iter_month(month) if row.team_id == team_id)


def _is_month_key(name: str) -> bool:
    try:
        validate_month_key(name)
    except LedgerError:
        return False
    return True
