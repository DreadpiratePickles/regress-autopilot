# Stage: 04_report

## Objective

Turn a month of ledger rows and stage 03's verdicts into one reviewable document:
what was spent, what was saved, what the saving cost in quality, what it cost to
find that out — and, where the evidence supports it, a specific change to
`autopilot.toml` written as a diff that a named human has to approve before
anything applies it.

The stage decides one word about the month — `SAFE`, `REGRET_TOO_HIGH` or
`INCONCLUSIVE` — and it is allowed to say `INCONCLUSIVE` far more often than
either of the others. A month whose sample cannot support a claim is a month
whose report says so.

## Inputs

| Path or source | Layer | Authority | Required | Relevant section |
|---|---:|---|---:|---|
| `ledger/<YYYY-MM>.jsonl` | 4 | Authoritative | Yes | Every row: spend, counterfactual, tier, status, `fallback_chain`, `chosen_model_id`. The whole spend half of the report |
| `validate/<YYYY-MM>/regret.json` | 4 | Authoritative | No | Regret already aggregated overall, per rung and per tier, with Wilson intervals, plus the sampling denominators and the validation cost. **Absent means `INCONCLUSIVE`, never "no regret"** |
| `validate/<YYYY-MM>/verdicts.jsonl` | 4 | Authoritative | No | Read only for the `request_id` consistency check against the ledger. The figures come from `regret.json` |
| `autopilot.toml` `[validate]` | 3 | Authoritative | Yes | `max_regret`, `min_samples`, `sample_percent`. The thresholds every verdict and every recommendation rests on |
| `autopilot.toml` `[report]` | 3 | Authoritative | No | `dir`, `min_comparisons`, `max_fallback_rate`. Optional, with documented defaults |
| `autopilot.toml` `[[ladder.rung]]`, `[policy]` | 3 | Authoritative | Yes | Which rung a tier starts on today, and what the rung above it is. A recommendation cannot name a rung it has not read |
| `src/cost_autopilot/config.py` | 3 | Authoritative | Yes | Resolves each rung's `model_ref`. The only module naming a model |
| `--month`, `--out`, `--dry-run`, `--config` (CLI) | 4 | Operator input | No | Which month, where to write, whether to label the artifacts synthetic, which configuration |
| A `proposal.json` from a previous run | 4 | Authoritative | Only for `apply-proposal` | The changes, their evidence, and the values the configuration held when they were computed |

The stage **cannot** see request text, answers, or the shadow file. It reports on
records, never on content. It also deliberately does not recompute regret: stage
03 owns that definition, and two stages computing the same number differently is
how a report starts disagreeing with the command that fed it.

No model is called by this stage, in either mode. `--dry-run` here means
something different from the other three commands: there is nothing to stub, so
the flag asserts that *the inputs came from a dry run*, and every artifact it
writes then carries a synthetic banner as its first line.

## Process

Every step is deterministic code. There is no step that calls out.

1. **Read the month.** Ledger rows, `regret.json` if it exists, verdict records
   if they exist. A ledger file that will not parse stops the run; a missing
   `regret.json` does not, because "this month was never validated" is a fact the
   report exists to state.
2. **Total the spend** with `ledger.summary.summarise` — the same function
   `ledger summary` prints, so the two commands cannot disagree — then break it
   down by team, by model and by tier, and separate the outcomes that mean
   different things: a fallback a dearer rung *answered* is evidence about
   routing, while a request that climbed the whole ladder and got nothing is
   evidence about reachability.
3. **Copy the quality figures across** from `regret.json`, and add the two ratios
   a report needs: what fraction of the month's cheap-routed requests was
   actually inspected, and what the validation cost was as a share of the saving.
   Each tier's one-sentence verdict comes from
   `validate.report.tier_verdict_line`, the same function `ledger summary` calls.
