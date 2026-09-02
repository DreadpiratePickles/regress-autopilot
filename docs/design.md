# Design

Why this system is shaped the way it is. Every section names a decision, the
alternative that was rejected, and the reason. Read this before changing
behaviour; the code says what happens, this says why.

---

## 1. The problem, stated without mentioning models

A team is spending more than it needs to on a service it buys per unit of text.
Most of that spend is a routing mistake: work that a cheap unit could do is
being sent to an expensive one, because nobody decides per request and the
expensive one is the safe default.

Two things are needed to fix that, and the second is the hard one:

1. Send each request to the cheapest supplier likely to do it well.
2. **Prove the cheap answer was actually good enough**, so the saving is real
   rather than a quality cut nobody measured.

Phase A builds the first and the record-keeping the second needs. Phase B builds
the second. It matters that they are separate stages and that Phase A claims
nothing about quality: a cost tool that reports a saving without a matching
quality measurement is reporting half a number.

---

## 2. Money is integer micro-USD

**Decision.** Every amount is an `int` of micro-USD (millionths of a dollar),
named `*_micro_usd`, carrying an explicit `USD` currency. No float touches an
amount, at any point, including in rendering.

**Why not floats.** Binary floating point cannot represent most decimal
fractions exactly, so sums drift, and `a + b == b + a` stops being reliable at
scale. The rulebook forbids it for authoritative financial arithmetic, and this
is authoritative: the ledger is what a budget is enforced against.

**Why not cents.** A single cheap call costs a small fraction of a cent. In
cents, almost every call rounds to zero and the ledger totals nothing.
Micro-USD is fine enough that a one-token call has a non-zero cost, and coarse
enough that an integer holds a large organisation's monthly spend comfortably.

**Why round up.** `token_cost_micro_usd` computes `ceil(tokens × price / 1000)`
as `-(-numerator // 1000)`, never constructing the quotient. Rounding down would
make a million one-token calls free. A ledger that under-reports spend is worse
than no ledger, because it is believed.

**Why each leg ceils separately.** Input and output are priced differently, so
they are two charges, not one. Ceiling them separately also makes a row's cost
reproducible from the four fields on that row alone — anyone can check the
arithmetic without knowing the implementation.

---

## 3. Classification is deterministic rules, not a model

**Decision.** Tier is decided by a transparent weighted sum of features
extracted from the request text: length bands, code fences, stack traces,
tables, task verbs from two fixed vocabularies, constraint markers, multi-step
markers, question count, numeric density, an output-length hint, and non-ASCII
share. Each term that fires appends a reason carrying its own weight, and the
weights sum to the score.

**Why not a model.** Three reasons, in order of importance:

1. **It would defeat the purpose.** Classifying with a model spends a model call
   on every request, on a system whose entire purpose is to spend fewer model
   calls. The saving has to survive its own overhead.
2. **It would destroy the audit trail.** The routing decision is the thing this
   system exists to justify. A ledger row saying `T3_COMPLEX` because a model
   said so cannot be argued with. A row saying `contains a stack trace (+25),
   reasoning verb(s): debug (+14)` can be checked by eye, disputed, and fixed at
   the rule that got it wrong.
3. **It would be non-deterministic.** Two identical requests could route
   differently, which makes month-over-month comparison meaningless.

**Why not embeddings or a trained classifier.** Almost certainly more accurate.
Also a training set nobody has, a model to serve, and an explanation nobody can
read. The rulebook's rule applies: use a model only where judgment is genuinely
needed. Deciding that a message containing a Python traceback is hard is not
judgment; it is a regex.

**What this costs.** Accuracy. Measured against `workloads/mixed_v1.jsonl` on
2026-09-02, the rules agreed with the hand-assigned tier on **26 of 30**
requests. The four disagreements, with their scores against thresholds of 15/45:

| Request | Expected | Got | Score |
|---|---|---|---|
| `t12_draft_polite_email` | T2 | T1 | 14 (one point below the T2 boundary) |
| `t25_multistep_calculation` | T3 | T2 | 26 |
| `t28_tradeoff_caching` | T3 | T2 | 44 (one point below the T3 boundary) |
| `t29_capacity_planning` | T3 | T2 | 23 |

