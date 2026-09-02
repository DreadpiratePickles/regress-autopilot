# LLM Cost Autopilot

Most LLM spend is a routing mistake: a one-line question answered by the most
expensive model in the account. This routes each request to the cheapest rung of
a model ladder likely to answer it well, refuses requests that would break a
team's monthly budget, falls back a rung when a call fails, and appends one
audited line per request to a ledger. Every ledger row also records what the
request *would* have cost on the top rung, so the saving is a subtraction rather
than a claim. Money is integer micro-USD end to end — never a float.

Then it checks its own homework. A deterministic sample of the cheap answers is
re-answered on the top rung and both answers are judged against each other, which
turns "the cheap model was good enough" into **routing regret**: the share of
sampled cheap answers that were not good enough, with a 95% interval, per rung
and per tier. Validation costs money too, and that cost is reported next to the
saving rather than netted out of it.

Status: **Phase B** — stages 01 (classify), 02 (route) and 03 (validate) are
built. Stage 04 (report), which attributes regret back to the classifier rule
that caused it, is specified and not yet implemented.

## Install

```bash
uv sync
```

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). The provider seam and
pacing helper are reused from
[project 1](https://github.com/DreadpiratePickles/model-regression-detection),
pinned to commit `5c1fa8b`.

Live calls need a Gemini API key. Create a `.env` file in the repository root
containing:

```
GEMINI_API_KEY=<your key>
```

`.env` is gitignored and is never read into a log, an error message, or a ledger
row. Everything below works without a key if you pass `--dry-run`.

## The four commands

```bash
# What tier is this request, and why? No model is called.
uv run python scripts/autopilot.py classify --text "What is the capital of Peru?"

# Route one request, or a whole workload file, and append to the ledger.
uv run python scripts/autopilot.py route --team demo --text "Summarise this..."
uv run python scripts/autopilot.py route --workload workloads/mixed_v1.jsonl \
    --team demo --min-interval-ms 6500

# Was the cheap answer good enough? Re-answer the sample on the top rung and judge.
uv run python scripts/autopilot.py validate --min-interval-ms 6500

# What did it cost, what did it save, what was refused, what did it cost in quality?
uv run python scripts/autopilot.py ledger summary
```

Add `--dry-run` to `route` or `validate` to use fakes: no network call is made
and no API key is needed. A dry run of the demo workload on 2026-09-02 routed all
30 requests across all three rungs — 11 to the cheapest, 12 to the middle, 7 to
the top — with 0 refusals and 0 fallbacks. The tier the rules chose matched the
workload's hand-assigned `expected_tier` on 26 of 30 requests; the four
disagreements are named and left untuned in `docs/design.md`, with the reason.

Costs printed by a dry run come from fixed synthetic token counts, and its judge
verdicts are constants. They exercise the arithmetic and the pipeline; they are
not a measurement of anything, and the command says so before it prints them.

`validate` is resumable: each verdict is written and marked done as it is
produced, so a run stopped by a quota or by `--limit N` is finished by running it
again, and nothing is judged or paid for twice.

## What `validate` does

For each sampled request it re-asks the same question on the top rung — the call
the router declined to make — and then judges the two answers two ways:

- **Against the request's criteria**, where the workload supplied them, judging
  the cheap answer and the reference answer *separately*. Regret means the cheap
  answer failed something the reference answer delivered; a criterion both models
  fail is a fact about the criterion, not about the routing.
- **Pairwise**, always, because most real traffic has no criteria. The pair is
  judged twice with the labels swapped, and a cheap answer counts as sufficient
  only if it wins or ties in both orders. When the two orders contradict each
  other the verdict tracked the slot rather than the content: that is recorded as
  `position_bias_detected` and counted as insufficient.

Every rate comes with a Wilson 95% interval, and a tier is called `safe` only
when the interval's *upper* bound is under `[validate] max_regret`. Below
`[validate] min_samples` it says "insufficient evidence" instead of a number.
`ledger summary` prints one plain-English line per tier; it never edits
`autopilot.toml`.

## Privacy

`shadow/<YYYY-MM>.jsonl` is the only file in this system that holds customer
text. It holds the sampled fraction only, it is gitignored, it is written mode
`0600`, and it exists only while `[validate] enabled = true`. The ledger stays
text-free either way — it carries a SHA-256 and a `shadow_sampled` flag. Setting
`enabled = false` costs the quality measurement and nothing else.

## Prices

`autopilot.toml` carries `prices_verified = true`. The three rungs' prices were
read from Google's published Gemini API pricing page on 2026-09-02 and converted
to integer micro-USD per 1,000 tokens. Two caveats a spend number depends on:

- The Flash and Flash-Lite rungs are on promotional pricing that the page says
  rises on 2027-01-01. Re-read the page and update `autopilot.toml` before
  trusting a spend figure dated after that.
- The top rung is `gemini-3.1-pro-preview`. A `gemini-3.6-pro` does not appear
  on the pricing page. The id was confirmed real by the live run on 2026-09-02
  (the API answers `429`, not `404`), which also showed it has a **free-tier
  limit of zero requests**: reaching the top rung needs billing enabled. See
  `docs/design.md` §9 and §20 for the full note.

Prices are configuration, not code. If `prices_verified` is ever set to `false`,
`ledger summary` says so in its output, because a cost table nobody checked is a
number nobody should quote.

## What this does not tell you yet

- **The judge is not calibrated.** No human has hand-graded a sample and
  confirmed the judge agrees with them. Until that happens a regret figure is a
  measurement of what one model thinks of another model's answer, and
  `stages/03_validate/CONTEXT.md` blocks acting on it.
- **The default judge is the cheapest rung**, which is the same model family that
  produced most of the answers it is judging. Models prefer their own output, so
  this points toward *under*-reporting regret. `autopilot.toml` says so at the
  setting.
- **No live regret figure has been produced.** The first live run on 2026-09-02
  reached the cheapest rung 11 times and then exhausted the key's free-tier
  quota; the top rung has a free-tier limit of **zero** requests, so no reference
  answer could be obtained. `ledger summary` printed "no regret figure" rather
  than a zero, which is the failure path working — but it is not a measurement.
- **Stage 04 does not exist**, so nothing yet attributes regret back to the
  classifier rule that caused it.

## Verify

```bash
uv run pytest -q          # 625 tests, none touching the network
uv run ruff check .       # clean at line-length 100
```

Coverage is 97% of `src/cost_autopilot`.

## Layout

`CONTEXT.md` routes to the stage that owns a task; each `stages/0N_*/CONTEXT.md`
declares that stage's inputs, process, outputs, verification and failure
behaviour. `docs/design.md` explains why each decision was made — sections 1-12
are Phase A, 13-21 are Phase B.

`ledger/`, `shadow/` and `validate/` are gitignored: they are per-deployment
operational data, not source.
