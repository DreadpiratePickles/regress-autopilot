> **LIVE — produced by a real run, and it did not complete.** Routed against `gemini-3.5-flash-lite` with `gemini-3.6-flash` as the top rung, using `autopilot.demo.toml` and `workloads/demo_quota_v1.jsonl`, on 2026-09-02. 4 of 12 requests were answered; the other 8 exhausted the free tier's 20-requests-per-day allowance on `gemini-3.6-flash` and are recorded as `failed`. No reference answer could be obtained, so the month has **no regret figure** — that is the failure path working, not a measurement. This blockquote is the only line added by hand.

# Cost autopilot report — 2026-09

## Verdict: INCONCLUSIVE

0 comparison(s) produced a readable verdict, fewer than [report] min_comparisons 4. Absence of evidence is not evidence of no regret.

## Spend

| Measure | Value |
|---|---:|
| Requests recorded | 12 |
| Spend | $0.000267 |
| If every request had gone to the top rung | $0.000474 |
| Saving | $0.000207 (43%) |
| Tokens | 243 in / 77 out |

The saving is a subtraction, not a claim: every row carries what its own token counts would have cost on the top rung, so the figure is checkable row by row. It is an approximation in one direction only — the top model would have produced a different number of output tokens for the same prompt — and it says nothing at all about quality. That is the Quality section below.

| Spend by team | spend |
|---|---:|
| `demo` | $0.000267 |

| Spend by model | spend |
|---|---:|
| `gemini-3.5-flash-lite` | $0.000267 |

| Spend by tier | spend |
|---|---:|
| `T1_TRIVIAL` | $0.000094 |
| `T2_STANDARD` | $0.000173 |
| `T3_COMPLEX` | $0.000000 |

| Requests by tier | requests |
|---|---:|
| `T1_TRIVIAL` | 5 |
| `T2_STANDARD` | 5 |
| `T3_COMPLEX` | 2 |

| Requests by status | requests |
|---|---:|
| `failed` | 8 |
| `ok` | 4 |

### Outcomes

| Outcome | Count |
|---|---:|
| Tried more than one rung | 6 |
| …of which a dearer rung answered | 0 |
| Refused by a budget | 0 |
| Failed on every rung | 8 |
| …of which reached the top rung and still got nothing | 8 |

The two split lines matter for what to do next. A fallback a dearer rung answered is evidence about routing — the cheap rung was tried, paid for, and not enough. A request that climbed the whole ladder and still got nothing is evidence about *reachability*: on a free-tier key a paid-only model answers `429` with `limit: 0`, which is indistinguishable from a rate limit and is honestly recorded as a transient failure.

## Quality

Read from stage 03's `regret.json`, never recomputed here.

| Measure | Value |
|---|---:|
| Sample rate | 50% |
| Cheap-routed requests | 4 |
| Shadow records kept | 2 |
| Validated | 2 |
| Fraction of cheap-routed requests inspected | 50% |
| Comparisons with a readable verdict | 0 |
| Regret | 0 |
| 95% Wilson interval | [0.000, 1.000] |
| Position bias detected | 0 |
| Judge errors | 2 |

### What the measurement cost

| Leg | Cost |
|---|---:|
| Reference answers on the top rung | $0.000000 |
| Judge calls | $0.000000 |
| **Total validation overhead** | **$0.000000** (0% of the saving) |

Overhead is reported next to the saving rather than netted out of it, so a reader can do the subtraction themselves.

### Regret by rung

| Rung | n | Regret | 95% CI | Judge errors |
|---|---:|---:|---|---:|
| rung 0 | 0 | 0 | [0.000, 1.000] | 2 |

### Regret by tier

| Tier | n | Regret | 95% CI | Verdict |
|---|---:|---:|---|---|
| `T1_TRIVIAL` | 0 | 0 | [0.000, 1.000] | insufficient evidence: 0 validated sample(s), fewer than min_samples 4 |
| `T2_STANDARD` | 0 | 0 | [0.000, 1.000] | insufficient evidence: 0 validated sample(s), fewer than min_samples 4 |

