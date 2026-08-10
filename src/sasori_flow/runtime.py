from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sasori import (
    Harness,
    Message,
    ModelReply,
    RunResult,
    Tool,
    ToolCall,
    is_valid_app_id,
    run_projection,
    tool_schema_sha256,
)

from .spec import (
    MAX_WORKFLOW_INPUT_BYTES,
    ToolStep,
    WorkflowError,
    WorkflowSpec,
    WorkflowValidationError,
    canonical_json,
    json_sha256,
    plain_json,
    resolve_arguments,
    validate_typed_value,
)


_CONTROL_PREFIX = "sasori.workflow.control.v1\n"
_INPUT_PREFIX = "sasori.workflow.input.v1\n"


class WorkflowCompileError(WorkflowError):
    pass


class WorkflowIntegrityError(WorkflowError):
    pass


class WorkflowStepFailed(WorkflowError):
    def __init__(self, step_id: str, error_code: str, message: str) -> None:
        self.step_id = step_id
        self.error_code = error_code
        super().__init__(f"workflow step {step_id} failed ({error_code}): {message}")


def workflow_app_id(spec: WorkflowSpec) -> str:
    stem = spec.workflow_id[:40].rstrip("._-") or "workflow"
    value = f"flow.{stem}.{spec.digest[:12]}"
    if not is_valid_app_id(value):
        raise WorkflowCompileError("workflow definition cannot produce a valid app_id")
    return value


def _control_data(spec: WorkflowSpec) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": spec.workflow_id,
        "workflow_version": spec.version,
        "spec_sha256": spec.digest,
        "execution": "single-harness-ordered-tools-v1",
    }


def _control_message(spec: WorkflowSpec) -> Message:
    return Message("system", _CONTROL_PREFIX + canonical_json(_control_data(spec)))


def _input_binding_message(
    value: Mapping[str, object], public_content: str
) -> Message:
    return Message(
        "system",
        _INPUT_PREFIX
        + canonical_json(
            {
                "value": value,
                "public_content_sha256": hashlib.sha256(
                    public_content.encode("utf-8", "strict")
                ).hexdigest(),
            }
        ),
    )


def _call_id(spec: WorkflowSpec, step: ToolStep) -> str:
    digest = hashlib.sha256(
        f"{spec.digest}\0{step.step_id}".encode("utf-8")
    ).hexdigest()
    return f"wf_{digest[:48]}"


def _wrapper_name(spec: WorkflowSpec, step: ToolStep) -> str:
    safe = step.step_id.replace(".", "_")[:36].rstrip("_-") or "step"
    digest = hashlib.sha256(
        f"{spec.digest}\0{step.step_id}\0{step.tool_name}".encode("utf-8")
    ).hexdigest()
    return f"wf_{safe}_{digest[:16]}"


def _wrapper_revision(spec: WorkflowSpec, step: ToolStep) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "definition_sha256": spec.digest,
                "step": step.as_data(),
                "wrapper": "typed-tool-envelope-v1",
            }
        ).encode("utf-8")
    ).hexdigest()


def _tool_map(tools: Sequence[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


def _validate_base_tools(spec: WorkflowSpec, tools: Sequence[Tool]) -> None:
    available = _tool_map(tools)
    for step in spec.steps:
        tool = available.get(step.tool_name)
        if tool is None:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} references unknown tool {step.tool_name}"
            )
        if tool.effect != step.effect:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} tool effect changed"
            )
        if tool.tool_revision != step.tool_revision:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} tool revision changed"
            )
        if tool_schema_sha256(tool) != step.schema_sha256:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} tool schema changed"
            )
        signature = inspect.signature(tool.handler)
        parameters: set[str] = set()
        for parameter in signature.parameters.values():
            if parameter.name == "idempotency_key":
                if (
                    tool.effect != "idempotent"
                    or parameter.kind is not inspect.Parameter.KEYWORD_ONLY
                ):
                    raise WorkflowCompileError(
                        f"workflow step {step.step_id} idempotency_key is reserved"
                    )
                continue
            if parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise WorkflowCompileError(
                    f"workflow step {step.step_id} tool parameters must be explicit keywords"
                )
            parameters.add(parameter.name)
        if set(step.arguments) != parameters:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} arguments do not match tool parameters"
            )
        probe = {name: object() for name in step.arguments}
        if tool.effect == "idempotent":
            probe["idempotency_key"] = "workflow-compile-probe"
        try:
            signature.bind(**probe)
        except TypeError:
            raise WorkflowCompileError(
                f"workflow step {step.step_id} tool arguments cannot bind as keywords"
            ) from None


