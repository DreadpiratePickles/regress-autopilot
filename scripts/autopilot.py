#!/usr/bin/env python
"""Command line entry point.

    uv run python scripts/autopilot.py classify --text "What is 2 + 2?"
    uv run python scripts/autopilot.py route --team demo --text "..." --dry-run
    uv run python scripts/autopilot.py route --team demo \
        --workload workloads/mixed_v1.jsonl --min-interval-ms 6500
    uv run python scripts/autopilot.py validate --min-interval-ms 6500
    uv run python scripts/autopilot.py ledger summary --month 2026-09
    uv run python scripts/autopilot.py report --month 2026-09
    uv run python scripts/autopilot.py apply-proposal \
        --file report/2026-09/proposal.json --approve --approved-by "your name"

Every command takes `--config <path>`; `autopilot.demo.toml` is the quota-fit
ladder used by `docs/runbook-live-demo.md`.

The logic lives in `cost_autopilot.cli` so the test suite can import and
exercise it directly. This file only wires the command line to it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cost_autopilot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
