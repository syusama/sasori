from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal as TypingLiteral, TypeAlias

from sasori import Tool, ToolEffect, is_valid_app_id, tool_schema_sha256


MAX_WORKFLOW_STEPS = 128
MAX_WORKFLOW_INPUT_SLOTS = 128
MAX_WORKFLOW_STEP_ARGUMENTS = 128
MAX_WORKFLOW_INPUT_BYTES = 256 * 1024
MAX_WORKFLOW_PAYLOAD_BYTES = 256 * 1024
MAX_WORKFLOW_DEFINITION_BYTES = 1024 * 1024
MAX_LITERAL_BYTES = 128 * 1024
MAX_JSON_DEPTH = 32
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

JsonType: TypeAlias = TypingLiteral[
    "string", "integer", "number", "boolean", "object", "array", "null"
]


class WorkflowError(Exception):
    pass


class WorkflowValidationError(WorkflowError, ValueError):
    pass


def _utf8(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise WorkflowValidationError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise WorkflowValidationError(f"{name} must contain valid Unicode") from None
    if not encoded or b"\x00" in encoded or len(encoded) > maximum:
        raise WorkflowValidationError(f"{name} has an invalid UTF-8 length")
    return value


def _identifier(value: object, name: str) -> str:
    if not is_valid_app_id(value):
        raise WorkflowValidationError(f"{name} must be a valid Sasori identifier")
    return value


def _freeze_json(value: object, *, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        raise WorkflowValidationError("JSON data exceeds the maximum nesting depth")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise WorkflowValidationError("JSON object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowValidationError("JSON numbers must be finite")
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise WorkflowValidationError(
                "JSON strings must contain valid Unicode"
            ) from None
        return value
    raise WorkflowValidationError("value must be finite JSON data")


def plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            plain_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        raise WorkflowValidationError("value is not canonical JSON data") from None


def json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InputSlot:
    key: str
    value_type: JsonType
    required: bool = True
    max_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _utf8(self.key, "input key", maximum=256))
        if self.value_type not in (
            "string",
            "integer",
            "number",
            "boolean",
            "object",
            "array",
            "null",
        ):
            raise WorkflowValidationError("input slot has an invalid JSON type")
        if type(self.required) is not bool:
            raise WorkflowValidationError("input slot required must be boolean")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 1024 * 1024:
            raise WorkflowValidationError("input slot max_bytes is invalid")


@dataclass(frozen=True, slots=True)
class InputRef:
    key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _utf8(self.key, "input key", maximum=256))


@dataclass(frozen=True, slots=True)
class StepRef:
    step_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "step_id"))


@dataclass(frozen=True, slots=True)
class Literal:
    value: object

    def __post_init__(self) -> None:
        frozen = _freeze_json(self.value)
        if len(canonical_json(frozen).encode("utf-8")) > MAX_LITERAL_BYTES:
            raise WorkflowValidationError("literal exceeds the size limit")
        object.__setattr__(self, "value", frozen)


ArgumentBinding: TypeAlias = InputRef | StepRef | Literal


def _binding_data(binding: ArgumentBinding) -> dict[str, object]:
    if isinstance(binding, InputRef):
        return {"kind": "input", "key": binding.key}
    if isinstance(binding, StepRef):
        return {"kind": "step_output", "step_id": binding.step_id}
    return {"kind": "literal", "value": plain_json(binding.value)}