async def _invoke_underlying(tool: Tool, payload: Mapping[str, object], key: str | None):
    arguments = dict(payload)
    if tool.effect == "idempotent":
        arguments["idempotency_key"] = key
    bound = inspect.signature(tool.handler).bind(**arguments)
    if inspect.iscoroutinefunction(tool.handler):
        return await tool.handler(*bound.args, **bound.kwargs)
    value = await asyncio.to_thread(tool.handler, *bound.args, **bound.kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


def _parse_payload(
    spec: WorkflowSpec,
    step: ToolStep,
    *,
    definition_sha256: str,
    step_id: str,
    payload_json: str,
) -> Mapping[str, object]:
    if definition_sha256 != spec.digest or step_id != step.step_id:
        raise WorkflowIntegrityError("workflow tool envelope binding changed")
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, UnicodeError):
        raise WorkflowIntegrityError("workflow tool payload is invalid JSON") from None
    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise WorkflowIntegrityError("workflow tool payload must be a JSON object")
    if payload_json != canonical_json(payload):
        raise WorkflowIntegrityError("workflow tool payload is not canonical")
    return payload


def _result_envelope(spec: WorkflowSpec, step: ToolStep, value: object) -> dict[str, object]:
    typed = validate_typed_value(
        value,
        step.result_type,
        maximum=step.max_result_bytes,
        name=f"workflow step {step.step_id} result",
    )
    return {
        "version": 1,
        "definition_sha256": spec.digest,
        "step_id": step.step_id,
        "value": plain_json(typed),
        "value_sha256": json_sha256(typed),
    }


def _base_idempotency_key(
    tool: Tool, step: ToolStep, payload: Mapping[str, object]
) -> str:
    try:
        value = tool.idempotency_key(payload)  # type: ignore[misc]
    except Exception as error:
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} base idempotency binding failed"
        ) from error
    if not isinstance(value, str) or not value:
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} base idempotency key is invalid"
        )
    return value


def _workflow_idempotency_key(
    spec: WorkflowSpec, step: ToolStep, base_key: str
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "definition_sha256": spec.digest,
                "step_id": step.step_id,
                "base_key": base_key,
            }
        ).encode("utf-8")
    ).hexdigest()


def _parse_result(spec: WorkflowSpec, step: ToolStep, content: str) -> object:
    if len(content.encode("utf-8", "strict")) > step.max_result_bytes + 1024:
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result envelope exceeds its limit"
        )
    try:
        envelope = json.loads(content)
    except (json.JSONDecodeError, UnicodeError):
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result envelope is invalid"
        ) from None
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "version",
        "definition_sha256",
        "step_id",
        "value",
        "value_sha256",
    }:
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result envelope shape changed"
        )
    if content != canonical_json(envelope):
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result envelope is not canonical"
        )
    if (
        envelope["version"] != 1
        or envelope["definition_sha256"] != spec.digest
        or envelope["step_id"] != step.step_id
    ):
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result envelope binding changed"
        )
    try:
        value = validate_typed_value(
            envelope["value"],
            step.result_type,
            maximum=step.max_result_bytes,
            name=f"workflow step {step.step_id} result",
        )
    except WorkflowValidationError as error:
        raise WorkflowIntegrityError(str(error)) from error
    if envelope["value_sha256"] != json_sha256(value):
        raise WorkflowIntegrityError(
            f"workflow step {step.step_id} result digest changed"
        )
    return value


