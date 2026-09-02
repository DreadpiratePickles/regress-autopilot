# Stage: 04_report — PLANNED, NOT BUILT

> **Status: planned (Phase C).** No code in this repository implements this
> stage. The `ledger summary` command in Phase A totals spend and the
> counterfactual; it deliberately reports **no** quality figure, because until
> stage 03 exists there is no evidence about quality to report.

## Objective

Turn a month of ledger rows and validation records into one reviewable verdict:
what was spent, what was saved, what the saving cost in quality, and which
specific routing rule to change.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Spend, counterfactual, tiers, fallbacks, refusals |
| `validations/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Pass rates per tier and per rung, and the shadow comparisons |
| `autopilot.toml` | 3 | Authoritative | Yes | A new `[report]` section: the regret threshold that decides the verdict |

## Process

1. Total spend, counterfactual and saving — integer arithmetic, as
   `ledger.summary` already does.
2. Compute **regret**: the share of requests where the routed rung's answer
   failed a criterion that the top rung's shadow answer passed. This is the
   number the whole system is judged on. A saving with unmeasured regret is not
   a saving, it is a deferred cost.
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

- Every figure recomputable from the two input files with a calculator.
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
| No validation records for the month | `INCONCLUSIVE`, exit 2. Never reports a saving without its regret |
| Too few shadow comparisons | `INCONCLUSIVE` with the count, exit 2 |
| Ledger and validations disagree on a `request_id` | Named in the report as an inconsistency; the figure is not silently computed around it |
| `prices_verified = false` | Every monetary figure is labelled as unverified, in the report as well as on screen |
