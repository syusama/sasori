from __future__ import annotations

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
    validate_positive_integer,
    validate_tool_arguments,
)
from .contracts import Message, ModelReply, Tool, ToolCall


_PROVIDER = "anthropic.messages"
_RESERVED_BODY = frozenset(
    {
        "max_tokens",
        "messages",
        "model",
        "stream",
        "system",
        "tool_choice",
        "tools",
    }
)
_RESERVED_HEADERS = frozenset(
    {
        "accept",
        "anthropic-version",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
        "x-api-key",
    }
)
_STREAM_TERMINALS = frozenset({"message_stop", "error"})
_ERROR_CODES = frozenset(
    {
        "api_error",
        "authentication_error",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
    }
)


def _protocol(message: str, request_id: str | None = None) -> ProviderProtocolError:
    return ProviderProtocolError(message, provider=_PROVIDER, request_id=request_id)


def _text_and_calls(
    content: list[object], request_id: str | None = None
) -> tuple[str, tuple[ToolCall, ...]]:
    text: list[str] = []
    calls: list[ToolCall] = []
    call_ids: set[str] = set()
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise _protocol("Anthropic content blocks must be typed objects", request_id)
        block_type = block["type"]
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise _protocol("Anthropic text blocks must contain text", request_id)
            text.append(block["text"])
            continue
        if block_type == "thinking":
            if (
                not isinstance(block.get("thinking"), str)
                or not isinstance(block.get("signature"), str)
                or not block["signature"]
            ):
                raise _protocol("Anthropic thinking block is structurally invalid", request_id)
            continue
        if block_type == "redacted_thinking":
            if not isinstance(block.get("data"), str) or not block["data"]:
                raise _protocol(
                    "Anthropic redacted_thinking block is structurally invalid",
                    request_id,
                )
            continue
        if block_type == "tool_use":
            caller = block.get("caller")
            if caller is not None and caller != {"type": "direct"}:
                raise _protocol(
                    "Anthropic server-originated tool_use is not supported", request_id
                )
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("input")
            if not isinstance(call_id, str) or not call_id:
                raise _protocol("Anthropic tool_use has no id", request_id)
            if call_id in call_ids:
                raise _protocol("Anthropic tool-use IDs must be unique", request_id)
            if not isinstance(name, str) or not name:
                raise _protocol("Anthropic tool_use has no name", request_id)
            if not isinstance(arguments, dict):
                raise _protocol("Anthropic tool_use input must be an object", request_id)
            call_ids.add(call_id)
            calls.append(ToolCall(call_id, name, arguments))
            continue
        raise _protocol("Anthropic returned an unsupported content block", request_id)
    if len(calls) > 1:
        raise _protocol("Anthropic violated disable_parallel_tool_use=true", request_id)
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
        raise _protocol("Anthropic response must be an object", request_id)
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        raise _protocol("Anthropic response must be an assistant message", request_id)
    content = payload.get("content")
    if not isinstance(content, list):
        raise _protocol("Anthropic response content must be an array", request_id)
    stop_reason = payload.get("stop_reason")
    if stop_reason in {
        "max_tokens",
        "model_context_window_exceeded",
        "pause_turn",
    }:
        raise ProviderIncompleteError(
            "Anthropic response is incomplete",
            provider=_PROVIDER,
            code=stop_reason,
            request_id=request_id,
        )
    if stop_reason == "refusal":
        raise ProviderRefusalError(
            "Anthropic refused the request",
            provider=_PROVIDER,
            code="refusal",
            request_id=request_id,
        )
    if stop_reason not in {"end_turn", "stop_sequence", "tool_use"}:
        raise _protocol("Anthropic stop_reason is missing or unsupported", request_id)
    text, calls = _text_and_calls(content, request_id)
    if stop_reason == "tool_use" and not calls:
        raise _protocol("Anthropic tool_use stop requires a tool_use block", request_id)
    if stop_reason != "tool_use" and calls:
        raise _protocol("Anthropic tool_use blocks require tool_use stop", request_id)
    validate_tool_arguments(calls, schemas, _PROVIDER, request_id)
    if not text and not calls:
        raise _protocol("Anthropic completed without text or a tool call", request_id)
    return ModelReply(
        content=text,
        tool_calls=calls,
        provider_state=encode_provider_state(_PROVIDER, "content", content),
    )


