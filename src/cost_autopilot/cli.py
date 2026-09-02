"""The command line: `classify`, `route`, and `ledger summary`.

The logic lives in the packages this imports; this module only wires arguments
to it and turns outcomes into exit codes and printed text, so the test suite can
exercise `main()` directly without a subprocess.

Three rules hold across every subcommand:

  - `--dry-run` never touches the network. It substitutes fakes at the provider
    factory, which is the single seam every model call passes through, so there
    is no path by which a dry run can reach a vendor.
  - A run that could not complete exits non-zero. Partial failure is visible in
    the counts, in the ledger rows, and in the exit code.
  - Nothing prints a credential, and nothing prints request text that the
    configuration said not to log.
"""

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from regression_detect.pacing import pace, validate_interval

from .classify.scorer import classify_text
from .config_file import AutopilotConfig, ConfigFileError, load_config
from .ledger.row import STATUS_OK, LedgerRow
from .ledger.store import LedgerError, LedgerStore, month_key, validate_month_key
from .ledger.summary import render, summarise
from .providers.fake_metered import FakeProviderFactory
from .providers.metered import MeteredProvider, ProviderError
from .route.router import ProviderFactory, Router, RoutingError
from .workload import WorkloadError, load_workload

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_BAD_CONFIG = 2
"""Same convention as project 1: 0 clean, 1 something failed but the run
completed and was recorded, 2 the run never started."""

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's request directly and "
    "completely, and do not add commentary about how you answered it."
)


def build_provider_factory(*, dry_run: bool) -> ProviderFactory:
    """The one place a real provider is chosen over a fake.

    Imported lazily so `--dry-run` and `classify` never import the vendor SDK,
    and a machine with no key can still run them.
    """
    if dry_run:
        return FakeProviderFactory("This is a dry-run answer. No model was called.")

    from .providers.gemini_metered import gemini_metered_provider_from_env

    cache: dict[str, MeteredProvider] = {}

    def factory(model_id: str) -> MeteredProvider:
        if model_id not in cache:
            cache[model_id] = gemini_metered_provider_from_env(model_id)
        return cache[model_id]

    return factory


def build_router(config: AutopilotConfig, *, dry_run: bool, store: LedgerStore) -> Router:
    """Assemble stage 02 from validated configuration."""
    return Router(
        ladder=config.ladder,
        policy=config.policy,
        budgets=config.budgets,
        thresholds=config.classifier.thresholds,
        provider_factory=build_provider_factory(dry_run=dry_run),
        read_spend_micro_usd=lambda team_id, month: store.spend_micro_usd(
            team_id=team_id, month=month
        ),
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        log_text=config.ledger.log_text,
        temperature=config.run.temperature,
    )


def _print_row(row: LedgerRow, *, label: str, echo: Callable[[str], None]) -> None:
    echo(
        f"{label}  {row.status:<8} tier={row.tier:<11} "
        f"model={row.chosen_model_id or '-':<24} "
        f"cost={row.cost_micro_usd} micro-USD  "
        f"counterfactual={row.counterfactual_top_model_cost_micro_usd}"
    )
    if row.status != STATUS_OK and row.error_type:
        echo(f"          error: {row.error_type}")


