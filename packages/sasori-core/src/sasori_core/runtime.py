from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import threading
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from .contracts import (
    ApprovalRequest,
    Event,
    MAX_TOOL_PROGRESS_EVENT_BYTES,
    MAX_TOOL_PROGRESS_EVENTS,
    MAX_TOOL_PROGRESS_TOTAL_BYTES,
    Message,
    Model,
    ModelReply,
    ModelStreamEvent,
    RunResult,
    SkillSpec,
    Tool,
    ToolCall,
    ToolExecutionContext,
    ToolProgressEvent,
    _freeze_event_value,
    is_valid_app_id,
    is_valid_tool_call_id,
    validate_run_id,
)
from .store import (
    ApprovalConflict,
    ApprovalMismatch,
    CallRecord,
    DuplicateCallIdError,
    EphemeralRunStore,
    RunStore,
    Snapshot,
    StoredEvent,
)


class SasoriError(Exception):
    code = "sasori_error"


class ModelCallError(SasoriError):
    code = "model_error"


class ModelTimeoutError(ModelCallError):
    code = "model_timeout"


class ModelStreamProtocolError(ModelCallError):
    code = "model_stream_protocol"


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


class RunBusy(SasoriError):
    code = "run_busy"


class _DeadlineExceeded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _InvalidToolArguments:
    type_name: str


@dataclass(frozen=True, slots=True)
class _InvalidToolCall:
    type_name: str


_IDEMPOTENCY_KEY_ARGUMENT = "idempotency_key"
_TOOL_CONTEXT_ARGUMENT = "tool_context"
_MAX_MODEL_STREAM_EVENTS = 4096
_MAX_MODEL_STREAM_BYTES = 4 * 1024 * 1024
_MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000


def _discard_outcome(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _observe_tool_progress(
    previous: asyncio.Task[None] | None,
    sink: Callable[[ToolProgressEvent], None],
    event: ToolProgressEvent,
) -> None:
    if previous is not None:
        try:
            await previous
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            pass
    try:
        await asyncio.to_thread(sink, event)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        pass
    except Exception:
        pass


class _ToolProgressReporter:
    """Thread-safe, bounded bridge from one live Tool to its event-loop observer."""

    __slots__ = (
        "_accepted_bytes",
        "_accepting",
        "_call_id",
        "_loop",
        "_ordinal",
        "_pending",
        "_run_id",
        "_sequence",
        "_sink",
        "_step",
        "_tail",
        "_tool_name",
        "_lock",
    )

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        sink: Callable[[ToolProgressEvent], None] | None,
        run_id: str,
        step: int,
        ordinal: int,
        call_id: str,
        tool_name: str,
    ) -> None:
        self._loop = loop
        self._sink = sink
        self._run_id = run_id
        self._step = step
        self._ordinal = ordinal
        self._call_id = call_id
        self._tool_name = tool_name
        self._sequence = 0
        self._accepted_bytes = 0
        self._accepting = sink is not None
        self._pending: set[asyncio.Task[None]] = set()
        self._tail: asyncio.Task[None] | None = None
        self._lock = threading.Lock()

    def report(self, data: Mapping[str, object]) -> bool:
        try:
            if not _bounded_json_mapping(data):
                return False
            plain = _json_copy(data)
            assert isinstance(plain, dict)
            encoded = json.dumps(
                plain,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8", "strict")
            size = len(encoded)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False
        if size > MAX_TOOL_PROGRESS_EVENT_BYTES:
            return False

        with self._lock:
            if (
                not self._accepting
                or self._sequence >= MAX_TOOL_PROGRESS_EVENTS
                or self._accepted_bytes + size > MAX_TOOL_PROGRESS_TOTAL_BYTES
            ):
                return False
            self._sequence += 1
            self._accepted_bytes += size
            event = ToolProgressEvent(
                self._run_id,
                self._step,
                self._ordinal,
                self._call_id,
                self._tool_name,
                self._sequence,
                plain,
            )
            try:
                self._loop.call_soon_threadsafe(self._schedule, event)
            except RuntimeError:
                self._accepting = False
                return False
        return True

    def _schedule(self, event: ToolProgressEvent) -> None:
        with self._lock:
            if self._sink is None:
                return
            task = self._loop.create_task(
                _observe_tool_progress(self._tail, self._sink, event)
            )
            self._tail = task
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def close(self) -> None:
        with self._lock:
            self._accepting = False

    def abandon(self) -> None:
        with self._lock:
            self._accepting = False
            self._sink = None
            pending = tuple(self._pending)
        for task in pending:
            task.cancel()

    async def drain(self) -> None:
        await asyncio.sleep(0)
        while True:
            with self._lock:
                pending = tuple(self._pending)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


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


def _valid_utf8_text(
    value: object, *, nonempty: bool = False, maximum_bytes: int | None = None
) -> bool:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return b"\0" not in encoded and (
        maximum_bytes is None or len(encoded) <= maximum_bytes
    )


def _bounded_json_value(value: object) -> bool:
    """Validate one provider JSON tree without recursive Python calls."""

    pending: list[tuple[object, int]] = [(value, 0)]
    seen = 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        if seen > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            return False
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                return False
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is None or type(item) in (bool, int, str):
            continue
        elif type(item) is float and math.isfinite(item):
            continue
        else:
            return False
    return True


def _bounded_json_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and _bounded_json_value(value)


def _json_arguments(arguments: object) -> tuple[object, bool, str]:
    if isinstance(arguments, _InvalidToolArguments):
        marker = {"__invalid_arguments__": arguments.type_name}
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
        return marker, False, encoded
    try:
        if not _bounded_json_mapping(arguments):
            raise ValueError("tool arguments are not bounded JSON")
        plain = _json_copy(arguments)
        assert isinstance(plain, dict)
        encoded = json.dumps(
            plain,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8", "strict")) > _MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        return json.loads(encoded), True, encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        marker = {"__invalid_arguments__": type(arguments).__name__}
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":"))
        return marker, False, encoded