def _stream_payload(events: tuple[object, ...], request_id: str | None) -> object:
    started = False
    message_delta = False
    stopped = False
    next_index = 0
    content: list[dict[str, object]] = []
    active: dict[str, object] | None = None
    stop_reason: str | None = None
    stop_sequence: str | None = None

    for item in events:
        event = getattr(item, "event", None)
        data = getattr(item, "data", None)
        if not isinstance(event, str) or not isinstance(data, dict):
            raise _protocol("Anthropic SSE events must contain JSON objects", request_id)
        if data.get("type") != event:
            raise _protocol("Anthropic SSE event and data types disagree", request_id)
        if event == "ping":
            continue
        if event == "error":
            error = data.get("error")
            raw_code = error.get("type") if isinstance(error, dict) else None
            code = stable_error_code(raw_code, _ERROR_CODES, "stream_error")
            raise ProviderResponseError(
                "Anthropic streaming response failed",
                provider=_PROVIDER,
                code=code,
                request_id=request_id,
                retryable=code in {"overloaded_error", "rate_limit_error"},
            )
        if event == "message_start":
            message = data.get("message")
            if (
                started
                or content
                or not isinstance(message, dict)
                or message.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("content") != []
                or message.get("stop_reason") is not None
            ):
                raise _protocol("Anthropic message_start is invalid", request_id)
            started = True
            continue
        if event == "content_block_start":
            index = data.get("index")
            block = data.get("content_block")
            if (
                not started
                or message_delta
                or active is not None
                or type(index) is not int
                or index != next_index
                or not isinstance(block, dict)
            ):
                raise _protocol("Anthropic content_block_start is out of order", request_id)
            block_type = block.get("type")
            if block_type == "text":
                valid = set(block) == {"type", "text"} and isinstance(
                    block.get("text"), str
                )
            elif block_type == "thinking":
                valid = set(block) == {"type", "thinking"} and isinstance(
                    block.get("thinking"), str
                )
            elif block_type == "redacted_thinking":
                valid = set(block) == {"type", "data"} and isinstance(
                    block.get("data"), str
                ) and bool(block["data"])
            elif block_type == "tool_use":
                valid = (
                    set(block).issubset({"type", "id", "name", "input", "caller"})
                    and isinstance(block.get("id"), str)
                    and bool(block["id"])
                    and isinstance(block.get("name"), str)
                    and bool(block["name"])
                    and block.get("input") == {}
                )
            else:
                valid = False
            if not valid:
                raise _protocol("Anthropic content block is unsupported or invalid", request_id)
            active = {
                "index": index,
                "block": dict(block),
                "partials": [],
                "signature": None,
            }
            continue
        if event == "content_block_delta":
            index = data.get("index")
            delta = data.get("delta")
            if (
                active is None
                or type(index) is not int
                or index != active["index"]
                or not isinstance(delta, dict)
            ):
                raise _protocol("Anthropic content_block_delta is out of order", request_id)
            block = active["block"]
            block_type = block["type"]
            delta_type = delta.get("type")
            if block_type == "text" and delta_type == "text_delta" and set(delta) == {
                "type",
                "text",
            } and isinstance(delta.get("text"), str):
                block["text"] += delta["text"]
            elif block_type == "tool_use" and delta_type == "input_json_delta" and set(
                delta
            ) == {"type", "partial_json"} and isinstance(
                delta.get("partial_json"), str
            ):
                active["partials"].append(delta["partial_json"])
            elif block_type == "thinking" and delta_type == "thinking_delta" and set(
                delta
            ) == {"type", "thinking"} and isinstance(delta.get("thinking"), str):
                block["thinking"] += delta["thinking"]
            elif block_type == "thinking" and delta_type == "signature_delta" and set(
                delta
            ) == {"type", "signature"} and isinstance(
                delta.get("signature"), str
            ) and delta["signature"] and active["signature"] is None:
                active["signature"] = delta["signature"]
            else:
                raise _protocol("Anthropic content block delta is unsupported", request_id)
            continue
        if event == "content_block_stop":
            index = data.get("index")
            if (
                active is None
                or type(index) is not int
                or index != active["index"]
            ):
                raise _protocol("Anthropic content_block_stop is out of order", request_id)
            block = active["block"]
            if block["type"] == "tool_use":
                try:
                    arguments = strict_json_loads(
                        "".join(active["partials"]) or "{}"
                    )
                except (ValueError, RecursionError):
                    raise _protocol("Anthropic tool input is malformed JSON", request_id) from None
                if not isinstance(arguments, dict):
                    raise _protocol("Anthropic tool input must be an object", request_id)
                block["input"] = arguments
            elif block["type"] == "thinking":
                if active["signature"] is None:
                    raise _protocol("Anthropic thinking block has no signature", request_id)
                block["signature"] = active["signature"]
            content.append(block)
            active = None
            next_index += 1
            continue
        if event == "message_delta":
            delta = data.get("delta")
            if (
                not started
                or active is not None
                or message_delta
                or not isinstance(delta, dict)
                or not set(delta).issubset({"stop_reason", "stop_sequence"})
            ):
                raise _protocol("Anthropic message_delta is invalid or out of order", request_id)
            candidate_reason = delta.get("stop_reason")
            candidate_sequence = delta.get("stop_sequence")
            if not isinstance(candidate_reason, str) or (
                candidate_sequence is not None and not isinstance(candidate_sequence, str)
            ):
                raise _protocol("Anthropic message_delta stop data is invalid", request_id)
            stop_reason = candidate_reason
            stop_sequence = candidate_sequence
            message_delta = True
            continue
        if event == "message_stop":
            if not started or active is not None or not message_delta or stopped:
                raise _protocol("Anthropic message_stop is invalid or out of order", request_id)
            stopped = True
            continue
        raise _protocol("Anthropic returned an unsupported SSE event", request_id)

    if not stopped:
        raise _protocol("Anthropic SSE stream has no message_stop", request_id)
    return {
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
    }