4. **Decide the month's verdict** by the same four-state logic applied to the
   overall group: below `min_comparisons` → `INCONCLUSIVE`; upper bound under
   `max_regret` → `SAFE`; zero regret observed but the interval too wide →
   `INCONCLUSIVE` with the shortfall computed; otherwise `REGRET_TOO_HIGH`.
5. **Run the recommendation rules.** One rule per verdict state, plus three
   operational ones (an unreachable ladder, an excessive fallback rate,
   unverified prices). Each names its evidence — n, the observed rate, the
   interval — and one specific action; where the action is a configuration
   change it also carries the section, key, current value and proposed value.
6. **Fold the config changes into a proposal**, one entry per key, merging
   duplicates toward the *higher* rung, and mark it `awaiting_human_approval`.
7. **Write four artifacts** and return the verdict's exit code.

`apply-proposal` is a separate command with its own gates; see Approval.

## Outputs

| Path | Schema or format | Consumer |
|---|---|---|
| `report/<YYYY-MM>/report.md` | Verdict and its reason; spend with team/model/tier breakdowns; outcomes; quality with the inspected fraction, the validation overhead and its share of the saving, regret per rung and per tier with intervals and verdict sentences; recommendations with evidence; a provenance block naming the configuration, the ladder and the thresholds | A human deciding whether to keep the current routing |
| `report/<YYYY-MM>/report.json` | The same figures, `schema_version` 1, mechanically projected from the same dataclasses | CI, and anything downstream |
| `report/<YYYY-MM>/proposal.md` | The `autopilot.toml` diff, the evidence per change, and the command that would apply it | The human who approves or rejects it |
| `report/<YYYY-MM>/proposal.json` | `schema_version` 1: `status`, `config_path`, `changes[]` (section, key, current, proposed, evidence, rule_ids), `approved_by`, `approved_utc`, `applied_utc` | `apply-proposal`, and the audit trail afterwards |
| stdout | The verdict, the headline numbers, every recommendation, and the paths written | The operator |
| Process exit code | `0` SAFE · `1` REGRET_TOO_HIGH · `2` INCONCLUSIVE · `3` could not run | CI, and the operator |

`report/` is gitignored: a rendered month is derived data. The copies worth
keeping are in `docs/examples/`, and each carries a banner on its first line
saying whether it came from a live run or a dry one.

**Exit 3 is a deviation from this repository's other commands**, which use 2 for
"the run never started". `report` needs 2 for `INCONCLUSIVE`, so its setup
failures move to 3 — project 1's convention, and the one this contract declared
in its stub. "We could not tell" and "the tool is broken" are different messages
to send a person, and collapsing them would make CI unable to distinguish them.

## Verify

- `uv run pytest -q` — 780 tests, none touching the network. The paths that
  matter here are covered directly: the four verdict wordings as exact strings;
  `clean_samples_needed` asserted to be the *smallest* n whose Wilson upper bound
  at zero regret clears the threshold, at five different thresholds, and `None`
  for the unreachable `max_regret = 0.0`; spend totals against rows added up by
  hand; a recovered fallback counted apart from an exhausted ladder; each
  recommendation rule against the committed `autopilot.toml` with one value
  substituted; the merge of two rules on one key resolving to the higher rung;
  the synthetic banner asserted to be line one of both Markdown artifacts; and
  every gate on `apply-proposal` — no `--approve`, no approver, an empty
  proposal, a different config file, a config that moved on, a second
  application, and a key this build may not edit.
- `uv run ruff check .` — clean at line-length 100.
- `uv run python scripts/autopilot.py report --dry-run` — writes four artifacts,
  makes no network call, and labels every one of them synthetic.
- After any run: every figure in `report.md` is recomputable from
  `ledger/<month>.jsonl` and `validate/<month>/regret.json` with a calculator,
  and `report.json`'s `verdict` matches the heading in `report.md`.
- A month with fewer than `[report] min_comparisons` readable verdicts reports
  `INCONCLUSIVE`, never a regret figure.
- No recommendation says "consider tuning". Each names a key and a value, or
  states plainly that no configuration change would help.

## Approval

