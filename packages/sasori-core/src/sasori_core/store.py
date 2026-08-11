from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from .contracts import Event, Message, ModelReply, _freeze_event_value


class StoreError(Exception):
    """Base error for storage-neutral run state operations."""


class RunNotFound(StoreError):
    pass


class RunAlreadyExists(StoreError):
    pass


class ConcurrentRunError(StoreError):
    pass


class DuplicateCallIdError(StoreError):
    pass


class ApprovalMismatch(StoreError):
    pass


class ApprovalConflict(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class Snapshot:
    run_id: str
    revision: int
    generation: int
    status: str
    step: int
    history: tuple[Message, ...]
    accepted_reply: ModelReply | None = None
    final_message: Message | None = None
    app_id: str | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    run_id: str
    step: int
    ordinal: int
    call_id: str | None
    fingerprint: str
    name: str | None
    arguments: object
    arguments_valid: bool
    complete: bool
    effect: str
    idempotency_key: str | None
    tool_revision: str
    status: str = "pending"
    result: Message | None = None

    def __post_init__(self) -> None:
        if isinstance(self.arguments, Mapping):
            object.__setattr__(
                self, "arguments", _freeze_event_value(self.arguments)
            )


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    event: Event


class RunStore(Protocol):
    """The exact storage surface required by the single-agent runtime."""

    def close(self) -> None: ...

    def start(
        self,
        run_id: str,
        messages: Sequence[Message],
        event: Event,
        *,
        app_id: str | None = None,
    ) -> Snapshot: ...

    def load(self, run_id: str) -> Snapshot: ...

    def list_runs(
        self,
        *,
        limit: int,
        before: int | None = None,
        app_id: str | None = None,
    ) -> tuple[tuple[int, Snapshot], ...]: ...

    def transition(
        self, current: Snapshot, updated: Snapshot, events: Sequence[Event] = ()
    ) -> Snapshot: ...

    def accept_reply(
        self,
        current: Snapshot,
        updated: Snapshot,
        reply: ModelReply,
        calls: Sequence[CallRecord],
        events: Sequence[Event],
    ) -> Snapshot: ...

    def calls(self, run_id: str, step: int) -> tuple[CallRecord, ...]: ...

    def update_call(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        status: str,
        events: Sequence[Event] = (),
        result: Message | None = None,
    ) -> Snapshot: ...

    def request_approval(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        event: Event,
    ) -> Snapshot: ...

    def approval(self, fingerprint: str) -> bool | None: ...

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool, event: Event
    ) -> tuple[Snapshot, bool]: ...

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]: ...

    def events(self, run_id: str) -> tuple[Event, ...]: ...


class RunViewSource(Protocol):
    """Read-only state used by public event and run projections."""

    def load(self, run_id: str) -> Snapshot: ...

    def list_runs(
        self,
        *,
        limit: int,
        before: int | None = None,
        app_id: str | None = None,
    ) -> tuple[tuple[int, Snapshot], ...]: ...

    def calls(self, run_id: str, step: int) -> tuple[CallRecord, ...]: ...

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]: ...


