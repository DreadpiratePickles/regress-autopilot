# Stage: 03_validate

## Objective

Turn "the cheap model was good enough" from an assumption into a measurement.

For a deterministic sample of the requests stage 02 routed below the top rung,
re-answer the same request **on the top rung** and judge the two answers against
each other. The output is **routing regret**: the share of sampled cheap answers
that were not good enough, with a 95% Wilson interval, broken down by rung and by
tier — and the cost of finding that out, reported as overhead rather than hidden.

A saving with unmeasured regret is not a saving. It is a cost that has been moved
somewhere nobody is looking, and this stage is where somebody looks.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `shadow/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Every sampled record: request text, cheap answer, criteria, tier, rung. Written by stage 02 |
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | `status: ok` rows below the top rung — the population the sample was drawn from, so the report can state what fraction was inspected |
| `validate/<YYYY-MM>/done.jsonl` | 4 | Authoritative | No | Request ids already validated. Absent on a first run |
| `autopilot.toml` | 3 | Authoritative | Yes | `[validate]` — `enabled`, `sample_percent`, `judge_model_ref`, `max_regret`, `min_samples`, `dir`, `shadow_dir`. Also `[[ladder.rung]]` prices and `[run] temperature` |
| `src/cost_autopilot/config.py` | 3 | Authoritative | Yes | Resolves `judge_model_ref` to a model id. The only module naming a model |
| `src/cost_autopilot/validate/prompts/pairwise_v1.md` | 3 | Authoritative | Yes | The pairwise system prompt, sent verbatim |
| `regression_detect.judge.criterion` + its `judge_v1.md` | 3 | Authoritative | Yes | Project 1's criterion judge and its strict parser, reused unchanged |
| `GEMINI_API_KEY` (`.env`, environment) | 3 | Authoritative | Unless `--dry-run` | The provider credential; never logged |
| `--month`, `--limit`, `--min-interval-ms`, `--dry-run` (CLI) | 4 | Operator input | No | Which month, how much of it, how fast, and whether to call anything |

The stage deliberately cannot see the ledger's cost columns per row, the budgets,
or the saving. A validator that could see what a request saved would be a
validator with a reason to prefer one answer.

## Process

Steps 1, 2 and 6-8 are deterministic code. Steps 3-5 are the only calls out.

1. **Read the month's shadow file.** No records is a real answer, not a failure:
   the command says so and exits 0.
2. **Skip what is already done.** `done.jsonl` holds one line per validated
   request id. A re-run judges nothing twice and pays for nothing twice.
   `--limit N` stops after N fresh records; the rest stay resumable.
3. **Reference answer.** Re-ask the same request on the ladder's **top rung** —
   the call the router chose not to make — at `[run] temperature`. Metered and
   priced at that rung's tariff. Without this there is nothing to compare
   against and regret has no meaning. If it fails, no judgement is attempted and
   none is invented.
4. **Criteria judging**, where the workload supplied criteria. Each criterion is
   judged **twice and independently** — once on the cheap answer, once on the
   reference answer — using `regression_detect.judge.criterion.judge_criterion`
   with its delimited inputs and its strict `{"passed", "reason"}` parser.
   Criteria regret for a request is: the cheap answer failed at least one
   criterion that the reference answer passed. Judging only the cheap answer
   would count every criterion no model could satisfy as a routing mistake.
5. **Pairwise judging**, always, because most traffic has no criteria. The
   request and the two answers go to the judge labelled A and B, which decides
   whether A is at least as good as B. **The same pair is judged twice with the
   labels swapped**, and the four outcomes are folded by the truth table in
   `validate/pairwise.py`: agreeing that the cheap answer is at least as good is
   sufficient; agreeing on the reference is regret; each order picking the other
   *slot* is a contradiction, recorded as `position_bias_detected` and counted as
   regret. Parse failures are judge errors, never verdicts.
6. **One verdict record per request**, appended to `verdicts.jsonl`, then the id
   appended to `done.jsonl` — in that order, so a crash between the two re-judges
   a request rather than losing it.
7. **Aggregate** every verdict for the month into overall, per-rung and per-tier
   groups: `n`, `regret_count`, `regret_rate`, `wilson_low`, `wilson_high` (from
   `regression_detect.compare.wilson_interval`), `judge_error_count` and
   `validation_cost_micro_usd`, plus `sample_percent` and the ledger's total
   cheap-routed count.
8. **Write `regret.json`.** `ledger summary` renders it when it exists.

A request whose judgements were all unreadable is excluded from `n` and counted
in `judge_error_count`. It is never counted as "no regret": that would let a
broken judge improve the numbers.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `validate/<YYYY-MM>/verdicts.jsonl` | One JSON object per request: `schema_version`, ids, `tier`, `rung_index`, `chosen_model_id`, `reference_model_id`, `judge_model_id`, `criteria_regret` (bool\|null), `criterion_verdicts[]` (criterion, cheap/reference passed and reasons), `pairwise_forward`, `pairwise_reverse`, `pairwise_regret` (bool\|null), `position_bias_detected`, `regret` (bool\|null), `judge_errors[]`, `reference_cost_micro_usd`, `judge_cost_micro_usd`, `reference_latency_ms`, `judge_latency_ms`, `currency` | Stage 04, and a human auditing a verdict |
| `validate/<YYYY-MM>/done.jsonl` | `{"request_id": ...}` per line | This stage, on its next run |
| `validate/<YYYY-MM>/regret.json` | Overall / per-rung / per-tier groups, the sampling rule, the cheap-routed count, and the validation cost | `ledger summary`, stage 04, CI |
| stdout | One line per validated request, then the totals and the overhead | The operator |
| Process exit code | `0` everything judged · `1` at least one judge call failed · `2` bad configuration or an unreadable shadow/verdict file | CI, and the operator |

`validate/` and `shadow/` are gitignored. `shadow/` is the only place in this
system that holds customer text; it is written mode `0600` and the ledger stays
text-free whether validation is on or off.

## Verify

- `uv run pytest -q` — 780 tests, none touching the network. The paths that
  matter here are covered directly: deterministic sampling (same id, same
  decision; the rate over 1,000 synthetic ids); a shadow record written only for
  a successful non-top-rung answer and only when `enabled`; request text never
  appearing in a ledger row; the pairwise truth table in all four states plus the
  three missing-verdict states; a parse failure recorded as a judge error rather
  than a failed criterion; exact regret counts and Wilson bounds against the same
  function recomputed in the test; an idempotent re-run adding no duplicates;
  `--limit`; and the four tier verdict lines asserted as exact strings.
- `uv run ruff check .` — clean at line-length 100.
- `uv run python scripts/autopilot.py validate --dry-run` — exits 0, makes no
  network call, and prints that every verdict below it is a constant.
- After any run: `wc -l validate/<month>/verdicts.jsonl` equals the number of
  unique ids in `done.jsonl`; `regret.json`'s `n` plus the count of records with
  no readable verdict equals `validated_count`; every cost field is an integer.
- **Judge calibration is not done and is not claimed.** No human has yet graded a
  sample by hand and checked that this judge agrees with them. Until that
  happens, a regret figure from this stage is a measurement of what one model
  thinks of another model's answer. See Approval.

## Approval

**A human owns the verdict.** This stage measures; it never acts.

- **Changing routing in response to these numbers is blocked without a human.**
  The summary prints "consider routing this tier up". It does not edit
  `autopilot.toml`, and nothing in this package can. Raising a tier's
  `[policy]` floor or a rung's `max_tier` is a reviewed diff.
- **Quoting a regret figure is blocked without calibration.** Before any number
  from here is used to justify a decision, a person reads a sample of judged
  answers against their criteria and confirms the judge agrees with them — the
  discipline project 1's `calibration.py` enforces. A tier reporting
  "insufficient evidence" is telling you this has not been earned yet.
- **Turning `enabled` on is a data-retention decision.** It starts storing
  customer request text and model answers on disk. That is a reviewed change, and
  a deployment that may not retain such text should set it to `false` and read
  `ledger summary` knowing it reports spend with the quality question open.
- **Spending is gated** the way stage 02's is: `--dry-run` absent and a key
  present. Validation costs one top-rung call plus `2 × criteria + 2` judge calls
  per sampled request — each criterion judged on both answers, the pair judged in
  both orders, so 8 judge calls for a 3-criterion request and 2 for one with no
  criteria; `sample_percent` is directly a bill.

**No creator grades its own work.** Stage 02 produced the cheap answers and this
stage did not. But the judge is a real weakness and is named rather than hidden:
`judge_model_ref` defaults to the cheapest rung, which is the same model family
that produced most of the answers, and models are known to prefer their own
output. Point it at a different family where one is available.

## Failure Behavior

| Failure | Behavior |
|---|---|
| No shadow file for the month | Message naming the directory; exit 0. Nothing to validate is not a failure |
| `[validate] enabled = false` | Stage 02 writes no shadow records, so this stage finds none and says so. It never invents a sample |
| Reference answer call fails after retries | Recorded as a judge error on that request; **no criterion or pairwise call is made**, because there is nothing to compare against. The request has no verdict and is excluded from `n`. The run continues; exit 1 |
| A judge call fails after retries, or its reply will not parse | `JudgeParseError` / `PairwiseParseError` / `ProviderError` recorded per judgement in `judge_errors`. That judgement has no verdict and never degrades into "failed the criterion" |
| Some criteria judged, some errored | Regret is computed from the criteria that produced verdicts on **both** answers. If none did, `criteria_regret` is null |
| The two pairwise orders contradict each other | `position_bias_detected: true`, counted as regret. Conservative on purpose: a judgement this system could not read is not evidence the cheap answer was fine |
| Provider quota exhausted mid-run | Every request already validated is on disk and marked done. Re-running finishes the rest and re-pays for nothing. Use `--min-interval-ms` to pace, and `--limit` to stop deliberately |
| A vendor reports a permanent entitlement problem as `429` | It is classified `ProviderTransientError` and retried, because the code is indistinguishable from a real rate limit. The run records judge errors honestly and produces no verdicts. Check the quota metric in the vendor's error before assuming the model is unhealthy |
| Shadow or verdict file corrupt, truncated, or an unknown schema version | `ShadowError` / `VerdictError` naming the file and line; exit 2. A partially readable sample is refused rather than worked around, because it would silently change the denominator every figure is divided by |
| A judge model that is not on the ladder | `ConfigFileError` at load; exit 2. Its calls could not be priced, and an unpriced call would make the reported overhead a guess |
| Workload criteria changed since routing | Not possible: criteria are copied onto the shadow record at routing time, so an answer is always judged against the standard it was produced under |
| Interrupted part-way | Nothing to clean up. Both files are append-only and the run is resumable from `done.jsonl` |

Retries are bounded at every level: the provider stops at 3 attempts, the runner
makes at most one attempt per judgement, and no failure is retried by being
re-asked in a different form. Escalation path: a run where every request has a
reference-answer error is a quota or entitlement problem on the top rung, not a
validation problem — check the vendor's quota metrics before touching the ladder.
