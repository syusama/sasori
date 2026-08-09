from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Sequence

from sasori import (
    AnthropicMessagesModel,
    Message,
    Model,
    ModelReply,
    OpenAIResponsesModel,
    PluginRegistration,
    SkillSpec,
    Tool,
)
from sasori_context import (
    BoundedContextModel,
    ContextBudget,
    ContextProjector,
    SemanticCompactionModel,
    SemanticCompactionPolicy,
)
from sasori_memory import (
    MemoryBinding,
    MemoryContextModel,
    MemoryError,
    MemoryRunBridge,
    MemoryStore,
    memory_registration,
)


class AppConfigurationError(Exception):
    pass


class PromptedModel:
    def __init__(
        self,
        model: Model,
        system_prompt: str,
        skills: Sequence[SkillSpec] = (),
    ) -> None:
        self.model = model
        self.skills = tuple(skills)
        rendered = "".join(
            (
                f"\n\n[Sasori skill {skill.skill_id}@{skill.version}]\n"
                f"{skill.instructions}"
            )
            for skill in self.skills
        )
        self.system_prompt = system_prompt + rendered

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        policy = Message("system", self.system_prompt)
        history = messages if messages and messages[0] == policy else (policy,) + messages
        return await self.model.complete(history, tools)


_PROVIDER_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
_HARNESS_DEADLINE_MARGIN_SECONDS = 5.0


def _float_setting(name: str, default: str) -> float:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
    except ValueError:
        raise AppConfigurationError(f"{name} is invalid") from None
    if not math.isfinite(value) or not 0 < value <= 600:
        raise AppConfigurationError(f"{name} is invalid")
    return value


def _integer_setting(
    name: str, default: str, *, minimum: int, maximum: int
) -> int:
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError:
        raise AppConfigurationError(f"{name} is invalid") from None
    if not minimum <= value <= maximum:
        raise AppConfigurationError(f"{name} is invalid")
    return value


def _flag_setting(name: str, default: str) -> bool:
    raw = os.environ.get(name, default).strip()
    if raw not in ("0", "1"):
        raise AppConfigurationError(f"{name} must be 0 or 1")
    return raw == "1"


def _provider_model(
    provider: str,
    model: str,
    *,
    timeout: float,
    allow_localhost: bool,
    base_url: str,
) -> Model:
    options: dict[str, object] = {
        "timeout": timeout,
        "allow_localhost": allow_localhost,
    }
    if base_url:
        options["base_url"] = base_url
    if provider == "openai":
        return OpenAIResponsesModel(model, **options)  # type: ignore[arg-type]
    return AnthropicMessagesModel(model, **options)  # type: ignore[arg-type]


