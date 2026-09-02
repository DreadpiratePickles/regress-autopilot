"""Monthly spend caps per team, checked before any model is called.

The check is deliberately conservative and deliberately cheap. It compares the
team's spend so far this month against its cap and refuses if the cap is already
reached — it does not try to predict what the pending request will cost, because
that cost depends on how many output tokens the model chooses to produce, which
is unknowable beforehand. A cap is therefore a floor on refusals, not a hard
ceiling on spend: the last permitted request can carry the total slightly past
the cap. Pretending otherwise would mean either estimating the cost (a guess in
the money column) or refusing requests that would have fit.

Being explicit about that is the point. A budget that silently truncated an
answer, or that quietly let spend run over without saying so, would be worse
than one whose exact semantics are written down.

The comparison is `spend >= cap`, on integers. At exactly the cap the team is
refused: a cap of five dollars means five dollars is the most it may spend, and
the request that would take it past that is the one to stop.
"""

from dataclasses import dataclass

from ..money import format_micro_usd, validate_micro_usd


class BudgetError(ValueError):
    """A budget configuration value is unusable."""


class BudgetExceededError(Exception):
    """The team has reached its monthly cap. The request is refused, not queued.

    Carries the numbers rather than only a sentence so the caller can put them
    on the ledger row without re-deriving them.
    """

    def __init__(self, *, team_id: str, month: str, spend_micro_usd: int, cap_micro_usd: int):
        self.team_id = team_id
        self.month = month
        self.spend_micro_usd = spend_micro_usd
        self.cap_micro_usd = cap_micro_usd
        super().__init__(
            f"team {team_id!r} has spent {format_micro_usd(spend_micro_usd)} of its "
            f"{format_micro_usd(cap_micro_usd)} cap for {month}; request refused"
        )


@dataclass(frozen=True)
class Budgets:
    """Per-team monthly caps in integer micro-USD, with a default for the rest."""

    default_monthly_cap_micro_usd: int
    teams: dict[str, int]

    def __post_init__(self) -> None:
        validate_micro_usd(
            self.default_monthly_cap_micro_usd, field="default_monthly_cap_micro_usd"
        )
        if not isinstance(self.teams, dict):
            raise BudgetError("budgets.teams must be a table of team id -> cap")
        for team_id, cap in self.teams.items():
            if not isinstance(team_id, str) or not team_id.strip():
                raise BudgetError(f"budget team id must be a non-empty string, got {team_id!r}")
            validate_micro_usd(cap, field=f"budgets.teams.{team_id}")

    def cap_for(self, team_id: str) -> int:
        """The cap this team is held to. An unlisted team gets the default.

        A default rather than a rejection because an unknown team id is far more
        likely to be a new team than an attack, and refusing every request from
        it would be a worse failure than holding it to a conservative cap. The
        cap still applies, so an unknown team can never spend without limit.
        """
        return self.teams.get(team_id, self.default_monthly_cap_micro_usd)


def check_budget(
    *, team_id: str, month: str, spend_micro_usd: int, budgets: Budgets
) -> int:
    """Raise if `team_id` has reached its cap for `month`; otherwise return the cap.

    Raises:
        BudgetExceededError: spend has reached or passed the cap.
    """
    spend = validate_micro_usd(spend_micro_usd, field="spend_micro_usd")
    cap = budgets.cap_for(team_id)
    if spend >= cap:
        raise BudgetExceededError(
            team_id=team_id, month=month, spend_micro_usd=spend, cap_micro_usd=cap
        )
    return cap
