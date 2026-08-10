from __future__ import annotations

import json
from collections.abc import Mapping

from sasori import Tool

from .spec import (
    ArgumentBinding,
    InputRef,
    InputSlot,
    JsonType,
    Literal,
    MAX_WORKFLOW_DEFINITION_BYTES,
    StepRef,
    ToolStep,
    WorkflowSpec,
    WorkflowValidationError,
    canonical_json,
)


_DEFINITION_FIELDS = {
    "schema_version",
    "workflow_id",
    "version",
    "execution",
    "inputs",
    "steps",
    "output_step",
}
_INPUT_FIELDS = {"key", "type", "required", "max_bytes"}
_STEP_FIELDS = {
    "step_id",
    "kind",
    "tool_name",
    "effect",
    "tool_revision",
    "schema_sha256",
    "arguments",
    "result",
}
_RESULT_FIELDS = {"type", "max_bytes"}


def _object(value: object, name: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise WorkflowValidationError(f"{name} must be a JSON object")
    actual = set(value)
    unknown = actual.difference(fields)
    if unknown:
        raise WorkflowValidationError(
            f"{name} contains unknown fields: " + ", ".join(sorted(unknown))
        )
    missing = fields.difference(actual)
    if missing:
        raise WorkflowValidationError(
            f"{name} is missing fields: " + ", ".join(sorted(missing))
        )
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise WorkflowValidationError(f"{name} must be a JSON array")
    return value


def _binding(value: object, name: str) -> ArgumentBinding:
    if not isinstance(value, Mapping):
        raise WorkflowValidationError(f"{name} must be a JSON object")
    kind = value.get("kind")
    if kind == "input":
        item = _object(value, name, {"kind", "key"})
        return InputRef(item["key"])  # type: ignore[arg-type]
    if kind == "step_output":
        item = _object(value, name, {"kind", "step_id"})
        return StepRef(item["step_id"])  # type: ignore[arg-type]
    if kind == "literal":
        item = _object(value, name, {"kind", "value"})
        return Literal(item["value"])
    raise WorkflowValidationError(f"{name} has an invalid binding kind")


def _validate_json_value(value: object) -> None:
    try:
        canonical_json(value).encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise WorkflowValidationError(
            "workflow definition must contain valid Unicode"
        ) from None


def workflow_spec_from_data(value: object) -> WorkflowSpec:
    """Parse the exact bounded data contract returned by ``WorkflowSpec.as_data``."""

    _validate_json_value(value)
    definition = _object(value, "workflow definition", _DEFINITION_FIELDS)
    if type(definition["schema_version"]) is not int or definition[
        "schema_version"
    ] != 1:
        raise WorkflowValidationError("workflow schema_version must be 1")
    if definition["execution"] != "single-harness-ordered-tools-v1":
        raise WorkflowValidationError("workflow execution contract is unsupported")

    inputs = []
    for index, raw in enumerate(_array(definition["inputs"], "workflow inputs")):
        item = _object(raw, f"workflow input {index}", _INPUT_FIELDS)
        inputs.append(
            InputSlot(
                key=item["key"],  # type: ignore[arg-type]
                value_type=item["type"],  # type: ignore[arg-type]
                required=item["required"],  # type: ignore[arg-type]
                max_bytes=item["max_bytes"],  # type: ignore[arg-type]
            )
        )

    steps = []
    for index, raw in enumerate(_array(definition["steps"], "workflow steps")):
        item = _object(raw, f"workflow step {index}", _STEP_FIELDS)
        if item["kind"] != "tool":
            raise WorkflowValidationError("workflow step kind must be tool")
        arguments_data = item["arguments"]
        if not isinstance(arguments_data, Mapping) or not all(
            isinstance(key, str) for key in arguments_data
        ):
            raise WorkflowValidationError("workflow step arguments must be a JSON object")
        arguments = {
            name: _binding(binding, f"workflow step {index} argument {name}")
            for name, binding in arguments_data.items()
        }
        result = _object(
            item["result"], f"workflow step {index} result", _RESULT_FIELDS
        )
        steps.append(
            ToolStep(
                step_id=item["step_id"],  # type: ignore[arg-type]
                tool_name=item["tool_name"],  # type: ignore[arg-type]
                effect=item["effect"],  # type: ignore[arg-type]
                tool_revision=item["tool_revision"],  # type: ignore[arg-type]
                schema_sha256=item["schema_sha256"],  # type: ignore[arg-type]
                arguments=arguments,
                result_type=result["type"],  # type: ignore[arg-type]
                max_result_bytes=result["max_bytes"],  # type: ignore[arg-type]
            )
        )

    spec = WorkflowSpec(
        workflow_id=definition["workflow_id"],  # type: ignore[arg-type]
        version=definition["version"],  # type: ignore[arg-type]
        inputs=tuple(inputs),
        steps=tuple(steps),
        output_step=definition["output_step"],  # type: ignore[arg-type]
    )
    if spec.as_data() != dict(definition):
        raise WorkflowValidationError("workflow definition is not canonical data")
    return spec


def workflow_spec_from_json(document: str | bytes) -> WorkflowSpec:
    """Decode strict UTF-8 JSON into the existing W0 ``WorkflowSpec`` contract."""

    if isinstance(document, bytes):
        if len(document) > MAX_WORKFLOW_DEFINITION_BYTES:
            raise WorkflowValidationError("workflow JSON exceeds the size limit")
        if document.startswith(b"\xef\xbb\xbf"):
            raise WorkflowValidationError("workflow JSON must not contain a byte-order mark")
        try:
            text = document.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise WorkflowValidationError("workflow JSON must be valid UTF-8") from None
    elif isinstance(document, str):
        if document.startswith("\ufeff"):
            raise WorkflowValidationError("workflow JSON must not contain a byte-order mark")
        try:
            encoded = document.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise WorkflowValidationError(
                "workflow JSON must contain valid Unicode"
            ) from None
        if len(encoded) > MAX_WORKFLOW_DEFINITION_BYTES:
            raise WorkflowValidationError("workflow JSON exceeds the size limit")
        text = document
    else:
        raise WorkflowValidationError("workflow JSON must be text or UTF-8 bytes")

    def reject_constant(token: str) -> object:
        raise WorkflowValidationError(f"workflow JSON constant {token} is invalid")

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise WorkflowValidationError(f"duplicate workflow JSON key: {key}")
            result[key] = item
        return result

    try:
        value = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except WorkflowValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise WorkflowValidationError("workflow JSON is malformed") from None
    return workflow_spec_from_data(value)


class SerialWorkflowBuilder:
    """Small definition-time convenience layer for an ordered W0 WorkflowSpec."""

    __slots__ = (
        "_built",
        "_input_keys",
        "_inputs",
        "_step_ids",
        "_steps",
        "version",
        "workflow_id",
    )

    def __init__(self, workflow_id: str, *, version: str) -> None:
        self.workflow_id = workflow_id
        self.version = version
        self._inputs: list[InputSlot] = []
        self._steps: list[ToolStep] = []
        self._input_keys: set[str] = set()
        self._step_ids: set[str] = set()
        self._built = False

    def _open(self) -> None:
        if self._built:
            raise WorkflowValidationError("workflow builder was already built")

    def input(
        self,
        key: str,
        *,
        value_type: JsonType,
        required: bool = True,
        max_bytes: int = 64 * 1024,
    ) -> InputRef:
        self._open()
        slot = InputSlot(key, value_type, required=required, max_bytes=max_bytes)
        if slot.key in self._input_keys:
            raise WorkflowValidationError("duplicate workflow input key")
        self._input_keys.add(slot.key)
        self._inputs.append(slot)
        return InputRef(slot.key)

    def step(
        self,
        step_id: str,
        tool: Tool,
        *,
        arguments: Mapping[str, ArgumentBinding],
        result_type: JsonType,
        max_result_bytes: int = 64 * 1024,
    ) -> StepRef:
        self._open()
        if not isinstance(tool, Tool):
            raise WorkflowValidationError("workflow step tool must be a Sasori Tool")
        step = ToolStep.from_tool(
            step_id,
            tool,
            arguments,
            result_type=result_type,
            max_result_bytes=max_result_bytes,
        )
        if step.step_id in self._step_ids:
            raise WorkflowValidationError("duplicate workflow step ID")
        self._step_ids.add(step.step_id)
        self._steps.append(step)
        return StepRef(step.step_id)

    def build(self, *, output: StepRef | str) -> WorkflowSpec:
        self._open()
        output_step = output.step_id if isinstance(output, StepRef) else output
        if not isinstance(output_step, str) or output_step not in self._step_ids:
            raise WorkflowValidationError("workflow output must reference a declared step")
        spec = WorkflowSpec(
            self.workflow_id,
            self.version,
            tuple(self._inputs),
            tuple(self._steps),
            output_step,
        )
        self._built = True
        return spec


__all__ = [
    "SerialWorkflowBuilder",
    "workflow_spec_from_data",
    "workflow_spec_from_json",
]