def _make_wrapper(spec: WorkflowSpec, step: ToolStep, tool: Tool) -> Tool:
    async def invoke(
        definition_sha256: str,
        step_id: str,
        payload_json: str,
    ) -> object:
        payload = _parse_payload(
            spec,
            step,
            definition_sha256=definition_sha256,
            step_id=step_id,
            payload_json=payload_json,
        )
        value = await _invoke_underlying(tool, payload, None)
        return _result_envelope(spec, step, value)

    async def invoke_idempotent(
        definition_sha256: str,
        step_id: str,
        payload_json: str,
        *,
        idempotency_key: str,
    ) -> object:
        payload = _parse_payload(
            spec,
            step,
            definition_sha256=definition_sha256,
            step_id=step_id,
            payload_json=payload_json,
        )
        base_key = _base_idempotency_key(tool, step, payload)
        if idempotency_key != _workflow_idempotency_key(spec, step, base_key):
            raise WorkflowIntegrityError(
                f"workflow step {step.step_id} persisted idempotency key changed"
            )
        value = await _invoke_underlying(tool, payload, base_key)
        return _result_envelope(spec, step, value)

    def idempotency(arguments: Mapping[str, object]) -> str:
        try:
            payload = _parse_payload(
                spec,
                step,
                definition_sha256=str(arguments["definition_sha256"]),
                step_id=str(arguments["step_id"]),
                payload_json=str(arguments["payload_json"]),
            )
            base_key = _base_idempotency_key(tool, step, payload)
        except Exception as error:
            raise WorkflowIntegrityError(
                f"workflow step {step.step_id} idempotency binding failed"
            ) from error
        return _workflow_idempotency_key(spec, step, base_key)

    return Tool(
        name=_wrapper_name(spec, step),
        handler=invoke_idempotent if tool.effect == "idempotent" else invoke,
        description=(
            f"Workflow {spec.workflow_id}@{spec.version} step {step.step_id}: "
            f"{tool.description or tool.name}"
        ),
        effect=tool.effect,
        idempotency_key=idempotency if tool.effect == "idempotent" else None,
        tool_revision=(
            _wrapper_revision(spec, step) if tool.effect != "read_only" else None
        ),
    )


@dataclass(frozen=True, slots=True)
class _CompiledStep:
    step: ToolStep
    source_tool: Tool
    wrapper_tool: Tool


@dataclass(frozen=True, slots=True)
class _ReplayStep:
    compiled: _CompiledStep
    call: ToolCall
    result: Message | None


@dataclass(frozen=True, slots=True)
class _WorkflowReplay:
    steps: tuple[_ReplayStep, ...]
    next_reply: ModelReply | None = None
    pending: ToolCall | None = None
    final: Message | None = None
    failure: tuple[str, str, str] | None = None


def _outer_arguments(
    spec: WorkflowSpec,
    compiled: _CompiledStep,
    workflow_input: Mapping[str, object],
    outputs: Mapping[str, object],
) -> Mapping[str, object]:
    payload = resolve_arguments(compiled.step, workflow_input, outputs)
    return {
        "definition_sha256": spec.digest,
        "step_id": compiled.step.step_id,
        "payload_json": canonical_json(payload),
    }


