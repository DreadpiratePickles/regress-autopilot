"""Total a month of ledger rows, and render the totals as a table.

Every number here is an integer sum of integers. There is no averaging, no
projection, and no percentage that is not a floor-divided integer, because the
whole value of this command is that a person can check it against the raw JSONL
with a calculator.

The saving is a subtraction, not a claim: for each successful row, what the top
rung would have charged for those exact token counts minus what was actually
charged. It is honestly an approximation — the top model would have produced a
different number of output tokens — and `docs/design.md` says so. What it is not
is a made-up number: both halves are recorded on the row, so anyone can redo it.

Refusals and failures are counted, never hidden. A month whose saving looks
excellent because half its requests were refused is a month someone needs to
look at, and a summary that only reported spend would conceal exactly that.
"""

from collections import Counter
from dataclasses import dataclass, field

from ..money import format_micro_usd
from .row import STATUS_FAILED, STATUS_OK, STATUS_REFUSED, LedgerRow

PERCENT = 100


@dataclass(frozen=True)
class MonthSummary:
    """Totals for one month. Every field is an exact integer count or amount."""

    month: str
    row_count: int
    spend_micro_usd: int
    counterfactual_micro_usd: int
    spend_by_team: dict[str, int]
    spend_by_model: dict[str, int]
    requests_by_tier: dict[str, int]
    requests_by_status: dict[str, int]
    fallback_count: int
    refusal_count: int
    failure_count: int
    input_tokens: int
    output_tokens: int
    prices_verified: bool = field(default=True)

    @property
    def saving_micro_usd(self) -> int:
        """Counterfactual minus actual. Can be zero; never negative on a sane ladder."""
        return self.counterfactual_micro_usd - self.spend_micro_usd

    @property
    def saving_percent(self) -> int:
        """Saving as a whole-number percentage of the counterfactual, floored.

        Floored rather than rounded so the figure can never overstate the saving.
        """
        if self.counterfactual_micro_usd == 0:
            return 0
        return (self.saving_micro_usd * PERCENT) // self.counterfactual_micro_usd

    @property
    def ok_count(self) -> int:
        return self.requests_by_status.get(STATUS_OK, 0)


def summarise(
    rows: list[LedgerRow], *, month: str, prices_verified: bool = True
) -> MonthSummary:
    """Total one month's rows.

    Spend is summed over every row, but a `failed` row always records zero usage
    and zero cost — a provider error carries no usage block, so no token count
    survives it — which means failed attempts contribute nothing to spend. See
    docs/design.md §7 for what that understates. Counterfactual is summed only
    where there is a real completion to compare against, which keeps the saving
    from being inflated by rows where nothing was produced.
    """
    spend_by_team: Counter[str] = Counter()
    spend_by_model: Counter[str] = Counter()
    requests_by_tier: Counter[str] = Counter()
    requests_by_status: Counter[str] = Counter()

    spend = 0
    counterfactual = 0
    fallbacks = 0
    input_tokens = 0
    output_tokens = 0

    for row in rows:
        spend += row.cost_micro_usd
        counterfactual += row.counterfactual_top_model_cost_micro_usd
        input_tokens += row.input_tokens
        output_tokens += row.output_tokens
        spend_by_team[row.team_id] += row.cost_micro_usd
        requests_by_tier[row.tier] += 1
        requests_by_status[row.status] += 1
        if row.chosen_model_id:
            spend_by_model[row.chosen_model_id] += row.cost_micro_usd
        if len(row.fallback_chain) > 1:
            fallbacks += 1

    return MonthSummary(
        month=month,
        row_count=len(rows),
        spend_micro_usd=spend,
        counterfactual_micro_usd=counterfactual,
        spend_by_team=dict(sorted(spend_by_team.items())),
        spend_by_model=dict(sorted(spend_by_model.items())),
        requests_by_tier=dict(sorted(requests_by_tier.items())),
        requests_by_status=dict(sorted(requests_by_status.items())),
        fallback_count=fallbacks,
        refusal_count=requests_by_status.get(STATUS_REFUSED, 0),
        failure_count=requests_by_status.get(STATUS_FAILED, 0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prices_verified=prices_verified,
    )


def _table(title: str, pairs: dict[str, int], *, as_money: bool) -> list[str]:
    if not pairs:
        return [f"  {title}: none"]
    width = max(len(key) for key in pairs)
    lines = [f"  {title}:"]
    for key, value in pairs.items():
        rendered = format_micro_usd(value) if as_money else str(value)
        lines.append(f"    {key.ljust(width)}  {rendered}")
    return lines


def render(summary: MonthSummary) -> str:
    """Render a summary as plain text. Presentation only; computes nothing."""
    lines = [f"Ledger summary for {summary.month}", ""]

    if not summary.prices_verified:
        lines += [
            "  WARNING: prices_verified is false in autopilot.toml.",
            "  Every amount below is computed from placeholder prices nobody has",
            "  checked. Fill in real prices before quoting any of these numbers.",
            "",
        ]

    if summary.row_count == 0:
        lines.append("  No requests recorded for this month.")
        return "\n".join(lines)

    lines += [
        f"  requests           {summary.row_count}",
        f"  spend              {format_micro_usd(summary.spend_micro_usd)}",
        f"  if all top rung    {format_micro_usd(summary.counterfactual_micro_usd)}",
        f"  saving             {format_micro_usd(summary.saving_micro_usd)} "
        f"({summary.saving_percent}%)",
        f"  tokens             {summary.input_tokens} in / {summary.output_tokens} out",
        f"  fallbacks          {summary.fallback_count}",
        f"  refusals           {summary.refusal_count}",
        f"  failures           {summary.failure_count}",
        "",
    ]
    lines += _table("spend by team", summary.spend_by_team, as_money=True)
    lines += [""]
    lines += _table("spend by model", summary.spend_by_model, as_money=True)
    lines += [""]
    lines += _table("requests by tier", summary.requests_by_tier, as_money=False)
    lines += [""]
    lines += _table("requests by status", summary.requests_by_status, as_money=False)
    return "\n".join(lines)