**These were not tuned away, deliberately.** Moving `t2_min_score` to 14 and
`t3_min_score` to 44 would score 28/30 on this file and would tell us nothing,
because it is fitting two thresholds to thirty examples chosen by the same
process that labelled them. Two of the four misses being exactly one point below
a boundary is itself the finding: it says the boundaries are roughly in the right
place and the score is roughly right, which is as much as thirty examples can
support. What would justify moving a threshold is stage 03 measuring that
requests like `t29` actually got worse answers on the middle rung — regret, not
label disagreement. Until then, tuning is guessing with extra steps.

**The escape hatch.** `llm_classifier = true` asks the cheapest model for a tier
instead. It is off by default and should stay off without a reason. It exists
because some requests genuinely are about meaning rather than surface features,
and because Phase B needs something to measure the rules against. Its reply is
parsed as strictly as project 1 parses a judge verdict, and a reply that fails
validation raises rather than defaulting to a tier — "the classifier failed" and
"the request is trivial" are different facts, and routing on the wrong one
spends real money.

**Ratios are integers per thousand, not floats.** `non_ascii_permille >= 200`
means the same thing on every machine and in every dump of a ledger row.
`>= 0.2` against a repeating binary fraction does not, at the boundary.

---

## 4. The ladder, and why the policy cannot override capability

**Decision.** A ladder is an ordered list of rungs, cheapest first, each with a
capability ceiling (`max_tier`). Routing takes the first rung whose ceiling
reaches the request's tier. A separate `[policy]` table gives a per-tier
*minimum* starting index, and the effective start is the later of the two.

**Why two mechanisms.** They answer different questions. The ladder answers
"which rungs *can* serve this tier" — a fact about capability. The policy answers
"which rungs should we bother trying" — an operational preference, e.g. "don't
try the middle rung for hard work, it usually falls back anyway". Making the
policy a *floor* rather than an override means an operator can express the
preference without being able to accidentally send complex work to a model the
ladder says cannot handle it.

**Two invariants, checked at load.** Rung indices are consecutive from zero, and
ceilings never decrease going up. Without the second, "the first capable rung"
would stop meaning "the cheapest capable rung", and the routing rule would
quietly be wrong rather than loudly rejected.

---

## 5. Budgets refuse at exactly the cap, and cannot be exact

**Decision.** Before any call, total the team's spend for the current UTC month
and refuse if `spend >= cap`. The refusal is a recorded outcome, not an
exception the caller sees: a `refused` ledger row costing zero, with
`error_type` and an empty `fallback_chain`.

**Why `>=` and not `>`.** A cap of five dollars means five dollars is the most
the team may spend. The request that would take it past that is the one to stop.
Off-by-one at a budget boundary is exactly the kind of thing that is argued
about later, so the rule is stated in the config file, in the docstring, and in
a test named after it.

**Why the cap is not a hard ceiling, and why that is admitted.** The cost of a
pending request cannot be known before the model answers — output token count is
the model's choice. So the last permitted request can carry the total slightly
past the cap. The alternatives were both worse: estimate the cost (a guess in the
money column, and the whole system's credibility rests on that column being
measured), or refuse anything that *might* exceed (refusing requests that would
have fitted). A cap is therefore a floor on refusals. Being explicit beats a
budget that silently ran over.

**Why an unknown team gets the default cap rather than a refusal.** An unknown
team id is far more likely to be a new team than an attack, and refusing all its
traffic is a worse failure than holding it to a conservative limit. It is still
capped, so it can never spend without limit.

---

## 6. Fallback is bounded by the ladder, and only on transient errors

**Decision.** On `ProviderTransientError` — and only that — step up one rung and
try once. At most one attempt per remaining rung, never wrapping around. A
`ProviderConfigError` or `ProviderResponseError` stops immediately.

**Why bounded by the ladder rather than a counter.** The bound is structural, so
it cannot drift out of step with the ladder's length when someone adds a rung.

**Why only transient errors.** A `ProviderTransientError` from this provider
means the vendor already retried three times with backoff and still failed;
another attempt on the same rung would just repeat it, and a different model is
the only useful move left. A rejected credential, by contrast, will be rejected
by the next rung too — falling back would spend money on a fault that has
nothing to do with the model. The rulebook's rule that repeating the same
failure is not progress is the same rule at a different altitude.

