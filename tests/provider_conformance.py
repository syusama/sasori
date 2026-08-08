from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from sasori import AnthropicMessagesModel, Model, OpenAIResponsesModel


WireEvent = tuple[str, object]


@dataclass(frozen=True)
class _Response:
    body: bytes
    status: int
    headers: dict[str, str]
    body_gate: threading.Event | None


class LoopbackProviderServer:
    """Deterministic real-HTTP fixture shared by every provider case."""

    def __init__(self) -> None:
        self._responses: deque[_Response] = deque()
        self._lock = threading.Lock()
        self._body_gates: list[threading.Event] = []
        self.requests: list[dict[str, object]] = []
        self.request_started = threading.Event()
        self.headers_sent = threading.Event()
        self.response_finished = threading.Event()
        self.client_disconnected = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.active_handlers = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner._handle(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def queue_json(
        self,
        body: object,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body_gate: threading.Event | None = None,
        declared_length: int | None = None,
    ) -> None:
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        response_headers = {"Content-Type": "application/json"}
        response_headers.update(headers or {})
        response_headers["Content-Length"] = str(
            len(encoded) if declared_length is None else declared_length
        )
        self._queue(encoded, status, response_headers, body_gate)

    def queue_sse(
        self,
        events: tuple[WireEvent, ...],
        *,
        headers: dict[str, str] | None = None,
        body_gate: threading.Event | None = None,
        declared_length: int | None = None,
    ) -> bytes:
        frames = []
        for event, data in events:
            encoded = json.dumps(
                data, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            frames.append(
                b"event: "
                + event.encode("ascii")
                + b"\n"
                + b"data: "
                + encoded
                + b"\n\n"
            )
        body = b"".join(frames)
        response_headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        response_headers.update(headers or {})
        if declared_length is not None:
            response_headers["Content-Length"] = str(declared_length)
        self._queue(body, 200, response_headers, body_gate)
        return body

    def _queue(
        self,
        body: bytes,
        status: int,
        headers: dict[str, str],
        body_gate: threading.Event | None,
    ) -> None:
        if body_gate is not None:
            self._body_gates.append(body_gate)
        with self._lock:
            self._responses.append(_Response(body, status, headers, body_gate))

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.active_handlers += 1
            self.idle.clear()
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            raw_request = handler.rfile.read(length) if length else b""
            try:
                request_body = json.loads(raw_request) if raw_request else None
            except json.JSONDecodeError:
                request_body = None
            with self._lock:
                self.requests.append(
                    {
                        "method": handler.command,
                        "path": handler.path,
                        "headers": dict(handler.headers.items()),
                        "body": request_body,
                    }
                )
                response = self._responses.popleft()
            self.request_started.set()
            handler.send_response(response.status)
            for name, value in response.headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            self.headers_sent.set()
            if response.body_gate is not None:
                response.body_gate.wait()
            handler.wfile.write(response.body)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            self.client_disconnected.set()
        finally:
            self.response_finished.set()
            with self._lock:
                self.active_handlers -= 1
                if self.active_handlers == 0:
                    self.idle.set()

    def close(self) -> None:
        for gate in self._body_gates:
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def __enter__(self) -> LoopbackProviderServer:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class ProviderFactory(Protocol):
    name: str
    error_provider: str
    rate_limit_code: str
    request_path: str

    def model(
        self,
        server: LoopbackProviderServer,
        *,
        stream: bool = False,
        timeout: float = 2.0,
    ) -> Model: ...

    def rate_limit_body(self) -> object: ...

    def malformed_tool_stream(self) -> tuple[WireEvent, ...]: ...

    def interrupted_stream(self) -> tuple[WireEvent, ...]: ...

    def duplicate_tool_stream(self) -> tuple[WireEvent, ...]: ...


@dataclass(frozen=True)
class OpenAIProviderFactory:
    name: str = "openai"
    error_provider: str = "openai.responses"
    rate_limit_code: str = "rate_limit_exceeded"
    request_path: str = "/v1/responses"

    def model(
        self,
        server: LoopbackProviderServer,
        *,
        stream: bool = False,
        timeout: float = 2.0,
    ) -> OpenAIResponsesModel:
        return OpenAIResponsesModel(
            "gpt-conformance",
            api_key="local-openai-key",
            base_url=server.base_url + "/v1",
            allow_localhost=True,
            stream=stream,
            timeout=timeout,
        )

    def rate_limit_body(self) -> object:
        return {
            "error": {
                "code": "rate_limit_exceeded",
                "message": "provider detail is not a stable contract",
            }
        }

    def malformed_tool_stream(self) -> tuple[WireEvent, ...]:
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "incomplete",
                    "name": "guarded",
                    "arguments": '{"value":',
                    "status": "completed",
                }
            ],
        }
        return (
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": 0,
                    "response": response,
                },
            ),
        )

    def interrupted_stream(self) -> tuple[WireEvent, ...]:
        return (
            (
                "response.created",
                {"type": "response.created", "sequence_number": 0},
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "call_id": "interrupted",
                        "name": "guarded",
                        "arguments": '{"value":"x"}',
                        "status": "completed",
                    },
                },
            ),
        )

    def duplicate_tool_stream(self) -> tuple[WireEvent, ...]:
        calls = [
            {
                "type": "function_call",
                "call_id": "duplicate",
                "name": "guarded",
                "arguments": '{"value":"x"}',
                "status": "completed",
            },
            {
                "type": "function_call",
                "call_id": "duplicate",
                "name": "guarded",
                "arguments": '{"value":"y"}',
                "status": "completed",
            },
        ]
        return (
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": 0,
                    "response": {"status": "completed", "output": calls},
                },
            ),
        )


