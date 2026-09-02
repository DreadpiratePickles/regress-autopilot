"""Turn measured features into a tier, and say out loud why.

The scorer is a weighted sum of features with a name attached to every term. It
is not a model, and that is the point. The routing decision is the thing this
whole system exists to justify later, so it has to be reproducible from the
request text alone, cheap enough to run on every request, and legible enough
that a human reading one ledger row can say "yes, that was the right rung" or
"no, and here is the rule that got it wrong".

A learned classifier would very likely be more accurate. It would also cost a
model call per request — on a system whose entire purpose is to not spend model
calls — and it would answer "why T3?" with a number nobody can argue with.
Phase B measures how often these rules are wrong; until there is that evidence,
tuning them would be guessing.

Every term contributes to `reasons` in the form `"<what fired> (+N)"`, and the
weights in the reasons sum exactly to the score before clamping. That invariant
is tested, because a reason list that does not add up is decoration.
"""

from dataclasses import dataclass, field
from enum import Enum

from .features import RequestFeatures, extract_features

MIN_SCORE = 0
MAX_SCORE = 100

WEIGHTS: dict[str, int] = {
    "words_long": 30,
    "words_medium": 18,
    "words_short": 10,
    "words_tiny": 0,
    "code_fence": 30,
    "stack_trace": 25,
    "complex_verb": 14,
    "simple_verb": 6,
    "multi_step": 10,
    "many_questions": 8,
    "constraint": 5,
    "bullets": 8,
    "table": 10,
    "many_numbers": 8,
    "non_ascii": 5,
    "output_length_hint": 4,
}
"""What each signal is worth. Weights are deliberately coarse multiples of a few
points: the boundaries between tiers are policy in `autopilot.toml`, and false
precision here would suggest a calibration nobody has done."""

CAPS: dict[str, int] = {
    "complex_verb": 28,
    "simple_verb": 12,
    "constraint": 20,
    "multi_step": 10,
}
"""Caps stop one repeated signal from deciding a request on its own. A request
naming six complex verbs is not three times harder than one naming two."""

WORDS_LONG = 150
WORDS_MEDIUM = 60
WORDS_SHORT = 25
"""Length bands. Length is the strongest single separator between a bare question
and a request with a body of text attached to it."""

MANY_QUESTIONS = 2
MANY_NUMBERS = 5
MANY_BULLETS = 3
NON_ASCII_PERMILLE_MIN = 200
"""At or above this share of non-ASCII characters the request is substantially
not in English, which makes it harder for a small model regardless of the task."""

DECIDED_BY_RULES = "rules"
DECIDED_BY_LLM = "llm"


