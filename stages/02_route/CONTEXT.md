# Stage: 02_route

## Objective

Serve one classified request on the cheapest rung the ladder permits, within the
team's monthly budget, falling back only on transient provider failure, and
record exactly one ledger row describing what happened and what it cost.

Since Phase B it also keeps a deterministic sample of the cheap answers it
produced, so stage 03 has something to validate. That is the stage's only side
effect beyond the ledger, and it is the only place this system stores request
text.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `Classification` from stage 01 | 4 | Authoritative | Yes | `tier` chooses the rung; `complexity_score` and `reasons` are copied to the row |
| The request text | 4 | Operator input | Yes | Sent as the user message; hashed onto the row |
| `--team` (CLI) | 4 | Operator input | Yes | The team the spend is charged to |
| `autopilot.toml` | 3 | Authoritative | Yes | `[ladder]` and `[[ladder.rung]]`, `[policy]`, `[budgets]`, `[ledger]`, `[run]` |
| `src/cost_autopilot/config.py` | 3 | Authoritative | Yes | Resolves each rung's `model_ref` to a model id |
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Read to total the team's spend so far this month; appended to afterwards |
| `GEMINI_API_KEY` (`.env`, environment) | 3 | Authoritative | Unless `--dry-run` | The provider credential; never logged |
| `--workload` (CLI) | 4 | Operator input | No | A JSONL batch, fully validated before the first call |
| `--min-interval-ms` (CLI) | 4 | Operator input | No | Overrides `[run] min_interval_ms` for one run |
| `--dry-run` (CLI) | 4 | Operator input | No | Substitutes fakes at the provider factory. No network call is possible |
| `autopilot.toml` `[validate]` | 3 | Authoritative | Yes | `enabled` and `sample_percent` decide whether a cheap answer is kept; `shadow_dir` says where |
| A workload line's `criteria` | 3 | Authoritative | No | Copied onto a shadow record if one is made. Plays no part in routing |

## Process

Every step is deterministic code except step 5, which is the only call out.

1. Load and validate `autopilot.toml`. An unknown key, a negative price, a
   non-integer amount, a decreasing capability ceiling or an absolute ledger
   path fails here, before anything is spent.
2. For a `--workload` run, load and validate the whole file. A malformed line 28
   is found while nothing has been spent, not after twenty-seven paid calls.
3. Choose the starting rung: the later of the ladder's capability floor (the
   first rung whose `max_tier >= tier`) and the `[policy]` floor for that tier.
   The policy may skip cheap rungs it does not trust; it can never send a tier to
   a rung below its ceiling.
4. **Budget check, before any call.** Total the team's `cost_micro_usd` for the
   current UTC month from the ledger and compare against its cap. `spend >= cap`
   refuses: the request is not queued, no model is called, and a `refused` row is
   written costing zero. Because a pending request's cost is unknowable before
   the model answers, the last permitted request may carry the total slightly
   past the cap — a cap is a floor on refusals, not a hard ceiling on spend.
5. **The model call.** `provider.complete(system, user, temperature)` on the
   chosen rung. The request is the user message; it is never formatted into the
   system prompt. The provider retries transient failures up to 3 times with
   exponential backoff and jitter, under a 60-second timeout — the policy is
   imported from project 1, not restated.
6. Validate the reply *and* its usage block. Empty text is
   `ProviderResponseError`; a missing or malformed `usage_metadata` is
   `UsageError`. Neither ever becomes a free successful call.
7. **Fallback.** On `ProviderTransientError` only — a failure the provider has
   already retried — step up to the next rung and try once. Bounded by the
   ladder: at most one attempt per remaining rung, never wrapping around. A
   `ProviderConfigError` or `ProviderResponseError` stops immediately, because a
   rejected credential or a malformed reply will not be fixed by a more
   expensive model.
8. Price the call in integer micro-USD from the rung that actually answered,
   ceiling each token leg separately, and price the same token counts again on
   the top rung as the counterfactual.
9. Append exactly one row to `ledger/<YYYY-MM>.jsonl`, filed under the month in
   its own timestamp. One `O_APPEND` write of one encoded line.
9a. **Shadow sample.** If `[validate] enabled`, the answer came from a rung
   *below* the top, and `sha256(request_id) mod 100 < sample_percent`, append the
   request, the answer and the workload's criteria to `shadow/<YYYY-MM>.jsonl`
   and set `shadow_sampled: true` on the row. The ledger row is written first:
   it is the authoritative record, and a failed shadow write must not take a
   paid-for routing decision down with it. The top rung is excluded because its
   reference answer would come from the rung that already answered it.
10. In a batch, pace consecutive calls by `min_interval_ms` and print a line per
    request, then the counts. Exit non-zero if any request was not `ok`.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `ledger/<YYYY-MM>.jsonl` | One JSON object per line: `schema_version`, `request_id` (uuid4), `ts_utc`, `team_id`, `tier`, `complexity_score`, `reasons[]`, `chosen_model_id`, `fallback_chain[]`, `input_tokens`, `output_tokens`, `cost_micro_usd`, `counterfactual_top_model_cost_micro_usd`, `currency` (`"USD"`), `latency_ms`, `status` (`ok`\|`refused`\|`failed`), `error_type`, `request_sha256`, `request_text` (null unless `log_text`), `shadow_sampled` | Stage 03, stage 04, `ledger summary`, and the next budget check |