def _wire_history(messages: tuple[Message, ...]) -> tuple[str | None, list[object]]:
    index = 0
    system: list[str] = []
    while index < len(messages) and messages[index].role == "system":
        message = messages[index]
        if not isinstance(message.content, str):
            raise _protocol("Anthropic message content must be a string")
        if message.provider_state is not None or message.tool_calls:
            raise _protocol("Anthropic system messages cannot contain provider or tool state")
        system.append(message.content)
        index += 1

    result: list[object] = []
    while index < len(messages):
        message = messages[index]
        if not isinstance(message.content, str):
            raise _protocol("Anthropic message content must be a string")
        if message.provider_state is not None and message.role != "assistant":
            raise _protocol("provider_state is allowed only on assistant messages")
        if message.role == "system":
            raise _protocol("Anthropic system messages must precede the conversation")
        if message.role == "user":
            if message.tool_calls:
                raise _protocol("Anthropic user messages cannot contain tool calls")
            result.append(
                {"role": "user", "content": [{"type": "text", "text": message.content}]}
            )
            index += 1
            continue
        if message.role == "tool":
            raise _protocol("Anthropic tool results must immediately follow tool_use")
        if message.role != "assistant":
            raise _protocol("Anthropic message history contains an unsupported role")

        calls: tuple[ToolCall, ...] = ()
        if message.provider_state is None:
            if message.tool_calls:
                raise _protocol("Anthropic tool continuation requires provider_state")
            content: list[object] = [{"type": "text", "text": message.content}]
        else:
            state_provider = provider_state_name(message.provider_state, _PROVIDER)
            if state_provider != _PROVIDER:
                if message.tool_calls:
                    raise _protocol("cannot switch providers during an unresolved tool turn")
                content = [{"type": "text", "text": message.content}]
            else:
                content = decode_provider_state(
                    message.provider_state, _PROVIDER, "content"
                )
                raw_text, calls = _text_and_calls(content)
                if raw_text != message.content or not _same_calls(
                    message.tool_calls, calls
                ):
                    raise _protocol(
                        "Anthropic provider_state does not match its projection"
                    )
        result.append({"role": "assistant", "content": content})
        index += 1

        if not calls:
            continue
        expected = {call.id for call in calls}
        seen: set[str] = set()
        tool_results: list[object] = []
        while index < len(messages) and messages[index].role == "tool":
            tool_message = messages[index]
            if not isinstance(tool_message.content, str):
                raise _protocol("Anthropic tool result content must be a string")
            if tool_message.provider_state is not None:
                raise _protocol("Anthropic tool results cannot contain provider_state")
            call_id = tool_message.tool_call_id
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id not in expected
                or call_id in seen
            ):
                raise _protocol("Anthropic tool result does not match the preceding tool_use")
            if tool_message.error_code is not None and not isinstance(
                tool_message.error_code, str
            ):
                raise _protocol("tool error codes must be strings")
            block: dict[str, object] = {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": tool_message.content,
            }
            if tool_message.error_code is not None:
                block["is_error"] = True
            tool_results.append(block)
            seen.add(call_id)
            index += 1
        if seen != expected:
            raise _protocol("Anthropic tool_use blocks require one immediate result each")
        result.append({"role": "user", "content": tool_results})

    if not result:
        raise _protocol("Anthropic Messages requires at least one conversation message")
    return "\n\n".join(system) if system else None, result


class AnthropicMessagesModel:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096,
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
        self.max_tokens = validate_positive_integer(max_tokens, "max_tokens")
        key = validate_api_key(api_key, "ANTHROPIC_API_KEY")
        endpoint = provider_endpoint(
            base_url, "/v1/messages", allow_localhost=allow_localhost
        )
        self._headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
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
            request_id_headers=("request-id", "x-request-id"),
        )

    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        system, wire_messages = _wire_history(messages)
        schemas: dict[str, dict[str, object]] = {}
        wire_tools: list[dict[str, object]] = []
        for tool in tools:
            schema = compile_tool_schema(tool)
            if tool.name in schemas:
                raise ProviderConfigurationError(
                    "provider tool names must be unique", provider=_PROVIDER
                )
            schemas[tool.name] = schema
            wire_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "strict": True,
                    "input_schema": schema,
                }
            )
        body: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": wire_messages,
            "stream": self.stream,
            **self._extra_body,
        }
        if system is not None:
            body["system"] = system
        if wire_tools:
            body["tools"] = wire_tools
            body["tool_choice"] = {
                "type": "auto",
                "disable_parallel_tool_use": True,
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


__all__ = ["AnthropicMessagesModel"]
