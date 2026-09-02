"""The `validate` command's body: read the shadow file, judge, write, report.

Kept out of `cli.py` so the command line stays a thin wiring layer, and kept out
of `runner.py` so the judging loop stays pure and file-free.

Two properties this module is responsible for and the runner is not:

  - **Resumable.** Each verdict is appended and marked done as it is produced,
    so a run stopped by a quota, a crash, or `--limit` can be finished later
    without re-paying for the requests already judged.
  - **Honest about the denominator.** The regret file records the month's total
    cheap-routed count from the ledger alongside the sample, so nobody can read
    a regret rate without seeing what fraction of traffic it was measured on.
"""

from collections.abc import Callable

from regression_detect.pacing import pace

from ..config_file import AutopilotConfig
from ..ledger.row import STATUS_OK
from ..ledger.store import LedgerStore, utc_timestamp
from ..money import format_micro_usd
from ..route.router import ProviderFactory
from .regret import build_report
from .report import write_report
from .runner import ValidationContext, validate_records
from .shadow import ShadowStore
from .verdicts import VerdictRecord, VerdictStore

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
"""Same convention as the other commands: 1 means the run completed and
something in it failed, which for this stage means a judge call did."""


class _Pacer:
    """Spread every model call in the run, not every record.

    One record costs `1 + 2 x criteria + 2` calls — one reference answer, each
    criterion judged on both answers, and the pair judged in both orders — so 9
    for a 3-criterion request and 3 for one with no criteria. Pacing per record
    would let a burst that size hit a per-minute quota together and come back as
    rate-limit errors that are honestly recorded and useless.
    """

    def __init__(self, interval_ms: int) -> None:
        self._interval_ms = interval_ms
        self._previous: float | None = None

    def __call__(self) -> None:
        self._previous = pace(self._previous, self._interval_ms)


def cheap_routed_count(store: LedgerStore, *, month: str, top_model_id: str) -> int:
    """Successful requests answered below the top rung: the sampled population."""
    return sum(
        1
        for row in store.iter_month(month)
        if row.status == STATUS_OK and row.chosen_model_id != top_model_id
    )


def run_validation(
    config: AutopilotConfig,
    provider_factory: ProviderFactory,
    *,
    month: str,
    interval_ms: int,
    limit: int | None,
    system_prompt: str,
    echo: Callable[[str], None],
) -> int:
    """Validate one month's shadow sample. Returns the process exit code."""
    shadow = ShadowStore(config.validate.shadow_directory)
    verdicts_store = VerdictStore(config.validate.directory)
    ledger = LedgerStore(config.ledger.directory)

    records = shadow.read_month(month)
    if not records:
        echo(
            f"No shadow records for {month} in {config.validate.shadow_directory}. "
            "Nothing to validate."
        )
        return EXIT_OK

    already = verdicts_store.done_ids(month)
    echo(
        f"Validating {month}: {len(records)} shadow record(s), "
        f"{len(already)} already validated."
    )
    echo(
        f"Reference answers on {config.ladder.top_rung.model_id}; "
        f"judge {config.validate.judge_model_id}."
    )

    context = ValidationContext(
        provider_factory=provider_factory,
        reference_rung=config.ladder.top_rung,
        judge_rung=config.judge_rung(),
        system_prompt=system_prompt,
        reference_temperature=config.run.temperature,
        pacer=_Pacer(interval_ms),
    )

    def persist(verdict: VerdictRecord) -> None:
        verdicts_store.append(verdict, month=month)
        verdicts_store.mark_done(verdict.request_id, month=month)
        state = {True: "regret", False: "ok", None: "no verdict"}[verdict.regret]
        echo(
            f"  {verdict.request_id:<40} {state:<10} "
            f"rung {verdict.rung_index}  {verdict.tier:<11} "
            f"cost {format_micro_usd(verdict.validation_cost_micro_usd)}"
            + ("  [position bias]" if verdict.position_bias_detected else "")
        )

    fresh = validate_records(
        records,
        context,
        limit=limit,
        skip_ids=already,
        on_verdict=persist,
    )

    everything = verdicts_store.read_month(month)
    report = build_report(
        everything,
        month=month,
        sample_percent=config.validate.sample_percent,
        cheap_routed_count=cheap_routed_count(
            ledger, month=month, top_model_id=config.ladder.top_rung.model_id
        ),
        shadow_record_count=len(records),
        generated_utc=utc_timestamp(),
    )
    path = write_report(verdicts_store.regret_path(month), report)

    judge_errors = report.overall.judge_error_count
    echo("")
    echo(f"{len(fresh)} newly validated, {len(everything)} validated in total.")
    echo(f"Regret {report.overall.regret_count} of {report.overall.n}; wrote {path}.")
    echo(
        f"Validation cost {format_micro_usd(report.validation_cost_micro_usd)} "
        "— overhead this month's saving has to survive."
    )
    if judge_errors:
        echo(f"{judge_errors} judge error(s); those requests have no verdict.")
    return EXIT_PARTIAL_FAILURE if judge_errors else EXIT_OK