def _summarizer_identity(provider: str, model: str, base_url: str) -> str:
    endpoint = base_url or _PROVIDER_DEFAULT_BASE_URLS[provider]
    value = json.dumps(
        {"provider": provider, "model": model, "endpoint": endpoint},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{provider}:{hashlib.sha256(value).hexdigest()}"


def _context_model(
    primary: Model,
    *,
    primary_provider: str,
    primary_timeout: float,
    primary_allow_localhost: bool,
    primary_base_url: str,
) -> tuple[Model, float]:
    max_units_raw = os.environ.get("SASORI_CONTEXT_MAX_UNITS", "").strip()
    related = (
        "SASORI_CONTEXT_RESERVE_UNITS",
        "SASORI_CONTEXT_HOT_TURNS",
        "SASORI_COMPACTION_PROVIDER",
        "SASORI_COMPACTION_MODEL",
        "SASORI_COMPACTION_BASE_URL",
        "SASORI_COMPACTION_ALLOW_LOCALHOST",
        "SASORI_COMPACTION_TIMEOUT",
        "SASORI_COMPACTION_MAX_SOURCE_BYTES",
        "SASORI_COMPACTION_MAX_SUMMARY_BYTES",
        "SASORI_COMPACTION_CACHE_ENTRIES",
        "SASORI_COMPACTION_DIAGNOSTIC_ENTRIES",
    )
    if not max_units_raw:
        if any(os.environ.get(name, "").strip() for name in related):
            raise AppConfigurationError(
                "SASORI_CONTEXT_MAX_UNITS is required for context configuration"
            )
        return primary, 0.0

    max_units = _integer_setting(
        "SASORI_CONTEXT_MAX_UNITS", max_units_raw, minimum=2, maximum=2**31 - 1
    )
    reserve_units = _integer_setting(
        "SASORI_CONTEXT_RESERVE_UNITS",
        str(max_units // 5),
        minimum=0,
        maximum=max_units - 1,
    )
    hot_turns = _integer_setting(
        "SASORI_CONTEXT_HOT_TURNS", "2", minimum=1, maximum=10_000
    )
    try:
        projector = ContextProjector(
            ContextBudget(
                max_units=max_units,
                reserve_units=reserve_units,
                hot_turns=hot_turns,
            )
        )
    except (TypeError, ValueError) as error:
        raise AppConfigurationError("context budget is invalid") from error

    summary_model = os.environ.get("SASORI_COMPACTION_MODEL", "").strip()
    summary_provider = os.environ.get("SASORI_COMPACTION_PROVIDER", "").strip().lower()
    if not summary_model:
        compaction_without_model = tuple(
            name
            for name in related
            if name.startswith("SASORI_COMPACTION_")
            and name != "SASORI_COMPACTION_MODEL"
        )
        if any(
            os.environ.get(name, "").strip() for name in compaction_without_model
        ):
            raise AppConfigurationError(
                "SASORI_COMPACTION_MODEL is required for compaction configuration"
            )
        return BoundedContextModel(primary, projector), 0.0
    if not summary_provider:
        summary_provider = primary_provider
    if summary_provider not in ("openai", "anthropic"):
        raise AppConfigurationError(
            "SASORI_COMPACTION_PROVIDER must be openai or anthropic"
        )

    summary_timeout = _float_setting(
        "SASORI_COMPACTION_TIMEOUT", str(min(primary_timeout, 30.0))
    )
    summary_localhost = _flag_setting(
        "SASORI_COMPACTION_ALLOW_LOCALHOST",
        "1" if primary_allow_localhost else "0",
    )
    summary_base_url = os.environ.get("SASORI_COMPACTION_BASE_URL", "").strip()
    if not summary_base_url and summary_provider == primary_provider:
        summary_base_url = primary_base_url
    summary_transport_timeout = summary_timeout + max(
        1.0, min(5.0, summary_timeout / 10)
    )
    summarizer = _provider_model(
        summary_provider,
        summary_model,
        timeout=summary_transport_timeout,
        allow_localhost=summary_localhost,
        base_url=summary_base_url,
    )
    try:
        policy = SemanticCompactionPolicy(
            max_source_bytes=_integer_setting(
                "SASORI_COMPACTION_MAX_SOURCE_BYTES",
                str(2 * 1024 * 1024),
                minimum=1,
                maximum=64 * 1024 * 1024,
            ),
            max_summary_bytes=_integer_setting(
                "SASORI_COMPACTION_MAX_SUMMARY_BYTES",
                str(16 * 1024),
                minimum=1,
                maximum=1024 * 1024,
            ),
            timeout_seconds=summary_timeout,
            cache_entries=_integer_setting(
                "SASORI_COMPACTION_CACHE_ENTRIES",
                "128",
                minimum=0,
                maximum=100_000,
            ),
            diagnostic_entries=_integer_setting(
                "SASORI_COMPACTION_DIAGNOSTIC_ENTRIES",
                "128",
                minimum=1,
                maximum=100_000,
            ),
        )
        return (
            SemanticCompactionModel(
                primary,
                projector,
                summarizer,
                summarizer_name=_summarizer_identity(
                    summary_provider, summary_model, summary_base_url
                ),
                policy=policy,
            ),
            summary_timeout,
        )
    except (TypeError, ValueError) as error:
        raise AppConfigurationError("semantic compaction configuration is invalid") from error


def configured_model_and_timeout() -> tuple[Model, float]:
    provider = os.environ.get("SASORI_PROVIDER", "").strip().lower()
    model = os.environ.get("SASORI_MODEL", "").strip()
    if provider not in ("openai", "anthropic") or not model:
        raise AppConfigurationError(
            "SASORI_PROVIDER and SASORI_MODEL must select openai or anthropic"
        )
    timeout = _float_setting("SASORI_PROVIDER_TIMEOUT", "60")
    localhost = _flag_setting("SASORI_PROVIDER_ALLOW_LOCALHOST", "0")
    base_url = os.environ.get("SASORI_PROVIDER_BASE_URL", "").strip()
    primary = _provider_model(
        provider,
        model,
        timeout=timeout,
        allow_localhost=localhost,
        base_url=base_url,
    )
    configured, summary_timeout = _context_model(
        primary,
        primary_provider=provider,
        primary_timeout=timeout,
        primary_allow_localhost=localhost,
        primary_base_url=base_url,
    )
    return (
        configured,
        timeout + summary_timeout + _HARNESS_DEADLINE_MARGIN_SECONDS,
    )


def configured_model() -> Model:
    return configured_model_and_timeout()[0]


def configured_memory_runtime(
    core_store,
    app_id: str,
    model: Model,
) -> tuple[Model, PluginRegistration | None, Callable[[object], None] | None]:
    database = os.environ.get("SASORI_MEMORY_DB", "").strip()
    settings = {
        "SASORI_MEMORY_OWNER_ID": os.environ.get("SASORI_MEMORY_OWNER_ID", "").strip(),
        "SASORI_MEMORY_SCOPE_ID": os.environ.get("SASORI_MEMORY_SCOPE_ID", "").strip(),
        "SASORI_MEMORY_SESSION_ID": os.environ.get("SASORI_MEMORY_SESSION_ID", "").strip(),
    }
    if not database:
        if any(settings.values()):
            raise AppConfigurationError(
                "SASORI_MEMORY_DB is required for Memory configuration"
            )
        return model, None, None
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise AppConfigurationError(
            "Memory configuration requires owner, scope, and session IDs"
        )
    try:
        binding = MemoryBinding(
            settings["SASORI_MEMORY_OWNER_ID"],
            app_id,
            settings["SASORI_MEMORY_SCOPE_ID"],
            settings["SASORI_MEMORY_SESSION_ID"],
            1,
        )
        memory_store = MemoryStore(database)
        registration = memory_registration(memory_store, binding)

        def resolve_app_id(run_id: str) -> str | None:
            try:
                return core_store.load(run_id).app_id
            except Exception:
                return None

        bridge = MemoryRunBridge(memory_store, binding, resolve_app_id)
        return (
            MemoryContextModel(model, memory_store, binding),
            registration,
            bridge,
        )
    except MemoryError as error:
        raise AppConfigurationError(
            f"Memory configuration is invalid: {type(error).__name__}"
        ) from error


__all__ = [
    "AppConfigurationError",
    "PromptedModel",
    "configured_model",
    "configured_model_and_timeout",
    "configured_memory_runtime",
]