@dataclass(frozen=True)
class AnthropicProviderFactory:
    name: str = "anthropic"
    error_provider: str = "anthropic.messages"
    rate_limit_code: str = "rate_limit_error"
    request_path: str = "/v1/messages"

    def model(
        self,
        server: LoopbackProviderServer,
        *,
        stream: bool = False,
        timeout: float = 2.0,
    ) -> AnthropicMessagesModel:
        return AnthropicMessagesModel(
            "claude-conformance",
            api_key="local-anthropic-key",
            base_url=server.base_url,
            allow_localhost=True,
            stream=stream,
            timeout=timeout,
        )

    def rate_limit_body(self) -> object:
        return {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "provider detail is not a stable contract",
            },
        }

    @staticmethod
    def _message_start() -> WireEvent:
        return (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "stop_reason": None,
                },
            },
        )

    @staticmethod
    def _tool_block(index: int, call_id: str, partial_json: str) -> tuple[WireEvent, ...]:
        return (
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "guarded",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": partial_json,
                    },
                },
            ),
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            ),
        )

    @staticmethod
    def _message_end() -> tuple[WireEvent, ...]:
        return (
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        )

    def malformed_tool_stream(self) -> tuple[WireEvent, ...]:
        return (
            self._message_start(),
            *self._tool_block(0, "incomplete", '{"value":'),
            *self._message_end(),
        )

    def interrupted_stream(self) -> tuple[WireEvent, ...]:
        return (
            self._message_start(),
            *self._tool_block(0, "interrupted", '{"value":"x"}'),
            self._message_end()[0],
        )

    def duplicate_tool_stream(self) -> tuple[WireEvent, ...]:
        return (
            self._message_start(),
            *self._tool_block(0, "duplicate", '{"value":"x"}'),
            *self._tool_block(1, "duplicate", '{"value":"y"}'),
            *self._message_end(),
        )


PROVIDER_FACTORIES: tuple[ProviderFactory, ...] = (
    OpenAIProviderFactory(),
    AnthropicProviderFactory(),
)


async def wait_for_event(event: threading.Event, timeout: float = 2.0) -> bool:
    return await asyncio.to_thread(event.wait, timeout)


def exception_in_chain(
    error: BaseException, expected_type: type[BaseException]
) -> BaseException | None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, expected_type):
            return current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None
