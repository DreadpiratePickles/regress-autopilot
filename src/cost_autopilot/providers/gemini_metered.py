"""Gemini adapter that reports token usage: the only module importing the SDK.

Project 1's `GeminiProvider` already owns the hard parts of talking to this
vendor — retry policy, backoff with jitter, timeout, and the mapping from SDK
exceptions onto typed errors. Those constants and that classification are
imported from it rather than restated, so the two projects cannot drift on what
counts as a transient failure.

What could not be reused is the call itself. `GeminiProvider.complete` returns
`response.text` and drops the response object, and the token counts this package
prices a call from live on `response.usage_metadata`. Reaching them means owning
the loop. That is the entire reason this file exists.
"""

import os
import random
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from regression_detect.providers.gemini import (
    API_KEY_ENV_VAR,
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    MAX_ATTEMPTS,
    REQUEST_TIMEOUT_MS,
    RETRYABLE_STATUS_CODES,
)

from .metered import (
    Completion,
    ProviderConfigError,
    ProviderResponseError,
    ProviderTransientError,
    UsageError,
    validate_token_count,
)

MILLISECONDS_PER_SECOND = 1000


def _sleep_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, capped. Same policy as project 1."""
    ceiling = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
    return random.uniform(0.0, ceiling)


class GeminiMeteredProvider:
    """One Gemini text call, returning the reply and what it consumed."""

    def __init__(self, model_id: str, api_key: str) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ProviderConfigError("model_id must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderConfigError(
                f"A Gemini API key is required. Set {API_KEY_ENV_VAR} in your .env file."
            )

        self.model_id = model_id
        try:
            self._client = genai.Client(
                api_key=api_key,
                http_options=genai_types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )
        except Exception as exc:  # the SDK raises bare exceptions on bad config
            raise ProviderConfigError(
                f"Could not build the Gemini client for model {self.model_id}"
            ) from exc

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        """Call the model, then validate both the text and the usage block.

        Raises:
            ProviderTransientError: still failing after `MAX_ATTEMPTS`. This is
                the error the router falls back a rung on.
            ProviderConfigError: credentials rejected. Never retried.
            ProviderResponseError / UsageError: the reply or its usage block did
                not validate.
        """
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
        )

        last_transient: ProviderTransientError | None = None
        for attempt in range(MAX_ATTEMPTS):
            started = time.monotonic()
            try:
                response = self._client.models.generate_content(
                    model=self.model_id,
                    contents=user,
                    config=config,
                )
            except genai_errors.APIError as exc:
                error = self._classify_api_error(exc)
                if not isinstance(error, ProviderTransientError):
                    raise error from exc
                last_transient = error
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_transient = ProviderTransientError(
                    f"Network failure calling model {self.model_id}: {type(exc).__name__}"
                )
            else:
                elapsed_ms = int((time.monotonic() - started) * MILLISECONDS_PER_SECOND)
                return self._to_completion(response, latency_ms=elapsed_ms)

            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_sleep_seconds(attempt))

        assert last_transient is not None  # only reachable after a transient failure
        raise ProviderTransientError(
            f"Model {self.model_id} still failing after {MAX_ATTEMPTS} attempts: {last_transient}"
        ) from last_transient

    def _classify_api_error(self, exc: genai_errors.APIError) -> Exception:
        """Map an SDK API error onto a typed error. The key is never in the message."""
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        detail = f"model {self.model_id}, status {code} {status}"

        if code in RETRYABLE_STATUS_CODES:
            return ProviderTransientError(f"Transient provider failure ({detail})")
        if code in (401, 403):
            return ProviderConfigError(
                f"Gemini rejected the credentials ({detail}). "
                f"Check that {API_KEY_ENV_VAR} is set to a valid, enabled key."
            )
        return ProviderResponseError(f"Provider call failed ({detail})")

    def _to_completion(self, response: object, *, latency_ms: int) -> Completion:
        """Validate the SDK response into a `Completion`.

        Both halves are untrusted. A missing usage block raises rather than
        defaulting to zero tokens: a call recorded as free is worse than a call
        recorded as failed, because it silently understates the month's spend.
        """
        text = getattr(response, "text", None)
        if text is None:
            reason = getattr(response, "prompt_feedback", None)
            raise ProviderResponseError(
                f"Model {self.model_id} returned no text "
                f"(finish reason or safety block: {reason})"
            )
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(f"Model {self.model_id} returned an empty response")

        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            raise UsageError(
                f"Model {self.model_id} returned no usage_metadata; the call cannot be priced"
            )

        input_tokens = validate_token_count(
            getattr(usage, "prompt_token_count", None), field="prompt_token_count"
        )
        # The SDK reports reasoning tokens separately from the visible answer.
        # They are billed as output, so both are charged; omitting the thinking
        # leg would understate the bill on exactly the requests that cost most.
        output_tokens = validate_token_count(
            getattr(usage, "candidates_token_count", None) or 0, field="candidates_token_count"
        ) + validate_token_count(
            getattr(usage, "thoughts_token_count", None) or 0, field="thoughts_token_count"
        )

        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=self.model_id,
            latency_ms=latency_ms,
        )


def gemini_metered_provider_from_env(model_id: str) -> GeminiMeteredProvider:
    """Build a provider, reading the API key from `.env` or the environment.

    Raises:
        ProviderConfigError: the key is absent, with a message that names the
            variable and never contains the key.
    """
    load_dotenv()
    api_key = os.environ.get(API_KEY_ENV_VAR, "")
    if not api_key.strip():
        raise ProviderConfigError(
            f"{API_KEY_ENV_VAR} is not set. Create a .env file in the repository root "
            f"containing a line '{API_KEY_ENV_VAR}=<your key>', or export the variable "
            "in your shell. Keys are never committed."
        )
    return GeminiMeteredProvider(model_id=model_id, api_key=api_key)
