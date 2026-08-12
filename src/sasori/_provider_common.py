from __future__ import annotations

import asyncio
import codecs
import http.client
import inspect
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal, Union, get_args, get_origin, get_type_hints

from .contracts import Tool, ToolCall


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.status_code = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = retryable


class ProviderConfigurationError(ProviderError):
    pass


class ProviderConnectionError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    pass


class ProviderAuthError(ProviderHTTPError):
    pass


class ProviderPermissionError(ProviderHTTPError):
    pass


class ProviderRateLimitError(ProviderHTTPError):
    pass


class ProviderResponseError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


class ProviderIncompleteError(ProviderError):
    pass


class ProviderRefusalError(ProviderError):
    pass


_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_STABLE_ERROR_CODES = frozenset(
    {
        "api_error",
        "authentication_error",
        "billing_hard_limit_reached",
        "content_filter",
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request_error",
        "model_error",
        "model_not_found",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "rate_limit_exceeded",
        "request_too_large",
        "server_error",
    }
)


def _configuration(message: str) -> ProviderConfigurationError:
    return ProviderConfigurationError(message, provider="sasori.provider")


def strict_json_loads(value: str | bytes) -> object:
    def reject_constant(constant: str) -> object:
        raise ValueError("non-finite JSON number")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    def finite_float(number: str) -> float:
        parsed = float(number)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    decoded = json.loads(
        value,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=reject_duplicate_keys,
    )
    _reject_json_surrogates(decoded)
    return decoded


def stable_error_code(
    value: object, allowed: frozenset[str], fallback: str | None
) -> str | None:
    return value if isinstance(value, str) and value in allowed else fallback


