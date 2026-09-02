"""Measurable properties of a request, extracted deterministically from its text.

This module counts; it does not judge. Nothing here decides a tier, and nothing
here calls a model. Separating the two matters because a feature is a fact that
can be checked by eye ("this text contains a stack trace") while a tier is a
policy decision that a threshold in `autopilot.toml` owns. Keeping the fact
layer pure is what makes the policy layer arguable.

The extractor sees the request text and nothing else — no user id, no history,
no embeddings. That is a deliberate limit: the routing decision has to be
explainable in one line on a ledger row, and a feature nobody can point at in
the text is not explainable.

Ratios are stored as integers per thousand rather than floats. A threshold
comparison should mean the same thing on every machine and in every dump of a
ledger row, and `>= 200` does; `>= 0.2` against a repeating binary fraction is
the sort of thing that behaves differently at the boundary.
"""

import re
from dataclasses import dataclass

MAX_TEXT_CHARS = 200_000
"""A request longer than this is refused rather than scanned. The regexes here
are linear, but an unbounded input is an unbounded amount of work, and the
rulebook requires payload sizes to be bounded."""

SIMPLE_VERBS: tuple[str, ...] = (
    "capitalise",
    "capitalize",
    "classify",
    "convert",
    "count",
    "define",
    "expand",
    "extract",
    "format",
    "label",
    "list",
    "reformat",
    "rewrite",
    "shorten",
    "spell",
    "summarise",
    "summarize",
    "tag",
    "translate",
)
"""Verbs naming a bounded transformation of text that is already present.

These are the tasks a small model is good at: the answer is mostly a rearranging
of the input, so there is little for extra capability to add."""

COMPLEX_VERBS: tuple[str, ...] = (
    "analyse",
    "analyze",
    "architect",
    "compare",
    "critique",
    "debug",
    "derive",
    "design",
    "diagnose",
    "evaluate",
    "forecast",
    "investigate",
    "justify",
    "optimise",
    "optimize",
    "plan",
    "prioritise",
    "prioritize",
    "prove",
    "reconcile",
    "refactor",
    "troubleshoot",
)
"""Verbs naming work that requires reasoning the input does not already contain."""

CONSTRAINT_MARKERS: tuple[str, ...] = (
    "at least",
    "at most",
    "cannot",
    "exactly",
    "must not",
    "must",
    "no fewer than",
    "no more than",
    "should not",
)
"""Phrases that add a checkable requirement to the answer.

Ordered longest-first where one contains another ("must not" before "must") so
the specific marker is credited rather than the general one."""

MULTI_STEP_MARKERS: tuple[str, ...] = (
    "pros and cons",
    "root cause",
    "show your work",
    "step by step",
    "step-by-step",
    "trade-off",
    "tradeoff",
    "walk me through",
    "walk through",
)
"""Phrases that say the answer needs several linked steps, not one lookup.

Kept deliberately short. Common connectives ("first", "then", "why") appear in
trivial requests just as often, so including them would fire on everything and
explain nothing."""

_WORD_RE = re.compile(r"\S+")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_CODE_FENCE_RE = re.compile(r"```")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)")
_PYTHON_TRACEBACK_RE = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)
_PYTHON_FRAME_RE = re.compile(r'^\s*File "[^"]+", line \d+', re.MULTILINE)
_JS_FRAME_RE = re.compile(r"^\s*at [\w.$<>\[\]]+ ?\(", re.MULTILINE)
_OUTPUT_LENGTH_HINT_RE = re.compile(
    r"(?:in|to|under|below|within|at most|no more than|no longer than|max(?:imum)? of)?\s*"
    r"\d+\s*(?:[-–]\s*\d+\s*)?(?:sentences?|words?|bullet points?|lines?|paragraphs?)",
    re.IGNORECASE,
)
_PIPE_TABLE_MIN_LINES = 2
_PIPE_TABLE_MIN_PIPES = 2
_TAB_TABLE_MIN_LINES = 2
_TAB_TABLE_MIN_TABS = 2
_PERMILLE = 1000
_ASCII_MAX_ORDINAL = 127


class FeatureExtractionError(ValueError):
    """The request text is not usable: wrong type, blank, or over the size limit."""


@dataclass(frozen=True)
class RequestFeatures:
    """Everything the scorer is allowed to look at, and nothing else.

    Frozen because a classification is evidence: once measured it is a record of
    what the text said, and a later stage must not be able to edit it.
    """

    char_count: int
    word_count: int
    question_count: int
    number_count: int
    bullet_count: int
    has_code_fence: bool
    has_stack_trace: bool
    has_table: bool
    simple_verbs: tuple[str, ...]
    complex_verbs: tuple[str, ...]
    constraint_markers: tuple[str, ...]
    constraint_count: int
    multi_step_markers: tuple[str, ...]
    output_length_hint: str | None
    non_ascii_permille: int


