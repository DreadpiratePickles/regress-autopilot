"""A model call that reports its token usage, not just its text.

Project 1's `Provider` protocol returns a string. That is the right shape for a
tool that grades answers, and the wrong shape for a tool that prices them: a
router cannot charge for a call whose token counts it never saw, and estimating
them from character counts would put a guess in the money column.

So this package widens the seam rather than changing project 1's. `Provider`
answers "what did the model say"; `MeteredProvider` answers "what did the model
say, and what did it consume". The typed error hierarchy is imported from
project 1 unchanged, so a caller catches exactly one set of exceptions no matter
which seam produced them, and `ProviderTransientError` keeps its meaning: the
signal the router falls back on.

Everything else about the arrangement is deliberately identical to project 1 —
one narrow adapter per vendor, no vendor exception escaping the package, no
credential in any message.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from regression_detect.providers.base import (
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    ProviderTransientError,
)

__all__ = [
    "Completion",
    "MeteredProvider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderTransientError",
    "UsageError",
    "validate_token_count",
]
"""Re-exported so no call site outside this package imports from project 1
directly. If project 1's error names ever change, this is the one file to fix."""


class UsageError(ProviderResponseError):
    """The provider replied but its reported token usage is unusable.

    A subclass of `ProviderResponseError` because that is what it is: a reply
    that failed validation. It is never retried and never defaulted to zero —
    a call billed as free because its usage block was missing is a silent
    accounting error, which is the one failure mode a cost tool must not have.
    """


def validate_token_count(value: object, *, field: str) -> int:
    """Validate a token count reported by a provider.

    Model output is untrusted input, and a usage block is model output.

    Raises:
        UsageError: the count is not a non-negative integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(
            f"{field} must be a non-negative integer, got {type(value).__name__}: {value!r}"
        )
    if value < 0:
        raise UsageError(f"{field} must be a non-negative integer, got {value}")
    return value


@dataclass(frozen=True)
class Completion:
    """One model reply, with the evidence needed to price and audit it.

    Frozen: a completion is a record of something that already happened and was
    already paid for.
    """

    text: str
    input_tokens: int
    output_tokens: int
    model_id: str
    latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ProviderResponseError(
                f"model {self.model_id!r} returned an empty or non-string reply"
            )
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ProviderResponseError("completion must name the model that produced it")
        validate_token_count(self.input_tokens, field="input_tokens")
        validate_token_count(self.output_tokens, field="output_tokens")
        validate_token_count(self.latency_ms, field="latency_ms")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@runtime_checkable
class MeteredProvider(Protocol):
    """A narrow adapter around one text-in / text-plus-usage-out model call.

    Implementations must raise only `ProviderError` subclasses, so the router
    never has to catch a vendor SDK exception to make a routing decision.
    """

    model_id: str
    """Identifier of the model actually called, recorded on the ledger row."""

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        """Return the model's reply to `user` under the `system` rules, metered."""
        ...
