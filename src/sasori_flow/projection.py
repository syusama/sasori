from __future__ import annotations

import re

from sasori import compose_run_projection, tool_schema_sha256
from sasori_core.contracts import is_valid_app_id, is_valid_tool_call_id
from sasori_core.projection import ProjectionIntegrityError

from .runtime import WorkflowHarness, WorkflowIntegrityError
from .spec import canonical_json


MAX_WORKFLOW_PROJECTION_BYTES = 256 * 1024
_MAX_WORKFLOW_PROJECTION_STEPS = 128
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_PAUSE_STATUSES = {
    "approval_required": "approval_required",
    "resume_required": "resume_required",
    "retryable_idempotent": "retryable_idempotent",
    "effect_unknown": "effect_unknown",
}
_CALL_STATUSES = {
    "pending": "requested",
    "requested": "requested",
    "awaiting_approval": "approval_required",
    "approved": "resume_required",
    "denied": "resume_required",
    "dispatching": "running",
    "effect_unknown": "effect_unknown",
}
_WORKFLOW_EFFECTS = {"read_only", "idempotent", "side_effecting"}
_WORKFLOW_RESULT_TYPES = {
    "string",
    "integer",
    "number",
    "boolean",
    "object",
    "array",
    "null",
}
_WORKFLOW_STATUSES = {
    "pending",
    "requested",
    "running",
    "approval_required",
    "resume_required",
    "retryable_idempotent",
    "effect_unknown",
    "completed",
    "failed",
    "stopped",
}
_WORKFLOW_PROJECTION_KEYS = {
    "schema_version",
    "workflow_id",
    "version",
    "definition_sha256",
    "app_id",
    "execution",
    "output_step",
    "current_step_id",
    "latest_seq",
    "steps",
}
_WORKFLOW_STEP_KEYS = {
    "position",
    "step_id",
    "kind",
    "logical_tool_name",
    "dispatch_tool_name",
    "effect",
    "logical_tool_revision",
    "dispatch_tool_revision",
    "logical_schema_sha256",
    "dispatch_schema_sha256",
    "result_type",
    "max_result_bytes",
    "call_id",
    "status",
    "error_code",
}


def _bounded_projection_text(
    value: object, *, maximum: int, nullable: bool = False
) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return bool(encoded) and b"\x00" not in encoded and len(encoded) <= maximum


def _pending_matches_step(pending: object, step: dict[str, object]) -> bool:
    return type(pending) is dict and (
        pending.get("call_id") == step["call_id"]
        and pending.get("tool_name") == step["dispatch_tool_name"]
        and pending.get("effect") == step["effect"]
    )