def _reject_json_surrogates(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON strings must contain Unicode scalar values")
        return
    if isinstance(value, list):
        for item in value:
            _reject_json_surrogates(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_json_surrogates(key)
            _reject_json_surrogates(item)


def json_values_equal(left: object, right: object) -> bool:
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left) != set(right):
            return False
        return all(json_values_equal(left[key], right[key]) for key in left)
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _json_copy(value: object) -> object:
    invalid = False
    try:
        copied = strict_json_loads(
            json.dumps(value, allow_nan=False, ensure_ascii=False)
        )
    except (TypeError, ValueError, RecursionError):
        invalid = True
        copied = None
    if invalid:
        raise _configuration("provider configuration must be JSON-compatible") from None
    return copied


def validate_model(model: object) -> str:
    if (
        not isinstance(model, str)
        or not model
        or model != model.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise _configuration("model must be a non-empty header-safe string")
    return model


def validate_api_key(value: object, environment_name: str) -> str:
    key = os.environ.get(environment_name) if value is None else value
    if (
        not isinstance(key, str)
        or not key
        or any(not 33 <= ord(character) <= 126 for character in key)
    ):
        raise _configuration("provider API key is not configured or is malformed")
    return key


def validate_positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _configuration(f"{name} must be a positive number")
    try:
        result = float(value)
    except OverflowError:
        raise _configuration(f"{name} must be a positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise _configuration(f"{name} must be a positive number")
    return result


def validate_positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _configuration(f"{name} must be a positive integer")
    return value


def validate_extra_body(
    extra_body: object, reserved: frozenset[str]
) -> dict[str, object]:
    if extra_body is None:
        return {}
    if not isinstance(extra_body, Mapping) or not all(
        isinstance(key, str) for key in extra_body
    ):
        raise _configuration("extra_body must be a JSON object with string keys")
    if reserved.intersection(extra_body):
        raise _configuration("extra_body cannot override adapter-managed fields")
    copied = _json_copy(dict(extra_body))
    if not isinstance(copied, dict):  # pragma: no cover - guarded above
        raise _configuration("extra_body must be a JSON object")
    return copied


def validate_extra_headers(
    extra_headers: object, reserved: frozenset[str]
) -> dict[str, str]:
    if extra_headers is None:
        return {}
    if not isinstance(extra_headers, Mapping):
        raise _configuration("extra_headers must be a string mapping")
    copied: dict[str, str] = {}
    for name, value in extra_headers.items():
        if not isinstance(name, str) or _HEADER_NAME.fullmatch(name) is None:
            raise _configuration("extra_headers contains an invalid header name")
        if name.lower() in reserved:
            raise _configuration("extra_headers cannot override adapter-managed headers")
        if not isinstance(value, str) or any(
            ord(character) < 32
            or ord(character) == 127
            or ord(character) > 255
            for character in value
        ):
            raise _configuration("extra_headers contains an invalid header value")
        copied[name] = value
    return copied


def provider_endpoint(
    base_url: object, suffix: str, *, allow_localhost: bool
) -> str:
    if not isinstance(allow_localhost, bool):
        raise _configuration("allow_localhost must be a boolean")
    if (
        not isinstance(base_url, str)
        or not base_url
        or base_url != base_url.strip()
        or "?" in base_url
        or "#" in base_url
        or any(not 33 <= ord(character) <= 126 for character in base_url)
    ):
        raise _configuration("provider base URL must be a non-empty string")
    invalid_url = False
    try:
        parsed = urllib.parse.urlsplit(base_url)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
    except ValueError:
        invalid_url = True
        parsed = None
        hostname = None
        username = None
        password = None
    if invalid_url or parsed is None:
        raise _configuration("provider base URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _configuration("provider base URL is invalid")
    invalid_port = False
    try:
        parsed.port
    except ValueError:
        invalid_port = True
    if invalid_port:
        raise _configuration("provider base URL is invalid") from None
    hostname = hostname.rstrip(".").lower()
    try:
        loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if parsed.scheme == "http" and (not loopback or not allow_localhost):
        raise _configuration(
            "plain HTTP is allowed only for explicitly enabled localhost endpoints"
        )
    return base_url.rstrip("/") + suffix


def _literal_type(value: object) -> str | None:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "number"
    if type(value) is str:
        return "string"
    return None


def _annotation_schema(annotation: object) -> dict[str, object]:
    if annotation is None or annotation is type(None):
        return {"type": "null"}
    scalar_types = {str: "string", int: "integer", float: "number", bool: "boolean"}
    for scalar_type, json_type in scalar_types.items():
        if annotation is scalar_type:
            return {"type": json_type}

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, types.UnionType):
        non_null = [item for item in arguments if item is not type(None)]
        if len(arguments) != 2 or len(non_null) != 1:
            raise _configuration("only nullable two-member unions are supported")
        return {
            "anyOf": [_annotation_schema(non_null[0]), {"type": "null"}],
        }
    if origin is Literal:
        if not arguments:
            raise _configuration("empty Literal annotations are not supported")
        kinds = {_literal_type(item) for item in arguments}
        if None in kinds or len(kinds) != 1:
            raise _configuration("Literal values must share one JSON scalar type")
        return {"type": kinds.pop(), "enum": list(arguments)}
    if origin is list:
        if len(arguments) != 1:
            raise _configuration("list annotations must have one item type")
        return {"type": "array", "items": _annotation_schema(arguments[0])}
    if origin is dict:
        if len(arguments) != 2 or arguments[0] is not str:
            raise _configuration("dict annotations must use string keys and one value type")
        return {
            "type": "object",
            "additionalProperties": _annotation_schema(arguments[1]),
        }
    raise _configuration("tool parameters require supported concrete annotations")


def compile_tool_schema(tool: Tool) -> dict[str, object]:
    if not isinstance(tool.name, str) or _TOOL_NAME.fullmatch(tool.name) is None:
        raise _configuration("tool names must use 1-64 letters, digits, underscores, or hyphens")
    if not isinstance(tool.description, str):
        raise _configuration("tool descriptions must be strings")
    invalid_signature = False
    try:
        signature = inspect.signature(tool.handler)
    except (TypeError, ValueError):
        invalid_signature = True
        signature = None
    if invalid_signature:
        raise _configuration("tool handler signature cannot be inspected") from None
    unresolved_annotations = False
    try:
        resolved = get_type_hints(tool.handler)
    except (NameError, TypeError, ValueError, RecursionError):
        unresolved_annotations = True
        resolved = {}
    if unresolved_annotations:
        raise _configuration("tool parameter annotations cannot be resolved") from None

    properties: dict[str, object] = {}
    assert signature is not None
    for parameter in signature.parameters.values():
        if parameter.name == "tool_context":
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                continue
            raise _configuration(
                "tool_context is reserved for keyword-only runtime injection"
            )
        if parameter.name == "idempotency_key":
            if (
                tool.effect == "idempotent"
                and parameter.kind is inspect.Parameter.KEYWORD_ONLY
            ):
                continue
            raise _configuration(
                "idempotency_key is reserved for idempotent tool injection"
            )
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise _configuration("tool handlers must accept explicit keyword arguments")
        annotation = resolved.get(parameter.name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            raise _configuration("tool parameters require concrete annotations")
        properties[parameter.name] = _annotation_schema(annotation)
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _matches_schema(value: object, schema: dict[str, object]) -> bool:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        return any(
            isinstance(item, dict) and _matches_schema(value, item)
            for item in alternatives
        )
    schema_type = schema.get("type")
    if schema_type == "null":
        matches = value is None
    elif schema_type == "boolean":
        matches = type(value) is bool
    elif schema_type == "integer":
        matches = type(value) is int
    elif schema_type == "number":
        matches = type(value) is int or (
            type(value) is float and math.isfinite(value)
        )
    elif schema_type == "string":
        matches = isinstance(value, str)
    elif schema_type == "array":
        item_schema = schema.get("items")
        matches = isinstance(value, list) and isinstance(item_schema, dict) and all(
            _matches_schema(item, item_schema) for item in value
        )
    elif schema_type == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", False)
        if not isinstance(properties, dict) or not isinstance(required, list):
            return False
        keys = set(value)
        if not set(required).issubset(keys):
            return False
        unknown = keys.difference(properties)
        if additional is False and unknown:
            return False
        if isinstance(additional, dict) and any(
            not _matches_schema(value[key], additional) for key in unknown
        ):
            return False
        matches = all(
            key not in value
            or isinstance(item_schema, dict)
            and _matches_schema(value[key], item_schema)
            for key, item_schema in properties.items()
        )
    else:
        return False
    enum = schema.get("enum")
    return matches and (not isinstance(enum, list) or value in enum)


def validate_tool_arguments(
    calls: tuple[ToolCall, ...],
    schemas: Mapping[str, dict[str, object]],
    provider: str,
    request_id: str | None = None,
) -> None:
    for call in calls:
        schema = schemas.get(call.name)
        if schema is None:
            raise ProviderProtocolError(
                "provider called a tool that was not advertised",
                provider=provider,
                request_id=request_id,
            )
        if not _matches_schema(call.arguments, schema):
            raise ProviderProtocolError(
                "provider tool arguments do not match the advertised schema",
                provider=provider,
                request_id=request_id,
            )


def encode_provider_state(provider: str, field: str, value: object) -> str:
    invalid = False
    try:
        encoded = json.dumps(
            {"provider": provider, "version": 1, field: value},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        invalid = True
        encoded = ""
    if invalid:
        raise ProviderProtocolError(
            "provider continuation is not JSON-compatible", provider=provider
        ) from None
    return encoded


def decode_provider_state(state: str, provider: str, field: str) -> list[object]:
    invalid = False
    try:
        decoded = strict_json_loads(state)
    except (TypeError, ValueError, RecursionError):
        invalid = True
        decoded = None
    if invalid:
        raise ProviderProtocolError(
            "provider_state is not valid JSON", provider=provider
        ) from None
    if not isinstance(decoded, dict):
        raise ProviderProtocolError("provider_state must be an object", provider=provider)
    state_provider = decoded.get("provider")
    if not isinstance(state_provider, str):
        raise ProviderProtocolError(
            "provider_state has no provider identifier", provider=provider
        )
    if state_provider != provider:
        raise ProviderProtocolError(
            "provider_state belongs to a different provider", provider=provider
        )
    version = decoded.get("version")
    if (
        set(decoded) != {"provider", "version", field}
        or type(version) is not int
        or version != 1
    ):
        raise ProviderProtocolError(
            "provider_state version or shape is unsupported", provider=provider
        )
    value = decoded[field]
    if not isinstance(value, list):
        raise ProviderProtocolError(
            "provider_state continuation must be an array", provider=provider
        )
    return value


def provider_state_name(state: str, provider: str) -> str:
    invalid = False
    try:
        decoded = strict_json_loads(state)
    except (TypeError, ValueError, RecursionError):
        invalid = True
        decoded = None
    if invalid:
        raise ProviderProtocolError(
            "provider_state is not valid JSON", provider=provider
        ) from None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("provider"), str):
        raise ProviderProtocolError(
            "provider_state has no provider identifier", provider=provider
        )
    return decoded["provider"]


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


@dataclass(frozen=True, slots=True)
class JSONResponse:
    body: object
    request_id: str | None
    status: int = 200


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: object


@dataclass(frozen=True, slots=True)
class SSEResponse:
    events: tuple[SSEEvent, ...]
    request_id: str | None
    status: int = 200


class _WorkerStopped(Exception):
    pass


class _WorkerTimedOut(Exception):
    pass


class _RequestControl:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._interrupt_socket: socket.socket | None = None
        self._request_id: str | None = None

    @property
    def request_id(self) -> str | None:
        with self._lock:
            return self._request_id

    def set_request_id(self, request_id: str | None) -> None:
        if request_id is not None:
            with self._lock:
                self._request_id = request_id

    def check(self, deadline: float) -> None:
        if self.stop.is_set():
            raise _WorkerStopped
        if time.monotonic() >= deadline:
            raise _WorkerTimedOut

    def attach_socket(self, source: socket.socket | None) -> None:
        if source is None:
            return
        with self._lock:
            self._interrupt_socket = source
            stopped = self.stop.is_set()
        if stopped:
            self._abort_socket(source)

    @staticmethod
    def _abort_socket(interrupt_socket: socket.socket | None) -> None:
        if interrupt_socket is None:
            return
        try:
            interrupt_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            descriptor = interrupt_socket.detach()
        except OSError:
            return
        if descriptor == -1:
            return
        try:
            socket.close(descriptor)
        except OSError:
            pass

    def cancel(self) -> None:
        self.stop.set()
        with self._lock:
            interrupt_socket = self._interrupt_socket
            self._interrupt_socket = None
        self._abort_socket(interrupt_socket)

    def finish(self) -> None:
        with self._lock:
            self._interrupt_socket = None
        self.finished.set()


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class _InterruptibleHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        *,
        control: _RequestControl,
        deadline: float,
        **options: object,
    ) -> None:
        self.control = control
        self.deadline = deadline
        super().__init__(host, **options)

    def connect(self) -> None:
        super().connect()
        self.control.attach_socket(self.sock)
        self.control.check(self.deadline)

    def send(self, data: object) -> None:
        self.control.check(self.deadline)
        if self.sock is None:
            self.connect()
        self.control.check(self.deadline)
        super().send(data)


class _InterruptibleHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        control: _RequestControl,
        deadline: float,
        **options: object,
    ) -> None:
        self.control = control
        self.deadline = deadline
        super().__init__(host, **options)

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.control.attach_socket(self.sock)
        self.control.check(self.deadline)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
            do_handshake_on_connect=False,
        )
        self.control.attach_socket(self.sock)
        self.control.check(self.deadline)
        self.sock.do_handshake()
        self.control.check(self.deadline)

    def send(self, data: object) -> None:
        self.control.check(self.deadline)
        if self.sock is None:
            self.connect()
        self.control.check(self.deadline)
        super().send(data)


class _ControlledHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, control: _RequestControl, deadline: float) -> None:
        super().__init__()
        self.control = control
        self.deadline = deadline

    def _connection(self, host: str, **options: object) -> http.client.HTTPConnection:
        return _InterruptibleHTTPConnection(
            host, control=self.control, deadline=self.deadline, **options
        )

    def http_open(self, request):
        return self.do_open(self._connection, request)


