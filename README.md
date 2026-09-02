# LLM Cost Autopilot

Most LLM spend is a routing mistake: a one-line question answered by the most
expensive model in the account. This routes each request to the cheapest rung of
a model ladder likely to answer it well, refuses requests that would break a
team's monthly budget, falls back a rung when a call fails, and appends one
audited line per request to a ledger. Every ledger row also records what the
request *would* have cost on the top rung, so the saving is a subtraction rather
than a claim. Money is integer micro-USD end to end — never a float.

Status: **Phase A** — stages 01 (classify) and 02 (route) are built. Stages 03
(validate) and 04 (report), which prove the cheap answer was actually good
enough, are specified and not yet implemented.

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

## The three commands

```bash
# What tier is this request, and why? No model is called.
uv run python scripts/autopilot.py classify --text "What is the capital of Peru?"

# Route one request, or a whole workload file, and append to the ledger.
uv run python scripts/autopilot.py route --team demo --text "Summarise this..."
uv run python scripts/autopilot.py route --workload workloads/mixed_v1.jsonl \
    --team demo --min-interval-ms 6500

# What did it cost, what did it save, what was refused?
uv run python scripts/autopilot.py ledger summary
```

Add `--dry-run` to `route` to use fakes: no network call is made and no API key
is needed. A dry run of the demo workload on 2026-09-02 routed all 30 requests
across all three rungs — 11 to the cheapest, 12 to the middle, 7 to the top —
with 0 refusals and 0 fallbacks. The tier the rules chose matched the workload's
hand-assigned `expected_tier` on 26 of 30 requests; the four disagreements are
named and left untuned in `docs/design.md`, with the reason.

Costs printed by a dry run come from fixed synthetic token counts. They exercise
the arithmetic; they are not a measurement of anything.

## Prices

`autopilot.toml` carries `prices_verified = true`. The three rungs' prices were
read from Google's published Gemini API pricing page on 2026-09-02 and converted
to integer micro-USD per 1,000 tokens. Two caveats a spend number depends on:

- The Flash and Flash-Lite rungs are on promotional pricing that the page says
  rises on 2027-01-01. Re-read the page and update `autopilot.toml` before
  trusting a spend figure dated after that.
- The top rung is `gemini-3.1-pro-preview`. A `gemini-3.6-pro` does not appear
  on the pricing page, and the SDK model list could not be queried without an
  API key. See `docs/design.md` for the full note.

Prices are configuration, not code. If `prices_verified` is ever set to `false`,
`ledger summary` says so in its output, because a cost table nobody checked is a
number nobody should quote.

## What this does not tell you yet

`ledger summary` reports a saving in **spend**. It reports nothing about whether
the cheap answers were good enough, because Phase A judges no answers — that is
stage 03, which is specified and not built. A cost figure without a matching
quality figure is half a number, and this README would rather say so than let
the other half be assumed.

## Verify

```bash
uv run pytest -q          # 395 tests, none touching the network
uv run ruff check .       # clean at line-length 100
```

Coverage is 95% of `src/cost_autopilot`.

## Layout

`CONTEXT.md` routes to the stage that owns a task; each `stages/0N_*/CONTEXT.md`
declares that stage's inputs, process, outputs, verification and failure
behaviour. `docs/design.md` explains why each decision was made.
