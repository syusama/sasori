from __future__ import annotations

import json
from collections.abc import Mapping

from ._provider_common import (
    JSONTransport,
    ProviderConfigurationError,
    ProviderIncompleteError,
    ProviderProtocolError,
    ProviderRefusalError,
    ProviderResponseError,
    compile_tool_schema,
    decode_provider_state,
    encode_provider_state,
    json_values_equal,
    provider_endpoint,
    provider_state_name,
    stable_error_code,
    strict_json_loads,
    validate_api_key,
    validate_extra_body,
    validate_extra_headers,
    validate_model,
    validate_tool_arguments,
)
from .contracts import Message, ModelReply, Tool, ToolCall


_PROVIDER = "openai.responses"
_RESERVED_BODY = frozenset(
    {
        "input",
        "model",
        "parallel_tool_calls",
        "previous_response_id",
        "conversation",
        "background",
        "stream",
        "tools",
    }
)
_RESERVED_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
    }
)
_STREAM_TERMINALS = frozenset(
    {"response.completed", "response.incomplete", "response.failed", "error"}
)
_ERROR_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "content_filter",
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request_error",
        "model_error",
        "model_not_found",
        "rate_limit_exceeded",
        "server_error",
    }
)


def _protocol(message: str, request_id: str | None = None) -> ProviderProtocolError:
    return ProviderProtocolError(message, provider=_PROVIDER, request_id=request_id)


def _text_and_calls(
    output: list[object], request_id: str | None = None
) -> tuple[str, tuple[ToolCall, ...]]:
    text: list[str] = []
    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise _protocol("OpenAI output items must be typed objects", request_id)
        item_type = item["type"]
        if item_type == "reasoning":
            summary = item.get("summary")
            content = item.get("content")
            encrypted_content = item.get("encrypted_content")
            status = item.get("status")
            if (
                not isinstance(item.get("id"), str)
                or not item["id"]
                or not isinstance(summary, list)
                or any(
                    not isinstance(part, dict)
                    or part.get("type") != "summary_text"
                    or not isinstance(part.get("text"), str)
                    for part in summary
                )
                or content is not None
                and (
                    not isinstance(content, list)
                    or any(
                        not isinstance(part, dict)
                        or part.get("type") != "reasoning_text"
                        or not isinstance(part.get("text"), str)
                        for part in content
                    )
                )
                or encrypted_content is not None
                and not isinstance(encrypted_content, str)
                or status is not None
                and status != "completed"
            ):
                raise _protocol("OpenAI reasoning item is structurally invalid", request_id)
            continue
        if item_type == "message":
            if item.get("role") != "assistant" or item.get("status") != "completed":
                raise _protocol("OpenAI output message is not completed assistant output", request_id)
            content = item.get("content")
            if not isinstance(content, list):
                raise _protocol("OpenAI output message content must be an array", request_id)
            for block in content:
                if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                    raise _protocol("OpenAI message content blocks must be typed objects", request_id)
                if block["type"] == "output_text":
                    if not isinstance(block.get("text"), str):
                        raise _protocol("OpenAI output_text must contain text", request_id)
                    text.append(block["text"])
                elif block["type"] == "refusal":
                    raise ProviderRefusalError(
                        "OpenAI refused the request",
                        provider=_PROVIDER,
                        code="refusal",
                        request_id=request_id,
                    )
                else:
                    raise _protocol("OpenAI returned an unsupported message block", request_id)
            continue
        if item_type == "function_call":
            if item.get("status") != "completed":
                raise ProviderIncompleteError(
                    "OpenAI returned an incomplete function call",
                    provider=_PROVIDER,
                    code="incomplete_function_call",
                    request_id=request_id,
                )
            item_id = item.get("id")
            if item_id is not None and (
                not isinstance(item_id, str) or not item_id
            ):
                raise _protocol("OpenAI function call has an invalid id", request_id)
            if item.get("namespace") is not None:
                raise _protocol(
                    "OpenAI namespaced function calls are not supported", request_id
                )
            if item.get("caller") is not None:
                raise _protocol(
                    "OpenAI programmatic function calls are not supported", request_id
                )
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                raise _protocol("OpenAI function call has no call_id", request_id)
            if call_id in call_ids:
                raise _protocol("OpenAI function call IDs must be unique", request_id)
            if not isinstance(name, str) or not name:
                raise _protocol("OpenAI function call has no name", request_id)
            if not isinstance(arguments, str):
                raise _protocol("OpenAI function call arguments must be a JSON string", request_id)
            invalid_arguments = False
            try:
                decoded = strict_json_loads(arguments)
            except (ValueError, RecursionError):
                invalid_arguments = True
                decoded = None
            if invalid_arguments:
                raise _protocol(
                    "OpenAI function call arguments are malformed JSON", request_id
                ) from None
            if not isinstance(decoded, dict):
                raise _protocol("OpenAI function call arguments must decode to an object", request_id)
            call_ids.add(call_id)
            calls.append(ToolCall(call_id, name, decoded))
            continue
        raise _protocol("OpenAI returned an unsupported output item", request_id)
    if len(calls) > 1:
        raise _protocol("OpenAI violated parallel_tool_calls=false", request_id)
    return "".join(text), tuple(calls)