class EphemeralRunStore:
    """Deterministic process-local state for the zero-dependency core Harness.

    This implementation is intentionally non-durable and single-process. It is
    useful for small scripts and tests; durable adapters implement ``RunStore``
    outside ``sasori-core``.
    """

    def __init__(self) -> None:
        self._closed = False
        self._runs: dict[str, Snapshot] = {}
        self._run_rows: dict[str, int] = {}
        self._next_row = 1
        self._events: dict[str, list[StoredEvent]] = {}
        self._calls: dict[tuple[str, int], list[CallRecord]] = {}
        self._approvals: dict[str, bool | None] = {}
        self._approval_runs: dict[str, str] = {}

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> EphemeralRunStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    def _append_events(self, run_id: str, events: Sequence[Event]) -> None:
        stored = self._events.setdefault(run_id, [])
        for event in events:
            stored.append(StoredEvent(len(stored) + 1, event))

    def _commit(
        self,
        current: Snapshot,
        updated: Snapshot,
        events: Sequence[Event] = (),
    ) -> Snapshot:
        self._ensure_open()
        actual = self._runs.get(current.run_id)
        if actual is None:
            raise RunNotFound(current.run_id)
        if actual.revision != current.revision:
            raise ConcurrentRunError("run revision changed; concurrent driver rejected")
        durable = replace(
            updated,
            revision=current.revision + 1,
            generation=current.generation + 1,
        )
        self._runs[current.run_id] = durable
        self._append_events(current.run_id, events)
        return durable

    def start(
        self,
        run_id: str,
        messages: Sequence[Message],
        event: Event,
        *,
        app_id: str | None = None,
    ) -> Snapshot:
        self._ensure_open()
        if run_id in self._runs:
            raise RunAlreadyExists(run_id)
        snapshot = Snapshot(
            run_id, 1, 1, "ready_model", 0, tuple(messages), app_id=app_id
        )
        self._runs[run_id] = snapshot
        self._run_rows[run_id] = self._next_row
        self._next_row += 1
        self._events[run_id] = []
        self._append_events(run_id, (event,))
        return snapshot

    def load(self, run_id: str) -> Snapshot:
        self._ensure_open()
        try:
            return self._runs[run_id]
        except KeyError:
            raise RunNotFound(run_id) from None

    def list_runs(
        self,
        *,
        limit: int,
        before: int | None = None,
        app_id: str | None = None,
    ) -> tuple[tuple[int, Snapshot], ...]:
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("before must be a positive integer")
        rows = []
        for run_id, row in self._run_rows.items():
            snapshot = self._runs[run_id]
            if before is not None and row >= before:
                continue
            if app_id is not None and snapshot.app_id != app_id:
                continue
            rows.append((row, snapshot))
        rows.sort(key=lambda item: item[0], reverse=True)
        return tuple(rows[:limit])

    def transition(
        self, current: Snapshot, updated: Snapshot, events: Sequence[Event] = ()
    ) -> Snapshot:
        return self._commit(current, updated, events)

    def accept_reply(
        self,
        current: Snapshot,
        updated: Snapshot,
        reply: ModelReply,
        calls: Sequence[CallRecord],
        events: Sequence[Event],
    ) -> Snapshot:
        existing_ids = {
            item.call_id
            for (stored_run_id, _), items in self._calls.items()
            if stored_run_id == current.run_id
            for item in items
            if item.call_id is not None
        }
        supplied_ids: set[str] = set()
        for call in calls:
            if call.call_id is not None and (
                call.call_id in existing_ids or call.call_id in supplied_ids
            ):
                raise DuplicateCallIdError("duplicate provider call id in one run")
            if call.call_id is not None:
                supplied_ids.add(call.call_id)
        durable = self._commit(current, updated, events)
        self._calls[(current.run_id, updated.step)] = list(calls)
        return durable

    def calls(self, run_id: str, step: int) -> tuple[CallRecord, ...]:
        self.load(run_id)
        return tuple(self._calls.get((run_id, step), ()))

    def update_call(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        status: str,
        events: Sequence[Event] = (),
        result: Message | None = None,
    ) -> Snapshot:
        items = self._calls.get((call.run_id, call.step), [])
        if not 0 <= call.ordinal < len(items) or items[call.ordinal] != call:
            raise ConcurrentRunError("tool call state changed; concurrent driver rejected")
        replacement = replace(call, status=status, result=result)
        durable = self._commit(current, updated, events)
        items[call.ordinal] = replacement
        return durable

    def request_approval(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        event: Event,
    ) -> Snapshot:
        if call.fingerprint in self._approvals:
            raise ConcurrentRunError("approval fingerprint already exists")
        durable = self._commit(current, updated, (event,))
        self._approvals[call.fingerprint] = None
        self._approval_runs[call.fingerprint] = call.run_id
        items = self._calls[(call.run_id, call.step)]
        items[call.ordinal] = replace(call, status="awaiting_approval")
        return durable

    def approval(self, fingerprint: str) -> bool | None:
        self._ensure_open()
        return self._approvals.get(fingerprint)

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool, event: Event
    ) -> tuple[Snapshot, bool]:
        current = self.load(run_id)
        if self._approval_runs.get(fingerprint) != run_id:
            raise ApprovalMismatch(
                "approval does not match the immutable call fingerprint"
            )
        decision = self._approvals[fingerprint]
        if decision is not None:
            if decision != approved:
                raise ApprovalConflict("approval was already resolved differently")
            return current, False
        updated = replace(current, status="awaiting_resume")
        durable = self._commit(current, updated, (event,))
        self._approvals[fingerprint] = approved
        for key, items in self._calls.items():
            if key[0] != run_id:
                continue
            for ordinal, call in enumerate(items):
                if call.fingerprint == fingerprint:
                    items[ordinal] = replace(
                        call, status="approved" if approved else "denied"
                    )
                    return durable, True
        raise ApprovalMismatch(
            "approval does not match the immutable call fingerprint"
        )

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]:
        self.load(run_id)
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        return tuple(item for item in self._events[run_id] if item.seq > after_seq)

    def events(self, run_id: str) -> tuple[Event, ...]:
        return tuple(item.event for item in self.stored_events(run_id))

    def counts(self, run_id: str) -> Mapping[str, int]:
        self.load(run_id)
        return {
            "events": len(self._events[run_id]),
            "checkpoints": self._runs[run_id].generation,
        }


__all__ = [
    "ApprovalConflict",
    "ApprovalMismatch",
    "CallRecord",
    "ConcurrentRunError",
    "DuplicateCallIdError",
    "EphemeralRunStore",
    "RunAlreadyExists",
    "RunNotFound",
    "RunStore",
    "RunViewSource",
    "Snapshot",
    "StoredEvent",
    "StoreError",
]