class WorkflowModel:
    """A stateless Model adapter that advances one typed Tool step per turn."""

    def __init__(self, spec: WorkflowSpec, steps: Sequence[_CompiledStep]) -> None:
        self.spec = spec
        self.steps = tuple(steps)
        self.tools = tuple(item.wrapper_tool for item in self.steps)

    def initial_messages(
        self, value: object, public_content: str | None = None
    ) -> tuple[Message, Message, Message]:
        workflow_input = self.spec.validate_input(value)
        content = canonical_json(workflow_input) if public_content is None else public_content
        if not isinstance(content, str):
            raise WorkflowValidationError("workflow public input must be a string")
        try:
            encoded = content.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise WorkflowValidationError(
                "workflow public input must contain valid Unicode"
            ) from None
        if len(encoded) > MAX_WORKFLOW_INPUT_BYTES:
            raise WorkflowValidationError("workflow public input exceeds the size limit")
        return (
            _control_message(self.spec),
            _input_binding_message(workflow_input, content),
            Message("user", content),
        )

    def input_from_messages(self, messages: Sequence[Message]) -> Mapping[str, object]:
        if len(messages) < 3 or messages[0] != _control_message(self.spec):
            raise WorkflowIntegrityError(
                "workflow control record is missing or bound to another definition"
            )
        message = messages[1]
        if (
            message.role != "system"
            or not message.content.startswith(_INPUT_PREFIX)
            or message.tool_calls
            or message.tool_call_id is not None
            or message.tool_name is not None
            or message.error_code is not None
            or message.provider_state is not None
        ):
            raise WorkflowIntegrityError("workflow input record is missing")
        encoded = message.content[len(_INPUT_PREFIX) :]
        if len(encoded.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES + 1024:
            raise WorkflowIntegrityError("workflow input record exceeds the size limit")
        try:
            binding = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeError):
            raise WorkflowIntegrityError("workflow input record is invalid JSON") from None
        if not isinstance(binding, Mapping) or set(binding) != {
            "value",
            "public_content_sha256",
        }:
            raise WorkflowIntegrityError("workflow input record shape changed")
        public = messages[2]
        if (
            public.role != "user"
            or public.tool_calls
            or public.tool_call_id is not None
            or public.tool_name is not None
            or public.error_code is not None
            or public.provider_state is not None
        ):
            raise WorkflowIntegrityError("workflow public input record changed")
        try:
            public_bytes = public.content.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise WorkflowIntegrityError(
                "workflow public input record is invalid Unicode"
            ) from None
        if len(public_bytes) > MAX_WORKFLOW_INPUT_BYTES:
            raise WorkflowIntegrityError("workflow public input record exceeds the size limit")
        public_sha256 = hashlib.sha256(public_bytes).hexdigest()
        if binding["public_content_sha256"] != public_sha256:
            raise WorkflowIntegrityError("workflow public input binding changed")
        try:
            frozen = self.spec.validate_input(binding["value"])
        except WorkflowValidationError as error:
            raise WorkflowIntegrityError(str(error)) from error
        if message != _input_binding_message(frozen, public.content):
            raise WorkflowIntegrityError("workflow input record is not canonical")
        return frozen

    def replay(self, messages: Sequence[Message]) -> _WorkflowReplay:
        workflow_input = self.input_from_messages(messages)
        outputs: dict[str, object] = {}
        replayed: list[_ReplayStep] = []
        cursor = 3

        for compiled in self.steps:
            step = compiled.step
            expected = ToolCall(
                id=_call_id(self.spec, step),
                name=compiled.wrapper_tool.name,
                arguments=_outer_arguments(
                    self.spec, compiled, workflow_input, outputs
                ),
                complete=True,
            )
            if cursor >= len(messages):
                return _WorkflowReplay(
                    tuple(replayed), next_reply=ModelReply(tool_calls=(expected,))
                )

            assistant = messages[cursor]
            if assistant != Message("assistant", tool_calls=(expected,)):
                raise WorkflowIntegrityError(
                    f"workflow step {step.step_id} accepted call record changed"
                )
            cursor += 1
            if cursor >= len(messages):
                replayed.append(_ReplayStep(compiled, expected, None))
                return _WorkflowReplay(tuple(replayed), pending=expected)

            result = messages[cursor]
            if (
                result.role != "tool"
                or result.tool_call_id != expected.id
                or result.tool_name != expected.name
                or result.tool_calls
                or result.provider_state is not None
            ):
                raise WorkflowIntegrityError(
                    f"workflow step {step.step_id} tool result binding changed"
                )
            replayed.append(_ReplayStep(compiled, expected, result))
            cursor += 1
            if result.error_code is not None:
                if cursor != len(messages):
                    raise WorkflowIntegrityError(
                        "workflow history continues after a failed step"
                    )
                return _WorkflowReplay(
                    tuple(replayed),
                    failure=(step.step_id, result.error_code, result.content),
                )
            outputs[step.step_id] = _parse_result(self.spec, step, result.content)

        final_reply = ModelReply(
            content=canonical_json(
                {
                    "version": 1,
                    "workflow_id": self.spec.workflow_id,
                    "workflow_version": self.spec.version,
                    "definition_sha256": self.spec.digest,
                    "status": "succeeded",
                    "output": {
                        "step_id": self.spec.output_step,
                        "value": plain_json(outputs[self.spec.output_step]),
                        "value_sha256": json_sha256(outputs[self.spec.output_step]),
                    },
                }
            )
        )
        if cursor == len(messages):
            return _WorkflowReplay(tuple(replayed), next_reply=final_reply)
        final = messages[cursor]
        if final != Message("assistant", final_reply.content) or cursor + 1 != len(messages):
            raise WorkflowIntegrityError("workflow final outcome record changed")
        return _WorkflowReplay(tuple(replayed), final=final)

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        if tuple(tools) != self.tools:
            raise WorkflowIntegrityError("workflow wrapper tool registry changed")
        replay = self.replay(messages)
        if replay.pending is not None:
            step_id = replay.steps[-1].compiled.step.step_id
            raise WorkflowIntegrityError(
                f"workflow step {step_id} is missing its tool result"
            )
        if replay.failure is not None:
            raise WorkflowStepFailed(*replay.failure)
        if replay.final is not None or replay.next_reply is None:
            raise WorkflowIntegrityError("workflow history contains unexpected records")
        return replay.next_reply


