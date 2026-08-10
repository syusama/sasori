from __future__ import annotations

import hashlib
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


__all__ = ["MAX_WORKFLOW_MANIFEST_BYTES", "compose_workflow_manifest"]