def _validate_text(text: object) -> str:
    if not isinstance(text, str):
        raise FeatureExtractionError(
            f"request text must be a string, got {type(text).__name__}"
        )
    if not text.strip():
        raise FeatureExtractionError(
            "request text must contain at least one non-whitespace character"
        )
    if len(text) > MAX_TEXT_CHARS:
        raise FeatureExtractionError(
            f"request text is {len(text)} characters, over the {MAX_TEXT_CHARS} limit"
        )
    return text


def _found_words(lowered: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    """Words from `vocabulary` present in `lowered`, deduplicated and sorted.

    Word boundaries are required so "listen" does not match "list" and
    "designer" does not match "design". A trailing "s" is allowed, because
    "summarizes" and "trade-offs" are the same signal as their singulars and
    requiring an exact match silently loses half of real requests.
    """
    return tuple(
        word
        for word in sorted(vocabulary)
        if re.search(rf"\b{re.escape(word)}s?\b", lowered) is not None
    )


def _constraint_markers(lowered: str) -> tuple[tuple[str, ...], int]:
    """Distinct constraint markers present, and the total number of occurrences.

    A marker is removed from the working copy once counted so that the "must" in
    "must not" is not also counted as a bare "must" — one requirement, one count.
    """
    remaining = lowered
    found: list[str] = []
    total = 0
    for marker in CONSTRAINT_MARKERS:
        pattern = re.compile(rf"\b{re.escape(marker)}\b")
        occurrences = len(pattern.findall(remaining))
        if occurrences:
            found.append(marker)
            total += occurrences
            remaining = pattern.sub(" ", remaining)
    return tuple(sorted(found)), total


def _has_table(text: str) -> bool:
    """Two or more rows delimited by pipes or tabs.

    Commas are not accepted as a delimiter: ordinary prose is full of them, and a
    false table would push plain sentences toward a more expensive rung.
    """
    lines = text.splitlines()
    pipe_rows = sum(1 for line in lines if line.count("|") >= _PIPE_TABLE_MIN_PIPES)
    if pipe_rows >= _PIPE_TABLE_MIN_LINES:
        return True
    tab_rows = sum(1 for line in lines if line.count("\t") >= _TAB_TABLE_MIN_TABS)
    return tab_rows >= _TAB_TABLE_MIN_LINES


def _has_stack_trace(text: str) -> bool:
    """A Python traceback header, a Python frame line, or a JavaScript frame line.

    The bare word "error" is deliberately not enough. People say "I got an error"
    constantly in requests that are otherwise trivial."""
    return bool(
        _PYTHON_TRACEBACK_RE.search(text)
        or _PYTHON_FRAME_RE.search(text)
        or _JS_FRAME_RE.search(text)
    )


def _output_length_hint(text: str) -> str | None:
    """The first explicit "how long should the answer be" phrase, if any.

    A bare number is not a hint: the number has to be attached to a unit of
    output length, which is what keeps "there were 12 incidents" out.
    """
    match = _OUTPUT_LENGTH_HINT_RE.search(text)
    return match.group(0).strip() if match else None


def _non_ascii_permille(text: str) -> int:
    """Share of characters outside ASCII, per thousand, rounded down."""
    non_ascii = sum(1 for character in text if ord(character) > _ASCII_MAX_ORDINAL)
    return (non_ascii * _PERMILLE) // len(text)


def extract_features(text: object) -> RequestFeatures:
    """Measure `text`. Pure: the same string always yields the same features.

    Raises:
        FeatureExtractionError: the text is not a string, is blank, or exceeds
            `MAX_TEXT_CHARS`.
    """
    validated = _validate_text(text)
    lowered = validated.lower()
    markers, constraint_count = _constraint_markers(lowered)

    return RequestFeatures(
        char_count=len(validated),
        word_count=len(_WORD_RE.findall(validated)),
        question_count=validated.count("?"),
        number_count=len(_NUMBER_RE.findall(validated)),
        bullet_count=sum(1 for line in validated.splitlines() if _BULLET_RE.match(line)),
        has_code_fence=bool(_CODE_FENCE_RE.search(validated)),
        has_stack_trace=_has_stack_trace(validated),
        has_table=_has_table(validated),
        simple_verbs=_found_words(lowered, SIMPLE_VERBS),
        complex_verbs=_found_words(lowered, COMPLEX_VERBS),
        constraint_markers=markers,
        constraint_count=constraint_count,
        multi_step_markers=_found_words(lowered, MULTI_STEP_MARKERS),
        output_length_hint=_output_length_hint(validated),
        non_ascii_permille=_non_ascii_permille(validated),
    )
