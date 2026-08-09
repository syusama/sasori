from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace

from .contracts import (
    ApprovalRequest,
    Event,
    Message,
    Model,
    ModelReply,
    RunResult,
    SkillSpec,
    Tool,
    ToolCall,
    is_valid_tool_call_id,
)
from .sqlite_store import (
    ApprovalConflict,
    ApprovalMismatch,
    CallRecord,
    DuplicateCallIdError,
    SQLiteStore,
    Snapshot,
    StoredEvent,
)


class SasoriError(Exception):
    code = "sasori_error"


class ModelCallError(SasoriError):
    code = "model_error"


class ModelTimeoutError(ModelCallError):
    code = "model_timeout"


class MaxStepsExceeded(SasoriError):
    code = "max_steps_exceeded"


class DuplicateToolCallError(SasoriError):
    code = "duplicate_call_id"


class InjectedFault(SasoriError):
    code = "injected_fault"


class RunPaused(SasoriError):
    code = "run_paused"

    def __init__(
        self,
        run_id: str,
        reason: str,
        request: ApprovalRequest | None = None,
    ) -> None:
        super().__init__(f"run {run_id} paused: {reason}")
        self.run_id = run_id
        self.reason = reason
        self.request = request


class RunCancelled(SasoriError):
    code = "run_cancelled"


class _DeadlineExceeded(Exception):
    pass


_IDEMPOTENCY_KEY_ARGUMENT = "idempotency_key"


def _discard_outcome(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _within(awaitable: Awaitable[object], timeout: float) -> object:
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
    except BaseException:
        task.cancel()
        task.add_done_callback(_discard_outcome)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_discard_outcome)
        raise _DeadlineExceeded
    return task.result()