def _same_call(actual: ToolCall, expected: ToolCall) -> bool:
    if (
        not isinstance(actual, ToolCall)
        or actual.id != expected.id
        or actual.name != expected.name
        or actual.complete is not True
    ):
        return False
    try:
        return canonical_json(actual.arguments) == canonical_json(expected.arguments)
    except WorkflowValidationError:
        return False


class WorkflowHarness(Harness):
    """A typed application facade whose execution is the core Harness run."""

    def __init__(self, spec: WorkflowSpec, model: WorkflowModel, base: Harness) -> None:
        self.spec = spec
        self.app_id = workflow_app_id(spec)
        super().__init__(
            model,
            model.tools,
            max_steps=len(spec.steps) + 1,
            model_timeout=base.model_timeout,
            tool_timeout=base.tool_timeout,
            event_sink=base.event_sink,
            store=base.store,
            fault_injector=base.fault_injector,
            skills=(),
        )

    @property
    def harness(self) -> WorkflowHarness:
        return self

    async def run(
        self,
        workflow_input: object,
        *,
        run_id: str | None = None,
        app_id: str | None = None,
    ) -> RunResult:
        if app_id is not None and app_id != self.app_id:
            raise WorkflowIntegrityError(
                "workflow invocation selected a different application binding"
            )
        value, public_content = self._invocation_input(workflow_input)
        messages = self.model.initial_messages(value, public_content)
        return await Harness.run(self, messages, run_id=run_id, app_id=self.app_id)

    async def resume(self, run_id: str) -> RunResult:
        self._assert_run(run_id)
        return await Harness.resume(self, run_id)

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool
    ) -> None:
        self._assert_run(run_id)
        Harness.resolve_approval(self, run_id, fingerprint, approved)

    def resolve_effect(
        self,
        run_id: str,
        fingerprint: str,
        action: str,
        *,
        reason: str,
        result: object | None = None,
    ) -> None:
        state = self._assert_run(run_id)
        if action == "record_result":
            calls = self.store.calls(run_id, state.step)
            pending = next((call for call in calls if call.status != "result"), None)
            if pending is None or not 1 <= state.step <= len(self.model.steps):
                raise WorkflowIntegrityError(
                    "workflow recovery is not bound to a pending step"
                )
            content = result if isinstance(result, str) else canonical_json(result)
            _parse_result(
                self.spec, self.model.steps[state.step - 1].step, content
            )
        Harness.resolve_effect(
            self,
            run_id,
            fingerprint,
            action,
            reason=reason,
            result=result,
        )

    def stored_events(self, run_id: str, after_seq: int = 0):
        self._assert_run(run_id)
        return Harness.stored_events(self, run_id, after_seq)

    def projection(self, run_id: str) -> dict[str, object]:
        state = self._assert_run(run_id)
        workflow_input = self.model.input_from_messages(state.history)
        steps = []
        for index, compiled in enumerate(self.model.steps, start=1):
            step = compiled.step
            calls = self.store.calls(run_id, index)
            call = calls[0] if len(calls) == 1 else None
            if call is not None and (
                call.call_id != _call_id(self.spec, step)
                or call.name != compiled.wrapper_tool.name
            ):
                raise WorkflowIntegrityError(
                    f"workflow step {step.step_id} durable call binding changed"
                )
            status = "pending"
            output = None
            error_code = None
            if call is not None:
                error_code = call.result.error_code if call.result is not None else None
                if call.result is not None and error_code is None:
                    output = plain_json(_parse_result(self.spec, step, call.result.content))
                if call.status == "result":
                    status = "failed" if error_code is not None else "completed"
                elif call.status in ("awaiting_approval", "effect_unknown"):
                    status = "paused"
                else:
                    status = "running"
            steps.append(
                {
                    "step_id": step.step_id,
                    "kind": "tool",
                    "tool_name": step.tool_name,
                    "dispatch_tool_name": compiled.wrapper_tool.name,
                    "effect": step.effect,
                    "tool_revision": step.tool_revision,
                    "schema_sha256": step.schema_sha256,
                    "call_id": _call_id(self.spec, step),
                    "status": status,
                    "output": output,
                    "error_code": error_code,
                }
            )
        return {
            "schema_version": 1,
            "workflow": {
                "workflow_id": self.spec.workflow_id,
                "version": self.spec.version,
                "definition_sha256": self.spec.digest,
                "app_id": self.app_id,
                "execution": "single-harness-ordered-tools-v1",
                "output_step": self.spec.output_step,
            },
            "run": run_projection(self.store, run_id),
            "input": plain_json(workflow_input),
            "steps": steps,
        }

    def public_projection(self, run_id: str) -> dict[str, object]:
        """Return the bounded, redacted Workflow step projection."""

        from .projection import workflow_public_projection

        return workflow_public_projection(self, run_id)

    def public_projection_extension(self, run_id: str) -> dict[str, object]:
        """Return the only public extension accepted by the core composer."""

        return {"workflow": self.public_projection(run_id)}

    def public_run_projection(self, run_id: str) -> dict[str, object]:
        """Compose the core run projection with the public Workflow extension."""

        from .projection import workflow_public_run_projection

        return workflow_public_run_projection(self, run_id)

    def _assert_run(self, run_id: str):
        state = self.store.load(run_id)
        if state.app_id != self.app_id:
            raise WorkflowIntegrityError(
                "workflow run is bound to another Sasori application"
            )
        replay = self.model.replay(state.history)
        expected_step = len(replay.steps) + (1 if replay.final is not None else 0)
        if state.step != expected_step:
            raise WorkflowIntegrityError("workflow durable model step changed")
        for index, compiled in enumerate(self.model.steps, start=1):
            calls = self.store.calls(run_id, index)
            if index > len(replay.steps):
                if calls:
                    raise WorkflowIntegrityError(
                        f"workflow step {compiled.step.step_id} has an unexpected durable call"
                    )
                continue
            replayed = replay.steps[index - 1]
            if len(calls) != 1:
                raise WorkflowIntegrityError(
                    f"workflow step {compiled.step.step_id} durable call count changed"
                )
            call = calls[0]
            actual = ToolCall(
                call.call_id or "",
                call.name or "",
                call.arguments,
                call.complete,
            )
            wrapper = replayed.compiled.wrapper_tool
            expected_revision = wrapper.tool_revision or "read-only-unversioned"
            if (
                call.ordinal != 0
                or not call.arguments_valid
                or not _same_call(actual, replayed.call)
                or call.effect != wrapper.effect
                or call.tool_revision != expected_revision
                or not isinstance(call.fingerprint, str)
                or len(call.fingerprint) != 64
            ):
                raise WorkflowIntegrityError(
                    f"workflow step {compiled.step.step_id} durable call binding changed"
                )
            if wrapper.effect == "idempotent":
                expected_key = wrapper.idempotency_key(replayed.call.arguments)  # type: ignore[misc]
                if call.idempotency_key != expected_key:
                    raise WorkflowIntegrityError(
                        f"workflow step {compiled.step.step_id} idempotency binding changed"
                    )
            elif call.idempotency_key is not None:
                raise WorkflowIntegrityError(
                    f"workflow step {compiled.step.step_id} has an unexpected idempotency key"
                )
            if replayed.result is None:
                if call.status == "result" or call.result is not None:
                    raise WorkflowIntegrityError(
                        f"workflow step {compiled.step.step_id} result binding changed"
                    )
            elif call.status != "result" or call.result != replayed.result:
                raise WorkflowIntegrityError(
                    f"workflow step {compiled.step.step_id} result binding changed"
                )
        if state.status == "completed":
            if replay.final is None or state.final_message != replay.final:
                raise WorkflowIntegrityError("workflow durable final binding changed")
        elif state.final_message is not None:
            raise WorkflowIntegrityError("workflow final was exposed before completion")
        return state

    def _invocation_input(self, value: object) -> tuple[object, str]:
        if isinstance(value, Mapping):
            return value, canonical_json(value)
        if isinstance(value, str):
            if len(self.spec.inputs) == 1 and self.spec.inputs[0].value_type == "string":
                return {self.spec.inputs[0].key: value}, value
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                raise WorkflowValidationError(
                    "workflow text input must be a JSON object"
                ) from None
            return parsed, value
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            messages = tuple(value)
            if (
                len(messages) != 1
                or not isinstance(messages[0], Message)
                or messages[0].role != "user"
                or messages[0].tool_calls
                or messages[0].tool_call_id is not None
                or messages[0].tool_name is not None
                or messages[0].error_code is not None
                or messages[0].provider_state is not None
            ):
                raise WorkflowIntegrityError(
                    "workflow applications accept one plain user input message"
                )
            return self._invocation_input(messages[0].content)
        raise WorkflowValidationError(
            "workflow invocation must be a JSON object or one plain user message"
        )


def compile_workflow(spec: WorkflowSpec, base: Harness) -> WorkflowHarness:
    if not isinstance(spec, WorkflowSpec):
        raise WorkflowCompileError("spec must be a WorkflowSpec")
    if not isinstance(base, Harness):
        raise WorkflowCompileError("base must be a Sasori Harness")
    _validate_base_tools(spec, base.tools)
    available = _tool_map(base.tools)
    compiled = tuple(
        _CompiledStep(
            step=step,
            source_tool=available[step.tool_name],
            wrapper_tool=_make_wrapper(spec, step, available[step.tool_name]),
        )
        for step in spec.steps
    )
    wrapper_names = {item.wrapper_tool.name for item in compiled}
    if len(wrapper_names) != len(compiled):
        raise WorkflowCompileError("workflow wrapper tool names collided")
    model = WorkflowModel(spec, compiled)
    return WorkflowHarness(spec, model, base)


__all__ = [
    "WorkflowCompileError",
    "WorkflowHarness",
    "WorkflowIntegrityError",
    "WorkflowModel",
    "WorkflowStepFailed",
    "compile_workflow",
    "workflow_app_id",
]