**Why fallback goes up and never down.** The cheaper rung already failed. There
is no cheaper option that is more likely to work.

---

## 7. The ledger, and the counterfactual on every row

**Decision.** One append-only JSON line per routed request, in
`ledger/<YYYY-MM>.jsonl`. Every row carries
`counterfactual_top_model_cost_micro_usd`: what those exact token counts would
have cost on the top rung.

**Why store the counterfactual per row rather than compute it later.** Prices
change. A saving computed months later against today's price table would silently
restate history. Storing both halves on the row makes the saving checkable
forever, by subtraction, by anyone.

**What the counterfactual honestly is.** An approximation. The top model would
have produced a *different* number of output tokens for the same prompt, so this
is not "what the request would have cost". It is exactly the claim the ledger
makes and no more: *these token counts, priced at the top rung's tariff*. That is
the comparison a finance reader wants, and the only one available without running
every request twice. Running every request twice is what stage 03's shadow
sampling does, on a subset, and that is where a stronger claim will come from.

**Why one row for every path.** `ok`, `refused` and `failed` are different facts
and none may decay into another. The row count is the request count, not the
success count — which is what makes a month with a suspiciously good saving
legible as "half of it was refused".

**Why the request text is not stored by default.** A ledger is read by finance
and operations people, copied into spreadsheets, and kept for years. Customer
text does not belong in it. Rows carry a SHA-256 instead, which is enough for
stage 03 to join an answer back to its request. `log_text = true` exists for
debugging and is a reviewed change.

**Why `O_APPEND` is enough, and where the limit is.** Each row is serialised,
encoded once, and written with a single `os.write` to a file opened `O_APPEND`.
POSIX guarantees such a write will not interleave with another process's, so
concurrent routers produce interleaved *rows*, never corrupted bytes within a
row. This is adequate because nothing is ever updated — only added. It is
explicitly **not** a queue and not a transactional database, and the rulebook's
warning against using a JSON file as either still stands. If this ever needs
atomic read-modify-write, it needs a real database.

**Why a schema version.** `ledger summary` refuses a version it does not
understand rather than adding up columns whose meaning may have changed.

---

## 8. Prices are configuration, with a verification flag

**Decision.** Prices live in `autopilot.toml` as integer micro-USD per 1,000
tokens, alongside `prices_verified`, `prices_source` and `prices_read_utc`. If
`prices_verified` is false, every total printed by `ledger summary` carries a
warning.

**Why the flag.** A cost table nobody checked produces numbers nobody should
quote — but the code cannot tell the difference between a checked price and a
plausible-looking invention. So the human states it, and the statement travels
with the number all the way to the screen. Inventing a price and presenting it as
real is the specific failure this guards against.

**What was actually done.** The three rungs' prices were read from Google's
published Gemini API pricing page on 2026-09-02 and converted to integer
micro-USD per 1k tokens, so the flag is `true`. Two caveats are recorded in the
config file itself: the Flash and Flash-Lite rungs are on promotional pricing the
page says rises on 2027-01-01, and the top rung is a preview model.

---

## 9. The top rung is `gemini-3.1-pro-preview`, and that is a compromise

**The brief asked for** `gemini-3.6-pro` as the top rung, verified against the
SDK's model list at runtime.

**What was found.** No 3.6-generation Pro model appears on the published pricing
page. The Pro entry that does appear is `gemini-3.1-pro-preview`, at $2.00 / $12.00
per 1M tokens. The runtime check could not be run when this was written: the
repository had no `GEMINI_API_KEY`, and the SDK model list requires one.

**Update, 2026-09-02, from the first live run.** A key is now present and all
three ids were called. **The id is real**: the API answers `429
RESOURCE_EXHAUSTED`, not `404 NOT_FOUND`, and names the model
`gemini-3.1-pro` in its quota metric. What it also revealed is that the free tier
grants this model `limit: 0` requests — it is a paid-only model, so a free-tier
key can never reach the top rung. The other two ids are real and reachable, at
free-tier daily limits of 500 (`gemini-3.5-flash-lite`) and 20
(`gemini-3.6-flash`) requests. The prices are unchanged; what is now known is
that the top rung needs billing enabled, which is an operational fact worth
knowing before anyone plans a validation run.