def _validate_workflow_semantics(
    workflow: dict[str, object], core: dict[str, object]
) -> None:
    steps = workflow["steps"]
    assert type(steps) is list
    statuses = [step["status"] for step in steps]
    state = core.get("state")
    pause_reason = core.get("pause_reason")
    pending = core.get("pending")
    detail = core.get("detail")
    position = core.get("step")
    step_count = len(steps)

    if state == "completed":
        if (
            pause_reason is not None
            or pending is not None
            or detail != "completed"
            or position != step_count + 1
            or any(status != "completed" for status in statuses)
        ):
            raise ProjectionIntegrityError
        return

    if state == "running":
        if pause_reason is not None or pending is not None:
            raise ProjectionIntegrityError
        if detail == "ready_model" and type(position) is int and 0 <= position <= step_count:
            if any(status != "completed" for status in statuses[:position]) or any(
                status != "pending" for status in statuses[position:]
            ):
                raise ProjectionIntegrityError
            return
        if (
            detail == "processing_reply"
            and type(position) is int
            and 1 <= position <= step_count
        ):
            if (
                any(status != "completed" for status in statuses[: position - 1])
                or statuses[position - 1] not in {"requested", "running"}
                or any(status != "pending" for status in statuses[position:])
            ):
                raise ProjectionIntegrityError
            return
        if (
            detail == "pending_final"
            and position == step_count + 1
            and all(status == "completed" for status in statuses)
        ):
            return
        raise ProjectionIntegrityError

    if state == "paused":
        expected = {
            "approval_required": ("approval_required", "awaiting_approval"),
            "resume_required": ("resume_required", "awaiting_resume"),
            "retryable_idempotent": ("retryable_idempotent", "paused_recovery"),
            "effect_unknown": ("effect_unknown", "effect_unknown"),
        }.get(pause_reason)
        if (
            expected is None
            or detail != expected[1]
            or type(position) is not int
            or not 1 <= position <= step_count
            or any(status != "completed" for status in statuses[: position - 1])
            or statuses[position - 1] != expected[0]
            or any(status != "pending" for status in statuses[position:])
            or not _pending_matches_step(pending, steps[position - 1])
        ):
            raise ProjectionIntegrityError
        return

    if state == "failed":
        if (
            pause_reason is not None
            or pending is not None
            or detail != "failed"
            or type(position) is not int
            or not 0 <= position <= step_count + 1
        ):
            raise ProjectionIntegrityError
        if position == 0:
            if any(status != "stopped" for status in statuses):
                raise ProjectionIntegrityError
            return
        if position == step_count + 1:
            if any(status != "completed" for status in statuses):
                raise ProjectionIntegrityError
            return
        if (
            any(status != "completed" for status in statuses[: position - 1])
            or statuses[position - 1] not in {"completed", "failed"}
            or any(status != "stopped" for status in statuses[position:])
        ):
            raise ProjectionIntegrityError
        return

    if state == "cancelled":
        if pause_reason == "effect_unknown":
            if (
                detail != "cancelled"
                or type(position) is not int
                or not 1 <= position <= step_count
                or any(status != "completed" for status in statuses[: position - 1])
                or statuses[position - 1] != "effect_unknown"
                or any(status != "stopped" for status in statuses[position:])
                or not _pending_matches_step(pending, steps[position - 1])
            ):
                raise ProjectionIntegrityError
            return
        if (
            pause_reason is not None
            or pending is not None
            or detail != "cancelled"
            or type(position) is not int
            or not 0 <= position <= step_count + 1
            or "effect_unknown" in statuses
        ):
            raise ProjectionIntegrityError
        if position == 0:
            if any(status != "stopped" for status in statuses):
                raise ProjectionIntegrityError
            return
        if position == step_count + 1:
            if any(status != "completed" for status in statuses):
                raise ProjectionIntegrityError
            return
        if (
            any(status != "completed" for status in statuses[: position - 1])
            or statuses[position - 1] not in {"completed", "failed", "stopped"}
            or any(status != "stopped" for status in statuses[position:])
        ):
            raise ProjectionIntegrityError
        return

    raise ProjectionIntegrityError


