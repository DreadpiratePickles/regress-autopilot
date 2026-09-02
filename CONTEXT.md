# Context router

Layer 1. This file answers "where do I go?" — it maps a task to the stage that
owns it. Read this, then read that stage's `CONTEXT.md`, then read only the
inputs that stage declares.

## Stages

The tool is a pipeline: decide how hard a request is, route it to the cheapest
model that can handle it, check that the cheap answer was actually good enough,
and report what the routing saved and what proving it cost. Each stage is one
job with one output.

| Stage | Job | Lives in | Built? |
|---|---|---|---|
| `01_classify` | Score a request's complexity from its text alone and assign it a tier, with the reasons that decided it | `stages/01_classify/CONTEXT.md`, `src/cost_autopilot/classify/` | Yes |
| `02_route` | Pick a rung, enforce the team's budget, call the model, fall back on transient failure, price the call, append one ledger row | `stages/02_route/CONTEXT.md`, `src/cost_autopilot/route/`, `src/cost_autopilot/ledger/`, `src/cost_autopilot/providers/` | Yes |
| `03_validate` | Re-answer a deterministic sample of cheap-routed requests on the top rung, judge both answers, and report routing regret with a 95% interval | `stages/03_validate/CONTEXT.md`, `src/cost_autopilot/validate/` | Yes |
| `04_report` | Turn a month of rows and verdicts into one verdict — spend, saving, regret, what the measurement cost — plus deterministic recommendations and an `autopilot.toml` diff a named human must approve | `stages/04_report/CONTEXT.md`, `src/cost_autopilot/report/` | Yes |

Stages 01 and 02 are Phase A; stage 03 is Phase B; stage 04 is Phase C. **All
four are implemented.**

Stages 02 and 03 are the only stages that spend money or leave the machine. Each
`CONTEXT.md` says exactly when, and `--dry-run` disables it entirely. Stage 04
calls no model in either mode: its `--dry-run` is a *labelling* flag, which
stamps a synthetic banner on every artifact so a document read out of context
cannot be mistaken for a measurement.

Stage 04 is also the only stage that can write to `autopilot.toml`, and only
through the separate `apply-proposal` command, only with `--approve` and a named
`--approved-by`, and only for `[policy] <TIER>` and `[validate] sample_percent`.
The report itself never edits anything.

Stage 03 is also the only stage that stores customer text. It stores it in
`shadow/`, never in the ledger, only for the sampled fraction, and only while
`[validate] enabled = true`.

## Shared resources

| Path | Layer | What it is |
|---|---:|---|
| `README.md` | 0 | Workspace identity: what the tool is for, the six commands, and the numbers it has and has not measured |
| `docs/design.md` | 3 | Every design decision and the reason for it. Read before changing behaviour |
| `autopilot.toml` | 3 | The ladder, its prices, the budgets, the tier thresholds. Everything that decides what a request costs. No model id lives here |
| `src/cost_autopilot/config.py` | 3 | Model identifiers, and nothing else. The only module that names a model |
| `src/cost_autopilot/money.py` | 3 | Integer micro-USD arithmetic. Every amount in the system passes through here |
| `src/cost_autopilot/providers/` | 3 | The metered provider seam. Only this package names a model vendor |
| `src/cost_autopilot/classify/prompts/classify_v1.md` | 3 | The v1 prompt for the optional LLM classifier, which is off by default |
| `src/cost_autopilot/validate/prompts/pairwise_v1.md` | 3 | The v1 prompt for the pairwise judge. Run twice per pair, with the labels swapped |
| `src/cost_autopilot/parsing.py` | 3 | The one tolerance every strict parser here shares: a single markdown fence |
| `workloads/mixed_v1.jsonl` | 3 | The demo workload: 30 requests across the three tiers, each with plain-English pass criteria for stage 03 |
| `workloads/demo_quota_v1.jsonl` | 3 | The quota-fit workload: 12 requests (5 T1, 5 T2, 2 T3) sized so one live run fits a free-tier key's daily allowance |
| `autopilot.demo.toml` | 3 | The two-rung ladder that makes that live run possible. **Not the configuration to run.** Its `min_samples = 4` cannot support a verdict anybody should act on |
| `docs/runbook-live-demo.md` | 3 | The exact commands, the computed call budget, how to tell a quota failure from a bug, and what happened when it was run |
| `docs/examples/` | 3 | Committed report, proposal and summary artifacts. Every one carries a banner on its **first line** saying whether it is synthetic or live |
| `ledger/<YYYY-MM>.jsonl` | 4 | One append-only row per routed request. Gitignored: operational data, not source |
| `shadow/<YYYY-MM>.jsonl` | 4 | The sampled requests and their cheap answers. **The only file holding customer text.** Gitignored, mode 0600, opt-in |
| `validate/<YYYY-MM>/` | 4 | `verdicts.jsonl`, `done.jsonl` and `regret.json` for one month. Gitignored |
| `.github/workflows/ci.yml` | 3 | Lint, the test suite, and all four stages offline. Needs no secret and spends nothing |
| `report/<YYYY-MM>/` | 4 | `report.md`, `report.json`, `proposal.md` and `proposal.json` for one month. Gitignored: derived data, regenerable from the two files above |
| `demo/` | 4 | Everything `autopilot.demo.toml` writes, kept apart so a demo run can never overwrite a real month. Gitignored |

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
- **A verdict sentence is written in exactly one place.** The four wordings —
  `safe`, `insufficient evidence`, `no regret observed … interval too wide`,
  `regret too high` — come from `validate.report.tier_verdict_line`, which both
  `ledger summary` and stage 04's report render verbatim. Two commands showing
  the same evidence in different words is how a reader learns to trust neither.
- **A synthetic number says so before it is read.** Any artifact produced from a
  dry run carries a banner on its first line, not a footnote.
- **The tool proposes; a person approves.** Nothing in this package changes
  routing, a budget or a sample rate on its own. `apply-proposal` is the single
  exception, it needs `--approve` and a named `--approved-by`, and it records
  both in the proposal file.
- Secrets live in `.env` and never enter source, prompts, logs, or a ledger row.