def _json_copy(value: object) -> object:
    """Detach one already validated JSON value from durable/runtime state."""

    if isinstance(value, Mapping):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError(f"value is not canonical JSON: {type(value).__name__}")


def _snapshot_invalid_tool_arguments(arguments: object) -> object:
    """Detach a safe JSON non-mapping while preserving its invalid root shape."""

    try:
        if isinstance(arguments, Mapping) or not _bounded_json_value(arguments):
            raise ValueError("arguments need an invalid JSON root shape")
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8", "strict")) > _MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("tool arguments exceed the byte limit")
        return _freeze_event_value(json.loads(encoded))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _InvalidToolArguments(type(arguments).__name__)


def _snapshot_tool_call(raw: object) -> object:
    """Detach one provider call without turning malformed input into valid input."""

    if isinstance(raw, _InvalidToolCall):
        return raw
    if not isinstance(raw, ToolCall):
        return _InvalidToolCall(type(raw).__name__)
    if isinstance(raw.arguments, _InvalidToolArguments):
        arguments: object = raw.arguments
    else:
        detached, valid, _ = _json_arguments(raw.arguments)
        arguments = (
            _freeze_event_value(detached)
            if valid
            else _snapshot_invalid_tool_arguments(raw.arguments)
        )
    return ToolCall(raw.id, raw.name, arguments, raw.complete)


def _snapshot_model_reply(reply: ModelReply) -> ModelReply:
    """Create the authoritative reply snapshot used after the model boundary."""

    return ModelReply(
        content=reply.content,
        tool_calls=tuple(_snapshot_tool_call(call) for call in reply.tool_calls),
        provider_state=reply.provider_state,
    )


