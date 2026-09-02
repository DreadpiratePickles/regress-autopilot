# Context router

Layer 1. This file answers "where do I go?" — it maps a task to the stage that
owns it. Read this, then read that stage's `CONTEXT.md`, then read only the
inputs that stage declares.

## Stages

The tool is a pipeline: decide how hard a request is, route it to the cheapest
model that can handle it, then — in a later phase — check that the cheap answer
was actually good enough and report what the routing saved. Each stage is one
job with one output.

| Stage | Job | Lives in | Built? |
|---|---|---|---|
| `01_classify` | Score a request's complexity from its text alone and assign it a tier, with the reasons that decided it | `stages/01_classify/CONTEXT.md`, `src/cost_autopilot/classify/` | Yes |
| `02_route` | Pick a rung, enforce the team's budget, call the model, fall back on transient failure, price the call, append one ledger row | `stages/02_route/CONTEXT.md`, `src/cost_autopilot/route/`, `src/cost_autopilot/ledger/`, `src/cost_autopilot/providers/` | Yes |
| `03_validate` | Re-answer a deterministic sample of cheap-routed requests on the top rung, judge both answers, and report routing regret with a 95% interval | `stages/03_validate/CONTEXT.md`, `src/cost_autopilot/validate/` | Yes |
| `04_report` | Turn a month of judged rows into a verdict: spend, saving, regret attributed to the classifier rule that fired, and which routing rules to change | `stages/04_report/CONTEXT.md` | **Planned** |

Stages 01 and 02 are Phase A; stage 03 is Phase B. All three are implemented.
Stage 04 is Phase C: its contract is written and its code is not. A stub contract
says what a stage will do, not what it does — nothing in this repository
currently attributes regret to a rule or emits a report, and no output claims to.

Stages 02 and 03 are the only stages that spend money or leave the machine. Each
`CONTEXT.md` says exactly when, and `--dry-run` disables it entirely.

Stage 03 is also the only stage that stores customer text. It stores it in
`shadow/`, never in the ledger, only for the sampled fraction, and only while
`[validate] enabled = true`.

## Shared resources

| Path | Layer | What it is |
|---|---:|---|
| `README.md` | 0 | Workspace identity: what the tool is for and the three commands |
| `docs/design.md` | 3 | Every design decision and the reason for it. Read before changing behaviour |
| `autopilot.toml` | 3 | The ladder, its prices, the budgets, the tier thresholds. Everything that decides what a request costs. No model id lives here |
| `src/cost_autopilot/config.py` | 3 | Model identifiers, and nothing else. The only module that names a model |
| `src/cost_autopilot/money.py` | 3 | Integer micro-USD arithmetic. Every amount in the system passes through here |
| `src/cost_autopilot/providers/` | 3 | The metered provider seam. Only this package names a model vendor |
| `src/cost_autopilot/classify/prompts/classify_v1.md` | 3 | The v1 prompt for the optional LLM classifier, which is off by default |
| `src/cost_autopilot/validate/prompts/pairwise_v1.md` | 3 | The v1 prompt for the pairwise judge. Run twice per pair, with the labels swapped |
| `src/cost_autopilot/parsing.py` | 3 | The one tolerance every strict parser here shares: a single markdown fence |
| `workloads/mixed_v1.jsonl` | 3 | The demo workload: 30 requests across the three tiers, each with plain-English pass criteria for stage 03 |
| `ledger/<YYYY-MM>.jsonl` | 4 | One append-only row per routed request. Gitignored: operational data, not source |
| `shadow/<YYYY-MM>.jsonl` | 4 | The sampled requests and their cheap answers. **The only file holding customer text.** Gitignored, mode 0600, opt-in |
| `validate/<YYYY-MM>/` | 4 | `verdicts.jsonl`, `done.jsonl` and `regret.json` for one month. Gitignored |

## Reused from project 1

`regression-detect`, pinned to commit `5c1fa8b`, supplies
`providers.base` (the `Provider` protocol and the typed error hierarchy),
`providers.gemini` (the retry policy, backoff constants and timeout), `pacing`
(spreading a burst of calls under a per-minute quota), `judge.criterion` (the
criterion judge, its delimited inputs and its strict verdict parser) and
`compare.wilson_interval` (the 95% interval on every rate stage 03 reports).
This package imports those rather than restating them, so the two projects cannot
drift on what counts as a transient failure or on how wide an interval is.

What it adds is the *metered* seam: `Completion` carries token usage, which
project 1's text-only `Provider` does not, and without which a call cannot be
priced. Stage 03 bridges the two — `MeteringTextProvider` narrows a
`MeteredProvider` to the `Provider` protocol project 1's judge expects, keeping
the usage that would otherwise be dropped so the judge's own cost is charged.

## Rules that hold across every stage

- Money is integer micro-USD with an explicit `USD` currency. No amount is ever
  a float, and every division rounds up.
- Prices are policy, not code. They live in `autopilot.toml` with a
  `prices_verified` flag, and an unverified price makes every total it touches
  print a warning.
- Model output is untrusted input — including a usage block. A missing token
  count is an error, never a free call.
- Deterministic work stays in deterministic code. Classification is rules;
  a model is called only to answer the request, and (opt-in, off by default) to
  classify one.
- Every routed request produces exactly one ledger row, whatever happened.
  `ok`, `refused` and `failed` are different facts and none decays into another.
- Request text is not written to the ledger, ever; a SHA-256 is. The sampled
  fraction of it lives in `shadow/`, which is opt-in, gitignored and mode 0600.
- A judgement that could not be read is a judge error, never a verdict. `True`,
  `False` and "no verdict" are three states everywhere, and a record with no
  verdict is excluded from the denominator rather than counted as a pass.
- Every rate is reported with its interval. A rate without one is a coin toss
  presented as a measurement.
- Secrets live in `.env` and never enter source, prompts, logs, or a ledger row.