class _ControlledHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, control: _RequestControl, deadline: float) -> None:
        super().__init__()
        self.control = control
        self.deadline = deadline

    def _connection(self, host: str, **options: object) -> http.client.HTTPSConnection:
        return _InterruptibleHTTPSConnection(
            host, control=self.control, deadline=self.deadline, **options
        )

    def https_open(self, request):
        return self.do_open(self._connection, request, context=self._context)


class JSONTransport:
    def __init__(
        self,
        *,
        provider: str,
        endpoint: str,
        timeout: object,
        max_response_bytes: object,
        request_id_headers: tuple[str, ...],
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.timeout = validate_positive_number(timeout, "timeout")
        self.max_response_bytes = validate_positive_integer(
            max_response_bytes, "max_response_bytes"
        )
        self.request_id_headers = request_id_headers

    async def post(self, body: dict[str, object], headers: dict[str, str]) -> JSONResponse:
        response = await self._request(body, headers, None)
        if not isinstance(response, JSONResponse):  # pragma: no cover - private invariant
            raise AssertionError("JSON transport returned an SSE response")
        return response

    async def post_sse(
        self,
        body: dict[str, object],
        headers: dict[str, str],
        terminal_events: frozenset[str],
    ) -> SSEResponse:
        if not terminal_events or any(
            not isinstance(item, str) or not item for item in terminal_events
        ):
            raise ProviderConfigurationError(
                "SSE terminal events must be non-empty strings", provider=self.provider
            )
        response = await self._request(body, headers, terminal_events)
        if not isinstance(response, SSEResponse):  # pragma: no cover - private invariant
            raise AssertionError("SSE transport returned a JSON response")
        return response

    async def _request(
        self,
        body: dict[str, object],
        headers: dict[str, str],
        terminal_events: frozenset[str] | None,
    ) -> JSONResponse | SSEResponse:
        control = _RequestControl()
        deadline = time.monotonic() + self.timeout
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._post, body, headers, control, deadline, terminal_events
            )
        )
        worker.add_done_callback(self._consume_worker)
        timed_out = False
        try:
            return await asyncio.wait_for(asyncio.shield(worker), self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            control.cancel()
            raise
        if timed_out:
            control.cancel()
            raise ProviderTimeoutError(
                "provider total deadline expired",
                provider=self.provider,
                request_id=control.request_id,
                retryable=True,
            ) from None
        raise AssertionError("unreachable")

    @staticmethod
    def _consume_worker(worker: asyncio.Task[JSONResponse | SSEResponse]) -> None:
        try:
            worker.exception()
        except BaseException:
            pass

    def _request_id(self, headers: object) -> str | None:
        for name in self.request_id_headers:
            value = headers.get(name)  # type: ignore[union-attr]
            if isinstance(value, str) and value:
                return value
        return None

    def _read_limited(
        self,
        response: object,
        request_id: str | None,
        control: _RequestControl,
        deadline: float,
    ) -> bytes:
        length = response.headers.get("Content-Length")  # type: ignore[union-attr]
        transfer_encoding = response.headers.get(  # type: ignore[union-attr]
            "Transfer-Encoding"
        )
        if length is not None and transfer_encoding is not None:
            raise ProviderProtocolError(
                "provider response has conflicting HTTP framing",
                provider=self.provider,
                request_id=request_id,
            )
        expected_length: int | None = None
        if length is not None:
            invalid_length = False
            try:
                parsed_length = int(length)
            except ValueError:
                invalid_length = True
                parsed_length = -1
            if invalid_length:
                raise ProviderProtocolError(
                    "provider returned an invalid Content-Length",
                    provider=self.provider,
                    request_id=request_id,
                ) from None
            if parsed_length < 0 or parsed_length > self.max_response_bytes:
                raise ProviderProtocolError(
                    "provider response exceeds the configured size limit",
                    provider=self.provider,
                    request_id=request_id,
                )
            expected_length = parsed_length
        content = bytearray()
        reader = getattr(response, "read1", None)
        if not callable(reader):
            reader = response.read  # type: ignore[union-attr]
        while expected_length is None or len(content) < expected_length:
            if control.stop.is_set():
                raise _WorkerStopped
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError(
                    "provider total deadline expired",
                    provider=self.provider,
                    request_id=request_id,
                    retryable=True,
                )
            remaining_limit = self.max_response_bytes + 1 - len(content)
            if expected_length is not None:
                remaining_limit = min(remaining_limit, expected_length - len(content))
            if remaining_limit <= 0:
                break
            read_timed_out = False
            try:
                chunk = reader(min(64 * 1024, remaining_limit))
            except (TimeoutError, socket.timeout):
                read_timed_out = True
                chunk = b""
            if read_timed_out:
                continue
            if control.stop.is_set():
                raise _WorkerStopped
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > self.max_response_bytes:
            raise ProviderProtocolError(
                "provider response exceeds the configured size limit",
                provider=self.provider,
                request_id=request_id,
            )
        if expected_length is not None and len(content) != expected_length:
            raise ProviderConnectionError(
                "provider transport ended before the declared response body",
                provider=self.provider,
                request_id=request_id,
                retryable=True,
            )
        return bytes(content)

    @staticmethod
    def _json_content_type(headers: object) -> bool:
        value = headers.get("Content-Type")  # type: ignore[union-attr]
        if not isinstance(value, str):
            return False
        media_type = value.split(";", 1)[0].strip().lower()
        return media_type == "application/json" or (
            media_type.startswith("application/") and media_type.endswith("+json")
        )

    @staticmethod
    def _sse_content_type(headers: object) -> bool:
        value = headers.get("Content-Type")  # type: ignore[union-attr]
        if not isinstance(value, str):
            return False
        parts = [item.strip() for item in value.split(";")]
        if parts[0].casefold() != "text/event-stream":
            return False
        for parameter in parts[1:]:
            if not parameter:
                continue
            name, separator, parameter_value = parameter.partition("=")
            if not separator:
                return False
            if name.strip().casefold() == "charset" and parameter_value.strip(
                ' "'
            ).casefold() not in {"utf-8", "utf8"}:
                return False
        return True

    def _read_sse(
        self,
        response: object,
        request_id: str | None,
        control: _RequestControl,
        deadline: float,
        terminal_events: frozenset[str],
    ) -> tuple[SSEEvent, ...]:
        length = response.headers.get("Content-Length")  # type: ignore[union-attr]
        transfer_encoding = response.headers.get(  # type: ignore[union-attr]
            "Transfer-Encoding"
        )
        if length is not None and transfer_encoding is not None:
            raise ProviderProtocolError(
                "provider response has conflicting HTTP framing",
                provider=self.provider,
                request_id=request_id,
            )
        expected_length: int | None = None
        if length is not None:
            try:
                expected_length = int(length)
            except ValueError:
                raise ProviderProtocolError(
                    "provider returned an invalid Content-Length",
                    provider=self.provider,
                    request_id=request_id,
                ) from None
            if expected_length < 0 or expected_length > self.max_response_bytes:
                raise ProviderProtocolError(
                    "provider response exceeds the configured size limit",
                    provider=self.provider,
                    request_id=request_id,
                )

        events: list[SSEEvent] = []
        event_name: str | None = None
        data_lines: list[str] = []
        line: list[str] = []
        byte_count = 0
        pending_cr = False
        first_character = True
        terminal = False
        decoder = codecs.getincrementaldecoder("utf-8")("strict")

        def dispatch_line(value: str) -> None:
            nonlocal event_name, terminal
            if value == "":
                if event_name is None and not data_lines:
                    return
                if event_name is None or not data_lines:
                    raise ProviderProtocolError(
                        "provider SSE frame requires one event and data",
                        provider=self.provider,
                        request_id=request_id,
                    )
                data = self._decode("\n".join(data_lines).encode("utf-8"), request_id)
                events.append(SSEEvent(event_name, data))
                terminal = event_name in terminal_events
                event_name = None
                data_lines.clear()
                return
            if value.startswith(":"):
                return
            field, separator, field_value = value.partition(":")
            if separator and field_value.startswith(" "):
                field_value = field_value[1:]
            if field == "event":
                if event_name is not None or not field_value:
                    raise ProviderProtocolError(
                        "provider SSE frame has an invalid event field",
                        provider=self.provider,
                        request_id=request_id,
                    )
                event_name = field_value
            elif field == "data":
                data_lines.append(field_value)
            else:
                raise ProviderProtocolError(
                    "provider SSE frame contains an unsupported field",
                    provider=self.provider,
                    request_id=request_id,
                )

        def feed_text(value: str) -> None:
            nonlocal pending_cr, first_character
            for character in value:
                if terminal:
                    return
                if first_character:
                    first_character = False
                    if character == "\ufeff":
                        continue
                elif character == "\ufeff":
                    raise ProviderProtocolError(
                        "provider SSE stream contains a misplaced BOM",
                        provider=self.provider,
                        request_id=request_id,
                    )
                if pending_cr:
                    pending_cr = False
                    if character == "\n":
                        continue
                if character == "\r":
                    dispatch_line("".join(line))
                    line.clear()
                    pending_cr = True
                elif character == "\n":
                    dispatch_line("".join(line))
                    line.clear()
                else:
                    line.append(character)

        reader = getattr(response, "read1", None)
        if not callable(reader):
            reader = response.read  # type: ignore[union-attr]
        while (not terminal or expected_length is not None) and (
            expected_length is None or byte_count < expected_length
        ):
            control.check(deadline)
            remaining = self.max_response_bytes + 1 - byte_count
            if expected_length is not None:
                remaining = min(remaining, expected_length - byte_count)
            if remaining <= 0:
                break
            try:
                chunk = reader(min(64 * 1024, remaining))
            except (TimeoutError, socket.timeout):
                continue
            control.check(deadline)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > self.max_response_bytes:
                raise ProviderProtocolError(
                    "provider response exceeds the configured size limit",
                    provider=self.provider,
                    request_id=request_id,
                )
            try:
                feed_text(decoder.decode(chunk))
            except UnicodeDecodeError:
                raise ProviderProtocolError(
                    "provider SSE stream is not valid UTF-8",
                    provider=self.provider,
                    request_id=request_id,
                ) from None

        try:
            feed_text(decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            raise ProviderProtocolError(
                "provider SSE stream is not valid UTF-8",
                provider=self.provider,
                request_id=request_id,
            ) from None
        if expected_length is not None and byte_count != expected_length:
            raise ProviderConnectionError(
                "provider transport ended before the declared response body",
                provider=self.provider,
                request_id=request_id,
                retryable=True,
            )
        if terminal:
            return tuple(events)
        if line or event_name is not None or data_lines:
            raise ProviderProtocolError(
                "provider SSE stream ended mid-frame",
                provider=self.provider,
                request_id=request_id,
            )
        raise ProviderIncompleteError(
            "provider SSE stream ended before a terminal event",
            provider=self.provider,
            code="interrupted_stream",
            request_id=request_id,
            retryable=True,
        )

    def _decode(self, content: bytes, request_id: str | None) -> object:
        invalid = False
        try:
            decoded = strict_json_loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            invalid = True
            decoded = None
        if invalid:
            raise ProviderProtocolError(
                "provider returned malformed JSON",
                provider=self.provider,
                request_id=request_id,
            ) from None
        return decoded

    @staticmethod
    def _error_metadata(body: object) -> tuple[str | None, str | None]:
        if not isinstance(body, dict):
            return None, None
        error = body.get("error")
        code = None
        if isinstance(error, dict):
            candidate = error.get("code")
            if not isinstance(candidate, str):
                candidate = error.get("type")
            code = stable_error_code(candidate, _STABLE_ERROR_CODES, None)
        # Response bodies are untrusted prose. Request IDs are accepted only from
        # the provider-specific HTTP headers selected by the adapter.
        return code, None

    def _http_error(
        self,
        error: urllib.error.HTTPError,
        control: _RequestControl,
        deadline: float,
    ) -> ProviderHTTPError:
        request_id = self._request_id(error.headers)
        control.set_request_id(request_id)
        retry_after = parse_retry_after(error.headers.get("Retry-After"))
        body: object = None
        try:
            try:
                content = self._read_limited(error, request_id, control, deadline)
            except _WorkerStopped:
                raise
            except (ProviderError, OSError, http.client.HTTPException, TimeoutError):
                content = b""
            if content and self._json_content_type(error.headers):
                try:
                    body = self._decode(content, request_id)
                except ProviderProtocolError:
                    body = None
        finally:
            error.close()
        code, body_request_id = self._error_metadata(body)
        request_id = request_id or body_request_id
        fields = {
            "provider": self.provider,
            "status": error.code,
            "code": code,
            "request_id": request_id,
            "retry_after": retry_after,
            "retryable": error.code in {408, 409, 429} or error.code >= 500,
        }
        if error.code == 401:
            return ProviderAuthError("provider authentication failed", **fields)
        if error.code == 403:
            return ProviderPermissionError("provider permission denied", **fields)
        if error.code == 429:
            return ProviderRateLimitError("provider rate limit exceeded", **fields)
        return ProviderHTTPError("provider HTTP request failed", **fields)

    @staticmethod
    def _clean_error(error: ProviderError) -> ProviderError:
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        return error

    def _post(
        self,
        body: dict[str, object],
        headers: dict[str, str],
        control: _RequestControl,
        deadline: float,
        terminal_events: frozenset[str] | None = None,
    ) -> JSONResponse | SSEResponse:
        encoding_failed = False
        try:
            payload = json.dumps(
                body, allow_nan=False, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            encoding_failed = True
            payload = b""
        if encoding_failed:
            raise self._clean_error(ProviderConfigurationError(
                "provider request is not JSON-compatible", provider=self.provider
            )) from None
        if control.stop.is_set():
            raise _WorkerStopped
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        opener = urllib.request.build_opener(
            _RejectRedirects(),
            _ControlledHTTPHandler(control, deadline),
            _ControlledHTTPSHandler(control, deadline),
        )
        result: JSONResponse | SSEResponse | None = None
        failure: ProviderError | None = None
        stopped = False
        try:
            response = opener.open(request, timeout=self.timeout)
            with response:
                request_id = self._request_id(response.headers)
                control.set_request_id(request_id)
                status = getattr(response, "status", None)
                if type(status) is not int:
                    failure = ProviderProtocolError(
                        "provider response has no HTTP status",
                        provider=self.provider,
                        request_id=request_id,
                    )
                elif status != 200:
                    failure = ProviderHTTPError(
                        "provider HTTP request failed",
                        provider=self.provider,
                        status=status,
                        request_id=request_id,
                        retry_after=parse_retry_after(
                            response.headers.get("Retry-After")
                        ),
                        retryable=status in {408, 409, 429} or status >= 500,
                    )
                elif terminal_events is None and not self._json_content_type(
                    response.headers
                ):
                    failure = ProviderProtocolError(
                        "provider response Content-Type is not JSON-compatible",
                        provider=self.provider,
                        request_id=request_id,
                    )
                elif terminal_events is not None and not self._sse_content_type(
                    response.headers
                ):
                    failure = ProviderProtocolError(
                        "provider response Content-Type is not text/event-stream",
                        provider=self.provider,
                        request_id=request_id,
                    )
                elif terminal_events is None:
                    content = self._read_limited(
                        response, request_id, control, deadline
                    )
                    result = JSONResponse(
                        self._decode(content, request_id), request_id, status
                    )
                else:
                    result = SSEResponse(
                        self._read_sse(
                            response,
                            request_id,
                            control,
                            deadline,
                            terminal_events,
                        ),
                        request_id,
                        status,
                    )
        except urllib.error.HTTPError as error:
            try:
                failure = self._http_error(error, control, deadline)
            except _WorkerStopped:
                stopped = True
            except Exception:
                failure = ProviderConnectionError(
                    "provider transport failed",
                    provider=self.provider,
                    request_id=control.request_id,
                    retryable=True,
                )
        except _WorkerStopped:
            stopped = True
        except _WorkerTimedOut:
            failure = ProviderTimeoutError(
                "provider total deadline expired",
                provider=self.provider,
                request_id=control.request_id,
                retryable=True,
            )
        except (TimeoutError, socket.timeout):
            failure = ProviderTimeoutError(
                "provider transport timed out",
                provider=self.provider,
                request_id=control.request_id,
                retryable=True,
            )
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                failure = ProviderTimeoutError(
                    "provider transport timed out",
                    provider=self.provider,
                    request_id=control.request_id,
                    retryable=True,
                )
            else:
                failure = ProviderConnectionError(
                    "provider transport failed",
                    provider=self.provider,
                    request_id=control.request_id,
                    retryable=True,
                )
        except ProviderError as error:
            failure = error
        except (OSError, ValueError, http.client.HTTPException):
            failure = ProviderConnectionError(
                "provider transport failed",
                provider=self.provider,
                request_id=control.request_id,
                retryable=True,
            )
        finally:
            control.finish()
        if stopped:
            raise _WorkerStopped
        if failure is not None:
            raise self._clean_error(failure) from None
        if result is None:
            raise self._clean_error(ProviderConnectionError(
                "provider transport failed",
                provider=self.provider,
                request_id=control.request_id,
                retryable=True,
            )) from None
        return result