## Recommendations

Produced by deterministic rules over the figures above and the thresholds in `autopilot.toml`. Each one names its evidence and one specific action; none of them has been applied.

#### `raise_sample_percent` — T1_TRIVIAL

- **Evidence:** n=0, regret 0 (0%), 95% CI [0.000, 1.000]
- **Action:** T1_TRIVIAL has 0 comparison(s), fewer than min_samples 4. Reaching n~35 — the smallest sample whose interval clears max_regret 0.100 at zero regret — needs ~35 more comparison(s), which at the current 50% means about 70 cheap-routed T1_TRIVIAL request(s); this month had 3. Raise [validate] sample_percent from 50 to 100.
- **Config:** `[validate] sample_percent` 50 → 100

#### `raise_sample_percent` — T2_STANDARD

- **Evidence:** n=0, regret 0 (0%), 95% CI [0.000, 1.000]
- **Action:** T2_STANDARD has 0 comparison(s), fewer than min_samples 4. Reaching n~35 — the smallest sample whose interval clears max_regret 0.100 at zero regret — needs ~35 more comparison(s), which at the current 50% means about 70 cheap-routed T2_STANDARD request(s); this month had 1. Raise [validate] sample_percent from 50 to 100.
- **Config:** `[validate] sample_percent` 50 → 100

#### `enable_billing` — T1_TRIVIAL

- **Evidence:** 2 T1_TRIVIAL request(s) recorded status=failed after trying every rung up to and including gemini-3.6-flash
- **Action:** enable billing for rung 1 (gemini-3.6-flash) — 2 T1_TRIVIAL request(s) failed with no reachable model. Read the vendor's error body before acting: a 429 carrying `limit: 0` means the model is paid-only and no amount of waiting will help, whereas a non-zero limit means a daily quota that resets. This system records both as a transient failure because the status code is genuinely the same. No configuration change is proposed either way: the fix is at the provider account, not in this file.
- **Config:** none — this is not a change a config file can make.

#### `enable_billing` — T2_STANDARD

- **Evidence:** 4 T2_STANDARD request(s) recorded status=failed after trying every rung up to and including gemini-3.6-flash
- **Action:** enable billing for rung 1 (gemini-3.6-flash) — 4 T2_STANDARD request(s) failed with no reachable model. Read the vendor's error body before acting: a 429 carrying `limit: 0` means the model is paid-only and no amount of waiting will help, whereas a non-zero limit means a daily quota that resets. This system records both as a transient failure because the status code is genuinely the same. No configuration change is proposed either way: the fix is at the provider account, not in this file.
- **Config:** none — this is not a change a config file can make.

#### `enable_billing` — T3_COMPLEX

- **Evidence:** 2 T3_COMPLEX request(s) recorded status=failed after trying every rung up to and including gemini-3.6-flash
- **Action:** enable billing for rung 1 (gemini-3.6-flash) — 2 T3_COMPLEX request(s) failed with no reachable model. Read the vendor's error body before acting: a 429 carrying `limit: 0` means the model is paid-only and no amount of waiting will help, whereas a non-zero limit means a daily quota that resets. This system records both as a transient failure because the status code is genuinely the same. No configuration change is proposed either way: the fix is at the provider account, not in this file.
- **Config:** none — this is not a change a config file can make.

## Provenance

- Month: `2026-09`
- Generated: `2026-09-02T14:11:22Z`
- Verdict: `INCONCLUSIVE` (exit code 2)
- Configuration: `autopilot.demo.toml`
- Ladder, cheapest first: `gemini-3.5-flash-lite`, `gemini-3.6-flash`. The saving above is measured against the last of these, so a figure from one ladder is not comparable to a figure from another.
- Thresholds: `max_fallback_rate` = 0.2, `max_regret` = 0.1, `min_comparisons` = 4, `min_samples` = 4, `sample_percent` = 50
- Prices verified: `true`
- Report schema version: `1`