async def _invoke(
    handler: Callable[..., object],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> object:
    if inspect.iscoroutinefunction(handler):
        return await handler(*args, **kwargs)
    value = await asyncio.to_thread(handler, *args, **kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


def _render(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return str(value)


def _json_arguments(arguments: object) -> tuple[object, bool, str]:
    try:
        plain = dict(arguments) if isinstance(arguments, Mapping) else arguments
        encoded = json.dumps(
            plain,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded), isinstance(arguments, Mapping), encoded
    except (TypeError, ValueError):
        marker = {"__invalid_arguments__": type(arguments).__name__}
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
        return marker, False, encoded


class Harness:
    def __init__(
        self,
        model: Model,
        tools: Sequence[Tool] = (),
        *,
        max_steps: int = 8,
        model_timeout: float = 30.0,
        tool_timeout: float = 30.0,
        event_sink: Callable[[Event], None] | None = None,
        store: SQLiteStore | None = None,
        fault_injector: Callable[[str], None] | None = None,
        skills: Sequence[SkillSpec] = (),
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if not math.isfinite(model_timeout) or model_timeout <= 0:
            raise ValueError("model_timeout must be finite and greater than 0")
        if not math.isfinite(tool_timeout) or tool_timeout <= 0:
            raise ValueError("tool_timeout must be finite and greater than 0")

        self.model = model
        self.tools = tuple(tools)
        self.skills = tuple(skills)
        self.max_steps = max_steps
        self.model_timeout = model_timeout
        self.tool_timeout = tool_timeout
        self.event_sink = event_sink
        self.fault_injector = fault_injector
        self._tools: dict[str, Tool] = {}
        for tool in self.tools:
            if not tool.name or not callable(tool.handler):
                raise ValueError("tools need a non-empty name and callable handler")
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            if tool.effect == "idempotent":
                parameter = inspect.signature(tool.handler).parameters.get(
                    _IDEMPOTENCY_KEY_ARGUMENT
                )
                if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                    raise ValueError(
                        "idempotent tool handlers require keyword-only idempotency_key"
                    )
            self._tools[tool.name] = tool
        skill_ids: set[str] = set()
        for skill in self.skills:
            if not isinstance(skill, SkillSpec) or not skill.skill_id:
                raise ValueError("skills must be SkillSpec values with an ID")
            if skill.skill_id in skill_ids:
                raise ValueError(f"duplicate skill ID: {skill.skill_id}")
            unknown = set(skill.tool_names).difference(self._tools)
            if unknown:
                raise ValueError(
                    f"skill {skill.skill_id} references unknown tools: "
                    + ", ".join(sorted(unknown))
                )
            skill_ids.add(skill.skill_id)
        self._owns_store = store is None
        self.store = SQLiteStore() if store is None else store

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    def _observe(self, events: Sequence[Event]) -> None:
        if self.event_sink is None:
            return
        for event in events:
            try:
                self.event_sink(event)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    def _event(
        self,
        state: Snapshot,
        event_type: str,
        *,
        step: int | None = None,
        data: Mapping[str, object] | None = None,
        tool_name: str | None = None,
        call_id: str | None = None,
    ) -> Event:
        return Event(
            event_type,
            state.run_id,
            state.step if step is None else step,
            data or {},
            tool_name=tool_name,
            call_id=call_id,
        )

    def _persist(
        self,
        action: Callable[[], Snapshot],
        events: Sequence[Event] = (),
    ) -> Snapshot:
        self._fault("before_checkpoint_commit")
        state = action()
        self._fault("after_checkpoint_commit")
        self._observe(events)
        return state

    async def run(
        self,
        messages: Sequence[Message],
        *,
        run_id: str | None = None,
        app_id: str | None = None,
    ) -> RunResult:
        run_id = run_id or uuid.uuid4().hex
        initial = Snapshot(run_id, 0, 0, "new", 0, tuple(messages), app_id=app_id)
        started = self._event(
            initial, "run.started", data={"message_count": len(messages)}
        )
        state = self._persist(
            lambda: self.store.start(
                run_id, tuple(messages), started, app_id=app_id
            ),
            (started,),
        )
        return await self._continue(state)

    async def resume(self, run_id: str) -> RunResult:
        state = self.store.load(run_id)
        if state.status == "completed":
            return self._result(state)
        if state.status == "cancelled":
            raise RunCancelled(f"run {run_id} was cancelled")
        if state.status == "effect_unknown":
            raise RunPaused(run_id, "effect_unknown")
        return await self._continue(state)

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]:
        """Read durable events by cursor; event_sink delivery is best-effort."""
        return self.store.stored_events(run_id, after_seq)

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool
    ) -> None:
        state = self.store.load(run_id)
        event = self._event(
            state,
            "approval.resolved",
            data={"approved": approved, "fingerprint": fingerprint},
        )
        self._fault("before_checkpoint_commit")
        _, changed = self.store.resolve_approval(run_id, fingerprint, approved, event)
        self._fault("after_checkpoint_commit")
        if changed:
            self._observe((event,))

    def resolve_effect(
        self,
        run_id: str,
        fingerprint: str,
        action: str,
        *,
        reason: str,
        result: object | None = None,
    ) -> None:
        if action not in ("record_result", "fail", "retry"):
            raise ValueError("action must be record_result, fail, or retry")
        if not reason.strip():
            raise ValueError("manual recovery requires an audit reason")
        state = self.store.load(run_id)
        calls = self.store.calls(run_id, state.step)
        call = next((item for item in calls if item.fingerprint == fingerprint), None)
        if (
            state.status not in ("effect_unknown", "cancelled")
            or call is None
            or call.status != "effect_unknown"
        ):
            raise ValueError("run has no matching effect_unknown call")
        if state.status == "cancelled" and action == "retry":
            raise ValueError("a cancelled run cannot retry or become completed")
        resolved = self._event(
            state,
            "recovery.resolved",
            data={
                "action": action,
                "fingerprint": fingerprint,
                "reason": reason,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        if action == "retry":
            updated = replace(state, status="awaiting_resume")
            self._persist(
                lambda: self.store.update_call(
                    state, updated, call, "approved", (resolved,)
                ),
                (resolved,),
            )
            return

        failed = action == "fail"
        message = Message(
            role="tool",
            content=(
                f"manual recovery failed: {reason}" if failed else _render(result)
            ),
            tool_call_id=call.call_id,
            tool_name=call.name,
            error_code="manual_recovery_failed" if failed else None,
        )
        outcome = self._event(
            state,
            "tool.failed" if failed else "tool.completed",
            data={
                "error_code": "manual_recovery_failed" if failed else None,
                "manual": True,
                "message": message.content,
                "fingerprint": fingerprint,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        updated = replace(
            state,
            status="cancelled" if state.status == "cancelled" else "awaiting_resume",
            history=state.history + (message,),
        )
        self._persist(
            lambda: self.store.update_call(
                state, updated, call, "result", (resolved, outcome), message
            ),
            (resolved, outcome),
        )

    async def _continue(self, state: Snapshot) -> RunResult:
        try:
            return await self._drive(state)
        except (InjectedFault, RunPaused):
            raise
        except asyncio.CancelledError:
            latest = self.store.load(state.run_id)
            if latest.status in ("completed", "failed", "cancelled"):
                raise
            cancelled = self._event(
                latest,
                "run.cancelled",
                data={
                    "effect_unknown": latest.status == "effect_unknown",
                    "previous_status": latest.status,
                    "resumable": False,
                },
            )
            self._persist(
                lambda: self.store.transition(
                    latest, replace(latest, status="cancelled"), (cancelled,)
                ),
                (cancelled,),
            )
            raise

    async def _drive(self, state: Snapshot) -> RunResult:
        while True:
            state = self.store.load(state.run_id)
            if state.status == "completed":
                return self._result(state)
            if state.status == "cancelled":
                raise RunCancelled(f"run {state.run_id} was cancelled")
            if state.status == "effect_unknown":
                raise RunPaused(state.run_id, "effect_unknown")
            if state.status == "awaiting_approval":
                call = next(
                    call
                    for call in self.store.calls(state.run_id, state.step)
                    if call.status == "awaiting_approval"
                )
                raise RunPaused(
                    state.run_id, "approval_required", self._approval_request(call)
                )
            if state.status == "pending_final":
                final = state.history[-1]
                completed = self._event(
                    state, "run.completed", data={"final": final.content}
                )
                updated = replace(
                    state,
                    status="completed",
                    accepted_reply=None,
                    final_message=final,
                )
                self._fault("before_final_commit")
                state = self._persist(
                    lambda: self.store.transition(state, updated, (completed,)),
                    (completed,),
                )
                self._fault("after_final_commit")
                return self._result(state)
            if state.status in ("processing_reply", "paused_recovery", "awaiting_resume"):
                calls = self.store.calls(state.run_id, state.step)
                pending = next((call for call in calls if call.status != "result"), None)
                if pending is None:
                    updated = replace(
                        state, status="ready_model", accepted_reply=None
                    )
                    state = self._persist(
                        lambda: self.store.transition(state, updated), ()
                    )
                    continue
                state = await self._process_call(state, pending)
                continue
            if state.status == "failed":
                raise SasoriError(f"run {state.run_id} is failed")
            if state.step >= self.max_steps:
                error = MaxStepsExceeded(
                    f"run exceeded the maximum of {self.max_steps} model steps"
                )
                failed = self._event(
                    state,
                    "run.failed",
                    data={"error_code": error.code, "message": str(error)},
                )
                self._persist(
                    lambda: self.store.transition(
                        state, replace(state, status="failed"), (failed,)
                    ),
                    (failed,),
                )
                raise error
            state = await self._call_model(state)

    async def _call_model(self, state: Snapshot) -> Snapshot:
        step = state.step + 1
        started = self._event(
            state,
            "model.started",
            step=step,
            data={"message_count": len(state.history)},
        )
        state = self._persist(
            lambda: self.store.transition(state, state, (started,)), (started,)
        )
        try:
            reply = await _within(
                self.model.complete(state.history, self.tools), self.model_timeout
            )
        except _DeadlineExceeded:
            error = ModelTimeoutError(
                f"model call exceeded {self.model_timeout:g} seconds"
            )
            return self._fail_model(state, step, error)
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            error = ModelCallError(
                f"model call failed: {type(cause).__name__}: {cause}"
            )
            self._fail_model(state, step, error)
            raise error from cause
        if not isinstance(reply, ModelReply):
            error = ModelCallError(
                f"model returned {type(reply).__name__}, expected ModelReply"
            )
            self._fail_model(state, step, error)
            raise error

        try:
            calls = self._call_records(state.run_id, step, reply)
        except DuplicateToolCallError as error:
            self._fail_model(state, step, error)
            raise
        assistant = Message(
            role="assistant",
            content=reply.content,
            tool_calls=tuple(
                call for call in reply.tool_calls if isinstance(call, ToolCall)
            ),
            provider_state=reply.provider_state,
        )
        completed = self._event(
            state,
            "model.completed",
            step=step,
            data={
                "final": not reply.tool_calls,
                "tool_call_count": len(reply.tool_calls),
            },
        )
        updated = replace(
            state,
            status="pending_final" if not calls else "processing_reply",
            step=step,
            history=state.history + (assistant,),
            accepted_reply=reply,
        )
        self._fault("before_model_reply_commit")
        try:
            state = self._persist(
                lambda: self.store.accept_reply(
                    state, updated, reply, calls, (completed,)
                ),
                (completed,),
            )
        except DuplicateCallIdError as cause:
            error = DuplicateToolCallError(str(cause))
            self._fail_model(self.store.load(state.run_id), step, error)
            raise error from cause
        self._fault("after_model_reply_commit")
        return state

    def _fail_model(self, state: Snapshot, step: int, error: SasoriError) -> Snapshot:
        model_failed = self._event(
            state,
            "model.failed",
            step=step,
            data={"error_code": error.code, "message": str(error)},
        )
        run_failed = self._event(
            state,
            "run.failed",
            step=step,
            data={"error_code": error.code, "message": str(error)},
        )
        self._persist(
            lambda: self.store.transition(
                state, replace(state, status="failed"), (model_failed, run_failed)
            ),
            (model_failed, run_failed),
        )
        raise error

    def _call_records(
        self, run_id: str, step: int, reply: ModelReply
    ) -> tuple[CallRecord, ...]:
        seen: set[str] = set()
        records = []
        for ordinal, raw in enumerate(reply.tool_calls):
            if isinstance(raw, ToolCall):
                call_id = raw.id if is_valid_tool_call_id(raw.id) else None
                if call_id and call_id in seen:
                    raise DuplicateToolCallError(
                        f"duplicate provider call id in one run: {call_id}"
                    )
                if call_id:
                    seen.add(call_id)
                name = raw.name or None
                arguments, arguments_valid, encoded = _json_arguments(raw.arguments)
                complete = raw.complete
            else:
                call_id = None
                name = None
                arguments = {"__invalid_call__": type(raw).__name__}
                arguments_valid = False
                encoded = json.dumps(arguments, sort_keys=True)
                complete = False
            tool = self._tools.get(name or "")
            effect = tool.effect if tool else "side_effecting"
            tool_revision = (
                tool.tool_revision
                if tool and tool.tool_revision
                else "read-only-unversioned" if tool else "unknown"
            )
            key = None
            if tool and effect == "idempotent" and arguments_valid:
                try:
                    candidate = tool.idempotency_key(dict(arguments))  # type: ignore[misc, arg-type]
                    key = candidate if isinstance(candidate, str) and candidate else None
                except Exception:
                    key = None
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        run_id,
                        step,
                        ordinal,
                        name,
                        json.loads(encoded),
                        tool_revision,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            records.append(
                CallRecord(
                    run_id,
                    step,
                    ordinal,
                    call_id,
                    fingerprint,
                    name,
                    arguments,
                    arguments_valid,
                    complete,
                    effect,
                    key,
                    tool_revision,
                )
            )
        return tuple(records)

    async def _process_call(self, state: Snapshot, call: CallRecord) -> Snapshot:
        if call.status == "pending":
            requested = self._event(
                state,
                "tool.requested",
                data={"complete": call.complete, "fingerprint": call.fingerprint},
                tool_name=call.name,
                call_id=call.call_id,
            )
            state = self._persist(
                lambda: self.store.update_call(
                    state, state, call, "requested", (requested,)
                ),
                (requested,),
            )
            call = self._call(state, call.ordinal)

        validation = self._validate_call(call)
        if validation is not None:
            return self._record_tool_error(state, call, *validation)
        tool = self._tools[call.name or ""]
        arguments = dict(call.arguments)  # validated mapping
        if tool.effect != call.effect:
            return self._mark_effect_unknown(
                state, call, "tool effect metadata changed during recovery"
            )
        if call.effect != "read_only" and (
            tool.tool_revision != call.tool_revision
        ):
            return self._mark_effect_unknown(
                state,
                call,
                "tool contract version or revision changed during recovery",
                error_code="tool_contract_changed",
                pause_reason="tool_contract_changed",
            )
        if tool.effect == "idempotent":
            try:
                key = tool.idempotency_key(arguments)  # type: ignore[misc]
            except Exception as error:
                return self._mark_effect_unknown(
                    state,
                    call,
                    f"idempotency key failed: {type(error).__name__}: {error}",
                )
            if not key or key != call.idempotency_key:
                return self._mark_effect_unknown(
                    state, call, "idempotency key changed during recovery"
                )

        invoke_arguments = dict(arguments)
        if tool.effect == "idempotent":
            invoke_arguments[_IDEMPOTENCY_KEY_ARGUMENT] = call.idempotency_key
        try:
            bound = inspect.signature(tool.handler).bind(**invoke_arguments)
        except (TypeError, ValueError) as error:
            return self._record_tool_error(
                state, call, "invalid_arguments", f"invalid tool arguments: {error}"
            )

        if tool.effect != "read_only":
            decision = self.store.approval(call.fingerprint)
            if call.status == "requested" and decision is None:
                approval = self._event(
                    state,
                    "approval.requested",
                    data={
                        "effect": call.effect,
                        "fingerprint": call.fingerprint,
                        "idempotency_key": call.idempotency_key,
                        "tool_revision": call.tool_revision,
                    },
                    tool_name=call.name,
                    call_id=call.call_id,
                )
                updated = replace(state, status="awaiting_approval")
                state = self._persist(
                    lambda: self.store.request_approval(
                        state, updated, call, approval
                    ),
                    (approval,),
                )
                raise RunPaused(
                    state.run_id,
                    "approval_required",
                    self._approval_request(self._call(state, call.ordinal)),
                )
            if decision is False or call.status == "denied":
                return self._record_tool_error(
                    state, call, "approval_denied", "tool call was denied"
                )
            if decision is None:
                raise RunPaused(
                    state.run_id, "approval_required", self._approval_request(call)
                )

        if call.status == "dispatching" and tool.effect == "side_effecting":
            return self._mark_effect_unknown(
                state,
                call,
                "side effect outcome is unknown; manual recovery is required",
            )

        self._fault("before_tool_dispatch")
        started = self._event(
            state,
            "tool.started",
            data={
                "effect": call.effect,
                "fingerprint": call.fingerprint,
                "idempotency_key": call.idempotency_key,
                "tool_revision": call.tool_revision,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        state = self._persist(
            lambda: self.store.update_call(
                state,
                replace(state, status="processing_reply"),
                call,
                "dispatching",
                (started,),
            ),
            (started,),
        )
        call = self._call(state, call.ordinal)
        self._fault("after_tool_dispatch")
        try:
            output = await _within(
                _invoke(tool.handler, bound.args, bound.kwargs), self.tool_timeout
            )
        except _DeadlineExceeded:
            if tool.effect == "read_only":
                return self._record_tool_error(
                    state,
                    call,
                    "tool_timeout",
                    f"tool call exceeded {self.tool_timeout:g} seconds",
                )
            return self._pause_unknown(
                state,
                call,
                "tool_timeout",
                f"tool outcome is unknown after {self.tool_timeout:g} seconds",
            )
        except asyncio.CancelledError:
            if tool.effect != "read_only":
                failed = self._event(
                    state,
                    "tool.failed",
                    data={
                        "error_code": "effect_unknown",
                        "message": "side effect outcome is unknown after cancellation",
                        "effect_unknown": True,
                        "fingerprint": call.fingerprint,
                    },
                    tool_name=call.name,
                    call_id=call.call_id,
                )
                self._persist(
                    lambda: self.store.update_call(
                        state,
                        replace(state, status="effect_unknown"),
                        call,
                        "effect_unknown",
                        (failed,),
                    ),
                    (failed,),
                )
            raise
        except Exception as error:
            if tool.effect == "read_only":
                return self._record_tool_error(
                    state,
                    call,
                    "tool_exception",
                    f"{type(error).__name__}: {error}",
                )
            return self._pause_unknown(
                state,
                call,
                "tool_exception",
                f"tool outcome is unknown: {type(error).__name__}: {error}",
            )

        self._fault("after_tool_return")
        content = _render(output)
        result = Message(
            role="tool",
            content=content,
            tool_call_id=call.call_id,
            tool_name=call.name,
        )
        completed = self._event(
            state,
            "tool.completed",
            data={"output": content, "fingerprint": call.fingerprint},
            tool_name=call.name,
            call_id=call.call_id,
        )
        updated = replace(state, history=state.history + (result,))
        self._fault("before_tool_result_commit")
        state = self._persist(
            lambda: self.store.update_call(
                state, updated, call, "result", (completed,), result
            ),
            (completed,),
        )
        self._fault("after_tool_result_commit")
        return state

    def _validate_call(self, call: CallRecord) -> tuple[str, str] | None:
        if not call.call_id or not call.name:
            return "malformed_tool_call", "tool call is structurally invalid"
        if not call.complete:
            return "incomplete_tool_call", "incomplete tool call was refused"
        if not call.arguments_valid or not isinstance(call.arguments, Mapping):
            return "malformed_arguments", "tool arguments must be a JSON mapping"
        if not all(isinstance(name, str) for name in call.arguments):
            return "malformed_arguments", "tool argument names must be strings"
        if _IDEMPOTENCY_KEY_ARGUMENT in call.arguments:
            return (
                "reserved_argument",
                "model may not supply the reserved idempotency_key argument",
            )
        if call.name not in self._tools:
            return "unknown_tool", f"unknown tool: {call.name}"
        if call.effect == "idempotent" and not call.idempotency_key:
            return "invalid_idempotency_key", "idempotency key must be non-empty"
        return None

    def _record_tool_error(
        self, state: Snapshot, call: CallRecord, error_code: str, message: str
    ) -> Snapshot:
        result = Message(
            role="tool",
            content=message,
            tool_call_id=call.call_id,
            tool_name=call.name,
            error_code=error_code,
        )
        failed = self._event(
            state,
            "tool.failed",
            data={
                "error_code": error_code,
                "message": message,
                "fingerprint": call.fingerprint,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        updated = replace(
            state, status="processing_reply", history=state.history + (result,)
        )
        return self._persist(
            lambda: self.store.update_call(
                state, updated, call, "result", (failed,), result
            ),
            (failed,),
        )

    def _pause_unknown(
        self,
        state: Snapshot,
        call: CallRecord,
        error_code: str,
        message: str,
    ) -> Snapshot:
        failed = self._event(
            state,
            "tool.failed",
            data={
                "error_code": error_code,
                "message": message,
                "effect_unknown": True,
                "fingerprint": call.fingerprint,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        status = "paused_recovery" if call.effect == "idempotent" else "effect_unknown"
        updated = replace(state, status=status)
        if call.effect == "idempotent":
            state = self._persist(
                lambda: self.store.transition(state, updated, (failed,)), (failed,)
            )
        else:
            state = self._persist(
                lambda: self.store.update_call(
                    state, updated, call, "effect_unknown", (failed,)
                ),
                (failed,),
            )
        raise RunPaused(state.run_id, status)

    def _mark_effect_unknown(
        self,
        state: Snapshot,
        call: CallRecord,
        message: str,
        *,
        error_code: str = "effect_unknown",
        pause_reason: str = "effect_unknown",
    ) -> Snapshot:
        failed = self._event(
            state,
            "tool.failed",
            data={
                "error_code": error_code,
                "message": message,
                "effect_unknown": True,
                "fingerprint": call.fingerprint,
            },
            tool_name=call.name,
            call_id=call.call_id,
        )
        updated = replace(state, status="effect_unknown")
        state = self._persist(
            lambda: self.store.update_call(
                state, updated, call, "effect_unknown", (failed,)
            ),
            (failed,),
        )
        raise RunPaused(state.run_id, pause_reason)

    def _call(self, state: Snapshot, ordinal: int) -> CallRecord:
        return self.store.calls(state.run_id, state.step)[ordinal]

    def _approval_request(self, call: CallRecord) -> ApprovalRequest:
        return ApprovalRequest(
            call.run_id,
            call.step,
            call.ordinal,
            call.call_id or "",
            call.name or "",
            dict(call.arguments) if isinstance(call.arguments, Mapping) else {},
            call.fingerprint,
            call.effect,  # type: ignore[arg-type]
            call.idempotency_key,
            call.tool_revision,
        )

    def _result(self, state: Snapshot) -> RunResult:
        if state.status != "completed" or state.final_message is None:
            raise ValueError("run result is not durably complete")
        return RunResult(
            state.run_id,
            state.final_message,
            state.history,
            self.store.events(state.run_id),
            state.step,
        )


__all__ = [
    "ApprovalConflict",
    "ApprovalMismatch",
    "DuplicateToolCallError",
    "Harness",
    "InjectedFault",
    "MaxStepsExceeded",
    "ModelCallError",
    "ModelTimeoutError",
    "RunPaused",
    "RunCancelled",
    "SasoriError",
]
