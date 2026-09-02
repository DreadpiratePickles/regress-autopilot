> **SYNTHETIC — produced by a dry run.** Every verdict below is a canned constant from an in-memory fake, not a model judgement, and every cost comes from fixed synthetic token counts. These numbers exercise the arithmetic and the pipeline; they measure nothing.

# Tuning proposal — 2026-09

- **Status:** `awaiting_human_approval`
- **Config:** `autopilot.toml`
- **Generated:** `2026-09-02T14:11:52Z`
- **Approved by:** _nobody yet_
- **Applied:** _not applied_

## The diff

```diff
 [validate]
-sample_percent = 20
+sample_percent = 100
```

## Why

### `[validate] sample_percent`: 20 → 100

- **Rules:** `raise_sample_percent`
- **Evidence:** raise_sample_percent (T1_TRIVIAL): n=4, regret 0 (0%), 95% CI [0.000, 0.490]; raise_sample_percent (T2_STANDARD): n=1, regret 0 (0%), 95% CI [0.000, 0.793]

## Approving this

This tool will not apply the diff above. Read the evidence, decide, and then either edit `autopilot.toml` by hand or run:

```bash
uv run python scripts/autopilot.py apply-proposal \
  --file report/2026-09/proposal.json \
  --approve --approved-by "your name"
```

Both flags are required. The command refuses if `autopilot.toml` has moved on since this proposal was generated, refuses a proposal that has already been applied, and writes your name and the timestamp back into `proposal.json` so the approval travels with the change.
