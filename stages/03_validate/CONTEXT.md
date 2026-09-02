# Stage: 03_validate — PLANNED, NOT BUILT

> **Status: planned (Phase B).** No code in this repository implements this
> stage. Nothing currently validates an answer, and no output claims an answer
> was good enough. This contract is the specification the stage will be built
> to, written now so stage 02's ledger schema is designed for it rather than
> retrofitted.

## Objective

Judge each answer stage 02 produced against its request's plain-English
criteria, so that "the cheap model was good enough" becomes recorded evidence
instead of an assumption the whole system rests on.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Rows with `status: ok`; `request_sha256` joins a row to its workload request |
| `workloads/<name>.jsonl` | 3 | Authoritative | Yes | `criteria` per request: the standard an answer is judged against |
| The answers | 4 | Authoritative | Yes | **Not currently persisted.** Stage 02 keeps the answer in memory only; this stage needs a store, and adding one is the first task of Phase B |
| `autopilot.toml` | 3 | Authoritative | Yes | A new `[validate]` section: judge model reference, sample rate, temperature |
| `GEMINI_API_KEY` | 3 | Authoritative | Yes | Judge calls are real model calls |

## Process

1. Read the month's `ok` rows and join each to its workload request by
   `request_sha256`.
2. Select which rows to judge. Judging every request doubles the system's model
   spend, which would defeat its purpose, so a sample rate is configuration and
   the sampling rule is recorded with the results.
3. For each selected row, for each criterion, ask a judge model whether the
   answer satisfies that criterion. Reuse project 1's judging seam directly:
   `regression_detect.judge.criterion` already implements delimited inputs,
   strict `{"passed", "reason"}` parsing, and a `JudgeParseError` that never
   degrades into `passed=False`.
4. **Shadow re-run.** For a sampled subset, re-answer the same request on the
   top rung and judge that answer too. Without this there is nothing to compare
   against and "regret" has no meaning.
5. Write one validation record per judged criterion.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `validations/<YYYY-MM>.jsonl` | Per judged criterion: `request_id`, `request_sha256`, `criterion`, `passed`, `reason`, `judged_model_id`, `judge_model_id`, `is_shadow`, `judge_error` | Stage 04 |
| Process exit code | `0` all judged · `1` some judge calls failed · `2` bad configuration | CI |

## Verify

- Judge parsing tested against malformed replies; a judge failure is recorded as
  a judge failure, never as a failed criterion.
- Every judged `request_id` exists in the month's ledger.
- Judge calibration against a human's hand-graded sample, before any judge score
  is trusted — the same discipline project 1's `calibration.py` enforces.

## Approval

A human owns the verdict. They read a sample of judged answers against their
criteria and confirm the judge agrees with them before any regret figure derived
from these records is quoted. Blocked without that: changing the ladder's
`max_tier` ceilings or the classifier thresholds in response to these numbers.

No stage grades its own work: stage 02 produced these answers, and it is not
this stage. The judge model should come from a different family from the models
being judged where one is available.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Answer store missing for a row | Row is skipped and counted as unjudgeable; never counted as a pass |
| Judge reply unparseable | `JudgeParseError`, recorded as `judge_error`; the criterion has no verdict |
| Judge call fails after retries | Recorded per criterion; the run continues; exit 1 |
| Workload no longer holds a matching request | Recorded as unmatched. A ledger row whose criteria have changed underneath it is not comparable |