**Decision.** Use `gemini-3.1-pro-preview` as the top rung, with three rungs
rather than two, and say plainly in `config.py`, `autopilot.toml` and the README
that the id was not verified at runtime and that the pricing page lists no 3.6
Pro. A preview id can be withdrawn; a 404 from this rung is a one-line change in
`config.py` plus the matching price, in one commit.

**Why not fall back to two rungs.** A two-rung ladder would make the
counterfactual the middle model, which understates the saving and removes the
fallback of last resort. A real, currently-priced Pro model is a better top rung
than no top rung — provided nothing claims a verification that did not happen.

**Why model ids live only in `config.py`.** A model id is a fact about the
outside world that changes without warning. `autopilot.toml` names rungs by
`model_ref` — the name of an environment variable — so committed configuration
holds no vendor strings and a deployment can repoint a rung without a commit.
This is project 1's convention, kept deliberately.

---

## 10. Reusing project 1, and why the seam had to be widened

**Decision.** Depend on `regression-detect` pinned to commit `5c1fa8b`, and
import from it: the `Provider` protocol's typed error hierarchy
(`ProviderError`, `ProviderConfigError`, `ProviderResponseError`,
`ProviderTransientError`), the Gemini retry constants (`MAX_ATTEMPTS`,
`REQUEST_TIMEOUT_MS`, `RETRYABLE_STATUS_CODES`, the backoff bounds), and
`pacing`.

**Why a pinned commit rather than a branch.** A routing decision recorded in the
ledger is only reproducible if the code that made it is.

**Why the seam had to be widened anyway.** `Provider.complete` returns a `str`.
That is the right shape for a tool that grades answers and the wrong shape for
one that prices them: `response.usage_metadata` is dropped, and a call whose
token counts were never seen cannot be charged for. Estimating them from
character counts would put a guess in the money column.

So this package defines `MeteredProvider`, whose `complete` returns a
`Completion(text, input_tokens, output_tokens, model_id, latency_ms)`. It does
not modify project 1 — project 1 is correct for its own job. The error types are
re-exported from `providers/metered.py` so no call site here imports project 1
directly; if its names ever change, one file needs fixing.

`GeminiMeteredProvider` reimplements the call loop (it needs the response
object, not just its text) but imports the retry policy rather than restating it,
so the two projects cannot drift on what counts as transient.

**One thing found while doing this:** reasoning tokens are reported separately
(`thoughts_token_count`) from the visible answer but are billed as output.
Charging only `candidates_token_count` would understate the bill on exactly the
requests that cost the most. Both are counted.

---

## 11. What Phase A does not claim

Stated explicitly because the temptation to over-claim is the main hazard of a
cost tool:

- **It does not know whether any answer was good.** No answer is judged, and
  `ledger summary` reports no quality figure. The saving it prints is a saving
  in spend, with the quality question open. That is stage 03's job.
- **It does not verify the model ids exist.** A green test run proves the routing
  logic, not the vendor. *(Superseded 2026-09-02: the first live run called all
  three and they exist — see §9's update. The test suite still proves nothing
  about the vendor, which is the point of this line.)*
- **The prices are read, not audited.** They come from the vendor's public page
  on a stated date, and two of the three are promotional.
- **The classifier is 26/30 against one 30-request file.** That is a weak
  measurement on a small sample that the same process both wrote and labelled.
  It is not an accuracy claim.

---

# Phase B — stage 03, `validate`

Phase A ends with a saving in spend and an open question about quality. This is
the answer to that question, and the reason the whole tool is worth anything: a
router that saves 72% by sending everything to the cheapest model is trivial to
write and impossible to defend. What is hard is knowing what the 72% cost.

---

## 13. Regret, and why it is the number

**Decision.** The headline output of stage 03 is **routing regret**: the fraction
of *sampled cheap-routed requests whose cheap answer was insufficient*, reported
with a Wilson 95% interval, broken down by rung and by tier.

