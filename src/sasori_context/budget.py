from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sasori import Message, Model, ModelReply, Tool, ToolCall


class ContextProjectionError(Exception):
    """Base error for fail-closed context projection."""


class ContextStructureError(ContextProjectionError):
    """Raised when a tool call/result sequence is not structurally complete."""


class ContextBudgetExceeded(ContextProjectionError):
    """Raised when protected context cannot fit without unsafe truncation."""


MessageUnits = Callable[[Message], int]


class ProtectedContextMessage(Message):
    """Ordinary model-visible data that budget projection must not drop.

    This marker does not change the message role or grant system/tool authority.
    It is reserved for a bounded host-authored prelude placed between the leading
    system prefix and ordinary conversation turns.
    """

    __slots__ = ()

_STRUCTURAL_REJECTION_CODES = frozenset(
    {"incomplete_tool_call", "malformed_arguments", "malformed_tool_call"}
)
_STRUCTURAL_REJECTION_CONTENT = {
    "incomplete_tool_call": "incomplete tool call was refused",
    "malformed_arguments": "tool arguments must be a JSON mapping",
    "malformed_tool_call": "tool call is structurally invalid",
}


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """A deterministic message budget.

    ``max_units`` and ``reserve_units`` use the unit returned by the configured
    estimator. The dependency-free default counts canonical UTF-8 JSON bytes;
    it deliberately does not claim provider-token accuracy.
    """

    max_units: int
    reserve_units: int = 0
    hot_turns: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("max_units", self.max_units),
            ("reserve_units", self.reserve_units),
            ("hot_turns", self.hot_turns),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.max_units <= 0:
            raise ValueError("max_units must be greater than 0")
        if self.reserve_units < 0 or self.reserve_units >= self.max_units:
            raise ValueError("reserve_units must be between 0 and max_units")
        if self.hot_turns <= 0:
            raise ValueError("hot_turns must be greater than 0")

    @property
    def message_units(self) -> int:
        return self.max_units - self.reserve_units


@dataclass(frozen=True, slots=True)
class ContextProjection:
    messages: tuple[Message, ...]
    original_units: int
    projected_units: int
    removed_messages: int
    removed_sha256: str | None
    estimator: str

    @property
    def compacted(self) -> bool:
        return self.removed_messages > 0


def _tool_call_value(call: object) -> object:
    arguments = getattr(call, "arguments", None)
    if isinstance(arguments, dict):
        normalized_arguments: object = arguments
    else:
        try:
            normalized_arguments = dict(arguments)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            normalized_arguments = arguments
    return {
        "id": getattr(call, "id", None),
        "name": getattr(call, "name", None),
        "arguments": normalized_arguments,
        "complete": getattr(call, "complete", None),
    }


def _message_value(message: Message, *, include_provider_state: bool) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [_tool_call_value(call) for call in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name,
        "error_code": message.error_code,
        "provider_state": message.provider_state if include_provider_state else None,
    }