def _same_calls(expected: tuple[ToolCall, ...], actual: tuple[ToolCall, ...]) -> bool:
    if len(expected) != len(actual):
        return False
    return all(
        left.complete
        and left.id == right.id
        and left.name == right.name
        and json_values_equal(left.arguments, right.arguments)
        for left, right in zip(expected, actual)
    )


def _reply_from_payload(
    payload: object,
    schemas: Mapping[str, dict[str, object]],
    request_id: str | None,
) -> ModelReply:
    if not isinstance(payload, dict):
        raise _protocol("OpenAI response must be an object", request_id)
    status = payload.get("status")
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        raise ProviderIncompleteError(
            "OpenAI response is incomplete",
            provider=_PROVIDER,
            code=reason if isinstance(reason, str) else "incomplete",
            request_id=request_id,
        )
    if status == "cancelled":
        raise ProviderIncompleteError(
            "OpenAI response was cancelled",
            provider=_PROVIDER,
            code="cancelled",
            request_id=request_id,
        )
    if status == "failed":
        error = payload.get("error")
        raw_code = error.get("code") if isinstance(error, dict) else None
        raise ProviderResponseError(
            "OpenAI response failed",
            provider=_PROVIDER,
            code=stable_error_code(raw_code, _ERROR_CODES, "failed"),
            request_id=request_id,
        )
    if status != "completed":
        raise _protocol("OpenAI response status is missing or unsupported", request_id)
    output = payload.get("output")
    if not isinstance(output, list):
        raise _protocol("OpenAI response output must be an array", request_id)
    content, calls = _text_and_calls(output, request_id)
    validate_tool_arguments(calls, schemas, _PROVIDER, request_id)
    if not content and not calls:
        raise _protocol("OpenAI completed without text or a tool call", request_id)
    return ModelReply(
        content=content,
        tool_calls=calls,
        provider_state=encode_provider_state(_PROVIDER, "output", output),
    )


def _stream_payload(events: tuple[object, ...], request_id: str | None) -> object:
    expected_sequence = 0
    for item in events:
        event = getattr(item, "event", None)
        data = getattr(item, "data", None)
        if not isinstance(event, str) or not isinstance(data, dict):
            raise _protocol("OpenAI SSE events must contain JSON objects", request_id)
        if data.get("type") != event:
            raise _protocol("OpenAI SSE event and data types disagree", request_id)
        candidate = data.get("sequence_number")
        if type(candidate) is not int or candidate != expected_sequence:
            raise _protocol("OpenAI SSE sequence numbers are not contiguous", request_id)
        expected_sequence += 1
        if event not in _STREAM_TERMINALS and not event.startswith("response."):
            raise _protocol("OpenAI returned an unsupported SSE event", request_id)

    terminal = events[-1]
    event = getattr(terminal, "event")
    data = getattr(terminal, "data")
    if event == "error":
        code = stable_error_code(data.get("code"), _ERROR_CODES, "stream_error")
        raise ProviderResponseError(
            "OpenAI streaming response failed",
            provider=_PROVIDER,
            code=code,
            request_id=request_id,
        )
    payload = data.get("response")
    if not isinstance(payload, dict):
        raise _protocol("OpenAI terminal SSE event has no response", request_id)
    expected_status = {
        "response.completed": "completed",
        "response.incomplete": "incomplete",
        "response.failed": "failed",
    }[event]
    if payload.get("status") != expected_status:
        raise _protocol("OpenAI terminal SSE event and response status disagree", request_id)
    return payload