@dataclass(frozen=True, slots=True)
class ToolStep:
    step_id: str
    tool_name: str
    effect: ToolEffect
    tool_revision: str | None
    schema_sha256: str
    arguments: Mapping[str, ArgumentBinding]
    result_type: JsonType
    max_result_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "step_id"))
        object.__setattr__(
            self, "tool_name", _utf8(self.tool_name, "tool_name", maximum=256)
        )
        if self.effect not in ("read_only", "idempotent", "side_effecting"):
            raise WorkflowValidationError("tool step effect is invalid")
        if self.effect == "read_only":
            if self.tool_revision is not None:
                _utf8(self.tool_revision, "tool_revision", maximum=256)
        elif not isinstance(self.tool_revision, str) or not self.tool_revision.strip():
            raise WorkflowValidationError(
                "non-read-only tool steps require tool_revision"
            )
        elif len(self.tool_revision.encode("utf-8", "strict")) > 256:
            raise WorkflowValidationError("tool_revision is too long")
        if not isinstance(self.schema_sha256, str) or _SHA256.fullmatch(
            self.schema_sha256
        ) is None:
            raise WorkflowValidationError("schema_sha256 must be a lowercase SHA-256")
        if not isinstance(self.arguments, Mapping) or not all(
            isinstance(name, str) for name in self.arguments
        ):
            raise WorkflowValidationError("step arguments must map string names to bindings")
        arguments = dict(self.arguments)
        if len(arguments) > MAX_WORKFLOW_STEP_ARGUMENTS:
            raise WorkflowValidationError("step arguments exceed the size limit")
        for name in arguments:
            _utf8(name, "step argument name", maximum=256)
        if any(
            not isinstance(binding, (InputRef, StepRef, Literal))
            for binding in arguments.values()
        ):
            raise WorkflowValidationError(
                "each step argument must be InputRef, StepRef, or Literal"
            )
        object.__setattr__(self, "arguments", MappingProxyType(arguments))
        if self.result_type not in (
            "string",
            "integer",
            "number",
            "boolean",
            "object",
            "array",
            "null",
        ):
            raise WorkflowValidationError("tool step result_type is invalid")
        if (
            type(self.max_result_bytes) is not int
            or not 1 <= self.max_result_bytes <= 1024 * 1024
        ):
            raise WorkflowValidationError("tool step max_result_bytes is invalid")

    @classmethod
    def from_tool(
        cls,
        step_id: str,
        tool: Tool,
        arguments: Mapping[str, ArgumentBinding],
        *,
        result_type: JsonType,
        max_result_bytes: int = 64 * 1024,
    ) -> ToolStep:
        return cls(
            step_id=step_id,
            tool_name=tool.name,
            effect=tool.effect,
            tool_revision=tool.tool_revision,
            schema_sha256=tool_schema_sha256(tool),
            arguments=arguments,
            result_type=result_type,
            max_result_bytes=max_result_bytes,
        )

    def as_data(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "kind": "tool",
            "tool_name": self.tool_name,
            "effect": self.effect,
            "tool_revision": self.tool_revision,
            "schema_sha256": self.schema_sha256,
            "arguments": {
                name: _binding_data(binding)
                for name, binding in sorted(self.arguments.items())
            },
            "result": {
                "type": self.result_type,
                "max_bytes": self.max_result_bytes,
            },
        }


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    workflow_id: str
    version: str
    inputs: tuple[InputSlot, ...]
    steps: tuple[ToolStep, ...]
    output_step: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workflow_id", _identifier(self.workflow_id, "workflow_id")
        )
        object.__setattr__(self, "version", _utf8(self.version, "version", maximum=64))
        inputs = tuple(self.inputs)
        steps = tuple(self.steps)
        if any(not isinstance(slot, InputSlot) for slot in inputs):
            raise WorkflowValidationError("workflow inputs must be InputSlot values")
        if len(inputs) > MAX_WORKFLOW_INPUT_SLOTS:
            raise WorkflowValidationError("workflow input slots exceed the size limit")
        if len({slot.key for slot in inputs}) != len(inputs):
            raise WorkflowValidationError("workflow input keys must be unique")
        if not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
            raise WorkflowValidationError(
                f"workflow must define 1 through {MAX_WORKFLOW_STEPS} steps"
            )
        if any(not isinstance(step, ToolStep) for step in steps):
            raise WorkflowValidationError("workflow steps must be ToolStep values")
        step_ids = tuple(step.step_id for step in steps)
        if len(set(step_ids)) != len(step_ids):
            raise WorkflowValidationError("workflow step IDs must be unique")
        input_keys = {slot.key for slot in inputs}
        completed: set[str] = set()
        for step in steps:
            for binding in step.arguments.values():
                if isinstance(binding, InputRef) and binding.key not in input_keys:
                    raise WorkflowValidationError(
                        f"step {step.step_id} references unknown input {binding.key}"
                    )
                if isinstance(binding, StepRef) and binding.step_id not in completed:
                    raise WorkflowValidationError(
                        "StepRef must reference an earlier declared step"
                    )
            completed.add(step.step_id)
        output_step = _identifier(self.output_step, "output_step")
        if output_step not in completed:
            raise WorkflowValidationError("output_step must name a workflow step")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "output_step", output_step)
        if len(canonical_json(self.as_data()).encode("utf-8")) > MAX_WORKFLOW_DEFINITION_BYTES:
            raise WorkflowValidationError("workflow definition exceeds the size limit")

    @property
    def digest(self) -> str:
        return json_sha256(self.as_data())

    def as_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "execution": "single-harness-ordered-tools-v1",
            "inputs": [
                {
                    "key": slot.key,
                    "type": slot.value_type,
                    "required": slot.required,
                    "max_bytes": slot.max_bytes,
                }
                for slot in self.inputs
            ],
            "steps": [step.as_data() for step in self.steps],
            "output_step": self.output_step,
        }

    def step(self, step_id: str) -> ToolStep:
        return next(step for step in self.steps if step.step_id == step_id)

    def validate_input(self, value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not all(
            isinstance(key, str) for key in value
        ):
            raise WorkflowValidationError("workflow input must be a JSON object")
        frozen = _freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise WorkflowValidationError("workflow input must be a JSON object")
        slots = {slot.key: slot for slot in self.inputs}
        unknown = set(frozen).difference(slots)
        if unknown:
            raise WorkflowValidationError(
                "workflow input contains unknown keys: " + ", ".join(sorted(unknown))
            )
        missing = {
            slot.key for slot in self.inputs if slot.required and slot.key not in frozen
        }
        if missing:
            raise WorkflowValidationError(
                "workflow input is missing required keys: " + ", ".join(sorted(missing))
            )
        for key, item in frozen.items():
            slot = slots[key]
            if not _matches_type(item, slot.value_type):
                raise WorkflowValidationError(
                    f"workflow input {key} must be {slot.value_type}"
                )
            if len(canonical_json(item).encode("utf-8")) > slot.max_bytes:
                raise WorkflowValidationError(f"workflow input {key} exceeds its limit")
        if len(canonical_json(frozen).encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
            raise WorkflowValidationError("workflow input exceeds the total size limit")
        return frozen


def _matches_type(value: object, expected: JsonType) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, Mapping)
    return isinstance(value, tuple)


def validate_typed_value(
    value: object,
    expected: JsonType,
    *,
    maximum: int,
    name: str,
) -> object:
    frozen = _freeze_json(value)
    if not _matches_type(frozen, expected):
        raise WorkflowValidationError(f"{name} must be {expected}")
    if len(canonical_json(frozen).encode("utf-8")) > maximum:
        raise WorkflowValidationError(f"{name} exceeds its size limit")
    return frozen


def resolve_arguments(
    step: ToolStep,
    workflow_input: Mapping[str, object],
    outputs: Mapping[str, object],
) -> Mapping[str, object]:
    resolved: dict[str, object] = {}
    for name, binding in step.arguments.items():
        if isinstance(binding, InputRef):
            if binding.key not in workflow_input:
                raise WorkflowValidationError(
                    f"optional workflow input {binding.key} is not available"
                )
            resolved[name] = workflow_input[binding.key]
        elif isinstance(binding, StepRef):
            if binding.step_id not in outputs:
                raise WorkflowValidationError(
                    f"step output {binding.step_id} is not available"
                )
            resolved[name] = outputs[binding.step_id]
        else:
            resolved[name] = binding.value
    frozen = _freeze_json(resolved)
    if not isinstance(frozen, Mapping):
        raise WorkflowValidationError("resolved tool arguments must be a mapping")
    if len(canonical_json(frozen).encode("utf-8")) > MAX_WORKFLOW_PAYLOAD_BYTES:
        raise WorkflowValidationError("resolved tool arguments exceed the size limit")
    return frozen


__all__ = [
    "ArgumentBinding",
    "InputRef",
    "InputSlot",
    "Literal",
    "MAX_WORKFLOW_DEFINITION_BYTES",
    "MAX_WORKFLOW_INPUT_BYTES",
    "MAX_WORKFLOW_INPUT_SLOTS",
    "MAX_WORKFLOW_PAYLOAD_BYTES",
    "MAX_WORKFLOW_STEP_ARGUMENTS",
    "MAX_WORKFLOW_STEPS",
    "StepRef",
    "ToolStep",
    "WorkflowError",
    "WorkflowSpec",
    "WorkflowValidationError",
    "canonical_json",
    "json_sha256",
    "plain_json",
    "resolve_arguments",
    "validate_typed_value",
]
