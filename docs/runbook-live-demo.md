# Runbook: the quota-fit live demo

A complete live run of all four stages — classify, route, validate, report — that
fits inside one free-tier Gemini API key's daily allowance. Read the budget
section before you start it, because the binding constraint is **20 requests per
day** on one model and there is no way to get more of them today.

Everything here uses `--config autopilot.demo.toml`. Nothing here touches
`autopilot.toml`, the real configuration, or the `ledger/`, `shadow/`,
`validate/` and `report/` directories it writes to.

---

## 1. Why the demo needs its own configuration

Three facts, all established by the Phase B live run on 2026-09-02 and recorded
in [`docs/design.md`](design.md) §9 and §20:

| Model | Free-tier limit | What that means here |
|---|---|---|
| `gemini-3.1-pro-preview` | **0 requests** — `429 RESOURCE_EXHAUSTED`, `limit: 0` | Paid-only. A free key can never reach it. The real ladder's top rung is unreachable. |
| `gemini-3.6-flash` | 20 requests / day | The demo's top rung, and the whole budget below. |
| `gemini-3.5-flash-lite` | 500 requests / day | The demo's cheap rung and its judge. Not the constraint. |

The real ladder's top rung being unreachable is not a bug and is not worked
around: with `autopilot.toml`, every request that reaches rung 2 records
`status: failed` honestly, and `validate` produces no reference answers at all,
so there is no regret figure to report. That is the failure path working. It is
also not a demo.

So [`autopilot.demo.toml`](../autopilot.demo.toml) drops to **two rungs** —
Flash-Lite → Flash — which makes Flash the top rung, the counterfactual baseline
and the source of every reference answer. Read the header of that file for the
two distortions it accepts (`min_samples = 4`, and rung 0's ceiling raised to
`T2_STANDARD`) and why.

**Quotas reset at midnight US/Pacific.** If you are out of Flash calls, the next
window opens then, not 24 hours from your last call.

---

## 2. The call budget, computed

The workload is [`workloads/demo_quota_v1.jsonl`](../workloads/demo_quota_v1.jsonl):
**12 requests — 5 T1, 5 T2, 2 T3**, each with 3 plain-English criteria. With the
demo ladder, T1 and T2 start on rung 0 and T3 starts on rung 1.

### Where each call goes

| Stage | Rung 1 — `gemini-3.6-flash` | Rung 0 — `gemini-3.5-flash-lite` |
|---|---:|---:|
| `route` — 10 T1/T2 requests | 0 | 10 |
| `route` — 2 T3 requests | 2 | 0 |
| `validate` — one reference answer per sampled record | 1 per sampled | 0 |
| `validate` — criteria judging, 3 criteria × 2 answers | 0 | 6 per sampled |
| `validate` — pairwise, both orders | 0 | 2 per sampled |
| **Per sampled record** | **1** | **8** |

`sample_percent = 50` and the sampling rule is `sha256(request_id) mod 100 < 50`
over a uuid4 request id, so the number sampled out of the 10 cheap-routed
requests is not fixed. Both cases are budgeted:

| | Sampled records | Flash calls | Flash-Lite calls |
|---|---:|---:|---:|
| **Expected** (half of 10) | 5 | 2 + 5 = **7** | 10 + 40 = **50** |
| **Worst case** (all 10) | 10 | 2 + 10 = **12** | 10 + 80 = **90** |
| Free-tier daily limit | | **20** | **500** |
| Headroom, worst case | | 8 | 410 |

**12 of 20 Flash calls, worst case.** That is the number the workload was sized
against. `report` makes no model call at all, in either mode.

If you have already spent some of today's Flash allowance, subtract it before
starting: 12 more calls need 12 free.

---

## 3. Run it

Four commands. Each one is safe to interrupt — `route` has already written a
ledger row for everything it finished, and `validate` is resumable from
`done.jsonl`.

```bash
# 0. Preconditions: a key in .env at the repository root, and dependencies.
uv sync

# 1. Route the workload. 10 Flash-Lite calls + 2 Flash calls.
uv run python scripts/autopilot.py --config autopilot.demo.toml \
  route --team demo --workload workloads/demo_quota_v1.jsonl \
  --min-interval-ms 6500

# 2. Totals so far. No model call.
uv run python scripts/autopilot.py --config autopilot.demo.toml ledger summary

# 3. Validate the shadow sample. 1 Flash + 8 Flash-Lite calls per sampled record.
uv run python scripts/autopilot.py --config autopilot.demo.toml \
  validate --min-interval-ms 6500

# 4. Render the month. No model call.
uv run python scripts/autopilot.py --config autopilot.demo.toml report
```

`--min-interval-ms 6500` is `60000 / ~9` requests per minute, the pacing project
1 settled on for this free tier. It is also `[run] min_interval_ms` in
`autopilot.demo.toml`, so the flag is belt and braces; pass it anyway, because a
future edit to the file should not silently un-pace a live run.

**Expect it to be slow.** At 6500 ms between calls, the worst case — 102 calls —
is about eleven minutes of wall clock. That is the point: the pacing is what
keeps the run from turning into a wall of 429s.

### Stopping deliberately

`validate --limit N` stops after N fresh records and leaves the rest resumable.
Use it to spend exactly as much of the Flash allowance as you have:

```bash
uv run python scripts/autopilot.py --config autopilot.demo.toml \
  validate --limit 3 --min-interval-ms 6500
```

