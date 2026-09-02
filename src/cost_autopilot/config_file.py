"""Read and validate `autopilot.toml`, the file that decides what a request costs.

Everything with a consequence lives in this file: the ladder and its prices, the
budgets, the tier thresholds, whether request text is logged. None of it is a
literal in the routing code, because all of it is policy that a human should be
able to change with a reviewable diff rather than a code change.

Validation is strict in both directions. A missing key is an error, and so is an
*unknown* key — a misspelled `output_price_micro_usd_per_1k_token` that was
silently ignored would leave a rung priced at zero and every saving figure
wrong. Every message names the file, the section, and the key at fault.

Model ids are the one thing deliberately absent. A rung names a `model_ref`, and
`config.py` resolves it. That keeps vendor strings out of committed
configuration and gives a deployment one env var per rung to override.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .classify.scorer import ScorerThresholds, Tier
from .config import UnknownModelRefError, model_id_for_ref
from .route.budget import Budgets
from .route.ladder import Ladder, LadderError, Rung
from .route.router import RoutePolicy

DEFAULT_CONFIG_PATH = Path("autopilot.toml")

LADDER_KEYS = frozenset({"prices_verified", "prices_source", "prices_read_utc", "rung"})
RUNG_KEYS = frozenset(
    {
        "model_ref",
        "input_price_micro_usd_per_1k_tokens",
        "output_price_micro_usd_per_1k_tokens",
        "max_tier",
    }
)
BUDGET_KEYS = frozenset({"default_monthly_cap_micro_usd", "teams"})
CLASSIFIER_KEYS = frozenset({"t2_min_score", "t3_min_score", "llm_classifier"})
LEDGER_KEYS = frozenset({"dir", "log_text"})
RUN_KEYS = frozenset({"min_interval_ms", "temperature"})
SECTIONS = frozenset({"ladder", "policy", "budgets", "classifier", "ledger", "run"})


class ConfigFileError(Exception):
    """`autopilot.toml` is missing, unparseable, or holds an unusable value."""


@dataclass(frozen=True)
class LedgerSettings:
    directory: Path
    log_text: bool


@dataclass(frozen=True)
class RunSettings:
    min_interval_ms: int
    temperature: float


@dataclass(frozen=True)
class ClassifierSettings:
    thresholds: ScorerThresholds
    llm_classifier: bool


@dataclass(frozen=True)
class AutopilotConfig:
    """The whole of `autopilot.toml`, validated and resolved."""

    ladder: Ladder
    policy: RoutePolicy
    budgets: Budgets
    classifier: ClassifierSettings
    ledger: LedgerSettings
    run: RunSettings
    prices_source: str
    prices_read_utc: str


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError as exc:
        raise ConfigFileError(
            f"Configuration file not found: {path}. The repository root must hold an "
            "autopilot.toml; copy the committed one rather than inventing prices."
        ) from exc
    except OSError as exc:
        raise ConfigFileError(f"Configuration file could not be read: {path}") from exc

    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"Configuration file is not valid TOML: {path} ({exc})") from exc


def _table(document: dict[str, Any], name: str, *, path: Path) -> dict[str, Any]:
    if name not in document:
        raise ConfigFileError(f"{path}: missing required section [{name}]")
    value = document[name]
    if not isinstance(value, dict):
        raise ConfigFileError(f"{path}: [{name}] must be a table, got {type(value).__name__}")
    return value


def _check_keys(
    table: dict[str, Any], allowed: frozenset[str], *, path: Path, where: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigFileError(
            f"{path}: {where} has unknown key(s): {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(allowed))}"
        )


def _int(table: dict[str, Any], key: str, *, path: Path, where: str, minimum: int = 0) -> int:
    if key not in table:
        raise ConfigFileError(f"{path}: {where} is missing key '{key}'")
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigFileError(
            f"{path}: {where} '{key}' must be an integer, got {type(value).__name__}: {value!r}. "
            "Money is integer micro-USD; a decimal here is a mistake, not a rounding."
        )
    if value < minimum:
        raise ConfigFileError(f"{path}: {where} '{key}' must be at least {minimum}, got {value}")
    return value


def _bool(table: dict[str, Any], key: str, *, path: Path, where: str) -> bool:
    if key not in table:
        raise ConfigFileError(f"{path}: {where} is missing key '{key}'")
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigFileError(
            f"{path}: {where} '{key}' must be true or false, got {type(value).__name__}"
        )
    return value


def _str(table: dict[str, Any], key: str, *, path: Path, where: str) -> str:
    if key not in table:
        raise ConfigFileError(f"{path}: {where} is missing key '{key}'")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigFileError(f"{path}: {where} '{key}' must be a non-empty string")
    return value.strip()


def _tier(value: object, *, path: Path, where: str) -> Tier:
    if not isinstance(value, str):
        raise ConfigFileError(f"{path}: {where} must be a tier name, got {type(value).__name__}")
    try:
        return Tier(value)
    except ValueError as exc:
        known = ", ".join(tier.value for tier in Tier)
        raise ConfigFileError(
            f"{path}: {where} is not a known tier: {value!r}. Known tiers: {known}"
        ) from exc


def _build_ladder(table: dict[str, Any], *, path: Path) -> Ladder:
    _check_keys(table, LADDER_KEYS, path=path, where="[ladder]")
    entries = table.get("rung")
    if not isinstance(entries, list) or not entries:
        raise ConfigFileError(
            f"{path}: [ladder] needs at least one [[ladder.rung]] entry, cheapest first"
        )

    rungs: list[Rung] = []
    for index, entry in enumerate(entries):
        where = f"[[ladder.rung]] #{index}"
        if not isinstance(entry, dict):
            raise ConfigFileError(f"{path}: {where} must be a table")
        _check_keys(entry, RUNG_KEYS, path=path, where=where)
        model_ref = _str(entry, "model_ref", path=path, where=where)
        try:
            model_id = model_id_for_ref(model_ref)
        except UnknownModelRefError as exc:
            raise ConfigFileError(f"{path}: {where} {exc}") from exc
        if "max_tier" not in entry:
            raise ConfigFileError(f"{path}: {where} is missing key 'max_tier'")
        rungs.append(
            Rung(
                index=index,
                model_ref=model_ref,
                model_id=model_id,
                input_price_micro_usd_per_1k_tokens=_int(
                    entry, "input_price_micro_usd_per_1k_tokens", path=path, where=where
                ),
                output_price_micro_usd_per_1k_tokens=_int(
                    entry, "output_price_micro_usd_per_1k_tokens", path=path, where=where
                ),
                max_tier=_tier(entry["max_tier"], path=path, where=f"{where} 'max_tier'"),
            )
        )

    try:
        return Ladder(
            rungs=tuple(rungs),
            prices_verified=_bool(table, "prices_verified", path=path, where="[ladder]"),
        )
    except LadderError as exc:
        raise ConfigFileError(f"{path}: [ladder] {exc}") from exc


def _build_policy(table: dict[str, Any], *, path: Path) -> RoutePolicy:
    known = {tier.value for tier in Tier}
    _check_keys(table, frozenset(known), path=path, where="[policy]")
    floors: dict[Tier, int] = {}
    for name in table:
        tier = _tier(name, path=path, where="[policy] key")
        floors[tier] = _int(table, name, path=path, where="[policy]")
    return RoutePolicy(start_rung_by_tier=floors)


def _build_budgets(table: dict[str, Any], *, path: Path) -> Budgets:
    _check_keys(table, BUDGET_KEYS, path=path, where="[budgets]")
    teams_table = table.get("teams", {})
    if not isinstance(teams_table, dict):
        raise ConfigFileError(f"{path}: [budgets.teams] must be a table of team id -> cap")
    teams = {
        team_id: _int(teams_table, team_id, path=path, where="[budgets.teams]")
        for team_id in teams_table
    }
    return Budgets(
        default_monthly_cap_micro_usd=_int(
            table, "default_monthly_cap_micro_usd", path=path, where="[budgets]"
        ),
        teams=teams,
    )


def _build_classifier(table: dict[str, Any], *, path: Path) -> ClassifierSettings:
    _check_keys(table, CLASSIFIER_KEYS, path=path, where="[classifier]")
    try:
        thresholds = ScorerThresholds(
            t2_min_score=_int(table, "t2_min_score", path=path, where="[classifier]"),
            t3_min_score=_int(table, "t3_min_score", path=path, where="[classifier]"),
        )
    except ValueError as exc:
        raise ConfigFileError(f"{path}: [classifier] {exc}") from exc
    return ClassifierSettings(
        thresholds=thresholds,
        llm_classifier=_bool(table, "llm_classifier", path=path, where="[classifier]"),
    )


def _build_ledger(table: dict[str, Any], *, path: Path, root: Path) -> LedgerSettings:
    _check_keys(table, LEDGER_KEYS, path=path, where="[ledger]")
    directory = Path(_str(table, "dir", path=path, where="[ledger]"))
    if directory.is_absolute():
        raise ConfigFileError(
            f"{path}: [ledger] 'dir' must be a path relative to the repository root, "
            f"got the absolute path {directory}. Committed configuration must be portable."
        )
    return LedgerSettings(
        directory=root / directory,
        log_text=_bool(table, "log_text", path=path, where="[ledger]"),
    )


def _build_run(table: dict[str, Any], *, path: Path) -> RunSettings:
    _check_keys(table, RUN_KEYS, path=path, where="[run]")
    temperature = table.get("temperature", 0.0)
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise ConfigFileError(f"{path}: [run] 'temperature' must be a number")
    if not 0.0 <= float(temperature) <= 2.0:
        raise ConfigFileError(f"{path}: [run] 'temperature' must lie in [0.0, 2.0]")
    return RunSettings(
        min_interval_ms=_int(table, "min_interval_ms", path=path, where="[run]"),
        temperature=float(temperature),
    )


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AutopilotConfig:
    """Read and validate `autopilot.toml`.

    Raises:
        ConfigFileError: the file is missing, is not TOML, omits a section or
            key, carries an unknown one, or holds a value outside its range.
    """
    path = Path(path)
    document = _read_toml(path)
    root = path.parent if path.parent != Path("") else Path(".")

    unknown = sorted(set(document) - SECTIONS)
    if unknown:
        raise ConfigFileError(
            f"{path}: unknown section(s): {', '.join(unknown)}. "
            f"Known sections: {', '.join(sorted(SECTIONS))}"
        )

    ladder_table = _table(document, "ladder", path=path)
    return AutopilotConfig(
        ladder=_build_ladder(ladder_table, path=path),
        policy=_build_policy(_table(document, "policy", path=path), path=path),
        budgets=_build_budgets(_table(document, "budgets", path=path), path=path),
        classifier=_build_classifier(_table(document, "classifier", path=path), path=path),
        ledger=_build_ledger(_table(document, "ledger", path=path), path=path, root=root),
        run=_build_run(_table(document, "run", path=path), path=path),
        prices_source=_str(ladder_table, "prices_source", path=path, where="[ladder]"),
        prices_read_utc=_str(ladder_table, "prices_read_utc", path=path, where="[ladder]"),
    )
