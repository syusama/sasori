from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence

from .contracts import Event, Message, ModelReply, ModelStreamEvent, Tool


ScriptItem = ModelReply | BaseException | Callable[
    [tuple[Message, ...], tuple[Tool, ...]], ModelReply
]


class ScriptedModel:
    """Offline model that consumes one deterministic reply per invocation."""

    def __init__(self, *items: ScriptItem) -> None:
        self._items = list(items)
        self.calls: list[tuple[tuple[Message, ...], tuple[Tool, ...]]] = []

    @property
    def remaining(self) -> int:
        return len(self._items)

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        self.calls.append((tuple(messages), tuple(tools)))
        if not self._items:
            raise AssertionError("ScriptedModel received an unexpected call")
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        value = item(messages, tools) if callable(item) else item
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ModelReply):
            raise TypeError("script item must resolve to ModelReply")
        return value

    def assert_consumed(self) -> None:
        if self._items:
            raise AssertionError(f"{len(self._items)} scripted model replies remain")


class FunctionModel:
    """Adapt one sync or async callback to the core Model protocol."""

    def __init__(
        self,
        callback: Callable[
            [tuple[Message, ...], tuple[Tool, ...]], object
        ],
    ) -> None:
        self.callback = callback
        self.calls = 0

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        self.calls += 1
        value = self.callback(messages, tools)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ModelReply):
            raise TypeError("FunctionModel callback must resolve to ModelReply")
        return value


class ScriptedStreamingModel:
    """Offline streaming model with one finite event script per invocation."""

    def __init__(self, *streams: Sequence[ModelStreamEvent] | BaseException) -> None:
        self._streams = list(streams)
        self.calls: list[tuple[tuple[Message, ...], tuple[Tool, ...]]] = []

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        raise AssertionError("runtime must prefer complete_stream when available")

    async def complete_stream(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ):
        self.calls.append((tuple(messages), tuple(tools)))
        if not self._streams:
            raise AssertionError("ScriptedStreamingModel received an unexpected call")
        stream = self._streams.pop(0)
        if isinstance(stream, BaseException):
            raise stream
        for event in stream:
            yield event

    def assert_consumed(self) -> None:
        if self._streams:
            raise AssertionError(f"{len(self._streams)} scripted model streams remain")


class TraceRecorder:
    """Callable event sink with a stable semantic projection for golden tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def semantic(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            {
                "type": event.type,
                "version": event.version,
                "run_id": event.run_id,
                "step": event.step,
                "tool_name": event.tool_name,
                "call_id": event.call_id,
                "data": dict(event.data),
            }
            for event in self.events
        )


def assert_semantic_trace(
    actual: Sequence[Mapping[str, object]],
    expected: Sequence[Mapping[str, object]],
) -> None:
    """Compare documented stable fields without timestamps or provider prose."""

    actual_tuple = tuple(dict(item) for item in actual)
    expected_tuple = tuple(dict(item) for item in expected)
    if actual_tuple != expected_tuple:
        raise AssertionError(
            "semantic trace mismatch\n"
            f"expected={expected_tuple!r}\n"
            f"actual={actual_tuple!r}"
        )


__all__ = [
    "FunctionModel",
    "ScriptedModel",
    "ScriptedStreamingModel",
    "TraceRecorder",
    "assert_semantic_trace",
]
