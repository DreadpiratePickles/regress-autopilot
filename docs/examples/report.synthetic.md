> **SYNTHETIC — produced by a dry run.** Every verdict below is a canned constant from an in-memory fake, not a model judgement, and every cost comes from fixed synthetic token counts. These numbers exercise the arithmetic and the pipeline; they measure nothing.

# Cost autopilot report — 2026-09

## Verdict: INCONCLUSIVE

5 comparison(s) produced a readable verdict, fewer than [report] min_comparisons 10. Absence of evidence is not evidence of no regret.

## Spend

| Measure | Value |
|---|---:|
| Requests recorded | 30 |
| Spend | $0.010461 |
| If every request had gone to the top rung | $0.024000 |
| Saving | $0.013539 (56%) |
| Tokens | 3000 in / 1500 out |

The saving is a subtraction, not a claim: every row carries what its own token counts would have cost on the top rung, so the figure is checkable row by row. It is an approximation in one direction only — the top model would have produced a different number of output tokens for the same prompt — and it says nothing at all about quality. That is the Quality section below.

| Spend by team | spend |
|---|---:|
| `demo` | $0.010461 |

| Spend by model | spend |
|---|---:|
| `gemini-3.1-pro-preview` | $0.005600 |
| `gemini-3.5-flash-lite` | $0.001705 |
| `gemini-3.6-flash` | $0.003156 |

| Spend by tier | spend |
|---|---:|
| `T1_TRIVIAL` | $0.001705 |
| `T2_STANDARD` | $0.003156 |
| `T3_COMPLEX` | $0.005600 |

| Requests by tier | requests |
|---|---:|
| `T1_TRIVIAL` | 11 |
| `T2_STANDARD` | 12 |
| `T3_COMPLEX` | 7 |

| Requests by status | requests |
|---|---:|
| `ok` | 30 |

### Outcomes

| Outcome | Count |
|---|---:|
| Tried more than one rung | 0 |
| …of which a dearer rung answered | 0 |
| Refused by a budget | 0 |
| Failed on every rung | 0 |
| …of which reached the top rung and still got nothing | 0 |

The two split lines matter for what to do next. A fallback a dearer rung answered is evidence about routing — the cheap rung was tried, paid for, and not enough. A request that climbed the whole ladder and still got nothing is evidence about *reachability*: on a free-tier key a paid-only model answers `429` with `limit: 0`, which is indistinguishable from a rate limit and is honestly recorded as a transient failure.

## Quality

Read from stage 03's `regret.json`, never recomputed here.

| Measure | Value |
|---|---:|
| Sample rate | 20% |
| Cheap-routed requests | 23 |
| Shadow records kept | 5 |
| Validated | 5 |
| Fraction of cheap-routed requests inspected | 21% |
| Comparisons with a readable verdict | 5 |
| Regret | 0 |
| 95% Wilson interval | [0.000, 0.434] |
| Position bias detected | 0 |
| Judge errors | 0 |

### What the measurement cost

| Leg | Cost |
|---|---:|
| Reference answers on the top rung | $0.004000 |
| Judge calls | $0.006510 |
| **Total validation overhead** | **$0.010510** (77% of the saving) |

Overhead is reported next to the saving rather than netted out of it, so a reader can do the subtraction themselves.

### Regret by rung

| Rung | n | Regret | 95% CI | Judge errors |
|---|---:|---:|---|---:|
| rung 0 | 4 | 0 | [0.000, 0.490] | 0 |
| rung 1 | 1 | 0 | [0.000, 0.793] | 0 |

### Regret by tier

| Tier | n | Regret | 95% CI | Verdict |
|---|---:|---:|---|---|
| `T1_TRIVIAL` | 4 | 0 | [0.000, 0.490] | insufficient evidence: 4 validated sample(s), fewer than min_samples 10 |
| `T2_STANDARD` | 1 | 0 | [0.000, 0.793] | insufficient evidence: 1 validated sample(s), fewer than min_samples 10 |

## Recommendations

Produced by deterministic rules over the figures above and the thresholds in `autopilot.toml`. Each one names its evidence and one specific action; none of them has been applied.

#### `raise_sample_percent` — T1_TRIVIAL

- **Evidence:** n=4, regret 0 (0%), 95% CI [0.000, 0.490]
- **Action:** T1_TRIVIAL has 4 comparison(s), fewer than min_samples 10. Reaching n~35 — the smallest sample whose interval clears max_regret 0.100 at zero regret — needs ~31 more comparison(s), which at the current 20% means about 175 cheap-routed T1_TRIVIAL request(s); this month had 11. Raise [validate] sample_percent from 20 to 100.
- **Config:** `[validate] sample_percent` 20 → 100

#### `raise_sample_percent` — T2_STANDARD

- **Evidence:** n=1, regret 0 (0%), 95% CI [0.000, 0.793]
- **Action:** T2_STANDARD has 1 comparison(s), fewer than min_samples 10. Reaching n~35 — the smallest sample whose interval clears max_regret 0.100 at zero regret — needs ~34 more comparison(s), which at the current 20% means about 175 cheap-routed T2_STANDARD request(s); this month had 12. Raise [validate] sample_percent from 20 to 100.
- **Config:** `[validate] sample_percent` 20 → 100

## Provenance

- Month: `2026-09`
- Generated: `2026-09-02T14:11:52Z`
- Verdict: `INCONCLUSIVE` (exit code 2)
- Configuration: `autopilot.toml`
- Ladder, cheapest first: `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.1-pro-preview`. The saving above is measured against the last of these, so a figure from one ladder is not comparable to a figure from another.
- Thresholds: `max_fallback_rate` = 0.2, `max_regret` = 0.1, `min_comparisons` = 10, `min_samples` = 10, `sample_percent` = 20
- Prices verified: `true`
- Report schema version: `1`