**This stage proposes; a human disposes.** It is the last stage, so it is where
the temptation to close the loop lives, and the loop is deliberately left open.

- **Nothing here edits `autopilot.toml`.** `report` writes only into its own
  output directory. The proposal it writes is marked `awaiting_human_approval`
  and stays that way until somebody acts on it.
- **Applying a proposal requires `--approve --approved-by <name>`.** Both, every
  time; the approver's name and the timestamp are written back into
  `proposal.json` so the approval travels with the change rather than living in
  a shell history. Blocked without them, and blocked on a proposal that has
  already been applied.
- **The applier's surface is two keys.** `[policy] <TIER>` and `[validate]
  sample_percent`, both integers. A proposal naming anything else — a budget, a
  price, a model reference — is refused and must be made by hand in a reviewed
  diff. Widening this is itself a reviewed change.
- **A stale proposal is refused.** If the configuration no longer holds the
  value the proposal was computed against, the refusal names both values rather
  than overwriting whatever somebody changed in the meantime.
- **A regret figure still may not be quoted without calibration.** Stage 03's
  Approval section blocks that, and rendering the figure in a nicer format does
  not lift the block. Every recommendation that rests on regret inherits it.
- **Publishing a report outside the repository is not implemented.** There is no
  Slack sender, no PR comment, no webhook. The stage writes files in a gitignored
  directory and prints to stdout; getting a report in front of anybody else is a
  human copying it.

**No creator grades its own work.** Stage 02 produced the routing, stage 03
judged it, and this stage does arithmetic over both and calls no model at all.
What it cannot escape is that the evidence it reasons over came from a judge in
the same model family as the answers — stage 03's weakness, inherited whole.

## Failure Behavior

| Failure | Behavior |
|---|---|
| No `regret.json` for the month | `INCONCLUSIVE`, exit 2, with the spend section still rendered and the reason "the month has spend but no quality measurement". Never a saving presented as if the quality question were settled |
| Fewer than `[report] min_comparisons` readable verdicts | `INCONCLUSIVE`, exit 2, naming the count and the threshold |
| Zero regret observed but the interval too wide | `INCONCLUSIVE`, exit 2, and the recommendation is more sampling — never "route this tier up". The rate is zero; the sample is the problem |
| No ledger file for the month | An empty month: zero rows, `INCONCLUSIVE`, exit 2. Nothing to report is not a crash |
| A ledger row, verdict or regret file that will not parse | `LedgerError` / `VerdictError` / `RegretError` naming the file and line; exit 3. A partially readable month is refused rather than worked around, because it would silently change every denominator |
| Ledger and verdicts disagree on a `request_id` | The orphaned ids are named in the report's provenance block. The figures are still computed, and the reader is told the denominator cannot be checked against the ledger alone |
| `prices_verified = false` | Every monetary figure is labelled unverified — in the report document, in the JSON, and on stdout — and a `verify_prices` recommendation fires |
| A tier is regretful but already starts at the top rung | `ladder_exhausted`: advice, no proposed change. There is no more expensive rung to route to |
| `max_regret = 0.0` | No sample size can clear it. The verdict and the recommendation both say so rather than quoting an impossible sample count |
| `apply-proposal` without `--approve` or `--approved-by` | Refused, exit 2, `autopilot.toml` untouched |
| `apply-proposal` on an already-applied proposal | Refused, naming who applied it and when |
| `apply-proposal` when the config has changed since | Refused, naming the value in the file and the value the proposal expected |
| The rewritten configuration would not load | The temporary file is deleted and `autopilot.toml` is left exactly as it was. The proposal is not marked applied |
| The proposal names a key outside `[policy]` / `[validate] sample_percent` | Refused, naming what this build may apply |

Nothing in this stage retries, because nothing in it can fail transiently: there
is no network call to fail. Escalation path: a month that reports `INCONCLUSIVE`
run after run is not a reporting problem — it is `sample_percent` set too low for
the traffic, and the report's own recommendation says by how much.
