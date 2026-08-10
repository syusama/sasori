from __future__ import annotations

from sasori import compose_run_projection, tool_schema_sha256

from .runtime import WorkflowHarness, WorkflowIntegrityError
from .spec import canonical_json


MAX_WORKFLOW_PROJECTION_BYTES = 256 * 1024
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
    "workflow_public_projection",
    "workflow_public_run_projection",
]
