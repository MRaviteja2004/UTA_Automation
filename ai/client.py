"""Uber GenAI Gateway client for requirement classification.

The USSO token is read from the environment and is omitted from repr and errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Protocol

from openai import OpenAI

_DEFAULT_MODEL = "gpt-5.5"


class RequirementAnalysisError(Exception):
    """Requirement analysis could not be completed."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details = details if details is not None else [message]
        super().__init__("\n".join(self.details))


class AIConfigurationError(RequirementAnalysisError):
    """The GenAI gateway is not configured."""


class AIProviderError(RequirementAnalysisError):
    """The GenAI gateway request failed."""


class InvalidAIResponseError(RequirementAnalysisError):
    """The model response could not be used."""


class RequirementAIClient(Protocol):
    """Minimal client used by the requirement analyzer."""

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return the raw model response text."""


@dataclass(frozen=True)
class AISettings:
    """Gateway settings. The USSO token is excluded from repr."""

    usso_token: str
    base_url: str
    model: str

    def __repr__(self) -> str:
        return (
            "AISettings("
            f"model={self.model!r}, "
            f"base_url={self.base_url!r}, "
            "usso_token='[redacted]')"
        )


def load_ai_settings(environ: Mapping[str, str] | None = None) -> AISettings:
    """Read Uber GenAI Gateway settings from the environment.

    ``USSO_TOKEN`` and ``GENAI_BASE_URL`` are required. ``GENAI_MODEL``
    defaults to ``gpt-5.5``. The token value is never included in errors.
    """

    env = os.environ if environ is None else environ
    token = (env.get("USSO_TOKEN") or "").strip()
    base_url = (env.get("GENAI_BASE_URL") or "").strip()
    model = (env.get("GENAI_MODEL") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

    missing: list[str] = []
    if not token:
        missing.append("USSO_TOKEN")
    if not base_url:
        missing.append("GENAI_BASE_URL")
    if missing:
        raise AIConfigurationError(
            "Uber GenAI Gateway is not configured. Missing: "
            + ", ".join(missing)
            + ". The token is not shown."
        )
    return AISettings(usso_token=token, base_url=base_url, model=model)


class OpenAIRequirementClient:
    """OpenAI SDK client pointed at the Uber GenAI Gateway."""

    def __init__(self, settings: AISettings) -> None:
        if not settings.usso_token or not settings.usso_token.strip():
            raise AIConfigurationError(
                "USSO_TOKEN is not set. The token is not shown."
            )
        if not settings.base_url or not settings.base_url.strip():
            raise AIConfigurationError("GENAI_BASE_URL is not set.")
        self._settings = settings
        self._client = OpenAI(
            base_url=settings.base_url,
            api_key=settings.usso_token,
        )

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._settings.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            raise AIProviderError(
                "The GenAI gateway request failed: "
                + _redact(str(exc), self._settings.usso_token)
            ) from exc

        content = None
        if response.choices:
            content = response.choices[0].message.content
        if not content or not str(content).strip():
            raise AIProviderError("The GenAI gateway returned an empty response.")
        return str(content)


def _redact(message: str, secret: str) -> str:
    if secret and secret in message:
        return message.replace(secret, "[redacted]")
    return message