**Why not "pass rate".** A pass rate against criteria measures whether the answer
was good. That is the wrong question. The question a routing decision has to
answer is *comparative*: would a better model have done better here? A request
that no model can answer well is not a routing mistake, and counting it as one
would make regret track task difficulty rather than routing quality. So every
judgement in this stage is a comparison against a reference answer, and a
criterion both models fail contributes nothing.

**Why intervals on everything.** Six samples with one regret is a rate of 17%
and a 95% interval from 3% to 56%. Quoting the 17% alone presents a coin toss as
a measurement. `wilson_interval` is imported from project 1 rather than
reimplemented, for the same reason its retry policy is: two projects that compute
"how sure are we" differently will eventually disagree about the same data.

**Why the *upper* bound decides the verdict.** A tier is called `safe` only when
the interval's upper bound is below `max_regret`. A point estimate of zero on
twelve samples has an upper bound around 0.24 and is therefore not safe at a 10%
threshold. That is the honest reading, and refusing to say "safe" on twelve
samples is the whole point of using an interval.

**The sharp edge that follows, named rather than smoothed over.** The verdict has
three states — `safe`, `insufficient evidence`, `regret too high` — checked in
that order. With `min_samples = 10` and `max_regret = 0.10` there is a band
between about n = 10 and n = 35 where a tier has *enough* samples to escape
"insufficient evidence" but not enough for a zero rate to clear the threshold, so
it reports **"regret too high — consider routing this tier up" while its observed
regret is zero**. The statement is true (the bound really is above the
threshold), but it reads as an accusation the data does not support.

This was left as specified rather than silently given a fourth state, because
three verdicts a reader can hold in their head is worth more than a taxonomy that
is exactly right. Two things make it survivable: the line immediately above the
verdict prints the observed count, the rate and the interval, so the distinction
is visible on screen; and the config file says so at `min_samples`, with the
arithmetic and the two ways to fix it. Anyone whose deployment lives in that band
should raise `min_samples` toward 35.

---

## 14. Shadow sampling, and why it is a hash

**Decision.** After a successful completion on any rung *below the top*, the
router keeps the request, the answer and the workload's criteria in
`shadow/<YYYY-MM>.jsonl` when `sha256(request_id) mod 100 < sample_percent`.

**Why sample at all.** Validating every request costs about one top-rung call
plus four judge calls per request — several times the spend the routing saved.
A tool whose measurement costs more than the thing it measures is a tool nobody
runs. `sample_percent` is directly a bill, and it is in the config file rather
than the code so that it is a reviewed number.

**Why a hash and not a random draw.** Three reasons, and the third is the one
that matters:

1. The same request is always sampled or always skipped, so a re-run of a
   workload inspects the same requests and two months are comparable.
2. Nothing has to be stored to remember what was sampled; the decision is
   recomputable from the id by anyone, forever.
3. **A random draw could correlate with the outcome.** A draw taken after the
   model answered — which is where it would naturally sit, since only successful
   calls are sampled — could favour the calls that went well. Regret computed
   from a biased sample is worse than no regret figure, because it would be
   believed. A hash of an id assigned before the call cannot know the outcome.

`sha256` rather than Python's `hash()`: `hash()` is salted per process, so the
same id would be sampled in one run and skipped in the next.

**Why `bucket < percent` rather than a modulo band.** Raising the rate can only
*add* requests to the sample; it never swaps one set for another. Two months at
different rates therefore still overlap on the lower rate's sample.

**Why the top rung is never sampled.** Its reference answer would come from the
rung that already answered it. There would be nothing to compare.

**Why this is the one place text is stored, and why it is opt-in.** The ledger
is read by finance people, copied into spreadsheets and kept for years, and
customer text does not belong in it — that rule from Phase A is unchanged.
Judging an answer, however, is impossible without the answer and the question.
So the text lives in exactly one file, for a configured fraction of traffic,
gitignored, mode `0600`, and behind `[validate] enabled`. Turning it off costs
the quality measurement and nothing else, and `ledger summary` then reports
spend with the quality question openly unanswered — which is Phase A's honest
position, not a degraded one.

