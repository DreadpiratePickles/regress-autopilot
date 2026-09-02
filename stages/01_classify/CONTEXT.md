# Stage: 01_classify

## Objective

Turn one request's text into a tier — `T1_TRIVIAL`, `T2_STANDARD` or
`T3_COMPLEX` — with a 0-100 complexity score and the list of rules that produced
it, using no information other than the text itself.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| The request text | 4 | Operator input | Yes | Whole string; the only evidence the stage may use |
| `autopilot.toml` | 3 | Authoritative | Yes | `[classifier]` — `t2_min_score`, `t3_min_score`, `llm_classifier` |
| `src/cost_autopilot/classify/features.py` | 3 | Authoritative | Yes | The feature vocabulary: verbs, constraint markers, multi-step markers |
| `src/cost_autopilot/classify/scorer.py` | 3 | Authoritative | Yes | `WEIGHTS`, `CAPS`, and the length bands |
| `src/cost_autopilot/classify/prompts/classify_v1.md` | 3 | Authoritative | Only when `llm_classifier = true` | Whole file: sent as the system prompt |
| `GEMINI_API_KEY` (`.env`, environment) | 3 | Authoritative | Only when `llm_classifier = true` | The provider credential; never logged |

The stage deliberately cannot see the team id, the workload's `expected_tier`,
the budget, or any history. A classifier that could see the expected answer
would not be measuring anything.

## Process

Steps 1-4 are deterministic code. Step 5 runs only when explicitly enabled.

1. Validate the text at the boundary: a string, non-blank after stripping, at
   most `MAX_TEXT_CHARS` (200,000). Anything else raises
   `FeatureExtractionError` before any work is done.
2. Extract features: character and word counts, question marks, numeric values,
   bullet lines, fenced code blocks, stack traces (Python traceback header,
   Python frame line, or JavaScript frame line), pipe or tab tables, task verbs
   from two fixed vocabularies, constraint markers, multi-step markers, an
   explicit output-length hint, and the share of non-ASCII characters as an
   integer per thousand.
3. Score: sum the weight of every feature that fired, applying the per-signal
   caps, and clamp into [0, 100]. Each term appends one reason of the form
   `"<what fired> (+N)"`; the weights in the reasons sum to the score, and the
   clamp appears as its own reason when it bites.
4. Map the score to a tier using the two inclusive lower bounds from
   `[classifier]`. A score of exactly `t2_min_score` is T2.
5. **Optional, off by default.** When `llm_classifier = true`, ask the cheapest
   rung for a tier instead. The request travels inside `<request>` delimiters as
   the user message, never formatted into the system prompt. The reply is parsed
   strictly — exactly the keys `tier` and `reason`, a known tier, a non-empty
   reason — and a reply that fails validation raises rather than falling back to
   a default tier. The rule-based score and reasons are still computed and still
   recorded alongside the model's tier.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `Classification` (in memory) | Frozen dataclass: `tier` (`Tier`), `complexity_score` (int 0-100), `reasons` (tuple of str), `features` (`RequestFeatures`), `decided_by` (`"rules"` or `"llm"`) | Stage 02, which routes on `tier` and copies `complexity_score`, `reasons` and `tier` onto the ledger row |
| stdout, via `autopilot classify --text` | Tier, score, `decided_by`, and one reason per line | A human checking why a request was routed the way it was |

The stage writes no file. Its output is consumed in memory by stage 02 and
persisted only as three fields on that stage's ledger row.

## Verify

- `uv run pytest -q tests/test_features.py tests/test_scorer.py
  tests/test_llm_classifier.py` — feature extraction against hand-built texts
  with exactly known counts, threshold behaviour at and either side of both
  boundaries, and strict parsing of the optional classifier's reply.
- `uv run ruff check .` — clean at line-length 100.
- `uv run python scripts/autopilot.py classify --text "What is the capital of
  Peru?"` prints `T1_TRIVIAL`, score 0, and at least one reason. It makes no
  network call and writes no ledger row.
- The arithmetic invariant is tested directly: the `+N` weights in `reasons` sum
  to `complexity_score`. A reason list that does not add up is decoration.
- **Accuracy is not verified by the test suite and is not claimed.** Measured
  against `workloads/mixed_v1.jsonl` on 2026-09-02, the rules agreed with the
  hand-assigned `expected_tier` on 26 of 30 requests. The four disagreements are
  named in `docs/design.md`. Stage 03 is what turns that number into evidence
  about answers rather than agreement about labels.

## Approval

No human gate. This stage spends nothing, writes nothing, and sends nothing.

What does require review is a change to its inputs: editing `WEIGHTS`, the verb
vocabularies, or the two thresholds in `autopilot.toml` changes which model
every future request is routed to, and therefore what the system costs. Such an
edit is a reviewed diff, and it invalidates comparisons against ledger rows
written before it. Turning `llm_classifier` on is the larger change: it makes
every classification a paid model call, and needs the same review as any
decision to spend.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Text is not a string, is blank, or exceeds 200,000 characters | `FeatureExtractionError` before any work; the request is never routed. CLI exit 2 |
| `[classifier]` missing, or a threshold outside 0-100 | `ConfigFileError` naming the key; nothing runs. CLI exit 2 |
| `t3_min_score < t2_min_score` | `ConfigFileError`; an incoherent band is refused rather than silently reordered |
| Optional classifier: reply is not JSON, has wrong keys, or names an unknown tier | `ClassifierParseError`. Never degrades into a default tier: "the classifier failed" and "the request is trivial" are different facts, and routing on the wrong one spends real money |
| Optional classifier: the provider call fails | The `ProviderError` propagates. The stage does not silently fall back to the rules, because a run that quietly changed classifier halfway through would make its own ledger rows incomparable |
| Prompt file missing (optional classifier only) | `FileNotFoundError`. Never falls back to an unguided classifier |

No cleanup or rollback is needed: the stage is pure and holds no state.
Escalation path: a request that will not classify is a malformed input, not a
routing problem — inspect the text before touching the thresholds.
