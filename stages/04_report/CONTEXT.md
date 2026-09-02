# Stage: 04_report — PLANNED, NOT BUILT

> **Status: planned (Phase C).** No code in this repository implements this
> stage. Phase B's `validate` command already computes regret with intervals
> and `ledger summary` already prints it per rung and per tier; what is still
> missing is *attribution* — grouping regret by the classifier rule that chose
> the rung — and the rendered report. Nothing yet does either, and no output
> claims to.

## Objective

Turn a month of ledger rows and validation records into one reviewable verdict:
what was spent, what was saved, what the saving cost in quality, and which
specific routing rule to change.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Spend, counterfactual, tiers, fallbacks, refusals |
| `validate/<YYYY-MM>/verdicts.jsonl` | 4 | Authoritative | Yes | One verdict per validated request: regret, per-criterion comparisons, position bias, judge errors, validation cost |
| `validate/<YYYY-MM>/regret.json` | 4 | Authoritative | Yes | Regret already aggregated overall, per rung and per tier, with Wilson intervals |
| `autopilot.toml` | 3 | Authoritative | Yes | A new `[report]` section: the regret threshold that decides the verdict |

## Process

1. Total spend, counterfactual and saving — integer arithmetic, as
   `ledger.summary` already does.
2. Read regret from stage 03 rather than recomputing it. Stage 03 owns the
   definition and the intervals; two stages computing the same number
   differently is how a report starts disagreeing with the command that fed it.
3. Attribute regret to a rule. Every ledger row carries the `reasons` that chose
   its rung, so regret can be grouped by which rule fired — turning "routing is
   too aggressive" into "the `words_medium` band is sending 60-word debugging
   requests to the middle rung".
4. Compare regret against the configured threshold and emit a verdict.
5. Render a Markdown report and, optionally, a PR comment.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `reports/<YYYY-MM>.md` | Spend, saving, regret overall and per tier, the rules most associated with regret, and a specific recommended threshold or ceiling change | A human deciding whether to keep the current routing |
| `reports/<YYYY-MM>.json` | The same figures, machine-readable | CI |
| Process exit code | `0` regret under threshold · `1` regret over threshold · `2` insufficient data | CI |

## Verify

- Every figure recomputable from the ledger and the validation files with a
  calculator.
- A month with too few shadow comparisons reports `INCONCLUSIVE`, never a
  regret figure. Absence of evidence is not evidence of no regret.
- Recommendations name a specific config key and value, never "consider tuning".

## Approval

A human approves every recommendation before it is applied. This stage proposes
changes to `autopilot.toml`; it never writes to it. Publishing a report outside
the repository — a PR comment, a message — is gated the way project 1 gates its
alerts: dry run by default, an explicit flag to send.

## Failure Behavior

| Failure | Behavior |
|---|---|
| No `regret.json` for the month | `INCONCLUSIVE`, exit 2. Never reports a saving without its regret |
| Too few shadow comparisons | `INCONCLUSIVE` with the count, exit 2 |
| Ledger and verdicts disagree on a `request_id` | Named in the report as an inconsistency; the figure is not silently computed around it |
| `prices_verified = false` | Every monetary figure is labelled as unverified, in the report as well as on screen |
