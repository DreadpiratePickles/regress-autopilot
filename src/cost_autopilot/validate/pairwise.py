"""Ask a judge which of two answers is better, twice, with the labels swapped.

Criteria judging only works where a request came with criteria. Most traffic does
not. So the general question — *did routing this to a cheaper model cost the user
anything?* — is asked directly: here are the two answers, is the cheap one at
least as good?

**Why twice.** A judge asked "is A at least as good as B" does not answer only
about the answers; part of its verdict is about the slot. Position bias in
pairwise LLM judging is well documented and large enough to invent or erase a
difference on its own. Running the same pair in both orders is the cheapest
defence there is: it costs one extra call and it converts a bias that would
silently skew every verdict into a flag on the individual verdicts it touched.

**How the two orders combine.** Each run answers "is the answer in slot A at
least as good as the answer in slot B", where "at least as good" includes
"equally good".

| forward | reverse | reading | verdict |
|---|---|---|---|
| true | true | both runs picked slot A, which only a tie satisfies | sufficient |
| true | false | the cheap answer, in both orders | sufficient |
| false | true | the reference, in both orders | **regret** |
| false | false | each run picked the *other* slot: it tracked position | **regret** + bias |

("forward" puts the cheap answer in slot A; "reverse" puts the reference there.)

The last row is the contradiction the swap exists to catch, and it is counted as
insufficient rather than discarded. That is deliberate: a judgement this system
could not read is not evidence that the cheap answer was fine.

**The blind spot, stated.** A judge that always favours slot A produces
`(true, true)`, which is indistinguishable from a genuine tie and is read as one.
The swap therefore catches contradictions, not a uniform slot preference. What
would catch that is calibration against a hand-graded sample, which stage 03's
contract requires of a human before any regret figure is quoted.

Parsing is as strict as project 1's `parse_verdict`, and for the same reason: a
reply that could not be read is a judge error, never a verdict.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from regression_detect.providers.base import Provider

from ..parsing import strip_one_fence

PAIRWISE_PROMPT_PATH = Path(__file__).parent / "prompts" / "pairwise_v1.md"
"""Resolved relative to this package, so a fresh clone works from any directory."""

REPLY_KEYS = frozenset({"reason", "a_at_least_as_good"})

REQUEST_OPEN_TAG, REQUEST_CLOSE_TAG = "<request>", "</request>"
ANSWER_A_OPEN_TAG, ANSWER_A_CLOSE_TAG = "<answer_a>", "</answer_a>"
ANSWER_B_OPEN_TAG, ANSWER_B_CLOSE_TAG = "<answer_b>", "</answer_b>"

PAIRWISE_TEMPERATURE = 0.0
"""Zero so a re-judged pair gives the same verdict as far as the provider allows.
A comparison that changes between runs is noise being reported as regret."""


class PairwiseError(Exception):
    """Base class for failures of the pairwise judge."""


class InvalidPairwiseInputError(PairwiseError, ValueError):
    """A field handed to the pairwise judge is not usable."""


class PairwiseParseError(PairwiseError, ValueError):
    """The judge's reply is not a usable comparison and must not be used."""


@dataclass(frozen=True)
class PairwiseVerdict:
    """One judge decision about one ordering of one pair."""

    a_at_least_as_good: bool
    reason: str


@dataclass(frozen=True)
class PairwiseOutcome:
    """What the two orderings together say about the cheap answer."""

    forward: bool | None
    reverse: bool | None
    regret: bool | None
    """`True` the cheap answer was insufficient, `False` it was sufficient,
    `None` at least one order produced no readable verdict."""

    position_bias_detected: bool


def load_pairwise_prompt(prompt_path: Path = PAIRWISE_PROMPT_PATH) -> str:
    """Read the pairwise system prompt.

    Raises:
        FileNotFoundError: the prompt is missing. A missing prompt is a broken
            install, never a silent fallback to an unguided judge.
    """
    return Path(prompt_path).read_text(encoding="utf-8")