Re-running `validate` later finishes the rest and re-pays for nothing: each
verdict is written and marked done as it is produced.

---

## 4. Telling a quota failure from a bug

This is the part worth reading twice, because the two look identical in the exit
code and nearly identical in the logs.

| What you see | What it is | What to do |
|---|---|---|
| `status: failed`, `error_type: ProviderTransientError`, on **every** request to one rung | A quota or entitlement problem on that rung. The vendor reports a per-minute rate limit, an exhausted daily quota and *an entitlement you do not have* with the same `429`. | Read the vendor's error body. `limit: 0` means paid-only — waiting will never help. A non-zero limit means wait for the reset. |
| Some requests `ok`, some `failed`, mixed through the run | A real per-minute rate limit. | Raise `--min-interval-ms` and re-run. The `ok` rows are already recorded. |
| `ProviderConfigError` before any ledger row exists | The key is missing or rejected. | Check `.env` at the repository root holds `GEMINI_API_KEY=…`. The error never prints the key. |
| `validate` reports judge errors on **every** record, all of them `reference answer: ProviderTransientError` | The top rung is out of quota. No reference answer means no comparison, so nothing is judged and nothing is invented. | Wait for the Pacific midnight reset, then re-run `validate` — it resumes. |
| `validate` reports judge errors on *some* records, mixed | Pacing, or an unparseable judge reply. Both are recorded per judgement in `judge_errors`. | Raise `--min-interval-ms`; if it persists, read the recorded error text. |
| `report` exits 2 with `INCONCLUSIVE` | Not a failure. Fewer than `[report] min_comparisons` comparisons produced a readable verdict. | Validate more, or accept that the month cannot say. |
| `report` exits 3 | The report could not be produced at all: bad config, unreadable ledger or an unparseable `regret.json`. | This one **is** a bug or a broken file. Read the message; it names the file. |

The distinction that matters: **exit 2 is an answer, exit 3 is a fault.** The
tool deliberately never turns "we could not tell" into "it is fine".

---

## 5. What to commit afterwards

The demo writes to `demo/`, which is gitignored. Three files are worth keeping,
and they go in [`docs/examples/`](examples/):

```bash
cp demo/report/$(date -u +%Y-%m)/report.md    docs/examples/report.live.md
cp demo/report/$(date -u +%Y-%m)/proposal.md  docs/examples/proposal.live.md
uv run python scripts/autopilot.py --config autopilot.demo.toml ledger summary \
  > docs/examples/summary.live.txt
```

Then **add a banner by hand** to the top of all three, saying what produced them:

```
LIVE — produced by a real run against gemini-3.5-flash-lite and gemini-3.6-flash
on <date>, using autopilot.demo.toml. <what completed and what did not>. This
banner is the only thing added by hand.
```

The tool only emits a banner of its own for `--dry-run`, because "synthetic" is
the claim that has to be impossible to miss. A live artifact has no such flag, so
the label is manual — and it must say which models, which config, which date and
**what did not finish**, because a demo ladder's saving is measured against Flash
rather than Pro and is not comparable to a figure from `autopilot.toml`.

The `report.md` files already carry a `## Provenance` block naming the
configuration and the ladder, so the two together leave nothing to guess at.

**Never edit the numbers.** If the run produced an ugly result, commit the ugly
result. The synthetic examples already in `docs/examples/` are labelled synthetic
on their first line for exactly this reason: a document that does not say where
its numbers came from is a document nobody can check.

---

## 6. What happened when this was run

Written on 2026-09-02 and **attempted the same day, on the same key that had
already run Phase B**. It did not complete, and the committed artifacts say so.

`route` recorded 12 ledger rows: **4 `ok`, 8 `failed`**. Every failure was a
`429` on `gemini-3.6-flash` — the day's 20 requests had already gone to the
Phase B run — so seven requests fell back from Flash-Lite to Flash and found
nothing there either, and both T3 requests, which start on Flash, failed
outright. One further failure was a `ProviderResponseError`, which correctly did
**not** fall back: a malformed reply is not fixed by a more expensive model.

Two requests were shadow-sampled. `validate` produced two verdict records with no
verdict at all, each carrying its error verbatim:

```
reference answer: ProviderTransientError: Model gemini-3.6-flash still failing
after 3 attempts: Transient provider failure (model gemini-3.6-flash,
status 429 RESOURCE_EXHAUSTED)

reference answer: ProviderResponseError: Provider call failed
(model gemini-3.6-flash, status 499 CANCELLED)
```

`report` then exited **2, INCONCLUSIVE**, with the reason "0 comparison(s)
produced a readable verdict", and recommended enabling billing for rung 1. That
is the whole system behaving correctly on a key that cannot afford the demo: no
regret figure was invented, no failure decayed into a success, and the ledger's
row count is still 12.

The artifacts are committed as
[`docs/examples/report.live.md`](examples/report.live.md),
[`proposal.live.md`](examples/proposal.live.md) and
[`summary.live.txt`](examples/summary.live.txt), each labelled LIVE and
incomplete.

**To get a complete run**, do one of:

- wait for the Pacific midnight reset and run the four commands as the first
  thing on the key that day — 12 Flash calls out of 20 is comfortable, but only
  from a full allowance;
- or enable billing on the key, in which case `autopilot.toml`'s three-rung
  ladder works directly and this demo configuration is unnecessary.
