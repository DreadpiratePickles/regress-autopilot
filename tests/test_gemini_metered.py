"""The Gemini adapter, with a stubbed SDK. No test here touches the network.

What is worth testing without a network is exactly what this adapter adds over
project 1's: reading `usage_metadata` correctly, refusing to price a call whose
usage block is missing or malformed, and mapping SDK errors onto typed ones.
"""

from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from cost_autopilot.providers.gemini_metered import (
    GeminiMeteredProvider,
    gemini_metered_provider_from_env,
)
from cost_autopilot.providers.metered import (
    ProviderConfigError,
    ProviderResponseError,
    ProviderTransientError,
    UsageError,
)

API_KEY_ENV_VAR = "GEMINI_API_KEY"


class StubModels:
    """Stands in for `client.models`, returning or raising a scripted result."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def build(result, *, model_id="model-x") -> GeminiMeteredProvider:
    provider = GeminiMeteredProvider.__new__(GeminiMeteredProvider)
    provider.model_id = model_id
    provider._client = SimpleNamespace(models=StubModels(result))
    return provider


def response(
    text="hello",
    *,
    prompt_tokens=120,
    candidate_tokens=40,
    thought_tokens=None,
    usage=True,
):
    usage_block = (
        SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidate_tokens,
            thoughts_token_count=thought_tokens,
        )
        if usage
        else None
    )
    return SimpleNamespace(text=text, usage_metadata=usage_block, prompt_feedback=None)


class TestConstruction:
    def test_rejects_a_blank_model_id(self):
        with pytest.raises(ProviderConfigError, match="model_id"):
            GeminiMeteredProvider(model_id="  ", api_key="k")

    def test_rejects_a_blank_api_key(self):
        with pytest.raises(ProviderConfigError, match=API_KEY_ENV_VAR):
            GeminiMeteredProvider(model_id="m", api_key="")

    def test_the_error_never_contains_the_key(self):
        with pytest.raises(ProviderConfigError) as caught:
            GeminiMeteredProvider(model_id="m", api_key="   ")
        assert "   " not in str(caught.value).replace(API_KEY_ENV_VAR, "")

    def test_from_env_without_a_key_is_a_config_error(self, monkeypatch):
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        with pytest.raises(ProviderConfigError, match=API_KEY_ENV_VAR):
            gemini_metered_provider_from_env("model-x")


class TestUsageParsing:
    def test_reads_the_token_counts(self):
        provider = build(response(prompt_tokens=120, candidate_tokens=40))
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.input_tokens == 120
        assert completion.output_tokens == 40
        assert completion.model_id == "model-x"
        assert completion.text == "hello"

    def test_reasoning_tokens_are_billed_as_output(self):
        # They are charged as output; omitting them would understate the bill on
        # exactly the requests that cost the most.
        provider = build(response(candidate_tokens=40, thought_tokens=200))
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.output_tokens == 240

    def test_a_missing_thoughts_count_is_treated_as_zero(self):
        provider = build(response(candidate_tokens=40, thought_tokens=None))
        assert provider.complete(system="s", user="u", temperature=0.0).output_tokens == 40

    def test_zero_tokens_are_allowed(self):
        provider = build(response(prompt_tokens=0, candidate_tokens=0))
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert completion.total_tokens == 0

    def test_latency_is_recorded_as_a_non_negative_int(self):
        provider = build(response())
        completion = provider.complete(system="s", user="u", temperature=0.0)
        assert isinstance(completion.latency_ms, int)
        assert completion.latency_ms >= 0

    def test_a_missing_usage_block_is_an_error_not_a_free_call(self):
        # A call recorded as free silently understates the month's spend.
        provider = build(response(usage=False))
        with pytest.raises(UsageError, match="usage_metadata"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_missing_prompt_token_count_is_an_error(self):
        provider = build(response(prompt_tokens=None))
        with pytest.raises(UsageError, match="prompt_token_count"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_negative_token_count_is_an_error(self):
        provider = build(response(prompt_tokens=-5))
        with pytest.raises(UsageError):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_non_integer_token_count_is_an_error(self):
        provider = build(response(prompt_tokens=12.5))
        with pytest.raises(UsageError):
            provider.complete(system="s", user="u", temperature=0.0)


class TestTextValidation:
    def test_a_none_text_is_a_response_error(self):
        provider = build(response(text=None))
        with pytest.raises(ProviderResponseError, match="no text"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_an_empty_text_is_a_response_error(self):
        provider = build(response(text="   "))
        with pytest.raises(ProviderResponseError, match="empty"):
            provider.complete(system="s", user="u", temperature=0.0)


class TestErrorClassification:
    def make_api_error(self, code, status="ERR"):
        error = genai_errors.APIError.__new__(genai_errors.APIError)
        error.code = code
        error.status = status
        error.message = "stub"
        return error

    @pytest.mark.parametrize("code", [408, 409, 429, 500, 502, 503, 504])
    def test_retryable_codes_end_as_a_transient_error(self, code, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        provider = build(self.make_api_error(code))
        with pytest.raises(ProviderTransientError):
            provider.complete(system="s", user="u", temperature=0.0)

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_codes_are_config_errors_and_are_not_retried(self, code):
        provider = build(self.make_api_error(code))
        with pytest.raises(ProviderConfigError, match=API_KEY_ENV_VAR):
            provider.complete(system="s", user="u", temperature=0.0)
        assert len(provider._client.models.calls) == 1

    def test_a_404_is_a_response_error_not_a_retry(self):
        # This is what a withdrawn preview model id looks like.
        provider = build(self.make_api_error(404, status="NOT_FOUND"))
        with pytest.raises(ProviderResponseError, match="404"):
            provider.complete(system="s", user="u", temperature=0.0)

    def test_a_transient_failure_is_retried_the_configured_number_of_times(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _seconds: None)
        provider = build(self.make_api_error(429))
        with pytest.raises(ProviderTransientError, match="after 3 attempts"):
            provider.complete(system="s", user="u", temperature=0.0)
        assert len(provider._client.models.calls) == 3

    def test_no_error_message_contains_a_credential(self):
        provider = build(self.make_api_error(403))
        with pytest.raises(ProviderConfigError) as caught:
            provider.complete(system="s", user="u", temperature=0.0)
        message = str(caught.value)
        assert API_KEY_ENV_VAR in message, "the message should name the variable"
        assert "sk-" not in message and "AIza" not in message


class TestCallShape:
    def test_the_system_prompt_and_user_message_are_kept_separate(self):
        provider = build(response())
        provider.complete(system="SYSTEM RULES", user="USER TEXT", temperature=0.0)
        call = provider._client.models.calls[0]
        assert call["contents"] == "USER TEXT"
        assert call["config"].system_instruction == "SYSTEM RULES"

    def test_the_temperature_is_passed_through(self):
        provider = build(response())
        provider.complete(system="s", user="u", temperature=0.7)
        assert provider._client.models.calls[0]["config"].temperature == 0.7

    def test_the_model_id_is_the_one_called(self):
        provider = build(response(), model_id="model-top")
        provider.complete(system="s", user="u", temperature=0.0)
        assert provider._client.models.calls[0]["model"] == "model-top"
