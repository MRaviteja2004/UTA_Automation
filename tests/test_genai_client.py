"""Uber GenAI Gateway configuration and error handling. No live calls."""

import pytest

from ai.client import (
    AIConfigurationError,
    AIProviderError,
    AISettings,
    OpenAIRequirementClient,
    load_ai_settings,
)


def _clear_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("USSO_TOKEN", "GENAI_BASE_URL", "GENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_missing_base_url_does_not_reveal_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("USSO_TOKEN", "usso-test-token")

    with pytest.raises(AIConfigurationError, match="GENAI_BASE_URL") as caught:
        load_ai_settings()

    assert "usso-test-token" not in str(caught.value)
    assert "token is not shown" in str(caught.value)


def test_blank_token_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("USSO_TOKEN", "   ")
    monkeypatch.setenv("GENAI_BASE_URL", "https://genai.example.test/v1")

    with pytest.raises(AIConfigurationError, match="USSO_TOKEN"):
        load_ai_settings()


def test_model_defaults_to_gpt_5_5(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("USSO_TOKEN", "usso-test-token")
    monkeypatch.setenv("GENAI_BASE_URL", "https://genai.example.test/v1")

    settings = load_ai_settings()

    assert settings.model == "gpt-5.5"
    assert settings.base_url == "https://genai.example.test/v1"
    assert "usso-test-token" not in repr(settings)


def test_empty_gateway_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMessage:
        content = "  "

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            del kwargs
            return FakeResponse()

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.chat = FakeChat()

    monkeypatch.setattr("ai.client.OpenAI", FakeOpenAI)
    client = OpenAIRequirementClient(
        AISettings(
            usso_token="usso-test-token",
            base_url="https://genai.example.test/v1",
            model="gpt-5.5",
        )
    )

    with pytest.raises(AIProviderError, match="empty response") as caught:
        client.complete(system_prompt="system", user_prompt="user")

    assert "usso-test-token" not in str(caught.value)