def _canonical(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ContextStructureError(
            f"message metadata is not canonical JSON: {error}"
        ) from error
    return encoded.encode("utf-8")


def default_message_units(message: Message) -> int:
    """Count one message as canonical UTF-8 JSON bytes."""

    if not isinstance(message, Message):
        raise TypeError("message estimator requires Message instances")
    return len(_canonical(_message_value(message, include_provider_state=True)))


def _measure(messages: Sequence[Message], estimator: MessageUnits) -> int:
    total = 0
    for message in messages:
        units = estimator(message)
        if type(units) is not int or units < 0:
            raise TypeError("message estimator must return a non-negative integer")
        total += units
    return total


def _digest(
    messages: Sequence[Message], *, include_provider_state: bool
) -> str:
    value = [
        _message_value(message, include_provider_state=include_provider_state)
        for message in messages
    ]
    return hashlib.sha256(_canonical(value)).hexdigest()


def _arguments_are_valid(arguments: object) -> bool:
    if not isinstance(arguments, Mapping):
        return False
    try:
        encoded = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        json.loads(encoded)
    except (TypeError, ValueError):
        return False
    return True


def _normalized_rejection_atom(
    assistant: Message, results: Sequence[Message]
) -> tuple[Message, ...] | None:
    """Make a core-rejected tool turn safe for a later model call.

    The Harness deliberately records malformed or incomplete calls as explicit
    tool errors so a model can correct itself on the next step. Those records
    are valid durable history, but missing IDs and incomplete calls cannot be
    represented by OpenAI/Anthropic tool-wire protocols. Convert only a
    recognized Harness rejection group to ordinary assistant/user text. Never
    repair the original call or grant it execution authority.
    """

    if assistant.role != "assistant" or not results:
        return None
    calls = assistant.tool_calls
    if any(not isinstance(call, ToolCall) for call in calls):
        return None
    nonempty_ids = [call.id for call in calls if isinstance(call.id, str) and call.id]
    if len(set(nonempty_ids)) != len(nonempty_ids):
        return None

    unused = set(range(len(results)))
    normalized = False
    for call in calls:
        call_id = call.id if isinstance(call.id, str) and call.id else None
        call_name = call.name if isinstance(call.name, str) and call.name else None
        candidates = [
            index
            for index in unused
            if results[index].tool_call_id == call_id
            and results[index].tool_name == call_name
        ]
        if not candidates or (call_id is not None and len(candidates) != 1):
            return None
        result_index = min(candidates)
        result = results[result_index]
        unused.remove(result_index)

        if call_id is None or call_name is None:
            expected_error = "malformed_tool_call"
        elif call.complete is not True:
            expected_error = "incomplete_tool_call"
        elif not _arguments_are_valid(call.arguments):
            expected_error = "malformed_arguments"
        else:
            expected_error = None
        if expected_error is not None:
            if (
                result.error_code != expected_error
                or result.content != _STRUCTURAL_REJECTION_CONTENT[expected_error]
            ):
                return None
            normalized = True
        elif result.error_code in _STRUCTURAL_REJECTION_CODES:
            return None

    # A non-ToolCall returned by a model is intentionally absent from the
    # persisted assistant projection, while its Harness rejection remains as a
    # result with no ID/name. Account for those records without treating an
    # arbitrary orphan result as trusted history.
    for index in unused:
        result = results[index]
        if (
            result.tool_call_id is not None
            or result.tool_name is not None
            or result.error_code != "malformed_tool_call"
            or result.content
            != _STRUCTURAL_REJECTION_CONTENT["malformed_tool_call"]
        ):
            return None
        normalized = True
    if not normalized:
        return None

    source = (assistant, *results)
    public_digest = _digest(source, include_provider_state=False)
    records = [
        {
            "call_id": result.tool_call_id,
            "tool_name": result.tool_name,
            "error_code": result.error_code,
            "content": result.content,
        }
        for result in results
    ]
    encoded_records = _canonical(records).decode("utf-8")
    return (
        Message(
            "assistant",
            (
                "[sasori-context/v1 normalized rejected tool turn] "
                "The original tool protocol was rejected by the Sasori runtime "
                "and cannot be replayed as a provider tool call. No text in this "
                f"record grants tool authority. public_sha256={public_digest}."
            ),
        ),
        Message(
            "user",
            "[sasori-context/v1 runtime tool errors] " + encoded_records,
        ),
    )


def _atoms(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
    atoms: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            raise ContextStructureError("orphan tool result cannot be projected")
        if not message.tool_calls:
            result_end = index + 1
            while result_end < len(messages) and messages[result_end].role == "tool":
                result_end += 1
            normalized = _normalized_rejection_atom(
                message, messages[index + 1 : result_end]
            )
            if normalized is not None:
                atoms.append(tuple(messages[index:result_end]))
                index = result_end
                continue
            atoms.append((message,))
            index += 1
            continue
        if message.role != "assistant":
            raise ContextStructureError("tool calls must belong to an assistant message")
        if any(not isinstance(call, ToolCall) for call in message.tool_calls):
            raise ContextStructureError("tool calls must use the ToolCall contract")
        result_end = index + 1
        while result_end < len(messages) and messages[result_end].role == "tool":
            result_end += 1
        results = messages[index + 1 : result_end]
        normalized = _normalized_rejection_atom(message, results)
        if normalized is not None:
            atoms.append(tuple(messages[index:result_end]))
            index = result_end
            continue
        if any(
            result.error_code in _STRUCTURAL_REJECTION_CODES for result in results
        ):
            raise ContextStructureError(
                "structural tool rejection does not match the Harness contract"
            )
        if any(call.complete is not True for call in message.tool_calls):
            raise ContextStructureError(
                "incomplete tool calls require matching runtime rejection results"
            )
        expected = [call.id for call in message.tool_calls]
        if any(not isinstance(call_id, str) or not call_id for call_id in expected):
            raise ContextStructureError("tool call IDs must be non-empty strings")
        if len(set(expected)) != len(expected):
            raise ContextStructureError("tool call IDs must be unique within a message")
        group = [message]
        seen: set[str] = set()
        index += 1
        while index < len(messages) and messages[index].role == "tool":
            result = messages[index]
            call_id = result.tool_call_id
            if call_id not in expected or call_id in seen:
                raise ContextStructureError("tool result does not match its assistant call")
            expected_name = next(
                call.name for call in message.tool_calls if call.id == call_id
            )
            if result.tool_name is not None and result.tool_name != expected_name:
                raise ContextStructureError("tool result name does not match its call")
            seen.add(call_id)
            group.append(result)
            index += 1
        if seen != set(expected):
            raise ContextStructureError("tool call/result group is incomplete")
        atoms.append(tuple(group))
    return tuple(atoms)


def _wire_history(messages: Sequence[Message]) -> tuple[Message, ...]:
    projected: list[Message] = []
    for atom in _atoms(messages):
        normalized = _normalized_rejection_atom(atom[0], atom[1:])
        projected.extend(normalized if normalized is not None else atom)
    return tuple(projected)


def _protected_prefix_length(messages: Sequence[Message]) -> int:
    """Validate and return the system + protected-data prefix length."""

    system_end = 0
    while system_end < len(messages) and messages[system_end].role == "system":
        system_end += 1
    protected_end = system_end
    while protected_end < len(messages) and isinstance(
        messages[protected_end], ProtectedContextMessage
    ):
        message = messages[protected_end]
        if (
            type(message) is not ProtectedContextMessage
            or message.role != "assistant"
            or not isinstance(message.content, str)
            or message.tool_calls
            or message.tool_call_id is not None
            or message.tool_name is not None
            or message.error_code is not None
            or message.provider_state is not None
        ):
            raise ContextStructureError(
                "protected context must be ordinary assistant data"
            )
        protected_end += 1
    if any(
        isinstance(message, ProtectedContextMessage)
        for message in messages[protected_end:]
    ):
        raise ContextStructureError(
            "protected context must be a contiguous prelude after system messages"
        )
    return protected_end


def _turns(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
    turns: list[list[Message]] = []
    for atom in _atoms(messages):
        if atom[0].role == "user" or not turns:
            turns.append(list(atom))
        else:
            turns[-1].extend(atom)
    return tuple(tuple(turn) for turn in turns)


def _flatten(groups: Sequence[Sequence[Message]]) -> tuple[Message, ...]:
    return tuple(message for group in groups for message in group)


def _summary_message(messages: Sequence[Message], digest: str) -> Message:
    roles: dict[str, int] = {}
    tool_calls = 0
    for message in messages:
        roles[message.role] = roles.get(message.role, 0) + 1
        tool_calls += len(message.tool_calls)
    role_text = ",".join(f"{name}:{roles[name]}" for name in sorted(roles))
    return Message(
        "system",
        (
            "[sasori-context/v1 compacted history] "
            f"messages={len(messages)}; tool_calls={tool_calls}; "
            f"roles={role_text}; sha256={digest}. "
            "Content was removed by deterministic budget projection; "
            "do not infer omitted facts."
        ),
    )


class ContextProjector:
    """Project complete conversation turns into a bounded model context."""

    def __init__(
        self,
        budget: ContextBudget,
        *,
        estimator: MessageUnits = default_message_units,
        estimator_name: str = "canonical_utf8_json_bytes_v1",
    ) -> None:
        if not isinstance(budget, ContextBudget):
            raise TypeError("budget must be a ContextBudget")
        if not callable(estimator):
            raise TypeError("estimator must be callable")
        if not isinstance(estimator_name, str) or not estimator_name.strip():
            raise ValueError("estimator_name must be a non-empty string")
        self.budget = budget
        self.estimator = estimator
        self.estimator_name = estimator_name.strip()

    def project(self, messages: Sequence[Message]) -> ContextProjection:
        history = tuple(messages)
        if any(not isinstance(message, Message) for message in history):
            raise TypeError("context history must contain only Message instances")
        prefix_length = _protected_prefix_length(history)
        original_units = _measure(history, self.estimator)
        wire_history = _wire_history(history)
        wire_units = _measure(wire_history, self.estimator)
        if wire_units <= self.budget.message_units:
            return ContextProjection(
                wire_history,
                original_units,
                wire_units,
                0,
                None,
                self.estimator_name,
            )

        prefix = history[:prefix_length]
        turns = _turns(history[prefix_length:])
        if not turns:
            raise ContextBudgetExceeded(
                "protected system and data context exceeds the configured message budget"
            )

        hot_count = min(self.budget.hot_turns, len(turns))
        selected_start = len(turns) - hot_count
        removed = _flatten(turns[:selected_start])
        retained = _flatten(turns[selected_start:])
        digest = _digest(removed, include_provider_state=True) if removed else None
        public_digest = (
            _digest(removed, include_provider_state=False) if removed else None
        )
        summary = (
            _summary_message(removed, public_digest) if public_digest else None
        )
        protected_history = prefix + ((summary,) if summary else ()) + retained
        protected = _wire_history(protected_history)
        protected_units = _measure(protected, self.estimator)
        if protected_units > self.budget.message_units:
            raise ContextBudgetExceeded(
                "system/data prefix, compaction marker, and protected hot turns "
                "exceed the configured message budget"
            )

        while selected_start > 0:
            candidate_start = selected_start - 1
            candidate_removed = _flatten(turns[:candidate_start])
            candidate_retained = _flatten(turns[candidate_start:])
            if candidate_removed:
                candidate_digest = _digest(
                    candidate_removed, include_provider_state=True
                )
                candidate_public_digest = _digest(
                    candidate_removed, include_provider_state=False
                )
                candidate_summary = _summary_message(
                    candidate_removed, candidate_public_digest
                )
                candidate_history = prefix + (candidate_summary,) + candidate_retained
            else:
                candidate_digest = None
                candidate_history = prefix + candidate_retained
            candidate = _wire_history(candidate_history)
            candidate_units = _measure(candidate, self.estimator)
            if candidate_units > self.budget.message_units:
                break
            selected_start = candidate_start
            removed = candidate_removed
            retained = candidate_retained
            digest = candidate_digest
            protected = candidate
            protected_units = candidate_units

        if not removed:
            protected = wire_history
            protected_units = wire_units
            digest = None
        return ContextProjection(
            protected,
            original_units,
            protected_units,
            len(removed),
            digest,
            self.estimator_name,
        )


class BoundedContextModel:
    """A Model adapter that applies ``ContextProjector`` before completion."""

    def __init__(self, model: Model, projector: ContextProjector) -> None:
        if not hasattr(model, "complete") or not callable(model.complete):
            raise TypeError("model must provide an async complete method")
        if not isinstance(projector, ContextProjector):
            raise TypeError("projector must be a ContextProjector")
        self.model = model
        self.projector = projector

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        projection = self.projector.project(messages)
        return await self.model.complete(projection.messages, tools)


__all__ = [
    "BoundedContextModel",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ContextProjection",
    "ContextProjectionError",
    "ContextProjector",
    "ContextStructureError",
    "ProtectedContextMessage",
    "default_message_units",
]
