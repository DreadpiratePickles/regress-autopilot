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
| `03_validate` | Judge each recorded answer against its workload criteria, so "the cheap model was good enough" becomes evidence rather than an assumption | `stages/03_validate/CONTEXT.md` | **Planned** |
| `04_report` | Turn a month of judged rows into a verdict: spend, saving, regret, and which routing rules to change | `stages/04_report/CONTEXT.md` | **Planned** |

Stages 01 and 02 are Phase A and are implemented. Stages 03 and 04 are Phase B
and C; their contracts are written and their code is not. A stub contract says
what a stage will do, not what it does — nothing in this repository currently
validates an answer or computes regret, and no output claims to.

Stage 02 is the only stage that spends money or leaves the machine. Its
`CONTEXT.md` says exactly when, and `--dry-run` disables it entirely.

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
| `workloads/mixed_v1.jsonl` | 3 | The demo workload: 30 requests across the three tiers, each with plain-English pass criteria for stage 03 |
| `ledger/<YYYY-MM>.jsonl` | 4 | One append-only row per routed request. Gitignored: operational data, not source |

## Reused from project 1

`regression-detect`, pinned to commit `5c1fa8b`, supplies
`providers.base` (the `Provider` protocol and the typed error hierarchy),
`providers.gemini` (the retry policy, backoff constants and timeout), and
`pacing` (spreading a burst of calls under a per-minute quota). This package
imports those rather than restating them, so the two projects cannot drift on
what counts as a transient failure. What it adds is the *metered* seam:
`Completion` carries token usage, which project 1's text-only `Provider` does
not, and without which a call cannot be priced.

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
- Request text is not written to the ledger by default; a SHA-256 is.
- Secrets live in `.env` and never enter source, prompts, logs, or a ledger row.