def _strict_schema_supported(schema: dict[str, object]) -> bool:
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or any(
            not isinstance(item, dict) or not _strict_schema_supported(item)
            for item in properties.values()
        ):
            return False
    item_schema = schema.get("items")
    if item_schema is not None and (
        not isinstance(item_schema, dict) or not _strict_schema_supported(item_schema)
    ):
        return False
    alternatives = schema.get("anyOf")
    return alternatives is None or (
        isinstance(alternatives, list)
        and all(
            isinstance(item, dict) and _strict_schema_supported(item)
            for item in alternatives
        )
    )


def _input_items(messages: tuple[Message, ...]) -> list[object]:
    result: list[object] = []
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message.content, str):
            raise _protocol("OpenAI message content must be a string")
        if message.provider_state is not None and message.role != "assistant":
            raise _protocol("provider_state is allowed only on assistant messages")
        if message.role != "assistant" and message.tool_calls:
            raise _protocol("tool calls are allowed only on assistant messages")
        if message.role == "tool":
            call_id = message.tool_call_id
            if not isinstance(call_id, str) or not call_id or call_id not in pending:
                raise _protocol("OpenAI tool output does not match the preceding function call")
            pending.remove(call_id)
            output = message.content
            if message.error_code is not None:
                if not isinstance(message.error_code, str):
                    raise _protocol("tool error codes must be strings")
                output = json.dumps(
                    {
                        "error": {
                            "code": message.error_code,
                            "message": message.content,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            result.append(
                {"type": "function_call_output", "call_id": call_id, "output": output}
            )
            continue
        if pending:
            raise _protocol("OpenAI function calls require immediate, complete tool outputs")
        if message.role in {"system", "user"}:
            result.append({"role": message.role, "content": message.content})
            continue
        if message.role != "assistant":
            raise _protocol("OpenAI message history contains an unsupported role")
        if message.provider_state is None:
            if message.tool_calls:
                raise _protocol("OpenAI tool continuation requires provider_state")
            result.append({"role": "assistant", "content": message.content})
            continue
        state_provider = provider_state_name(message.provider_state, _PROVIDER)
        if state_provider != _PROVIDER:
            if message.tool_calls:
                raise _protocol("cannot switch providers during an unresolved tool turn")
            result.append({"role": "assistant", "content": message.content})
            continue
        output = decode_provider_state(message.provider_state, _PROVIDER, "output")
        raw_text, raw_calls = _text_and_calls(output)
        if raw_text != message.content or not _same_calls(message.tool_calls, raw_calls):
            raise _protocol("OpenAI provider_state does not match its projection")
        result.extend(output)
        pending = {call.id for call in raw_calls}
    if pending:
        raise _protocol("OpenAI function calls are missing tool outputs")
    return result


class OpenAIResponsesModel:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        stream: bool = False,
        allow_localhost: bool = False,
        extra_body: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        if type(stream) is not bool:
            raise ProviderConfigurationError(
                "stream must be a boolean", provider=_PROVIDER
            )
        self.model = validate_model(model)
        self.stream = stream
        key = validate_api_key(api_key, "OPENAI_API_KEY")
        endpoint = provider_endpoint(
            base_url, "/responses", allow_localhost=allow_localhost
        )
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            **({"Accept": "text/event-stream"} if stream else {}),
            **validate_extra_headers(extra_headers, _RESERVED_HEADERS),
        }
        self._extra_body = validate_extra_body(extra_body, _RESERVED_BODY)
        self._transport = JSONTransport(
            provider=_PROVIDER,
            endpoint=endpoint,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            request_id_headers=("x-request-id",),
        )

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        schemas: dict[str, dict[str, object]] = {}
        wire_tools: list[dict[str, object]] = []
        for tool in tools:
            schema = compile_tool_schema(tool)
            if tool.name in schemas:
                raise ProviderConfigurationError(
                    "provider tool names must be unique", provider=_PROVIDER
                )
            if not _strict_schema_supported(schema):
                raise ProviderConfigurationError(
                    "OpenAI strict tools do not support dynamic object schemas",
                    provider=_PROVIDER,
                )
            schemas[tool.name] = schema
            wire_tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "strict": True,
                    "parameters": schema,
                }
            )
        body = {
            "model": self.model,
            "input": _input_items(messages),
            "tools": wire_tools,
            "parallel_tool_calls": False,
            "stream": self.stream,
            **self._extra_body,
        }
        if self.stream:
            response = await self._transport.post_sse(
                body, self._headers, _STREAM_TERMINALS
            )
            payload = _stream_payload(response.events, response.request_id)
        else:
            response = await self._transport.post(body, self._headers)
            payload = response.body
        return _reply_from_payload(payload, schemas, response.request_id)


__all__ = ["OpenAIResponsesModel"]