def _validate_workflow_projection(
    workflow: object, core: dict[str, object]
) -> None:
    if type(workflow) is not dict or set(workflow) != _WORKFLOW_PROJECTION_KEYS:
        raise ProjectionIntegrityError
    if workflow["schema_version"] != 1 or type(workflow["schema_version"]) is not int:
        raise ProjectionIntegrityError
    if not is_valid_app_id(workflow["workflow_id"]):
        raise ProjectionIntegrityError
    if not _bounded_projection_text(workflow["version"], maximum=64):
        raise ProjectionIntegrityError
    if (
        not isinstance(workflow["definition_sha256"], str)
        or _SHA256.fullmatch(workflow["definition_sha256"]) is None
        or not is_valid_app_id(workflow["app_id"])
        or workflow["app_id"] != core.get("app_id")
        or workflow["execution"] != "single-harness-ordered-tools-v1"
        or not is_valid_app_id(workflow["output_step"])
    ):
        raise ProjectionIntegrityError
    latest_seq = workflow["latest_seq"]
    if (
        type(latest_seq) is not int
        or latest_seq < 0
        or latest_seq != core.get("latest_seq")
    ):
        raise ProjectionIntegrityError
    steps = workflow["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= _MAX_WORKFLOW_PROJECTION_STEPS:
        raise ProjectionIntegrityError

    step_ids: set[str] = set()
    dispatch_names: set[str] = set()
    call_ids: set[str] = set()
    for position, step in enumerate(steps, start=1):
        if type(step) is not dict or set(step) != _WORKFLOW_STEP_KEYS:
            raise ProjectionIntegrityError
        step_id = step["step_id"]
        dispatch_name = step["dispatch_tool_name"]
        effect = step["effect"]
        status = step["status"]
        call_id = step["call_id"]
        error_code = step["error_code"]
        if (
            type(step["position"]) is not int
            or step["position"] != position
            or not is_valid_app_id(step_id)
            or step_id in step_ids
            or step["kind"] != "tool"
            or not _bounded_projection_text(step["logical_tool_name"], maximum=256)
            or not _bounded_projection_text(dispatch_name, maximum=256)
            or dispatch_name in dispatch_names
            or effect not in _WORKFLOW_EFFECTS
            or not _bounded_projection_text(
                step["logical_tool_revision"], maximum=256, nullable=True
            )
            or (effect != "read_only" and step["logical_tool_revision"] is None)
            or not _bounded_projection_text(
                step["dispatch_tool_revision"], maximum=256, nullable=True
            )
            or (effect != "read_only" and step["dispatch_tool_revision"] is None)
            or not isinstance(step["logical_schema_sha256"], str)
            or _SHA256.fullmatch(step["logical_schema_sha256"]) is None
            or not isinstance(step["dispatch_schema_sha256"], str)
            or _SHA256.fullmatch(step["dispatch_schema_sha256"]) is None
            or step["result_type"] not in _WORKFLOW_RESULT_TYPES
            or type(step["max_result_bytes"]) is not int
            or not 1 <= step["max_result_bytes"] <= 1024 * 1024
            or status not in _WORKFLOW_STATUSES
            or (
                status in {"approval_required", "resume_required", "effect_unknown"}
                and effect == "read_only"
            )
            or (status == "retryable_idempotent" and effect != "idempotent")
            or not _bounded_projection_text(error_code, maximum=256, nullable=True)
            or (status == "failed") != (error_code is not None)
        ):
            raise ProjectionIntegrityError
        if call_id is not None and (
            not is_valid_tool_call_id(call_id) or call_id in call_ids
        ):
            raise ProjectionIntegrityError
        if (status == "pending" and call_id is not None) or (
            status not in {"pending", "stopped"} and call_id is None
        ):
            raise ProjectionIntegrityError
        step_ids.add(step_id)
        dispatch_names.add(dispatch_name)
        if call_id is not None:
            call_ids.add(call_id)

    if workflow["output_step"] not in step_ids:
        raise ProjectionIntegrityError
    terminal = core.get("state") in _TERMINAL_STATES
    expected_current = None
    if not terminal:
        expected_current = next(
            (
                step["step_id"]
                for step in steps
                if step["status"] not in {"completed", "stopped"}
            ),
            None,
        )
    if workflow["current_step_id"] != expected_current:
        raise ProjectionIntegrityError
    _validate_workflow_semantics(workflow, core)


def validate_workflow_projection_extension(
    extension: object, core: dict[str, object]
) -> None:
    """Fail closed unless ``extension`` is the exact bound Workflow view."""

    if type(extension) is not dict or set(extension) != {"workflow"}:
        raise ProjectionIntegrityError
    _validate_workflow_projection(extension["workflow"], core)


def _step_status(
    *,
    call,
    position: int,
    current_position: int,
    pause_reason: object,
    terminal: bool,
) -> tuple[str, str | None]:
    if call is None:
        return ("stopped" if terminal else "pending"), None
    if call.result is not None:
        error_code = call.result.error_code
        return ("failed" if error_code is not None else "completed"), error_code
    if call.status == "effect_unknown":
        return "effect_unknown", None
    if terminal:
        return "stopped", None
    if position == current_position and pause_reason in _PAUSE_STATUSES:
        return _PAUSE_STATUSES[pause_reason], None
    try:
        return _CALL_STATUSES[call.status], None
    except KeyError:
        raise WorkflowIntegrityError(
            f"workflow step has an unknown durable call status: {call.status}"
        ) from None


def _public_from_detailed(
    harness: WorkflowHarness,
    detailed: dict[str, object],
) -> dict[str, object]:
    core = detailed["run"]
    if not isinstance(core, dict):
        raise WorkflowIntegrityError("workflow core projection shape changed")
    run_id = core.get("run_id")
    if not isinstance(run_id, str):
        raise WorkflowIntegrityError("workflow core projection run ID changed")
    snapshot = harness.store.load(run_id)
    terminal = snapshot.status in _TERMINAL_STATES
    pause_reason = core.get("pause_reason")
    detailed_steps = detailed.get("steps")
    if not isinstance(detailed_steps, list) or len(detailed_steps) != len(
        harness.spec.steps
    ):
        raise WorkflowIntegrityError("workflow detailed step projection changed")

    steps = []
    for position, (step, wrapper, trusted) in enumerate(
        zip(harness.spec.steps, harness.tools, detailed_steps, strict=True), start=1
    ):
        if not isinstance(trusted, dict):
            raise WorkflowIntegrityError("workflow detailed step shape changed")
        calls = harness.store.calls(run_id, position)
        call = calls[0] if len(calls) == 1 else None
        if len(calls) > 1:
            raise WorkflowIntegrityError(
                f"workflow step {step.step_id} durable call count changed"
            )
        status, error_code = _step_status(
            call=call,
            position=position,
            current_position=snapshot.step,
            pause_reason=pause_reason,
            terminal=terminal,
        )
        steps.append(
            {
                "position": position,
                "step_id": step.step_id,
                "kind": "tool",
                "logical_tool_name": step.tool_name,
                "dispatch_tool_name": wrapper.name,
                "effect": step.effect,
                "logical_tool_revision": step.tool_revision,
                "dispatch_tool_revision": wrapper.tool_revision,
                "logical_schema_sha256": step.schema_sha256,
                "dispatch_schema_sha256": tool_schema_sha256(wrapper),
                "result_type": step.result_type,
                "max_result_bytes": step.max_result_bytes,
                "call_id": call.call_id if call is not None else None,
                "status": status,
                "error_code": error_code,
            }
        )

    current_step_id = None
    if not terminal:
        current_step_id = next(
            (
                str(item["step_id"])
                for item in steps
                if item["status"] not in ("completed", "stopped")
            ),
            None,
        )
    projection = {
        "schema_version": 1,
        "workflow_id": harness.spec.workflow_id,
        "version": harness.spec.version,
        "definition_sha256": harness.spec.digest,
        "app_id": harness.app_id,
        "execution": "single-harness-ordered-tools-v1",
        "output_step": harness.spec.output_step,
        "current_step_id": current_step_id,
        "latest_seq": core.get("latest_seq"),
        "steps": steps,
    }
    if len(canonical_json(projection).encode("utf-8")) > MAX_WORKFLOW_PROJECTION_BYTES:
        raise WorkflowIntegrityError("workflow public projection exceeds the size limit")
    return projection


def workflow_public_projection(
    harness: WorkflowHarness, run_id: str
) -> dict[str, object]:
    if not isinstance(harness, WorkflowHarness):
        raise TypeError("harness must be a WorkflowHarness")
    return _public_from_detailed(harness, harness.projection(run_id))


def workflow_public_run_projection(
    harness: WorkflowHarness, run_id: str
) -> dict[str, object]:
    if not isinstance(harness, WorkflowHarness):
        raise TypeError("harness must be a WorkflowHarness")
    return compose_run_projection(harness.store, run_id, harness)


__all__ = [
    "MAX_WORKFLOW_PROJECTION_BYTES",
    "validate_workflow_projection_extension",
    "workflow_public_projection",
    "workflow_public_run_projection",
]