The ledger row gained one field, `shadow_sampled`, so a reader of the ledger
alone can see which fraction of the month has evidence behind it. It is a flag,
not the data.

---

## 15. Re-answering on the top rung, and what it costs

**Decision.** For every sampled request, call the top rung with the same request
and the same system prompt at the same temperature, and price it at the top
rung's tariff.

**Why re-answer rather than reuse the counterfactual.** Phase A's counterfactual
is *these token counts at the top rung's price* — an arithmetic claim, deliberately
weaker than "what the request would have cost". It says nothing at all about
quality. Only an actual top-rung answer can support the statement "a better model
would have delivered this and the cheap one did not".

**Why the top rung specifically.** It is the model the router chose not to use.
Regret is defined against the alternative that was declined, not against a
hypothetical best.

**Why the cost is reported rather than netted off.** Validation is overhead, and
the honest place for overhead is next to the saving it qualifies, in the same
units, so a reader can do the subtraction themselves. Hiding it would be exactly
the accounting mistake this system exists to catch, one level up. `regret.json`
splits it into reference and judge legs; `ledger summary` prints the total.

**Why the judge must be on the ladder.** `judge_model_ref` is refused at config
load if it resolves to a model with no rung. A judge call this system could not
price would make the reported overhead a guess, and a guess in the money column
is the one thing this system may not produce.

---

## 16. Two judges, because one question is not enough

**Decision.** Every sampled request is judged twice over: against its criteria
where it has them, and pairwise against the reference answer always. Regret is
either signal firing.

**Why criteria.** They are specific, auditable, and written by a human before the
answer existed. "States that the capital of Peru is Lima" is a claim anybody can
check, and a per-criterion verdict tells you *what* was lost, not just that
something was. Project 1's `judge_criterion` already implements the delimited
inputs and the strict `{"passed", "reason"}` parser, so it is imported rather
than rewritten.

**Why both answers are judged against each criterion.** Judging only the cheap
answer measures difficulty, not routing. Regret is `cheap failed AND reference
passed` — the cheap model lost something a better model actually delivered. A
criterion both models fail is a fact about the criterion.

**Why pairwise as well.** Most real traffic has no criteria attached, and never
will. Criteria judging alone would make this stage a workload-file feature rather
than a production measurement. The pairwise question — did routing this cheaper
cost the user anything — needs nothing but the two answers.

**Why regret is `either`, not `both`.** The two judges answer different questions
and a failure of either is a real loss. Requiring both to agree would define
regret as the intersection of two conservative measures, which is a measure that
almost never fires — which is how a quality metric becomes decorative.

---

## 17. Swapped-order pairwise, and the bias it does and does not catch

**Decision.** The pairwise comparison is run twice, once with the cheap answer as
A and once with the reference as A, and the two results are folded by a truth
table. The cheap answer is sufficient only if it wins or ties in both orders.
A contradiction is `position_bias_detected` and counts as regret.

**Why twice.** A judge asked "is A at least as good as B" does not answer only
about the answers; part of its verdict is about the slot. Position bias in
pairwise LLM judging is well documented and is large enough to invent or erase a
difference on its own. One extra call per pair is the cheapest defence available,
and it converts a bias that would silently skew every verdict into a flag on the
specific verdicts it touched.

**The truth table**, where "forward" puts the cheap answer in slot A and
"reverse" puts the reference there, and "at least as good" includes "equally
good":

| forward | reverse | reading | verdict |
|---|---|---|---|
| true | true | both runs picked slot A; only a tie satisfies both claims | sufficient |
| true | false | the cheap answer, in both orders | sufficient |
| false | true | the reference, in both orders | **regret** |
| false | false | each run picked the *other* slot: the verdict tracked position | **regret**, flagged |

**Why the contradiction counts as regret rather than being discarded.** A
judgement this system could not read is not evidence that the cheap answer was
fine. Dropping contradictions would quietly remove the hardest cases from the
denominator, which is the direction that flatters the result.

**The blind spot, stated rather than glossed.** A judge that *uniformly* favours
slot A produces `(true, true)`, which is indistinguishable from a genuine tie and
is read as one. The swap catches contradictions, not a consistent slot
preference. What would catch that is calibration against a hand-graded sample —
which stage 03's contract requires of a human before any regret figure from it is
quoted, and which has not been done.

