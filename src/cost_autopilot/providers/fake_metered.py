"""An in-memory metered provider: scripted replies, no network, every call recorded.

Used by `--dry-run` and by the whole test suite, exactly as project 1's
`FakeProvider` is. It exists so the router's interesting paths — a rung failing
transiently, a rung failing every attempt, a budget refusal happening before any
call — can be exercised without an API key, without spending money, and without
depending on a real provider being unhealthy at the right moment.

A scripted entry may be an exception instance, which is raised instead of
returned. That is the only way to test fallback deterministically.
"""

from typing import Any

from .metered import Completion

DEFAULT_MODEL_ID = "fake-metered-provider"
DEFAULT_INPUT_TOKENS = 100
DEFAULT_OUTPUT_TOKENS = 50
DEFAULT_LATENCY_MS = 7
"""Small, fixed, and obviously synthetic. A dry run must never produce numbers
that could be mistaken for a measurement."""

ScriptEntry = Completion | Exception | str


class SharedCursor:
    """A position in a script shared by every provider a factory builds.

    Without this, each rung's fake would start the script from the beginning and
    "the first attempt fails, the second succeeds" would be untestable — every
    rung would see the same first entry and fail identically. The router's
    fallback path is the thing most worth testing, so the harness has to be able
    to express a sequence of attempts across different models.
    """

    def __init__(self) -> None:
        self.taken = 0

    def take(self) -> int:
        index = self.taken
        self.taken += 1
        return index


class FakeMeteredProvider:
    """Return scripted completions, or raise scripted errors, and record calls.

    Args:
        script: one entry, or a list of entries consumed in order and then
            cycled. A `str` entry becomes a `Completion` with the default token
            counts; an `Exception` entry is raised.
        model_id: identifier reported on the completion and the ledger row.
        input_tokens: token count reported for `str` entries.
        output_tokens: token count reported for `str` entries.
        latency_ms: latency reported for `str` entries.
    """

    def __init__(
        self,
        script: list[ScriptEntry] | ScriptEntry = "fake completion",
        *,
        model_id: str = DEFAULT_MODEL_ID,
        input_tokens: int = DEFAULT_INPUT_TOKENS,
        output_tokens: int = DEFAULT_OUTPUT_TOKENS,
        latency_ms: int = DEFAULT_LATENCY_MS,
        cursor: SharedCursor | None = None,
    ) -> None:
        entries = [script] if not isinstance(script, list) else list(script)
        if not entries:
            raise ValueError("FakeMeteredProvider needs at least one scripted entry")
        for entry in entries:
            if not isinstance(entry, Completion | Exception | str):
                raise ValueError(
                    "FakeMeteredProvider entries must be Completion, Exception or str, "
                    f"got {type(entry).__name__}"
                )

        self._script = entries
        self._cursor = cursor or SharedCursor()
        self.model_id = model_id
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._latency_ms = latency_ms
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        entry = self._script[self._cursor.take() % len(self._script)]
        self.calls.append({"system": system, "user": user, "temperature": temperature})

        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, Completion):
            return entry
        return Completion(
            text=entry,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            model_id=self.model_id,
            latency_ms=self._latency_ms,
        )


def fake_metered_provider_factory(
    script: list[ScriptEntry] | ScriptEntry = "fake completion",
) -> "FakeProviderFactory":
    """Build a factory that hands every rung its own fake, keyed by model id."""
    return FakeProviderFactory(script)


class FakeProviderFactory:
    """A `model_id -> MeteredProvider` factory backed by fakes.

    The router asks for a provider per rung. Keeping one fake per model id makes
    "the cheap rung was called twice and the middle rung once" directly
    assertable, which is what the fallback tests need to check.
    """

    def __init__(self, script: list[ScriptEntry] | ScriptEntry = "fake completion") -> None:
        self._script = script
        self._cursor = SharedCursor()
        self.built: dict[str, FakeMeteredProvider] = {}

    def __call__(self, model_id: str) -> FakeMeteredProvider:
        if model_id not in self.built:
            self.built[model_id] = FakeMeteredProvider(
                self._script, model_id=model_id, cursor=self._cursor
            )
        return self.built[model_id]

    @property
    def total_calls(self) -> int:
        """Calls made across every rung, which is what pacing is measured in."""
        return sum(provider.call_count for provider in self.built.values())
