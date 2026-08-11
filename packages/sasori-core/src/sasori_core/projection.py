from __future__ import annotations

import re
from collections.abc import Mapping

from .contracts import Event
from .runtime import SasoriError
from .store import RunViewSource, StoredEvent


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


def run_projection(store: RunViewSource, run_id: str) -> dict[str, object]:
    run_id = validate_run_id(run_id)
    snapshot = store.load(run_id)
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


def run_list_projection(
    store: RunViewSource,
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
    "ProjectionIntegrityError",
    "event_projection",
    "run_list_projection",
    "run_projection",
    "validate_run_id",
]
