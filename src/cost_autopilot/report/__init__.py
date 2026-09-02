"""Stage 04: turn one month of ledger rows and verdicts into a reviewable report.

Four modules, in the order the data moves through them:

  - `model` — the shapes. What a month's report *is*, what a recommendation is,
    and how both serialise. No arithmetic, no files.
  - `build` — the arithmetic. Ledger rows and stage 03's `regret.json` in, a
    `MonthReport` out. Pure: it opens nothing and calls nothing.
  - `rules` — the recommendations. Deterministic rules over the built report and
    the configuration, each naming its evidence and one specific action.
  - `proposal` — the `autopilot.toml` diff those recommendations imply, and the
    only code in this package that may write to that file — and only when a
    named human has passed `--approve`.
  - `render` — Markdown. Presentation only; it decides nothing.
  - `run` — the command body: read the month's files, write the four artifacts,
    return an exit code.

Two rules hold across all of them. **Regret is read, never recomputed** — stage
03 owns the definition and the intervals, and two stages computing the same
number differently is how a report starts disagreeing with the command that fed
it. And **nothing here changes routing**: the strongest thing stage 04 does on
its own is write a proposal file marked `awaiting_human_approval`.
"""