| `shadow/<YYYY-MM>.jsonl` | One JSON object per sampled request: ids, `tier`, `complexity_score`, `chosen_model_id`, `rung_index`, `request_text`, `answer_text`, `criteria[]`, token counts. **The only file holding customer text.** Gitignored, mode 0600 | Stage 03 |
| The answer text | In memory on `RouteOutcome.completion` | The caller. Deliberately not persisted by this stage |
| stdout | One line per request: status, tier, model, cost, counterfactual | The operator |
| Process exit code | `0` every request ok · `1` at least one refused or failed · `2` bad configuration, unreadable workload, or a provider that could not be built | CI, and the operator |

## Verify

- `uv run pytest -q` — 776 tests, none touching the network. The paths that
  matter here are covered directly: rung choice per tier, the policy floor being
  unable to defeat the capability floor, refusal at exactly the cap, transient
  failure stepping up one rung and succeeding, exhaustion recording
  `status: failed` with the last error type, config and response errors not
  falling back, cost and counterfactual arithmetic, and one row per path.
- `uv run ruff check .` — clean at line-length 100.
- `uv run python scripts/autopilot.py route --workload workloads/mixed_v1.jsonl
  --team demo --dry-run` — exits 0, writes exactly 30 rows, makes no network
  call. Verified 2026-09-02: 30/30 `ok`, spread across all three rungs.
- After any run: the line count of `ledger/<month>.jsonl` equals the number of
  requests routed; `ledger summary`'s spend equals the sum of the rows'
  `cost_micro_usd` computed independently; every `cost_micro_usd` is an integer.
- A green test run proves the routing logic, not the vendor. It does not prove
  the model ids exist, the prices are current, or the answers were any good.
  Only stage 03 speaks to the last of those.

## Approval

This is the stage that spends money, so the gates are here.

- **Spending at all** is gated by `--dry-run` being absent and a key being
  present. A machine without `GEMINI_API_KEY` cannot spend.
- **How much may be spent** is gated by `[budgets]`. Changing a cap, or adding a
  team, is a reviewed diff.
- **What a call costs** is gated by `[[ladder.rung]]` prices and the
  `prices_verified` flag. A human must confirm prices against the vendor's
  published page before any spend figure from this stage is quoted; while
  `prices_verified = false`, every total prints a warning.
- **Which models may be called** is gated by `config.py` plus the ladder.
- **Whether request text is stored at all** is gated by `[validate] enabled`,
  and how much of it by `sample_percent`. Both are reviewed diffs.

Blocked without a human: raising a budget in response to a refusal, adding a
rung, setting `log_text = true` (which puts customer text into the ledger), and
turning `[validate] enabled` on (which starts writing customer text to
`shadow/`). The stage performs no external write beyond the model call
and its own ledger directory. It never pushes, publishes, or messages anyone.

## Failure Behavior

| Failure | Behavior |
|---|---|
| `GEMINI_API_KEY` missing or rejected | `ProviderConfigError` before any row is written; exit 2. The message names the variable, never the key |
| `autopilot.toml` missing, unparseable, or holding an unknown key or bad price | `ConfigFileError` naming file, section and key; exit 2; nothing runs |
| Workload file missing, malformed, or holding a duplicate id | `WorkloadError` naming the line; exit 2; **no ledger row is written** |
| Team is at or over its monthly cap | `BudgetExceededError` caught and recorded as a `refused` row costing zero, with `error_type` and an empty `fallback_chain`. No model is called. Exit 1 |
| One rung returns a rate limit, timeout or 5xx | The provider retries 3 times with backoff and jitter; still failing, the router steps up one rung and tries once |
| Every rung fails transiently | One `failed` row with the full `fallback_chain` and the last `error_type`; the batch continues to the next request; exit 1 |
| A rung rejects credentials, or returns a malformed reply or usage block | No fallback — a more expensive model would spend money on the same fault. One `failed` row naming the error type; exit 1 |
| Ladder has no rung whose ceiling reaches the tier, or the policy points past its end | `RoutingError`; exit 2. A configuration mistake fails loudly rather than silently collapsing onto the top rung |
| A short write to the ledger | `LedgerError` saying the row may be truncated and the ledger must not be treated as complete |
| A shadow write fails | `ShadowError`; exit 2. The ledger row for that request is already on disk, so the accounting is complete and only the evidence for one validation is lost |
| The month rolls over mid-batch | Rows file themselves under the month in their own timestamps; two files result, and both budget checks read the correct one |

Retries are bounded at every level and never repeat an identical failure: the
provider stops at 3 attempts, the router stops at the top of the ladder. No
cleanup or rollback is needed — the ledger is append-only and never edited, so a
crashed run leaves a short but valid file. Escalation path: a run where every
request failed is a credential, model-id or provider-health problem, not a
routing problem. Check the key, then the model ids in `config.py`, then the
provider's status, before changing anything in the ladder.