def build_pairwise_user_message(request: str, answer_a: str, answer_b: str) -> str:
    """Wrap the three inputs in delimiters as the user message.

    None of them is ever formatted into the system prompt: the request came from
    a user and both answers came from models, so grader instructions and graded
    material stay separable whatever any of them contains.
    """
    return (
        f"{REQUEST_OPEN_TAG}\n{request}\n{REQUEST_CLOSE_TAG}\n\n"
        f"{ANSWER_A_OPEN_TAG}\n{answer_a}\n{ANSWER_A_CLOSE_TAG}\n\n"
        f"{ANSWER_B_OPEN_TAG}\n{answer_b}\n{ANSWER_B_CLOSE_TAG}"
    )


def parse_pairwise_verdict(raw: object) -> PairwiseVerdict:
    """Parse the judge's reply into a `PairwiseVerdict`.

    Tolerant of surrounding whitespace and of one markdown fence. Strict about
    everything else: exactly the keys `reason` and `a_at_least_as_good`, a real
    JSON boolean, and a non-empty reason.

    Raises:
        PairwiseParseError: on anything else.
    """
    if not isinstance(raw, str):
        raise PairwiseParseError(
            f"pairwise judge reply must be a string, got {type(raw).__name__}"
        )

    candidate = strip_one_fence(raw.strip()).strip()
    if not candidate:
        raise PairwiseParseError("pairwise judge returned an empty reply")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PairwiseParseError(
            f"pairwise judge reply is not JSON: {exc.msg} (at position {exc.pos})"
        ) from exc

    if not isinstance(payload, dict):
        raise PairwiseParseError(
            f"pairwise judge reply must be a JSON object, got {type(payload).__name__}"
        )

    keys = set(payload)
    if keys != REPLY_KEYS:
        missing = sorted(REPLY_KEYS - keys)
        extra = sorted(keys - REPLY_KEYS)
        raise PairwiseParseError(
            "pairwise judge reply must have exactly the keys 'reason' and "
            f"'a_at_least_as_good' (missing: {missing or 'none'}; "
            f"unexpected: {extra or 'none'})"
        )

    verdict = payload["a_at_least_as_good"]
    if not isinstance(verdict, bool):
        raise PairwiseParseError(
            f"'a_at_least_as_good' must be a JSON boolean, got "
            f"{type(verdict).__name__}: {verdict!r}"
        )

    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise PairwiseParseError("'reason' must be a non-empty string")

    return PairwiseVerdict(a_at_least_as_good=verdict, reason=reason.strip())


def _validate_field(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidPairwiseInputError(f"{field} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise InvalidPairwiseInputError(
            f"{field} must contain at least one non-whitespace character"
        )
    return value


def judge_pairwise(
    *,
    request: str,
    answer_a: str,
    answer_b: str,
    provider: Provider,
    prompt_path: Path = PAIRWISE_PROMPT_PATH,
    temperature: float = PAIRWISE_TEMPERATURE,
) -> PairwiseVerdict:
    """Ask the judge whether `answer_a` is at least as good as `answer_b`.

    Raises:
        InvalidPairwiseInputError: any of the three fields is blank or not a string.
        FileNotFoundError: the pairwise prompt file is missing.
        PairwiseParseError: the reply is not a valid verdict.
        ProviderError: the provider call itself failed.
    """
    validated_request = _validate_field(request, field="request")
    validated_a = _validate_field(answer_a, field="answer_a")
    validated_b = _validate_field(answer_b, field="answer_b")
    system_prompt = load_pairwise_prompt(prompt_path)

    raw = provider.complete(
        system=system_prompt,
        user=build_pairwise_user_message(validated_request, validated_a, validated_b),
        temperature=temperature,
    )
    return parse_pairwise_verdict(raw)


def combine_orders(*, forward: bool | None, reverse: bool | None) -> PairwiseOutcome:
    """Fold the two orderings into one verdict about the cheap answer.

    See the module docstring for the truth table and why the contradictory case
    is counted as regret rather than discarded.
    """
    if forward is None or reverse is None:
        return PairwiseOutcome(
            forward=forward, reverse=reverse, regret=None, position_bias_detected=False
        )
    contradictory = forward is False and reverse is False
    return PairwiseOutcome(
        forward=forward,
        reverse=reverse,
        regret=contradictory or forward is False,
        position_bias_detected=contradictory,
    )