---

## 18. Why regret is conservative in every direction it can be

Each of these choices makes the reported regret rate the same or higher, never
lower. That is deliberate: this number exists to stop somebody claiming a saving
they did not make, so every judgement call goes against the claim.

- A record with **no readable verdict is excluded from `n`**, not counted as a
  pass. A broken judge shrinks the sample rather than improving the rate.
- A **parse failure is a judge error**, never `passed=False` and never
  `passed=True`. Project 1's rule, unchanged.
- A **pairwise contradiction counts as insufficient**.
- Regret is **either** signal, not both.
- The verdict compares `max_regret` against the **Wilson upper bound**, so a low
  rate on a small sample cannot pass as safe.
- Below `min_samples` a tier reports **"insufficient evidence"** rather than a
  rate. Absence of evidence is not evidence of no regret.

The one place it is *not* conservative is the judge itself: the default
`judge_model_ref` is the cheapest rung, the same family that produced most of the
answers, and models prefer their own output. That points the wrong way — toward
under-reporting regret — and is written into `autopilot.toml` next to the setting
so nobody has to find it here.

---

## 19. What Phase B does not claim

- **The judge is not calibrated.** No human has hand-graded a sample and checked
  the judge agrees with them. Until that happens, a regret figure is a
  measurement of what one model thinks of another model's answer, and the stage's
  Approval section blocks acting on it.
- **The sample is a sample.** `regret.json` carries `sample_percent` and the
  month's total cheap-routed count precisely so nobody reads a rate without
  seeing the fraction it came from.
- **No live regret figure has been produced.** The first live run reached the
  cheapest rung 11 times and then exhausted the key's free-tier quota; the top
  rung has a free-tier limit of zero requests, so no reference answer could be
  obtained and the month reports no regret figure at all. That is the failure
  path working — `ledger summary` says "no regret figure" rather than printing a
  zero — but it is not a measurement, and this document will not present it as
  one.
- **Nothing here changes routing.** The summary prints "consider routing this
  tier up". A human writes the diff.

---

## 20. One thing the live run taught: a `429` is not always transient

The first live run failed 18 of 30 requests with `ProviderTransientError`. The
diagnosis is worth keeping.

The vendor reports three different situations with the same HTTP status:

- a genuine per-minute rate limit, which retrying does fix;
- a per-day quota that is exhausted, which retrying does not fix today;
- **an entitlement the key does not have at all** — `gemini-3.1-pro` returns
  `429 RESOURCE_EXHAUSTED` with `limit: 0` on the free tier, meaning the model is
  paid-only and no amount of waiting will ever help.

The error taxonomy inherited from project 1 maps `429` to
`ProviderTransientError`, so the third case is retried three times with backoff
and then fallen back into on every single request. The behaviour is correct given
the information available — the status code genuinely is indistinguishable — and
the system recorded it honestly: 18 `failed` rows, each naming the full fallback
chain and the error type, and a summary that reports 12 requests rather than
pretending 30.

**Not changed, and why.** Parsing the vendor's quota metric out of an error
message to reclassify `limit: 0` as a config error would put vendor-specific
string matching inside the error taxonomy, and it would break the moment the
message wording changes. The honest fix is operational, and it is now written
down in the stage's failure table: when every request to one rung fails
transiently, read the quota metric in the vendor's error before assuming the
model is unhealthy.

---

## 21. What Phase C adds

**Phase C (stage 04).** Attribute regret to a rule. Every ledger row carries the
`reasons` that chose its rung, and every verdict record carries the request id
that joins back to it, so regret can be grouped by which classifier rule fired —
turning "routing is too aggressive" into "the `words_medium` band is sending
60-word debugging requests to the middle rung", which is a change somebody can
make. It also renders the month as a report, and emits `INCONCLUSIVE` rather than
a number when there are too few comparisons.

The schemas were designed for it: `reasons`, `complexity_score`,
`request_sha256`, `shadow_sampled` and the counterfactual are on the ledger row,
and `rung_index`, `tier` and the per-criterion verdicts are on the validation
record, so Phase C is an addition rather than a migration.