def command_classify(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    """Print a tier and the rules that produced it. No model is ever called."""
    config = load_config(args.config)
    result = classify_text(args.text, config.classifier.thresholds)
    echo(f"tier              {result.tier.value}")
    echo(f"complexity_score  {result.complexity_score}")
    echo(f"decided_by        {result.decided_by}")
    echo("reasons:")
    for reason in result.reasons:
        echo(f"  - {reason}")
    return EXIT_OK


def _route_one(
    router: Router, store: LedgerStore, *, text: str, team_id: str
) -> LedgerRow:
    outcome = router.route(text=text, team_id=team_id)
    store.append(outcome.row)
    return outcome.row


def command_route(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    """Route one request or a whole workload, appending one ledger row each."""
    config = load_config(args.config)
    store = LedgerStore(config.ledger.directory)
    router = build_router(config, dry_run=args.dry_run, store=store)

    if not config.ladder.prices_verified:
        echo("WARNING: prices_verified is false; recorded costs are placeholders.")
    if args.dry_run:
        echo("Dry run: using fakes, no network call will be made.")

    interval_ms = validate_interval(
        args.min_interval_ms if args.min_interval_ms is not None else config.run.min_interval_ms
    )

    if args.workload:
        return _route_workload(args, router, store, echo=echo, interval_ms=interval_ms)

    text = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
    row = _route_one(router, store, text=text, team_id=args.team)
    _print_row(row, label="", echo=echo)
    return EXIT_OK if row.status == STATUS_OK else EXIT_PARTIAL_FAILURE


def _route_workload(
    args: argparse.Namespace,
    router: Router,
    store: LedgerStore,
    *,
    echo: Callable[[str], None],
    interval_ms: int,
) -> int:
    requests = load_workload(args.workload)
    echo(f"Routing {len(requests)} request(s) from {args.workload} as team {args.team!r}.")

    previous_start: float | None = None
    statuses: list[str] = []
    for position, request in enumerate(requests, start=1):
        previous_start = pace(previous_start, interval_ms)
        row = _route_one(
            router,
            store,
            text=request.text,
            team_id=request.team_id or args.team,
        )
        statuses.append(row.status)
        _print_row(row, label=f"[{position:>2}/{len(requests)}] {request.id:<28}", echo=echo)

    ok = statuses.count(STATUS_OK)
    echo("")
    echo(f"Recorded {len(statuses)} ledger row(s): {ok} ok, {len(statuses) - ok} not ok.")
    return EXIT_OK if ok == len(statuses) else EXIT_PARTIAL_FAILURE


def command_ledger_summary(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    """Total one month of ledger rows and print the table."""
    config = load_config(args.config)
    store = LedgerStore(config.ledger.directory)
    month = validate_month_key(args.month) if args.month else month_key()
    rows = store.read_month(month)
    echo(render(summarise(rows, month=month, prices_verified=config.ladder.prices_verified)))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopilot",
        description="Route each LLM request to the cheapest model likely to answer it well.",
    )
    parser.add_argument(
        "--config",
        default="autopilot.toml",
        help="Path to autopilot.toml (default: autopilot.toml).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser(
        "classify", help="Print the tier and the rules that produced it. Calls no model."
    )
    classify.add_argument("--text", required=True, help="The request text to classify.")

    route = subparsers.add_parser("route", help="Route a request or a workload.")
    route.add_argument("--team", required=True, help="Team id the spend is charged to.")
    source = route.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="A single request, inline.")
    source.add_argument("--file", help="A file holding a single request.")
    source.add_argument("--workload", help="A JSONL workload: one request per line.")
    route.add_argument(
        "--dry-run",
        action="store_true",
        help="Use fakes. Makes no network call and needs no API key.",
    )
    route.add_argument(
        "--min-interval-ms",
        type=int,
        default=None,
        help="Minimum gap between model calls. Overrides [run] min_interval_ms.",
    )

    ledger = subparsers.add_parser("ledger", help="Read the ledger.")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    summary = ledger_sub.add_parser("summary", help="Totals for one month.")
    summary.add_argument("--month", default=None, help="YYYY-MM (default: the current UTC month).")

    return parser


def main(argv: Sequence[str] | None = None, echo: Callable[[str], None] = print) -> int:
    """Run the CLI. Returns the process exit code rather than calling `exit`."""
    args = build_parser().parse_args(argv)

    handlers: dict[str, Callable[[argparse.Namespace, Callable[[str], None]], int]] = {
        "classify": command_classify,
        "route": command_route,
        "ledger": command_ledger_summary,
    }

    try:
        return handlers[args.command](args, echo)
    except (ConfigFileError, WorkloadError, LedgerError, RoutingError) as exc:
        echo(f"error: {exc}")
        return EXIT_BAD_CONFIG
    except ProviderError as exc:
        # A provider that could not even be built (a missing key) stopped the
        # run before anything was recorded, so this is a setup failure, not a
        # routed outcome. Routed provider failures never reach here: they are
        # recorded on a ledger row and reported through the exit code.
        echo(f"error: {exc}")
        return EXIT_BAD_CONFIG
    except (OSError, ValueError) as exc:
        echo(f"error: {exc}")
        return EXIT_BAD_CONFIG
