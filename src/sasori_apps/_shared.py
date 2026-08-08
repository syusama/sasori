from __future__ import annotations

import math
import os

from sasori import (
    AnthropicMessagesModel,
    Message,
    Model,
    ModelReply,
    OpenAIResponsesModel,
    Tool,
)


class AppConfigurationError(Exception):
    pass


class PromptedModel:
    def __init__(self, model: Model, system_prompt: str) -> None:
        self.model = model
        self.system_prompt = system_prompt

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        policy = Message("system", self.system_prompt)
        history = messages if messages and messages[0] == policy else (policy,) + messages
        return await self.model.complete(history, tools)


def configured_model() -> Model:
    provider = os.environ.get("SASORI_PROVIDER", "").strip().lower()
    model = os.environ.get("SASORI_MODEL", "").strip()
    if provider not in ("openai", "anthropic") or not model:
        raise AppConfigurationError(
            "SASORI_PROVIDER and SASORI_MODEL must select openai or anthropic"
        )
    raw_timeout = os.environ.get("SASORI_PROVIDER_TIMEOUT", "60").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise AppConfigurationError("SASORI_PROVIDER_TIMEOUT is invalid") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise AppConfigurationError("SASORI_PROVIDER_TIMEOUT is invalid")
    localhost = os.environ.get("SASORI_PROVIDER_ALLOW_LOCALHOST", "0").strip()
    if localhost not in ("0", "1"):
        raise AppConfigurationError("SASORI_PROVIDER_ALLOW_LOCALHOST must be 0 or 1")
    base_url = os.environ.get("SASORI_PROVIDER_BASE_URL", "").strip()
    options = {"timeout": timeout, "allow_localhost": localhost == "1"}
    if base_url:
        options["base_url"] = base_url  # type: ignore[assignment]
    if provider == "openai":
        return OpenAIResponsesModel(model, **options)  # type: ignore[arg-type]
    return AnthropicMessagesModel(model, **options)  # type: ignore[arg-type]


__all__ = [
    "AppConfigurationError",
    "PromptedModel",
    "configured_model",
]
