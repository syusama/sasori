from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol


ToolEffect = Literal["read_only", "idempotent", "side_effecting"]
ModelStreamEventType = Literal[
    "start",
    "text_delta",
    "thinking_delta",
    "tool_call_delta",
    "done",
    "error",
    "aborted",
]
MAX_TOOL_CALL_ID_BYTES = 256
MAX_APP_ID_BYTES = 64


def is_valid_app_id(value: object) -> bool:
    """Return whether an application ID fits the public runtime contract."""

    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= MAX_APP_ID_BYTES
        and ("a" <= value[0] <= "z" or "0" <= value[0] <= "9")
    ) and all(
        "a" <= character <= "z"
        or "0" <= character <= "9"
        or character in "._-"
        for character in value[1:]
    )


def is_valid_tool_call_id(value: object) -> bool:
    """Return whether an opaque provider call ID fits the public core contract."""

    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return b"\x00" not in encoded and len(encoded) <= MAX_TOOL_CALL_ID_BYTES


def _freeze_event_value(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("event data keys must be strings")
        return MappingProxyType(
            {key: _freeze_event_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_event_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"event data must be JSON-like, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: object = field(default_factory=dict)
    complete: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.arguments, Mapping):
            object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    error_code: str | None = None
    provider_state: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if self.provider_state is not None and not isinstance(self.provider_state, str):
            raise TypeError("provider_state must be a string or None")


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider_state: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if self.provider_state is not None and not isinstance(self.provider_state, str):
            raise TypeError("provider_state must be a string or None")


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """One immutable event in the provider-neutral transient model stream."""

    type: ModelStreamEventType
    delta: str = ""
    reply: ModelReply | None = None
    error_code: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.delta, str)
            or not isinstance(self.message, str)
            or (self.error_code is not None and not isinstance(self.error_code, str))
        ):
            raise ValueError(f"invalid {self.type!r} model stream event")
        if self.type == "start":
            valid = (
                not self.delta
                and self.reply is None
                and self.error_code is None
                and not self.message
            )
        elif self.type in {"text_delta", "thinking_delta", "tool_call_delta"}:
            valid = (
                bool(self.delta)
                and self.reply is None
                and self.error_code is None
                and not self.message
            )
        elif self.type == "done":
            valid = (
                not self.delta
                and isinstance(self.reply, ModelReply)
                and self.error_code is None
                and not self.message
            )
        elif self.type == "error":
            valid = (
                not self.delta
                and self.reply is None
                and isinstance(self.error_code, str)
                and bool(self.error_code.strip())
                and isinstance(self.message, str)
                and bool(self.message.strip())
            )
        elif self.type == "aborted":
            valid = (
                not self.delta
                and self.reply is None
                and self.error_code is None
                and isinstance(self.message, str)
            )
        else:
            valid = False
        if not valid:
            raise ValueError(f"invalid {self.type!r} model stream event")


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    handler: Callable[..., object]
    description: str = ""
    effect: ToolEffect = "side_effecting"
    idempotency_key: Callable[[Mapping[str, object]], str] | None = None
    tool_revision: str | None = None

    def __post_init__(self) -> None:
        if self.effect not in ("read_only", "idempotent", "side_effecting"):
            raise ValueError(f"invalid tool effect: {self.effect}")
        if self.effect == "idempotent" and not callable(self.idempotency_key):
            raise ValueError("idempotent tools require an idempotency_key function")
        if self.effect != "read_only" and (
            not isinstance(self.tool_revision, str)
            or not self.tool_revision.strip()
        ):
            raise ValueError("non-read-only tools require tool_revision")


@dataclass(frozen=True, slots=True)
class SkillSpec:
    skill_id: str
    version: str
    title: str
    description: str
    instructions: str
    tool_names: tuple[str, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_names", tuple(self.tool_names))


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    run_id: str
    step: int
    ordinal: int
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    fingerprint: str
    effect: ToolEffect
    idempotency_key: str | None = None
    tool_revision: str = ""

    def __post_init__(self) -> None:
        frozen = _freeze_event_value(self.arguments)
        if not isinstance(frozen, Mapping):
            raise TypeError("approval arguments must be a mapping")
        object.__setattr__(self, "arguments", frozen)


class Model(Protocol):
    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply: ...


class StreamingModel(Model, Protocol):
    def complete_stream(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> AsyncIterator[ModelStreamEvent]: ...


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    run_id: str
    step: int
    data: Mapping[str, object] = field(default_factory=dict)
    version: int = 1
    tool_name: str | None = None
    call_id: str | None = None

    def __post_init__(self) -> None:
        frozen = _freeze_event_value(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("event data must be a mapping")
        object.__setattr__(self, "data", frozen)


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    final_message: Message
    messages: tuple[Message, ...]
    events: tuple[Event, ...]
    steps: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "events", tuple(self.events))


__all__ = [
    "ApprovalRequest",
    "Event",
    "MAX_APP_ID_BYTES",
    "MAX_TOOL_CALL_ID_BYTES",
    "Message",
    "Model",
    "ModelReply",
    "ModelStreamEvent",
    "ModelStreamEventType",
    "RunResult",
    "SkillSpec",
    "StreamingModel",
    "Tool",
    "ToolCall",
    "ToolEffect",
    "is_valid_app_id",
    "is_valid_tool_call_id",
]
