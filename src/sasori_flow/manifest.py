from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from sasori import Tool, tool_schema_sha256

from .spec import (
    InputRef,
    Literal,
    StepRef,
    WorkflowSpec,
    WorkflowValidationError,
    canonical_json,
    plain_json,
)


MAX_WORKFLOW_MANIFEST_BYTES = 4 * 1024 * 1024

_RECOVERY_POLICY = {
    "read_only": "read_only_replay_allowed",
    "idempotent": "same_verified_business_key_only",
    "side_effecting": "manual_effect_resolution_on_ambiguity",
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_KEYS = {
    "app_id",
    "definition_sha256",
    "execution",
    "inputs",
    "output_step",
    "schema_version",
    "step_count",
    "steps",
    "supports_agent_nodes",
    "supports_branches",
    "supports_parallel",
    "trust",
    "version",
    "workflow_id",
}
_INPUT_KEYS = {"key", "max_bytes", "required", "type"}
_STEP_KEYS = {
    "argument_sources",
    "depends_on",
    "dispatch_schema_sha256",
    "dispatch_tool_name",
    "dispatch_tool_revision",
    "effect",
    "is_output",
    "logical_schema_sha256",
    "logical_tool_name",
    "logical_tool_revision",
    "max_result_bytes",
    "position",
    "recovery_policy",
    "requires_approval",
    "result_type",
    "step_id",
}


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise WorkflowValidationError(f"{label} does not match the exact contract")
    return value


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def validate_workflow_manifest(
    spec: WorkflowSpec,
    manifest: Mapping[str, object],
    *,
    app_id: str,
) -> None:
    """Validate exact detached manifest shape without consulting live Tools."""

    if not isinstance(spec, WorkflowSpec) or not _nonempty_text(app_id):
        raise WorkflowValidationError("Workflow manifest validation input is invalid")
    value = _exact_object(manifest, _MANIFEST_KEYS, "Workflow manifest")
    trust = _exact_object(
        value["trust"], {"execution_mode", "sandboxed"}, "Workflow manifest trust"
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["workflow_id"] != spec.workflow_id
        or value["version"] != spec.version
        or value["definition_sha256"] != spec.digest
        or value["app_id"] != app_id
        or value["execution"] != spec.as_data()["execution"]
        or value["output_step"] != spec.output_step
        or type(value["step_count"]) is not int
        or value["step_count"] != len(spec.steps)
        or value["supports_parallel"] is not False
        or value["supports_branches"] is not False
        or value["supports_agent_nodes"] is not False
        or trust["execution_mode"] != "trusted_installed_python"
        or trust["sandboxed"] is not False
    ):
        raise WorkflowValidationError("Workflow manifest identity is invalid")

    expected_inputs = [
        {
            "key": slot.key,
            "type": slot.value_type,
            "required": slot.required,
            "max_bytes": slot.max_bytes,
        }
        for slot in spec.inputs
    ]
    inputs = value["inputs"]
    if type(inputs) is not list or any(
        type(item) is not dict or set(item) != _INPUT_KEYS for item in inputs
    ):
        raise WorkflowValidationError("Workflow manifest inputs are invalid")
    if canonical_json(inputs) != canonical_json(expected_inputs):
        raise WorkflowValidationError("Workflow manifest inputs changed the definition")

    steps = value["steps"]
    if type(steps) is not list or len(steps) != len(spec.steps):
        raise WorkflowValidationError("Workflow manifest steps are invalid")
    positions = {step.step_id: index for index, step in enumerate(spec.steps)}
    dispatch_names: set[str] = set()
    for position, (stored, step) in enumerate(zip(steps, spec.steps, strict=True), start=1):
        item = _exact_object(stored, _STEP_KEYS, "Workflow manifest step")
        dependencies = sorted(
            {
                binding.step_id
                for binding in step.arguments.values()
                if isinstance(binding, StepRef)
            },
            key=positions.__getitem__,
        )
        expected = {
            "position": position,
            "step_id": step.step_id,
            "depends_on": dependencies,
            "argument_sources": [
                _argument_source(name, binding)
                for name, binding in sorted(step.arguments.items())
            ],
            "logical_tool_name": step.tool_name,
            "effect": step.effect,
            "requires_approval": step.effect != "read_only",
            "recovery_policy": _RECOVERY_POLICY[step.effect],
            "logical_tool_revision": step.tool_revision,
            "logical_schema_sha256": step.schema_sha256,
            "result_type": step.result_type,
            "max_result_bytes": step.max_result_bytes,
            "is_output": step.step_id == spec.output_step,
        }
        actual = {key: item[key] for key in expected}
        if canonical_json(actual) != canonical_json(expected):
            raise WorkflowValidationError(
                "Workflow manifest step changed the definition"
            )
        dispatch_name = item["dispatch_tool_name"]
        dispatch_revision = item["dispatch_tool_revision"]
        if (
            not _nonempty_text(dispatch_name)
            or dispatch_name in dispatch_names
            or not isinstance(item["dispatch_schema_sha256"], str)
            or not _SHA256.fullmatch(item["dispatch_schema_sha256"])
            or (
                step.effect == "read_only"
                and dispatch_revision is not None
            )
            or (
                step.effect != "read_only"
                and not _nonempty_text(dispatch_revision)
            )
        ):
            raise WorkflowValidationError(
                "Workflow manifest dispatch contract is invalid"
            )
        dispatch_names.add(dispatch_name)


def _literal_type(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, tuple):
        return "array"
    raise WorkflowValidationError("literal has an invalid manifest type")


def _argument_source(name: str, binding: object) -> dict[str, object]:
    if isinstance(binding, InputRef):
        return {"name": name, "kind": "input", "ref": binding.key}
    if isinstance(binding, StepRef):
        return {"name": name, "kind": "step", "ref": binding.step_id}
    if isinstance(binding, Literal):
        encoded = canonical_json(binding.value).encode("utf-8")
        return {
            "name": name,
            "kind": "literal",
            "value_type": _literal_type(binding.value),
            "canonical_bytes": len(encoded),
            "value_sha256": hashlib.sha256(encoded).hexdigest(),
        }
    raise WorkflowValidationError("step argument has an invalid manifest binding")


def compose_workflow_manifest(
    spec: WorkflowSpec,
    wrappers: Sequence[Tool],
    *,
    app_id: str,
) -> dict[str, object]:
    """Compose detached static metadata from an already validated compilation."""

    compiled_wrappers = tuple(wrappers)
    if len(compiled_wrappers) != len(spec.steps) or any(
        not isinstance(wrapper, Tool) for wrapper in compiled_wrappers
    ):
        raise WorkflowValidationError(
            "compiled Workflow wrappers do not match the definition"
        )
    if len({wrapper.name for wrapper in compiled_wrappers}) != len(
        compiled_wrappers
    ):
        raise WorkflowValidationError("compiled Workflow wrapper names collided")

    positions = {step.step_id: position for position, step in enumerate(spec.steps)}
    steps: list[dict[str, object]] = []
    for position, (step, wrapper) in enumerate(
        zip(spec.steps, compiled_wrappers, strict=True), start=1
    ):
        if wrapper.effect != step.effect:
            raise WorkflowValidationError(
                f"compiled Workflow step {step.step_id} changed effect"
            )
        if step.effect == "read_only":
            if wrapper.tool_revision is not None:
                raise WorkflowValidationError(
                    f"compiled Workflow step {step.step_id} changed revision policy"
                )
        elif not isinstance(wrapper.tool_revision, str) or not wrapper.tool_revision:
            raise WorkflowValidationError(
                f"compiled Workflow step {step.step_id} is missing its revision"
            )
        dependencies = sorted(
            {
                binding.step_id
                for binding in step.arguments.values()
                if isinstance(binding, StepRef)
            },
            key=positions.__getitem__,
        )
        steps.append(
            {
                "position": position,
                "step_id": step.step_id,
                "depends_on": dependencies,
                "argument_sources": [
                    _argument_source(name, binding)
                    for name, binding in sorted(step.arguments.items())
                ],
                "logical_tool_name": step.tool_name,
                "dispatch_tool_name": wrapper.name,
                "effect": step.effect,
                "requires_approval": step.effect != "read_only",
                "recovery_policy": _RECOVERY_POLICY[step.effect],
                "logical_tool_revision": step.tool_revision,
                "dispatch_tool_revision": wrapper.tool_revision,
                "logical_schema_sha256": step.schema_sha256,
                "dispatch_schema_sha256": tool_schema_sha256(wrapper),
                "result_type": step.result_type,
                "max_result_bytes": step.max_result_bytes,
                "is_output": step.step_id == spec.output_step,
            }
        )

    manifest: dict[str, object] = {
        "schema_version": 1,
        "workflow_id": spec.workflow_id,
        "version": spec.version,
        "definition_sha256": spec.digest,
        "app_id": app_id,
        "execution": spec.as_data()["execution"],
        "output_step": spec.output_step,
        "step_count": len(spec.steps),
        "supports_parallel": False,
        "supports_branches": False,
        "supports_agent_nodes": False,
        "trust": {
            "execution_mode": "trusted_installed_python",
            "sandboxed": False,
        },
        "inputs": [
            {
                "key": slot.key,
                "type": slot.value_type,
                "required": slot.required,
                "max_bytes": slot.max_bytes,
            }
            for slot in spec.inputs
        ],
        "steps": steps,
    }
    if len(canonical_json(manifest).encode("utf-8")) > MAX_WORKFLOW_MANIFEST_BYTES:
        raise WorkflowValidationError("workflow manifest exceeds the size limit")
    detached = plain_json(manifest)
    if not isinstance(detached, dict):
        raise WorkflowValidationError("workflow manifest is not a JSON object")
    return detached


__all__ = [
    "MAX_WORKFLOW_MANIFEST_BYTES",
    "compose_workflow_manifest",
    "validate_workflow_manifest",
]