async def _collect_model_stream(
    value: object,
    observe: Callable[[ModelStreamEvent], None],
) -> ModelReply:
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, AsyncIterable):
        raise ModelStreamProtocolError(
            "complete_stream must return an async iterable"
        )

    started = False
    terminal: ModelStreamEvent | None = None
    event_count = 0
    payload_bytes = 0
    async for event in value:
        if not isinstance(event, ModelStreamEvent):
            raise ModelStreamProtocolError(
                "model stream yielded a non-ModelStreamEvent value"
            )
        event_count += 1
        try:
            payload_bytes += len(event.delta.encode("utf-8", "strict"))
            payload_bytes += len(event.message.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            raise ModelStreamProtocolError(
                "model stream contains invalid Unicode"
            ) from None
        if (
            event_count > _MAX_MODEL_STREAM_EVENTS
            or payload_bytes > _MAX_MODEL_STREAM_BYTES
        ):
            raise ModelStreamProtocolError("model stream exceeds its bounded limits")
        if terminal is not None:
            raise ModelStreamProtocolError("model stream continued after its terminal")
        if not started:
            if event.type != "start":
                raise ModelStreamProtocolError("model stream must start with start")
            started = True
            observe(event)
            continue
        if event.type == "start":
            raise ModelStreamProtocolError("model stream emitted duplicate start")
        if event.type in {"text_delta", "thinking_delta", "tool_call_delta"}:
            observe(event)
            continue
        if event.type == "done":
            assert event.reply is not None
            authoritative = _snapshot_model_reply(event.reply)
            terminal = ModelStreamEvent("done", reply=authoritative)
            observe(
                ModelStreamEvent(
                    "done", reply=_snapshot_model_reply(authoritative)
                )
            )
            continue
        if event.type == "error":
            terminal = event
            observe(event)
            continue
        if event.type == "aborted":
            terminal = event
            observe(event)
            continue
        raise ModelStreamProtocolError(f"unknown model stream event: {event.type}")

    if not started:
        raise ModelStreamProtocolError("model stream ended before start")
    if terminal is None:
        raise ModelStreamProtocolError("model stream ended without a terminal")
    if terminal.type == "done":
        assert terminal.reply is not None
        return terminal.reply
    if terminal.type == "error":
        raise ModelCallError(
            f"model stream failed ({terminal.error_code}): {terminal.message}"
        )
    assert terminal.type == "aborted"
    raise asyncio.CancelledError(terminal.message)


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
        model_stream_sink: Callable[[ModelStreamEvent], None] | None = None,
        tool_progress_sink: Callable[[ToolProgressEvent], None] | None = None,
        store: RunStore | None = None,
        fault_injector: Callable[[str], None] | None = None,
        skills: Sequence[SkillSpec] = (),
        _store_factory: Callable[[], RunStore] = EphemeralRunStore,
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
        self.model_stream_sink = model_stream_sink
        self.tool_progress_sink = tool_progress_sink
        self.fault_injector = fault_injector
        self._active_runs: set[str] = set()
        self._idle_waiters: set[asyncio.Future[None]] = set()
        self._tools: dict[str, Tool] = {}
        for tool in self.tools:
            if (
                not isinstance(tool, Tool)
                or not _valid_utf8_text(
                    tool.name, nonempty=True, maximum_bytes=256
                )
                or not callable(tool.handler)
                or (
                    tool.tool_revision is not None
                    and not _valid_utf8_text(
                        tool.tool_revision,
                        nonempty=True,
                        maximum_bytes=256,
                    )
                )
            ):
                raise ValueError(
                    "tools need a valid name, revision, and callable handler"
                )
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            try:
                handler_signature = inspect.signature(tool.handler)
            except (TypeError, ValueError):
                handler_signature = None
            if tool.effect == "idempotent":
                parameter = (
                    None
                    if handler_signature is None
                    else handler_signature.parameters.get(
                        _IDEMPOTENCY_KEY_ARGUMENT
                    )
                )
                if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                    raise ValueError(
                        "idempotent tool handlers require keyword-only idempotency_key"
                    )
            context_parameter = (
                None
                if handler_signature is None
                else handler_signature.parameters.get(_TOOL_CONTEXT_ARGUMENT)
            )
            if (
                context_parameter is not None
                and context_parameter.kind is not inspect.Parameter.KEYWORD_ONLY
            ):
                raise ValueError(
                    "tool_context is reserved for keyword-only runtime injection"
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
        self.store = _store_factory() if store is None else store

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

    def _observe_model_stream(self, event: ModelStreamEvent) -> None:
        if self.model_stream_sink is None:
            return
        try:
            self.model_stream_sink(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @property
    def is_idle(self) -> bool:
        return not self._active_runs

    async def wait_for_idle(self) -> None:
        """Wait until every admitted run/resume drive has fully unwound."""

        if self.is_idle:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._idle_waiters.add(waiter)
        try:
            await waiter
        finally:
            self._idle_waiters.discard(waiter)

    def _enter_drive(self, run_id: str) -> None:
        if run_id in self._active_runs:
            raise RunBusy(f"run {run_id} already has an active drive")
        self._active_runs.add(run_id)

    def _leave_drive(self, run_id: str) -> None:
        self._active_runs.discard(run_id)
        if self._active_runs:
            return
        waiters, self._idle_waiters = self._idle_waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

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
        run_id = (
            uuid.uuid4().hex if run_id is None else validate_run_id(run_id)
        )
        if app_id is not None and not is_valid_app_id(app_id):
            raise ValueError("app_id must match [a-z0-9][a-z0-9._-]{0,63}")
        self._enter_drive(run_id)
        try:
            initial = Snapshot(
                run_id, 0, 0, "new", 0, tuple(messages), app_id=app_id
            )
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
        finally:
            self._leave_drive(run_id)

    async def resume(self, run_id: str) -> RunResult:
        run_id = validate_run_id(run_id)
        self._enter_drive(run_id)
        try:
            state = self.store.load(run_id)
            if state.status == "completed":
                return self._result(state)
            if state.status == "cancelled":
                raise RunCancelled(f"run {run_id} was cancelled")
            if state.status == "effect_unknown":
                raise RunPaused(run_id, "effect_unknown")
            return await self._continue(state)
        finally:
            self._leave_drive(run_id)

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]:
        """Read durable events by cursor; event_sink delivery is best-effort."""
        return self.store.stored_events(validate_run_id(run_id), after_seq)

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool
    ) -> None:
        run_id = validate_run_id(run_id)
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
        run_id = validate_run_id(run_id)
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
            return await run_agent_loop(self, state)
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
            complete_stream = getattr(self.model, "complete_stream", None)
            if callable(complete_stream):
                reply = await _within(
                    _collect_model_stream(
                        complete_stream(state.history, self.tools),
                        self._observe_model_stream,
                    ),
                    self.model_timeout,
                )
            else:
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
        except ModelCallError as error:
            self._fail_model(state, step, error)
            raise AssertionError("_fail_model always raises")
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
        reply = _snapshot_model_reply(reply)
        if not _valid_utf8_text(reply.content) or (
            reply.provider_state is not None
            and not _valid_utf8_text(reply.provider_state)
        ):
            error = ModelCallError("model reply contains invalid text fields")
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
                name = (
                    raw.name
                    if _valid_utf8_text(
                        raw.name, nonempty=True, maximum_bytes=256
                    )
                    else None
                )
                arguments, arguments_valid, encoded = _json_arguments(raw.arguments)
                complete = raw.complete is True
            else:
                call_id = None
                name = None
                raw_type = (
                    raw.type_name
                    if isinstance(raw, _InvalidToolCall)
                    else type(raw).__name__
                )
                arguments = {"__invalid_call__": raw_type}
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
                    candidate = tool.idempotency_key(_json_copy(arguments))  # type: ignore[misc, arg-type]
                    key = (
                        candidate
                        if _valid_utf8_text(
                            candidate, nonempty=True, maximum_bytes=1024
                        )
                        else None
                    )
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
        arguments = _json_copy(call.arguments)
        assert isinstance(arguments, dict)  # guaranteed by _validate_call
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
                key = tool.idempotency_key(_json_copy(arguments))  # type: ignore[misc]
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

        invoke_arguments = _json_copy(arguments)
        assert isinstance(invoke_arguments, dict)
        if tool.effect == "idempotent":
            invoke_arguments[_IDEMPOTENCY_KEY_ARGUMENT] = call.idempotency_key
        progress = _ToolProgressReporter(
            loop=asyncio.get_running_loop(),
            sink=self.tool_progress_sink,
            run_id=state.run_id,
            step=state.step,
            ordinal=call.ordinal,
            call_id=call.call_id or "",
            tool_name=call.name or "",
        )
        try:
            signature = inspect.signature(tool.handler)
            if _TOOL_CONTEXT_ARGUMENT in signature.parameters:
                invoke_arguments[_TOOL_CONTEXT_ARGUMENT] = ToolExecutionContext(
                    progress.report
                )
            bound = signature.bind(**invoke_arguments)
        except (TypeError, ValueError) as error:
            progress.close()
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
            progress.close()
            await progress.drain()
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
            progress.abandon()
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
            progress.close()
            await progress.drain()
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

        progress.close()
        await progress.drain()
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
        if (
            not call.call_id
            or not _valid_utf8_text(
                call.name, nonempty=True, maximum_bytes=256
            )
        ):
            return "malformed_tool_call", "tool call is structurally invalid"
        if call.complete is not True:
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
        if _TOOL_CONTEXT_ARGUMENT in call.arguments:
            return (
                "reserved_argument",
                "model may not supply the reserved tool_context argument",
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


async def run_agent_loop(harness: Harness, state: Snapshot) -> RunResult:
    """Drive one admitted snapshot through the canonical single-agent loop."""

    if not isinstance(harness, Harness):
        raise TypeError("harness must be a Harness")
    if not isinstance(state, Snapshot):
        raise TypeError("state must be a Snapshot")
    while True:
        state = harness.store.load(state.run_id)
        if state.status == "completed":
            return harness._result(state)
        if state.status == "cancelled":
            raise RunCancelled(f"run {state.run_id} was cancelled")
        if state.status == "effect_unknown":
            raise RunPaused(state.run_id, "effect_unknown")
        if state.status == "awaiting_approval":
            call = next(
                call
                for call in harness.store.calls(state.run_id, state.step)
                if call.status == "awaiting_approval"
            )
            raise RunPaused(
                state.run_id,
                "approval_required",
                harness._approval_request(call),
            )
        if state.status == "pending_final":
            final = state.history[-1]
            completed = harness._event(
                state, "run.completed", data={"final": final.content}
            )
            updated = replace(
                state,
                status="completed",
                accepted_reply=None,
                final_message=final,
            )
            harness._fault("before_final_commit")
            state = harness._persist(
                lambda: harness.store.transition(
                    state, updated, (completed,)
                ),
                (completed,),
            )
            harness._fault("after_final_commit")
            return harness._result(state)
        if state.status in (
            "processing_reply",
            "paused_recovery",
            "awaiting_resume",
        ):
            calls = harness.store.calls(state.run_id, state.step)
            pending = next(
                (call for call in calls if call.status != "result"), None
            )
            if pending is None:
                updated = replace(
                    state, status="ready_model", accepted_reply=None
                )
                state = harness._persist(
                    lambda: harness.store.transition(state, updated), ()
                )
                continue
            state = await harness._process_call(state, pending)
            continue
        if state.status == "failed":
            raise SasoriError(f"run {state.run_id} is failed")
        if state.step >= harness.max_steps:
            error = MaxStepsExceeded(
                f"run exceeded the maximum of {harness.max_steps} model steps"
            )
            failed = harness._event(
                state,
                "run.failed",
                data={"error_code": error.code, "message": str(error)},
            )
            harness._persist(
                lambda: harness.store.transition(
                    state, replace(state, status="failed"), (failed,)
                ),
                (failed,),
            )
            raise error
        state = await harness._call_model(state)


__all__ = [
    "ApprovalConflict",
    "ApprovalMismatch",
    "DuplicateToolCallError",
    "Harness",
    "InjectedFault",
    "MaxStepsExceeded",
    "ModelCallError",
    "ModelStreamProtocolError",
    "ModelTimeoutError",
    "RunBusy",
    "RunPaused",
    "RunCancelled",
    "SasoriError",
    "run_agent_loop",
]
