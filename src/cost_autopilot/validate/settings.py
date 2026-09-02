"""The `[validate]` section, validated and resolved.

Kept next to the stage it configures rather than inside the config loader, so the
rules about what a sample rate or a regret threshold may be live with the code
that means something by them.

`judge_model_ref` is a reference name, not a model id, exactly as a ladder rung
is: model ids live only in `config.py`. The resolved id must also name a rung on
the ladder, because a judge model with no price in `autopilot.toml` would make
the reported validation overhead a guess, and a guess in the money column is the
one thing this system may not produce.
"""

from dataclasses import dataclass
from pathlib import Path

from .sampling import SamplingError, validate_sample_percent

MIN_MAX_REGRET = 0.0
MAX_MAX_REGRET = 1.0
MIN_MIN_SAMPLES = 1


class ValidateSettingsError(ValueError):
    """A `[validate]` value is outside the range the stage can act on."""


@dataclass(frozen=True)
class ValidateSettings:
    """What stage 03 samples, who judges it, and when the result counts as safe."""

    enabled: bool
    """Whether stage 02 keeps shadow records at all. Off means no request text is
    stored anywhere and no quality figure can be produced — a real trade, made
    deliberately, not a feature flag."""

    sample_percent: int
    judge_model_ref: str
    judge_model_id: str
    max_regret: float
    """The regret rate above which a tier is called too aggressive. Compared
    against the Wilson *upper* bound, never the point estimate."""

    min_samples: int
    """Below this many validated records a tier reports "insufficient evidence"
    rather than a rate. A number from four samples is a number about four
    samples."""

    directory: Path
    shadow_directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValidateSettingsError("enabled must be true or false")
        try:
            validate_sample_percent(self.sample_percent)
        except SamplingError as exc:
            raise ValidateSettingsError(str(exc)) from exc
        if isinstance(self.max_regret, bool) or not isinstance(self.max_regret, float | int):
            raise ValidateSettingsError(
                f"max_regret must be a number in [{MIN_MAX_REGRET}, {MAX_MAX_REGRET}], "
                f"got {self.max_regret!r}"
            )
        if not MIN_MAX_REGRET <= float(self.max_regret) <= MAX_MAX_REGRET:
            raise ValidateSettingsError(
                f"max_regret must lie in [{MIN_MAX_REGRET}, {MAX_MAX_REGRET}], "
                f"got {self.max_regret}"
            )
        if (
            isinstance(self.min_samples, bool)
            or not isinstance(self.min_samples, int)
            or self.min_samples < MIN_MIN_SAMPLES
        ):
            raise ValidateSettingsError(
                f"min_samples must be an integer of at least {MIN_MIN_SAMPLES}, "
                f"got {self.min_samples!r}"
            )
        for name in ("judge_model_ref", "judge_model_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValidateSettingsError(f"{name} must be a non-empty string")
