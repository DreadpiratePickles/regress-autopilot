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
from .validate.settings import ValidateSettings, ValidateSettingsError

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
VALIDATE_KEYS = frozenset(
    {
        "enabled",
        "sample_percent",
        "judge_model_ref",
        "max_regret",
        "min_samples",
        "dir",
        "shadow_dir",
    }
)
REPORT_KEYS = frozenset({"dir", "min_comparisons", "max_fallback_rate"})
SECTIONS = frozenset(
    {"ladder", "policy", "budgets", "classifier", "ledger", "run", "validate", "report"}
)

DEFAULT_REPORT_DIR = "report"
DEFAULT_MIN_COMPARISONS = 10
DEFAULT_MAX_FALLBACK_RATE = 0.20
"""Defaults for an absent `[report]` section.

The section is optional, unlike every other one, because stage 04 was added a
phase after the file's shape was fixed and a configuration written for Phase B
is still a valid configuration. Every other section stays required: a missing
price or budget is a mistake, whereas a missing report threshold has a defensible
default that is printed in the report it decides."""


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
class ReportSettings:
    """The `[report]` section: where stage 04 writes, and the two numbers it decides on."""

    directory: Path
    min_comparisons: int
    """Fewest validated records with a readable verdict before the month's report
    may say anything other than INCONCLUSIVE. Distinct from `[validate]
    min_samples`, which is the same idea applied to one tier."""

    max_fallback_rate: float
    """Share of successful requests that fell back to a more expensive rung, above
    which the report recommends raising that tier's policy floor. Falling back is
    a paid-for retry: past this rate the cheap rung is costing more than it saves."""


@dataclass(frozen=True)
class AutopilotConfig:
    """The whole of `autopilot.toml`, validated and resolved."""

    ladder: Ladder
    policy: RoutePolicy
    budgets: Budgets
    classifier: ClassifierSettings
    ledger: LedgerSettings
    run: RunSettings
    validate: ValidateSettings
    report: ReportSettings
    prices_source: str
    prices_read_utc: str

    def judge_rung(self) -> Rung:
        """The ladder rung whose tariff prices a judge call.

        Guaranteed to exist: the loader refuses a judge model that is not on the
        ladder, because its calls could not otherwise be priced.
        """
        for rung in self.ladder.rungs:
            if rung.model_id == self.validate.judge_model_id:
                return rung
        raise ConfigFileError(
            f"the judge model {self.validate.judge_model_id!r} is not on the ladder"
        )


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
    return LedgerSettings(
        directory=_relative_directory(table, "dir", path=path, where="[ledger]", root=root),
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


def _build_report(
    table: dict[str, Any] | None, *, path: Path, root: Path
) -> ReportSettings:
    """Build `[report]`, or its documented defaults when the section is absent."""
    if table is None:
        return ReportSettings(
            directory=root / DEFAULT_REPORT_DIR,
            min_comparisons=DEFAULT_MIN_COMPARISONS,
            max_fallback_rate=DEFAULT_MAX_FALLBACK_RATE,
        )

    where = "[report]"
    _check_keys(table, REPORT_KEYS, path=path, where=where)
    rate = table.get("max_fallback_rate", DEFAULT_MAX_FALLBACK_RATE)
    if isinstance(rate, bool) or not isinstance(rate, int | float):
        raise ConfigFileError(f"{path}: {where} 'max_fallback_rate' must be a number")
    if not 0.0 <= float(rate) <= 1.0:
        raise ConfigFileError(
            f"{path}: {where} 'max_fallback_rate' must lie in [0.0, 1.0], got {rate}"
        )

    directory = (
        _relative_directory(table, "dir", path=path, where=where, root=root)
        if "dir" in table
        else root / DEFAULT_REPORT_DIR
    )
    min_comparisons = (
        _int(table, "min_comparisons", path=path, where=where, minimum=1)
        if "min_comparisons" in table
        else DEFAULT_MIN_COMPARISONS
    )
    return ReportSettings(
        directory=directory,
        min_comparisons=min_comparisons,
        max_fallback_rate=float(rate),
    )


def _relative_directory(
    table: dict[str, Any], key: str, *, path: Path, where: str, root: Path
) -> Path:
    directory = Path(_str(table, key, path=path, where=where))
    if directory.is_absolute():
        raise ConfigFileError(
            f"{path}: {where} '{key}' must be a path relative to the repository root, "
            f"got the absolute path {directory}. Committed configuration must be portable."
        )
    return root / directory


def _build_validate(
    table: dict[str, Any], *, path: Path, root: Path, ladder: Ladder
) -> ValidateSettings:
    _check_keys(table, VALIDATE_KEYS, path=path, where="[validate]")
    where = "[validate]"

    model_ref = _str(table, "judge_model_ref", path=path, where=where)
    try:
        judge_model_id = model_id_for_ref(model_ref)
    except UnknownModelRefError as exc:
        raise ConfigFileError(f"{path}: {where} {exc}") from exc
    if all(rung.model_id != judge_model_id for rung in ladder.rungs):
        raise ConfigFileError(
            f"{path}: {where} 'judge_model_ref' resolves to a model that is not on the "
            "ladder, so its calls have no price here and the validation overhead could "
            "not be reported. Point it at a rung's model_ref, or add a rung for it."
        )

    max_regret = table.get("max_regret")
    if max_regret is None:
        raise ConfigFileError(f"{path}: {where} is missing key 'max_regret'")

    try:
        return ValidateSettings(
            enabled=_bool(table, "enabled", path=path, where=where),
            sample_percent=_int(table, "sample_percent", path=path, where=where),
            judge_model_ref=model_ref,
            judge_model_id=judge_model_id,
            max_regret=max_regret,
            min_samples=_int(table, "min_samples", path=path, where=where, minimum=1),
            directory=_relative_directory(table, "dir", path=path, where=where, root=root),
            shadow_directory=_relative_directory(
                table, "shadow_dir", path=path, where=where, root=root
            ),
        )
    except ValidateSettingsError as exc:
        raise ConfigFileError(f"{path}: {where} {exc}") from exc


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
    ladder = _build_ladder(ladder_table, path=path)
    return AutopilotConfig(
        ladder=ladder,
        policy=_build_policy(_table(document, "policy", path=path), path=path),
        budgets=_build_budgets(_table(document, "budgets", path=path), path=path),
        classifier=_build_classifier(_table(document, "classifier", path=path), path=path),
        ledger=_build_ledger(_table(document, "ledger", path=path), path=path, root=root),
        run=_build_run(_table(document, "run", path=path), path=path),
        validate=_build_validate(
            _table(document, "validate", path=path), path=path, root=root, ladder=ladder
        ),
        report=_build_report(
            _table(document, "report", path=path) if "report" in document else None,
            path=path,
            root=root,
        ),
        prices_source=_str(ladder_table, "prices_source", path=path, where="[ladder]"),
        prices_read_utc=_str(ladder_table, "prices_read_utc", path=path, where="[ladder]"),
    )
