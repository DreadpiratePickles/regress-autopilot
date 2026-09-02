> **LIVE — produced by a real run, and it did not complete.** Routed against `gemini-3.5-flash-lite` with `gemini-3.6-flash` as the top rung, using `autopilot.demo.toml` and `workloads/demo_quota_v1.jsonl`, on 2026-09-02. 4 of 12 requests were answered; the other 8 exhausted the free tier's 20-requests-per-day allowance on `gemini-3.6-flash` and are recorded as `failed`. No reference answer could be obtained, so the month has **no regret figure** — that is the failure path working, not a measurement. This blockquote is the only line added by hand.

# Tuning proposal — 2026-09

- **Status:** `awaiting_human_approval`
- **Config:** `autopilot.demo.toml`
- **Generated:** `2026-09-02T14:11:22Z`
- **Approved by:** _nobody yet_
- **Applied:** _not applied_

## The diff

```diff
 [validate]
-sample_percent = 50
+sample_percent = 100
```

## Why

### `[validate] sample_percent`: 50 → 100

- **Rules:** `raise_sample_percent`
- **Evidence:** raise_sample_percent (T1_TRIVIAL): n=0, regret 0 (0%), 95% CI [0.000, 1.000]; raise_sample_percent (T2_STANDARD): n=0, regret 0 (0%), 95% CI [0.000, 1.000]

## Approving this

This tool will not apply the diff above. Read the evidence, decide, and then either edit `autopilot.toml` by hand or run:

```bash
uv run python scripts/autopilot.py apply-proposal \
  --file report/2026-09/proposal.json \
  --approve --approved-by "your name"
```

Both flags are required. The command refuses if `autopilot.toml` has moved on since this proposal was generated, refuses a proposal that has already been applied, and writes your name and the timestamp back into `proposal.json` so the approval travels with the change.
