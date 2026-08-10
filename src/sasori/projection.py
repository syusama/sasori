from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .contracts import Event, is_valid_app_id, is_valid_tool_call_id
from .runtime import SasoriError
from .sqlite_store import SQLiteStore, StoredEvent


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_EXTERNAL_STATES = {
    "awaiting_approval": "paused",
    "paused_recovery": "paused",
    "effect_unknown": "paused",
    "awaiting_resume": "paused",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_PAUSE_REASONS = {
    "awaiting_approval": "approval_required",
    "paused_recovery": "retryable_idempotent",
    "effect_unknown": "effect_unknown",
    "awaiting_resume": "resume_required",
}
MAX_PUBLIC_PROJECTION_EXTENSION_BYTES = 256 * 1024
_MAX_WORKFLOW_PROJECTION_STEPS = 128
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
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
_MISSING = object()


class ProjectionIntegrityError(SasoriError):
    code = "projection_integrity_failed"

    def __init__(self) -> None:
        super().__init__("public projection extension failed integrity validation")


def validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return run_id


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def event_projection(stored: StoredEvent) -> dict[str, object]:
    event: Event = stored.event
    return {
        "seq": stored.seq,
        "event": {
            "type": event.type,
            "run_id": event.run_id,
            "step": event.step,
            "version": event.version,
            "tool_name": event.tool_name,
            "call_id": event.call_id,
            "data": _plain(event.data),
        },
    }


def run_projection(store: SQLiteStore, run_id: str) -> dict[str, object]:
    run_id = validate_run_id(run_id)
    snapshot = store.load(run_id)
    # ponytail: O(events) latest cursor; add a store query only when long traces prove it matters.
    stored = store.stored_events(run_id)
    pending = None
    pause_reason = _PAUSE_REASONS.get(snapshot.status)
    if snapshot.status in _PAUSE_REASONS or snapshot.status == "cancelled":
        call = next(
            (
                item
                for item in store.calls(run_id, snapshot.step)
                if item.status != "result"
            ),
            None,
        )
        if snapshot.status == "cancelled" and (
            call is None or call.status != "effect_unknown"
        ):
            call = None
        if call is not None:
            if snapshot.status == "cancelled":
                pause_reason = "effect_unknown"
            pending = {
                "fingerprint": call.fingerprint,
                "call_id": call.call_id,
                "tool_name": call.name,
                "arguments": _plain(call.arguments),
                "effect": call.effect,
                "idempotency_key": call.idempotency_key,
                "tool_revision": call.tool_revision,
                "status": call.status,
            }
    final = snapshot.final_message
    initial = next(
        (message.content for message in snapshot.history if message.role == "user"),
        "",
    )
    return {
        "run_id": run_id,
        "app_id": snapshot.app_id,
        "input": initial,
        "state": _EXTERNAL_STATES.get(snapshot.status, "running"),
        "pause_reason": pause_reason,
        "detail": snapshot.status,
        "step": snapshot.step,
        "revision": snapshot.revision,
        "generation": snapshot.generation,
        "latest_seq": stored[-1].seq if stored else 0,
        "final_message": (
            {"role": final.role, "content": final.content} if final is not None else None
        ),
        "pending": pending,
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
        if (
            detail == "ready_model"
            and type(position) is int
            and 0 <= position <= step_count
        ):
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
            or (
                effect != "read_only" and step["logical_tool_revision"] is None
            )
            or not _bounded_projection_text(
                step["dispatch_tool_revision"], maximum=256, nullable=True
            )
            or (
                effect != "read_only" and step["dispatch_tool_revision"] is None
            )
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
    terminal = core.get("state") in {"completed", "failed", "cancelled"}
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


def _validated_projection_extension(
    extension: object, core: dict[str, object]
) -> dict[str, object]:
    if type(extension) is not dict or set(extension) != {"workflow"}:
        raise ProjectionIntegrityError
    try:
        encoded = json.dumps(
            extension,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise ProjectionIntegrityError from None
    if len(encoded) > MAX_PUBLIC_PROJECTION_EXTENSION_BYTES:
        raise ProjectionIntegrityError
    decoded = json.loads(encoded)
    if type(decoded) is not dict or set(decoded) != {"workflow"}:
        raise ProjectionIntegrityError
    _validate_workflow_projection(decoded["workflow"], core)
    return decoded


def compose_run_projection(
    store: SQLiteStore, run_id: str, harness: object | None = None
) -> dict[str, object]:
    """Compose the immutable core run view with one validated public extension."""

    core = run_projection(store, run_id)
    if harness is None:
        return core
    try:
        projector = getattr(harness, "public_projection_extension", _MISSING)
        if projector is _MISSING:
            return core
        if not callable(projector):
            raise ProjectionIntegrityError
        extension = projector(run_id)
        validated = _validated_projection_extension(extension, core)
    except ProjectionIntegrityError:
        raise
    except Exception:
        raise ProjectionIntegrityError from None
    return {**core, **validated}


def run_list_projection(
    store: SQLiteStore,
    *,
    limit: int = 50,
    before: int | None = None,
    app_id: str | None = None,
) -> dict[str, object]:
    rows = store.list_runs(limit=limit, before=before, app_id=app_id)
    items = []
    for rowid, snapshot in rows:
        projected = run_projection(store, snapshot.run_id)
        pending = projected["pending"]
        items.append(
            {
                "cursor": rowid,
                "run_id": snapshot.run_id,
                "app_id": snapshot.app_id,
                "state": projected["state"],
                "pause_reason": projected["pause_reason"],
                "step": snapshot.step,
                "latest_seq": projected["latest_seq"],
                "input_preview": str(projected["input"])[:160],
                "final_preview": (
                    snapshot.final_message.content[:160]
                    if snapshot.final_message is not None
                    else None
                ),
                "pending": (
                    {
                        "tool_name": pending["tool_name"],
                        "effect": pending["effect"],
                    }
                    if isinstance(pending, dict)
                    else None
                ),
            }
        )
    return {
        "items": items,
        "next_before": rows[-1][0] if len(rows) == limit else None,
    }


__all__ = [
    "MAX_PUBLIC_PROJECTION_EXTENSION_BYTES",
    "ProjectionIntegrityError",
    "compose_run_projection",
    "event_projection",
    "run_list_projection",
    "run_projection",
    "validate_run_id",
]