class Tier(Enum):
    """How hard a request looks. Ordered: T1 is the cheapest work.

    The three values are the whole vocabulary. A tier read from a config file, a
    ledger row, or a model reply is constructed through `Tier(value)`, which
    rejects anything else rather than inventing a fourth state.
    """

    T1_TRIVIAL = "T1_TRIVIAL"
    T2_STANDARD = "T2_STANDARD"
    T3_COMPLEX = "T3_COMPLEX"

    @property
    def rank(self) -> int:
        return _TIER_RANKS[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return self.rank >= other.rank


_TIER_RANKS = {"T1_TRIVIAL": 1, "T2_STANDARD": 2, "T3_COMPLEX": 3}


@dataclass(frozen=True)
class ScorerThresholds:
    """Where the two tier boundaries sit on the 0-100 score.

    Both are inclusive lower bounds: a score of exactly `t2_min_score` is T2.
    An inclusive bound is the one a person predicts correctly when reading the
    config, which is the only reason to prefer either.
    """

    t2_min_score: int
    t3_min_score: int

    def __post_init__(self) -> None:
        for name in ("t2_min_score", "t3_min_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{name} must be an integer, got {type(value).__name__}: {value!r}"
                )
            if not MIN_SCORE <= value <= MAX_SCORE:
                raise ValueError(f"{name} must lie in [{MIN_SCORE}, {MAX_SCORE}], got {value}")
        if self.t3_min_score < self.t2_min_score:
            raise ValueError(
                f"t3_min_score ({self.t3_min_score}) must be at least "
                f"t2_min_score ({self.t2_min_score})"
            )


@dataclass(frozen=True)
class Classification:
    """One request's tier, the score behind it, and the rules that fired."""

    tier: Tier
    complexity_score: int
    reasons: tuple[str, ...]
    features: RequestFeatures
    decided_by: str = field(default=DECIDED_BY_RULES)
    """`"rules"` for the deterministic scorer, `"llm"` when the optional
    classifier overrode it. Recorded so a ledger row never hides which one
    chose."""


def _length_term(word_count: int) -> tuple[int, str]:
    if word_count >= WORDS_LONG:
        return WEIGHTS["words_long"], f"long request: {word_count} words"
    if word_count >= WORDS_MEDIUM:
        return WEIGHTS["words_medium"], f"medium-length request: {word_count} words"
    if word_count >= WORDS_SHORT:
        return WEIGHTS["words_short"], f"short request with a body: {word_count} words"
    return WEIGHTS["words_tiny"], f"very short request: {word_count} words"


def score_features(features: RequestFeatures) -> tuple[int, tuple[str, ...]]:
    """Score `features` and return the score with the reasons that produced it.

    The score is clamped into [0, 100] only at the end. The reasons always sum
    to the pre-clamp total, and the clamp itself appears as a reason when it
    bites, so the arithmetic on a ledger row is always checkable.
    """
    terms: list[tuple[int, str]] = [_length_term(features.word_count)]

    if features.has_code_fence:
        terms.append((WEIGHTS["code_fence"], "contains a fenced code block"))
    if features.has_stack_trace:
        terms.append((WEIGHTS["stack_trace"], "contains a stack trace"))
    if features.has_table:
        terms.append((WEIGHTS["table"], "contains a table"))

    if features.complex_verbs:
        weight = min(
            WEIGHTS["complex_verb"] * len(features.complex_verbs), CAPS["complex_verb"]
        )
        terms.append((weight, f"reasoning verb(s): {', '.join(features.complex_verbs)}"))
    if features.simple_verbs:
        weight = min(WEIGHTS["simple_verb"] * len(features.simple_verbs), CAPS["simple_verb"])
        terms.append((weight, f"bounded task verb(s): {', '.join(features.simple_verbs)}"))
    if features.multi_step_markers:
        terms.append(
            (CAPS["multi_step"], f"multi-step marker(s): {', '.join(features.multi_step_markers)}")
        )

    if features.question_count >= MANY_QUESTIONS:
        terms.append((WEIGHTS["many_questions"], f"asks {features.question_count} questions"))
    if features.constraint_count:
        weight = min(WEIGHTS["constraint"] * features.constraint_count, CAPS["constraint"])
        terms.append(
            (weight, f"{features.constraint_count} constraint(s): "
                     f"{', '.join(features.constraint_markers)}")
        )
    if features.bullet_count >= MANY_BULLETS:
        terms.append((WEIGHTS["bullets"], f"{features.bullet_count} bullet points"))
    if features.number_count >= MANY_NUMBERS:
        terms.append((WEIGHTS["many_numbers"], f"{features.number_count} numeric values"))
    if features.non_ascii_permille >= NON_ASCII_PERMILLE_MIN:
        terms.append(
            (WEIGHTS["non_ascii"], f"{features.non_ascii_permille} per-mille non-ASCII characters")
        )
    if features.output_length_hint is not None:
        terms.append(
            (WEIGHTS["output_length_hint"], f"output length hint: {features.output_length_hint!r}")
        )

    raw_total = sum(weight for weight, _ in terms)
    reasons = [f"{label} (+{weight})" for weight, label in terms]

    clamped = max(MIN_SCORE, min(MAX_SCORE, raw_total))
    if clamped != raw_total:
        # `:+d` rather than a literal '+', because a clamp's delta is negative
        # and prefixing one produced `(+-14)`.
        reasons.append(f"clamped from {raw_total} to {clamped} ({clamped - raw_total:+d})")
    return clamped, tuple(reasons)


def tier_for_score(score: int, thresholds: ScorerThresholds) -> Tier:
    """Map a 0-100 score onto a tier using the configured inclusive bounds."""
    if score >= thresholds.t3_min_score:
        return Tier.T3_COMPLEX
    if score >= thresholds.t2_min_score:
        return Tier.T2_STANDARD
    return Tier.T1_TRIVIAL


def classify_features(
    features: RequestFeatures, thresholds: ScorerThresholds
) -> Classification:
    """Classify already-extracted features."""
    score, reasons = score_features(features)
    return Classification(
        tier=tier_for_score(score, thresholds),
        complexity_score=score,
        reasons=reasons,
        features=features,
    )


def classify_text(text: object, thresholds: ScorerThresholds) -> Classification:
    """Measure `text` and classify it. The whole of stage 01's default path.

    Raises:
        FeatureExtractionError: the text is not usable.
    """
    return classify_features(extract_features(text), thresholds)
