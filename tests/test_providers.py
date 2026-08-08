from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    Harness,
    Message,
    ModelCallError,
    Tool,
    ToolCall,
    event_projection,
)
from sasori._provider_common import (  # noqa: E402
    _ControlledHTTPSHandler,
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderIncompleteError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderResponseError,
    ProviderTimeoutError,
    compile_tool_schema,
    parse_retry_after,
    provider_endpoint,
)
from sasori.provider_anthropic import AnthropicMessagesModel  # noqa: E402
from sasori.provider_openai import OpenAIResponsesModel  # noqa: E402


_TLS_CERT = """-----BEGIN CERTIFICATE-----
MIIEWTCCAsGgAwIBAgIJAJinz4jHSjLtMA0GCSqGSIb3DQEBCwUAMF8xCzAJBgNV
BAYTAlhZMRcwFQYDVQQHDA5DYXN0bGUgQW50aHJheDEjMCEGA1UECgwaUHl0aG9u
IFNvZnR3YXJlIEZvdW5kYXRpb24xEjAQBgNVBAMMCWxvY2FsaG9zdDAeFw0xODA4
MjkxNDIzMTVaFw0yODA4MjYxNDIzMTVaMF8xCzAJBgNVBAYTAlhZMRcwFQYDVQQH
DA5DYXN0bGUgQW50aHJheDEjMCEGA1UECgwaUHl0aG9uIFNvZnR3YXJlIEZvdW5k
YXRpb24xEjAQBgNVBAMMCWxvY2FsaG9zdDCCAaIwDQYJKoZIhvcNAQEBBQADggGP
ADCCAYoCggGBALKUqUtopT6E68kN+uJNEt34i2EbmG/bwjcD8IaMsgJPSsMO2Bpd
3S6qWgkCeOyCfmAwBxK2kNbxGb63ouysEv7l8GCTJTWv3hG/HQcejJpnAEGi6K1U
fDbyE/db6yZ12SoHVTGkadN4vYGCPd1Wj9ZO1F877SHQ8rDWX3xgTWkxN2ojBw44
T8RHSDiG8D/CvG4uEy+VUszL+Uvny5y2poNSqvI3J56sptWSrh8nIIbkPZPBdUne
LYMOHTFK3ZjXSmhlXgziTxK71nnzM3Y9K9gxPnRqoXbvu/wFo55hQCkETiRkYgmm
jXcBMZ0TClQVnQWuLjMthRnWFZs4Lfmwqjs7FZD/61581R2BYehvpWbLvvuOJhwv
DFzexL2sXcAl7SsxbzeQKRHqGbIDfbnQTXfs3/VC6Ye5P82P2ucj+XC32N9piRmO
gCBP8L3ub+YzzdxikZN2gZXXE2jsb3QyE/R2LkWdWyshpKe+RsZP1SBRbHShUyOh
yJ90baoiEwj2mwIDAQABoxgwFjAUBgNVHREEDTALgglsb2NhbGhvc3QwDQYJKoZI
hvcNAQELBQADggGBAHRUO/UIHl3jXQENewYayHxkIx8t7nu40iO2DXbicSijz5bo
5//xAB6RxhBAlsDBehgQP1uoZg+WJW+nHu3CIVOU3qZNZRaozxiCl2UFKcNqLOmx
R3NKpo1jYf4REQIeG8Yw9+hSWLRbshNteP6bKUUf+vanhg9+axyOEOH/iOQvgk/m
b8wA8wNa4ujWljPbTQnj7ry8RqhTM0GcAN5LSdSvcKcpzLcs3aYwh+Z8e30sQWna
F40sa5u7izgBTOrwpcDm/w5kC46vpRQ5fnbshVw6pne2by0mdMECASid/p25N103
jMqTFlmO7kpf/jpCSmamp3/JSEE1BJKHwQ6Ql4nzRA2N1mnvWH7Zxcv043gkHeAu
0x8evpvwuhdIyproejNFlBpKmW8OX7yKTCPPMC/VkX8Q1rVkxU0DQ6hmvwZlhoKa
9Wc2uXpw9xF8itV4Uvcdr3dwqByvIqn7iI/gB+4l41e0u8OmH2MKOx4Nxlly5TNW
HcVKQHyOeyvnINuBAQ==
-----END CERTIFICATE-----
"""

_TLS_KEY = """-----BEGIN PRIVATE KEY-----
MIIG/wIBADANBgkqhkiG9w0BAQEFAASCBukwggblAgEAAoIBgQCylKlLaKU+hOvJ
DfriTRLd+IthG5hv28I3A/CGjLICT0rDDtgaXd0uqloJAnjsgn5gMAcStpDW8Rm+
t6LsrBL+5fBgkyU1r94Rvx0HHoyaZwBBouitVHw28hP3W+smddkqB1UxpGnTeL2B
gj3dVo/WTtRfO+0h0PKw1l98YE1pMTdqIwcOOE/ER0g4hvA/wrxuLhMvlVLMy/lL
58uctqaDUqryNyeerKbVkq4fJyCG5D2TwXVJ3i2DDh0xSt2Y10poZV4M4k8Su9Z5
8zN2PSvYMT50aqF277v8BaOeYUApBE4kZGIJpo13ATGdEwpUFZ0Fri4zLYUZ1hWb
OC35sKo7OxWQ/+tefNUdgWHob6Vmy777jiYcLwxc3sS9rF3AJe0rMW83kCkR6hmy
A3250E137N/1QumHuT/Nj9rnI/lwt9jfaYkZjoAgT/C97m/mM83cYpGTdoGV1xNo
7G90MhP0di5FnVsrIaSnvkbGT9UgUWx0oVMjocifdG2qIhMI9psCAwEAAQKCAYBT
sHmaPmNaZj59jZCqp0YVQlpHWwBYQ5vD3pPE6oCttm0p9nXt/VkfenQRTthOtmT1
POzDp00/feP7zeGLmqSYUjgRekPw4gdnN7Ip2PY5kdW77NWwDSzdLxuOS8Rq1MW9
/Yu+ZPe3RBlDbT8C0IM+Atlh/BqIQ3zIxN4g0pzUlF0M33d6AYfYSzOcUhibOO7H
j84r+YXBNkIRgYKZYbutRXuZYaGuqejRpBj3voVu0d3Ntdb6lCWuClpB9HzfGN0c
RTv8g6UYO4sK3qyFn90ibIR/1GB9watvtoWVZqggiWeBzSWVWRsGEf9O+Cx4oJw1
IphglhmhbgNksbj7bD24on/icldSOiVkoUemUOFmHWhCm4PnB1GmbD8YMfEdSbks
qDr1Ps1zg4mGOinVD/4cY7vuPFO/HCH07wfeaUGzRt4g0/yLr+XjVofOA3oowyxv
JAzr+niHA3lg5ecj4r7M68efwzN1OCyjMrVJw2RAzwvGxE+rm5NiT08SWlKQZnkC
gcEA4wvyLpIur/UB84nV3XVJ89UMNBLm++aTFzld047BLJtMaOhvNqx6Cl5c8VuW
l261KHjiVzpfNM3/A2LBQJcYkhX7avkqEXlj57cl+dCWAVwUzKmLJTPjfaTTZnYJ
xeN3dMYjJz2z2WtgvfvDoJLukVwIMmhTY8wtqqYyQBJ/l06pBsfw5TNvmVIOQHds
8ASOiFt+WRLk2bl9xrGGayqt3VV93KVRzF27cpjOgEcG74F3c0ZW9snERN7vIYwB
JfrlAoHBAMlahPwMP2TYylG8OzHe7EiehTekSO26LGh0Cq3wTGXYsK/q8hQCzL14
kWW638vpwXL6L9ntvrd7hjzWRO3vX/VxnYEA6f0bpqHq1tZi6lzix5CTUN5McpDg
QnjenSJNrNjS1zEF8WeY9iLEuDI/M/iUW4y9R6s3WpgQhPDXpSvd2g3gMGRUYhxQ
Xna8auiJeYFq0oNaOxvJj+VeOfJ3ZMJttd+Y7gTOYZcbg3SdRb/kdxYki0RMD2hF
4ZvjJ6CTfwKBwQDiMqiZFTJGQwYqp4vWEmAW+I4r4xkUpWatoI2Fk5eI5T9+1PLX
uYXsho56NxEU1UrOg4Cb/p+TcBc8PErkGqR0BkpxDMOInTOXSrQe6lxIBoECVXc3
HTbrmiay0a5y5GfCgxPKqIJhfcToAceoVjovv0y7S4yoxGZKuUEe7E8JY2iqRNAO
yOvKCCICv/hcN235E44RF+2/rDlOltagNej5tY6rIFkaDdgOF4bD7f9O5eEni1Bg
litfoesDtQP/3rECgcEAkQfvQ7D6tIPmbqsbJBfCr6fmoqZllT4FIJN84b50+OL0
mTGsfjdqC4tdhx3sdu7/VPbaIqm5NmX10bowWgWSY7MbVME4yQPyqSwC5NbIonEC
d6N0mzoLR0kQ+Ai4u+2g82gicgAq2oj1uSNi3WZi48jQjHYFulCbo246o1NgeFFK
77WshYe2R1ioQfQDOU1URKCR0uTaMHClgfu112yiGd12JAD+aF3TM0kxDXz+sXI5
SKy311DFxECZeXRLpcC3AoHBAJkNMJWTyPYbeVu+CTQkec8Uun233EkXa2kUNZc/
5DuXDaK+A3DMgYRufTKSPpDHGaCZ1SYPInX1Uoe2dgVjWssRL2uitR4ENabDoAOA
ICVYXYYNagqQu5wwirF0QeaMXo1fjhuuHQh8GsMdXZvYEaAITZ9/NG5x/oY08+8H
kr78SMBOPy3XQn964uKG+e3JwpOG14GKABdAlrHKFXNWchu/6dgcYXB87mrC/GhO
zNwzC+QhFTZoOomFoqMgFWujng==
-----END PRIVATE KEY-----
"""


class LocalJSONServer:
    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self.responses: deque[dict[str, object]] = deque()
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.request_started = threading.Event()
        self.headers_sent = threading.Event()
        self.response_finished = threading.Event()
        self.client_disconnected = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                owner._handle(self)

            def do_GET(self) -> None:
                owner._handle(self)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.scheme = "https" if ssl_context is not None else "http"
        if ssl_context is not None:
            self.server.socket = ssl_context.wrap_socket(
                self.server.socket, server_side=True
            )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.server.server_port}"

    def queue(
        self,
        body: object = None,
        *,
        raw: bytes | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        delay: float = 0,
        content_length: bool = True,
        chunks: list[bytes] | None = None,
        chunk_delay: float = 0,
        body_gate: threading.Event | None = None,
    ) -> None:
        with self.lock:
            self.responses.append(
                {
                    "body": {} if body is None else body,
                    "raw": raw,
                    "status": status,
                    "headers": headers or {},
                    "delay": delay,
                    "content_length": content_length,
                    "chunks": chunks,
                    "chunk_delay": chunk_delay,
                    "body_gate": body_gate,
                }
            )

    def queue_sse(
        self,
        events: list[tuple[str, object]],
        *,
        newline: bytes = b"\n",
        bom: bool = False,
        comment: bool = False,
        chunks: list[bytes] | None = None,
        chunk_size: int | None = None,
        chunk_delay: float = 0,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> bytes:
        if raw is None:
            frames = [b"\xef\xbb\xbf" if bom else b""]
            if comment:
                frames.append(b": sasori keepalive" + newline + newline)
            for event, data in events:
                encoded = json.dumps(
                    data, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                frames.append(
                    b"event: "
                    + event.encode("ascii")
                    + newline
                    + b"data: "
                    + encoded
                    + newline
                    + newline
                )
            raw = b"".join(frames)
        if chunk_size is not None:
            chunks = [raw[index : index + chunk_size] for index in range(0, len(raw), chunk_size)]
        response_headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        response_headers.update(headers or {})
        self.queue(
            raw=raw,
            headers=response_headers,
            content_length=False,
            chunks=chunks,
            chunk_delay=chunk_delay,
        )
        return raw

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        raw_request = handler.rfile.read(length) if length else b""
        try:
            request_body = json.loads(raw_request) if raw_request else None
        except json.JSONDecodeError:
            request_body = None
        with self.lock:
            self.requests.append(
                {
                    "method": handler.command,
                    "path": handler.path,
                    "headers": dict(handler.headers.items()),
                    "body": request_body,
                }
            )
            response = self.responses.popleft() if self.responses else {
                "body": {"error": {"code": "unscripted"}},
                "raw": None,
                "status": 500,
                "headers": {},
                "delay": 0,
                "content_length": True,
                "chunks": None,
                "chunk_delay": 0,
                "body_gate": None,
            }
        self.request_started.set()
        time.sleep(float(response["delay"]))
        raw_response = response["raw"]
        if raw_response is None:
            raw_response = json.dumps(
                response["body"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        headers = dict(response["headers"])
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"
        if response["content_length"] and not any(
            name.lower() == "content-length" for name in headers
        ):
            headers["Content-Length"] = str(len(raw_response))
        try:
            handler.send_response(int(response["status"]))
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            self.headers_sent.set()
            body_gate = response["body_gate"]
            if body_gate is not None:
                body_gate.wait(2)
            chunks = response["chunks"]
            if chunks is None:
                handler.wfile.write(raw_response)
            else:
                for chunk in chunks:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    time.sleep(float(response["chunk_delay"]))
        except (
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
            ssl.SSLError,
        ):
            self.client_disconnected.set()
        finally:
            self.response_finished.set()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def openai_text(text: str) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


def anthropic_text(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


def lookup(city: str) -> dict[str, int]:
    return {"temperature": 20}


LOOKUP = Tool("lookup", lookup, "Look up a city", effect="read_only")


def exception_chain(error: BaseException) -> list[BaseException]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return result


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = LocalJSONServer()
        self.addCleanup(self.server.close)

    def _harness(self, *args, **kwargs):
        return self.enterContext(Harness(*args, **kwargs))

    def openai(self, **options: object) -> OpenAIResponsesModel:
        return OpenAIResponsesModel(
            "gpt-test",
            api_key="local-openai-key",
            base_url=self.server.base_url + "/v1",
            allow_localhost=True,
            **options,
        )

    def anthropic(self, **options: object) -> AnthropicMessagesModel:
        return AnthropicMessagesModel(
            "claude-test",
            api_key="local-anthropic-key",
            base_url=self.server.base_url,
            allow_localhost=True,
            **options,
        )

    def test_signature_compiler_is_strict_and_deterministic(self) -> None:
        def query(
            term: str,
            limit: int = 10,
            tags: list[str] | None = None,
            mode: Literal["fast", "safe"] = "safe",
            weights: dict[str, float] | None = None,
            *,
            idempotency_key: str,
        ) -> str:
            return term

        schema = compile_tool_schema(
            Tool(
                "query",
                query,
                effect="idempotent",
                idempotency_key=lambda arguments: str(arguments["term"]),
                tool_revision="1",
            )
        )
        self.assertEqual(
            schema,
            {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "limit": {"type": "integer"},
                    "tags": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "null"},
                        ]
                    },
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                    "weights": {
                        "anyOf": [
                            {
                                "type": "object",
                                "additionalProperties": {"type": "number"},
                            },
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["term", "limit", "tags", "mode", "weights"],
                "additionalProperties": False,
            },
        )

        def missing(value):
            return value

        def positional(value: str, /) -> str:
            return value

        def variadic(*values: str) -> tuple[str, ...]:
            return values

        def broad(value: str | int) -> object:
            return value

        def reserved(*, idempotency_key: str) -> str:
            return idempotency_key

        for name, handler in (
            ("missing", missing),
            ("positional", positional),
            ("variadic", variadic),
            ("broad", broad),
            ("reserved", reserved),
        ):
            with self.subTest(name=name), self.assertRaises(
                ProviderConfigurationError
            ):
                compile_tool_schema(Tool(name, handler, effect="read_only"))

    async def test_openai_harness_replays_reasoning_before_tool_output(self) -> None:
        raw_output = [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "private"}],
                "encrypted_content": "opaque-reasoning",
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"city":"上海"}',
                "status": "completed",
            },
        ]
        self.server.queue({"status": "completed", "output": raw_output})
        self.server.queue(openai_text("Shanghai is 20 C."))
        model = self.openai(extra_body=MappingProxyType({"temperature": 0}))

        result = await self._harness(model, (LOOKUP,)).run(
            (Message("user", "Weather in Shanghai?"),), run_id="openai-provider"
        )

        self.assertEqual(result.final_message.content, "Shanghai is 20 C.")
        self.assertEqual(len(self.server.requests), 2)
        first = self.server.requests[0]
        self.assertEqual(first["path"], "/v1/responses")
        first_body = first["body"]
        self.assertFalse(first_body["parallel_tool_calls"])
        self.assertFalse(first_body["stream"])
        self.assertEqual(first_body["temperature"], 0)
        self.assertTrue(first["headers"].get("Authorization", "").startswith("Bearer "))
        self.assertTrue(first_body["tools"][0]["strict"])
        second_input = self.server.requests[1]["body"]["input"]
        self.assertEqual(second_input[1:3], raw_output)
        self.assertEqual(
            second_input[3],
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": '{"temperature":20}',
            },
        )
        state = json.loads(result.messages[1].provider_state)
        self.assertEqual(state["provider"], "openai.responses")
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["output"], raw_output)

    async def test_anthropic_harness_replays_thinking_and_tool_error(self) -> None:
        called = 0

        def failing_lookup(city: str) -> str:
            nonlocal called
            called += 1
            raise RuntimeError("weather database offline")

        raw_content = [
            {
                "type": "thinking",
                "thinking": "private",
                "signature": "signed-thinking",
            },
            {"type": "redacted_thinking", "data": "opaque-redaction"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "lookup",
                "input": {"city": "上海"},
            },
        ]
        self.server.queue(
            {
                "type": "message",
                "role": "assistant",
                "content": raw_content,
                "stop_reason": "tool_use",
            }
        )
        self.server.queue(anthropic_text("The weather service is unavailable."))
        model = self.anthropic(max_tokens=512)
        tool = Tool("lookup", failing_lookup, effect="read_only")

        result = await self._harness(model, (tool,)).run(
            (
                Message("system", "Answer briefly."),
                Message("user", "Weather in Shanghai?"),
            ),
            run_id="anthropic-provider",
        )

        self.assertEqual(called, 1)
        self.assertEqual(result.final_message.content, "The weather service is unavailable.")
        self.assertEqual(len(self.server.requests), 2)
        first_body = self.server.requests[0]["body"]
        self.assertEqual(first_body["system"], "Answer briefly.")
        self.assertEqual(first_body["max_tokens"], 512)
        self.assertTrue(first_body["tool_choice"]["disable_parallel_tool_use"])
        self.assertFalse(first_body["stream"])
        second_messages = self.server.requests[1]["body"]["messages"]
        self.assertEqual(second_messages[1]["content"], raw_content)
        tool_result = second_messages[2]["content"]
        self.assertEqual(len(tool_result), 1)
        self.assertEqual(tool_result[0]["type"], "tool_result")
        self.assertEqual(tool_result[0]["tool_use_id"], "toolu_1")
        self.assertTrue(tool_result[0]["is_error"])
        state = json.loads(result.messages[2].provider_state)
        self.assertEqual(state["provider"], "anthropic.messages")
        self.assertEqual(state["content"], raw_content)

    async def test_openai_rejects_malformed_or_unsafe_tool_calls(self) -> None:
        cases = (
            (
                "duplicate IDs",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "same",
                            "name": "lookup",
                            "arguments": '{"city":"a"}',
                            "status": "completed",
                        },
                        {
                            "type": "function_call",
                            "call_id": "same",
                            "name": "lookup",
                            "arguments": '{"city":"b"}',
                            "status": "completed",
                        },
                    ],
                },
            ),
            (
                "parallel calls",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "one",
                            "name": "lookup",
                            "arguments": '{"city":"a"}',
                            "status": "completed",
                        },
                        {
                            "type": "function_call",
                            "call_id": "two",
                            "name": "lookup",
                            "arguments": '{"city":"b"}',
                            "status": "completed",
                        },
                    ],
                },
            ),
            (
                "truncated arguments",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "lookup",
                            "arguments": '{"city":',
                            "status": "completed",
                        }
                    ],
                },
            ),
            (
                "duplicate argument key",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "lookup",
                            "arguments": '{"city":"a","city":"b"}',
                            "status": "completed",
                        }
                    ],
                },
            ),
            (
                "non-finite argument",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "lookup",
                            "arguments": '{"city":NaN}',
                            "status": "completed",
                        }
                    ],
                },
            ),
            (
                "wrong schema",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "lookup",
                            "arguments": '{"city":7}',
                            "status": "completed",
                        }
                    ],
                },
            ),
            (
                "unknown tool",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call",
                            "name": "other",
                            "arguments": "{}",
                            "status": "completed",
                        }
                    ],
                },
            ),
        )
        model = self.openai()
        for name, response in cases:
            with self.subTest(name=name):
                self.server.queue(response)
                with self.assertRaises(ProviderProtocolError):
                    await model.complete((Message("user", "call"),), (LOOKUP,))

    async def test_anthropic_rejects_malformed_or_unsafe_tool_calls(self) -> None:
        cases = (
            (
                "duplicate IDs",
                [
                    {"type": "tool_use", "id": "same", "name": "lookup", "input": {"city": "a"}},
                    {"type": "tool_use", "id": "same", "name": "lookup", "input": {"city": "b"}},
                ],
            ),
            (
                "parallel calls",
                [
                    {"type": "tool_use", "id": "one", "name": "lookup", "input": {"city": "a"}},
                    {"type": "tool_use", "id": "two", "name": "lookup", "input": {"city": "b"}},
                ],
            ),
            (
                "non-object input",
                [{"type": "tool_use", "id": "one", "name": "lookup", "input": []}],
            ),
            (
                "wrong schema",
                [{"type": "tool_use", "id": "one", "name": "lookup", "input": {"city": 7}}],
            ),
            (
                "unknown tool",
                [{"type": "tool_use", "id": "one", "name": "other", "input": {}}],
            ),
        )
        model = self.anthropic()
        for name, content in cases:
            with self.subTest(name=name):
                self.server.queue(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": content,
                        "stop_reason": "tool_use",
                    }
                )
                with self.assertRaises(ProviderProtocolError):
                    await model.complete((Message("user", "call"),), (LOOKUP,))

    async def test_response_arguments_must_match_every_schema_constraint(self) -> None:
        def configure(
            mode: Literal["fast", "safe"],
            retries: int | None,
            labels: list[str],
        ) -> str:
            return mode

        tool = Tool("configure", configure, effect="read_only")
        invalid_arguments = (
            {"mode": "fast", "labels": []},
            {
                "mode": "fast",
                "retries": None,
                "labels": [],
                "unexpected": True,
            },
            {"mode": "other", "retries": None, "labels": []},
            {"mode": "fast", "retries": "none", "labels": []},
            {"mode": "fast", "retries": 1, "labels": [7]},
        )
        model = self.openai()
        for ordinal, arguments in enumerate(invalid_arguments):
            with self.subTest(ordinal=ordinal):
                self.server.queue(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": f"call-{ordinal}",
                                "name": "configure",
                                "arguments": json.dumps(arguments),
                                "status": "completed",
                            }
                        ],
                    },
                    headers={"x-request-id": f"schema-{ordinal}"},
                )
                with self.assertRaises(ProviderProtocolError) as raised:
                    await model.complete((Message("user", "configure"),), (tool,))
                self.assertEqual(raised.exception.request_id, f"schema-{ordinal}")

    async def test_dynamic_dict_schema_is_anthropic_only_under_strict_mode(self) -> None:
        def weigh(values: dict[str, float]) -> float:
            return sum(values.values())

        tool = Tool("weigh", weigh, effect="read_only")
        before = len(self.server.requests)
        with self.assertRaises(ProviderConfigurationError):
            await self.openai().complete((Message("user", "weigh"),), (tool,))
        self.assertEqual(len(self.server.requests), before)

        self.server.queue(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-weights",
                        "name": "weigh",
                        "input": {"values": {"a": 1.5, "b": 2}},
                    }
                ],
                "stop_reason": "tool_use",
            }
        )
        reply = await self.anthropic().complete(
            (Message("user", "weigh"),), (tool,)
        )
        self.assertEqual(
            dict(reply.tool_calls[0].arguments),
            {"values": {"a": 1.5, "b": 2}},
        )

    async def test_incomplete_failed_and_refused_responses_are_distinct(self) -> None:
        openai_cases = (
            (
                {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []},
                ProviderIncompleteError,
                "max_output_tokens",
            ),
            ({"status": "cancelled", "output": []}, ProviderIncompleteError, "cancelled"),
            (
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "cut",
                            "name": "lookup",
                            "arguments": '{"city":"x"}',
                            "status": "incomplete",
                        }
                    ],
                },
                ProviderIncompleteError,
                "incomplete_function_call",
            ),
            ({"status": "failed", "error": {"code": "model_error"}}, ProviderResponseError, "model_error"),
            (
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "refusal", "refusal": "no"}],
                        }
                    ],
                },
                ProviderRefusalError,
                "refusal",
            ),
        )
        model = self.openai()
        for body, error_type, code in openai_cases:
            with self.subTest(provider="openai", code=code):
                self.server.queue(body)
                with self.assertRaises(error_type) as raised:
                    await model.complete((Message("user", "unsafe"),), ())
                self.assertEqual(raised.exception.code, code)

        anthropic_cases = (
            ("max_tokens", ProviderIncompleteError),
            ("model_context_window_exceeded", ProviderIncompleteError),
            ("pause_turn", ProviderIncompleteError),
            ("refusal", ProviderRefusalError),
            (None, ProviderProtocolError),
        )
        anthropic = self.anthropic()
        for reason, error_type in anthropic_cases:
            with self.subTest(provider="anthropic", reason=reason):
                self.server.queue(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "partial"}],
                        "stop_reason": reason,
                    }
                )
                with self.assertRaises(error_type):
                    await anthropic.complete((Message("user", "unsafe"),), ())

        self.server.queue(
            {"status": "completed", "output": [{"type": "reasoning", "id": "r"}]}
        )
        with self.assertRaises(ProviderProtocolError):
            await model.complete((Message("user", "blank"),), ())
        self.server.queue(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "private"}],
                "stop_reason": "end_turn",
            }
        )
        with self.assertRaises(ProviderProtocolError):
            await anthropic.complete((Message("user", "blank"),), ())

    async def test_incomplete_provider_call_never_executes_through_harness(self) -> None:
        calls = 0

        def guarded(city: str) -> str:
            nonlocal calls
            calls += 1
            return city

        self.server.queue(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "cut",
                        "name": "guarded",
                        "arguments": '{"city":"x"}',
                        "status": "completed",
                    }
                ],
            }
        )
        with self.assertRaises(ModelCallError):
            await self._harness(
                self.openai(),
                (Tool("guarded", guarded, effect="read_only"),),
            ).run((Message("user", "call"),), run_id="provider-incomplete")
        self.assertEqual(calls, 0)

        self.server.queue(
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "broken"},
                    {
                        "type": "function_call",
                        "call_id": "unsafe-openai",
                        "name": "guarded",
                        "arguments": '{"city":"x"}',
                        "status": "completed",
                    },
                ],
            }
        )
        with self.assertRaises(ModelCallError):
            await self._harness(
                self.openai(),
                (Tool("guarded", guarded, effect="read_only"),),
            ).run((Message("user", "call"),), run_id="malformed-reasoning")
        self.assertEqual(calls, 0)

        self.server.queue(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "missing signature"},
                    {
                        "type": "tool_use",
                        "id": "unsafe-anthropic",
                        "name": "guarded",
                        "input": {"city": "x"},
                    },
                ],
                "stop_reason": "tool_use",
            }
        )
        with self.assertRaises(ModelCallError):
            await self._harness(
                self.anthropic(),
                (Tool("guarded", guarded, effect="read_only"),),
            ).run((Message("user", "call"),), run_id="malformed-thinking")
        self.assertEqual(calls, 0)

    async def test_http_errors_capture_metadata_without_retry(self) -> None:
        cases = (
            (400, ProviderHTTPError, False),
            (401, ProviderAuthError, False),
            (403, ProviderPermissionError, False),
            (408, ProviderHTTPError, True),
            (409, ProviderHTTPError, True),
            (429, ProviderRateLimitError, True),
            (500, ProviderHTTPError, True),
        )
        model = self.openai()
        for status, error_type, retryable in cases:
            with self.subTest(status=status):
                before = len(self.server.requests)
                self.server.queue(
                    {"error": {"code": "wire_error", "message": "hidden"}},
                    status=status,
                    headers={
                        "Retry-After": "1.5",
                        "x-request-id": f"request-{status}",
                    },
                )
                with self.assertRaises(error_type) as raised:
                    await model.complete((Message("user", "fail"),), ())
                error = raised.exception
                self.assertEqual(error.status, status)
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.request_id, f"request-{status}")
                self.assertEqual(error.retry_after, 1.5)
                self.assertIsNone(error.code)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(len(self.server.requests), before + 1)

        self.server.queue(
            {"error": {"code": None, "type": "invalid_request_error"}},
            status=400,
        )
        with self.assertRaises(ProviderHTTPError) as fallback:
            await model.complete((Message("user", "fail"),), ())
        self.assertEqual(fallback.exception.code, "invalid_request_error")

        self.server.queue(
            {
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "hidden"},
                "request_id": "anthropic-body-request",
            },
            status=429,
            headers={"Retry-After": "2"},
        )
        with self.assertRaises(ProviderRateLimitError) as anthropic_error:
            await self.anthropic().complete((Message("user", "fail"),), ())
        self.assertIsNone(anthropic_error.exception.request_id)
        self.assertEqual(anthropic_error.exception.code, "rate_limit_error")
        self.assertEqual(anthropic_error.exception.retry_after, 2)

        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        parsed = parse_retry_after(future)
        self.assertIsNotNone(parsed)
        self.assertGreaterEqual(parsed, 0)
        self.assertLessEqual(parsed, 30)

    async def test_redirect_is_rejected_and_never_followed(self) -> None:
        self.server.queue(
            raw=b"",
            status=302,
            headers={"Location": self.server.base_url + "/redirect-target"},
        )
        with self.assertRaises(ProviderHTTPError) as raised:
            await self.openai().complete((Message("user", "redirect"),), ())
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["method"], "POST")

    async def test_only_http_200_can_complete_a_provider_turn(self) -> None:
        model = self.openai()
        for status in (201, 202, 204):
            with self.subTest(status=status):
                before = len(self.server.requests)
                self.server.queue(openai_text("not accepted"), status=status)
                with self.assertRaises(ProviderHTTPError) as raised:
                    await model.complete((Message("user", "status"),), ())
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(len(self.server.requests), before + 1)

    async def test_malformed_content_type_and_oversize_are_protocol_errors(self) -> None:
        self.server.queue(raw=b"{")
        with self.assertRaises(ProviderProtocolError):
            await self.openai().complete((Message("user", "malformed"),), ())

        self.server.queue([])
        with self.assertRaises(ProviderProtocolError):
            await self.openai().complete((Message("user", "shape"),), ())

        self.server.queue(
            openai_text("hidden"), headers={"Content-Type": "text/plain"}
        )
        with self.assertRaises(ProviderProtocolError) as content_type:
            await self.openai().complete((Message("user", "text"),), ())
        self.assertIsNone(content_type.exception.request_id)

        self.server.queue(raw=b"{" + b" " * 256, content_length=False)
        with self.assertRaises(ProviderProtocolError):
            await self.openai(max_response_bytes=128).complete(
                (Message("user", "large"),), ()
            )

        self.server.queue(
            raw=b'{"status":"completed","status":"failed","output":[]}',
            headers={"x-request-id": "malformed-request"},
        )
        with self.assertRaises(ProviderProtocolError) as malformed:
            await self.openai().complete((Message("user", "duplicate"),), ())
        self.assertEqual(malformed.exception.request_id, "malformed-request")

    async def test_timeout_transport_failure_and_cancellation_are_distinct(self) -> None:
        self.server.queue(openai_text("late"), delay=0.5)
        timed = asyncio.create_task(
            self.openai(timeout=0.2).complete((Message("user", "wait"),), ())
        )
        self.assertTrue(await asyncio.to_thread(self.server.request_started.wait, 1))
        with self.assertRaises(ProviderTimeoutError):
            await timed
        self.assertEqual(len(self.server.requests), 1)

        self.server.queue(
            raw=b"{}",
            headers={"Content-Length": "20"},
        )
        with self.assertRaises(ProviderConnectionError):
            await self.openai(timeout=0.5).complete(
                (Message("user", "truncated transport"),), ()
            )

        self.server.request_started.clear()
        self.server.queue(openai_text("ignored"), delay=0.2)
        task = asyncio.create_task(
            self.openai(timeout=1).complete((Message("user", "cancel"),), ())
        )
        self.assertTrue(await asyncio.to_thread(self.server.request_started.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_slow_drip_obeys_total_deadline_and_releases_worker(self) -> None:
        raw = json.dumps(openai_text("too late"), separators=(",", ":")).encode()
        self.server.queue(
            raw=b"",
            headers={"x-request-id": "slow-drip-request"},
            content_length=False,
            chunks=[bytes([byte]) for byte in raw],
            chunk_delay=0.02,
        )
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            await self.openai(timeout=0.5).complete(
                (Message("user", "slow drip"),), ()
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(raised.exception.request_id, "slow-drip-request")
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(
            await asyncio.to_thread(self.server.response_finished.wait, 1)
        )
        self.assertTrue(self.server.client_disconnected.is_set())

    async def test_slow_drip_cancellation_propagates_after_worker_release(self) -> None:
        raw = json.dumps(openai_text("ignored"), separators=(",", ":")).encode()
        self.server.queue(
            raw=b"",
            content_length=False,
            chunks=[bytes([byte]) for byte in raw],
            chunk_delay=0.02,
        )
        task = asyncio.create_task(
            self.openai(timeout=2).complete((Message("user", "cancel"),), ())
        )
        self.assertTrue(await asyncio.to_thread(self.server.headers_sent.wait, 1))
        started = time.monotonic()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(
            await asyncio.to_thread(self.server.response_finished.wait, 1)
        )
        self.assertTrue(self.server.client_disconnected.is_set())

    async def test_stalled_body_cancellation_does_not_block_event_loop(self) -> None:
        body_gate = threading.Event()
        worker_finished = threading.Event()
        model = self.openai(timeout=2)
        original_post = model._transport._post

        def tracked_post(*args: object, **kwargs: object):
            try:
                return original_post(*args, **kwargs)
            finally:
                worker_finished.set()

        self.server.queue(
            raw=b"{}",
            headers={"Content-Length": "100"},
            body_gate=body_gate,
        )
        with mock.patch.object(model._transport, "_post", side_effect=tracked_post):
            task = asyncio.create_task(
                model.complete((Message("user", "stall"),), ())
            )
            self.assertTrue(await asyncio.to_thread(self.server.headers_sent.wait, 1))

            async def ticker() -> int:
                ticks = 0
                until = asyncio.get_running_loop().time() + 0.25
                while asyncio.get_running_loop().time() < until:
                    ticks += 1
                    await asyncio.sleep(0.01)
                return ticks

            ticker_task = asyncio.create_task(ticker())
            started = time.monotonic()
            try:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(
                    await asyncio.to_thread(worker_finished.wait, 0.5)
                )
                self.assertGreaterEqual(await ticker_task, 10)
            finally:
                body_gate.set()
        self.assertTrue(
            await asyncio.to_thread(self.server.response_finished.wait, 1)
        )

    async def test_cancelled_gated_connect_never_sends_and_worker_is_consumed(self) -> None:
        original_create_connection = socket.create_connection
        entered = threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()

        def gated_create_connection(address, *args, **kwargs):
            if address[1] == self.server.server.server_port:
                entered.set()
                release.wait(2)
            return original_create_connection(address, *args, **kwargs)

        loop.set_exception_handler(lambda current_loop, context: loop_errors.append(context))
        try:
            with mock.patch("socket.create_connection", side_effect=gated_create_connection):
                task = asyncio.create_task(
                    self.openai(timeout=3).complete(
                        (Message("user", "cancel before connect"),), ()
                    )
                )
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await asyncio.sleep(0.3)
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(loop_errors, [])

    async def test_configuration_rejects_unsafe_urls_keys_and_escape_hatches(self) -> None:
        before = len(self.server.requests)
        with self.assertRaises(ProviderConfigurationError):
            OpenAIResponsesModel(
                "gpt-test",
                api_key="key",
                base_url=self.server.base_url,
            )
        with self.assertRaises(ProviderConfigurationError):
            OpenAIResponsesModel(
                "gpt-test",
                api_key="key",
                base_url="http://example.com/v1",
                allow_localhost=True,
            )
        with self.assertRaises(ProviderConfigurationError):
            provider_endpoint(
                "https://user:password@example.com/v1",
                "/responses",
                allow_localhost=False,
            )
        for unsafe_url in (
            "https://example.com/v1?",
            "https://example.com/v1#fragment",
            " https://example.com/v1",
            "https://exa mple.com/v1",
            "https://example.com/a b",
            "https://[::1",
            "https://example.com：443",
        ):
            with self.assertRaises(ProviderConfigurationError) as malformed_url:
                provider_endpoint(
                    unsafe_url, "/responses", allow_localhost=False
                )
            self.assertEqual(
                exception_chain(malformed_url.exception), [malformed_url.exception]
            )
        for key in (
            "",
            " spaced ",
            "embedded space",
            "unicode-\u0100",
            "key\r\nInjected: value",
        ):
            with self.subTest(key=bool(key)), self.assertRaises(
                ProviderConfigurationError
            ):
                OpenAIResponsesModel("gpt-test", api_key=key)
        for options in (
            {"extra_body": {"input": []}},
            {"extra_body": {"previous_response_id": "response"}},
            {"extra_body": {"conversation": "conversation"}},
            {"extra_body": {"background": True}},
            {"extra_body": {"temperature": float("nan")}},
            {"extra_headers": {"Authorization": "override"}},
            {"extra_headers": {"Accept": "application/json"}},
            {"extra_headers": {"X-Test": "bad\r\nInjected"}},
            {"extra_headers": {"X-Test": "unicode-\u0100"}},
        ):
            with self.assertRaises(ProviderConfigurationError):
                self.openai(**options)
        with self.assertRaises(ProviderConfigurationError):
            self.anthropic(extra_body={"tool_choice": {"type": "any"}})
        with self.assertRaises(ProviderConfigurationError):
            self.openai(stream=1)
        with self.assertRaises(ProviderConfigurationError):
            self.anthropic(stream="yes")
        with self.assertRaises(ProviderConfigurationError):
            self.openai(timeout=10**10000)
        self.assertEqual(len(self.server.requests), before)

    async def test_key_and_server_error_body_never_enter_exception_text(self) -> None:
        secret = "TOP-SECRET-PROVIDER-KEY"
        self.server.queue(
            {"error": {"code": secret, "message": secret}, "request_id": secret},
            status=400,
            headers={"x-request-id": "safe-id"},
        )
        model = OpenAIResponsesModel(
            "gpt-test",
            api_key=secret,
            base_url=self.server.base_url + "/v1",
            allow_localhost=True,
        )
        with self.assertRaises(ProviderHTTPError) as raised:
            await model.complete((Message("user", "fail"),), ())
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn("safe-id", str(raised.exception))
        self.assertEqual(exception_chain(raised.exception), [raised.exception])

    async def test_stream_error_metadata_is_stable_and_secret_free(self) -> None:
        secret = "STREAM-SECRET-CONTENT"
        cases = (
            (
                self.openai(stream=True),
                [
                    (
                        "error",
                        {
                            "type": "error",
                            "sequence_number": 0,
                            "code": secret,
                            "message": secret,
                        },
                    )
                ],
            ),
            (
                self.anthropic(stream=True),
                [
                    (
                        "error",
                        {
                            "type": "error",
                            "error": {"type": secret, "message": secret},
                        },
                    )
                ],
            ),
            (
                self.anthropic(stream=True),
                [("error", {"type": "error", "error": {"type": []}})],
            ),
        )
        for model, events in cases:
            with self.subTest(provider=type(model).__name__):
                self.server.queue_sse(events)
                with self.assertRaises(ProviderResponseError) as raised:
                    await model.complete((Message("user", "fail"),), ())
                error = raised.exception
                self.assertEqual(error.code, "stream_error")
                rendered = " ".join(
                    repr(item) + repr(vars(item)) for item in exception_chain(error)
                )
                self.assertNotIn(secret, rendered)
                self.assertEqual(exception_chain(error), [error])

    async def test_private_json_never_survives_in_exception_chains(self) -> None:
        secret = "CHAIN-SECRET-CONTENT"
        errors: list[BaseException] = []

        state = Message(
            "assistant",
            provider_state=(
                '{"provider":"openai.responses","version":1,'
                f'"output":["{secret}"'
            ),
        )
        try:
            await self.openai().complete((Message("user", "state"), state), ())
        except ProviderProtocolError as error:
            errors.append(error)

        self.server.queue(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "secret-arguments",
                        "name": "lookup",
                        "arguments": f'{{"city":"{secret}"',
                        "status": "completed",
                    }
                ],
            }
        )
        try:
            await self.openai().complete((Message("user", "arguments"),), (LOOKUP,))
        except ProviderProtocolError as error:
            errors.append(error)

        self.server.queue(raw=f'{{"{secret}"'.encode())
        try:
            await self.openai().complete((Message("user", "response"),), ())
        except ProviderProtocolError as error:
            errors.append(error)

        self.server.queue(
            {"error": {"code": "bad_request", "message": secret}}, status=400
        )
        try:
            await self.openai().complete((Message("user", "HTTP"),), ())
        except ProviderHTTPError as error:
            errors.append(error)

        self.assertEqual(len(errors), 4)
        for error in errors:
            with self.subTest(error=type(error).__name__):
                chain = exception_chain(error)
                rendered = " ".join(
                    repr(item) + repr(vars(item)) for item in chain
                )
                self.assertEqual(chain, [error])
                self.assertNotIn(secret, rendered)

    async def test_malformed_state_and_unresolved_provider_switch_fail_before_network(self) -> None:
        model = self.openai()
        malformed_states = (
            '{"provider":"openai.responses","version":true,"output":[]}',
            (
                '{"provider":"openai.responses","provider":"openai.responses",'
                '"version":1,"output":[]}'
            ),
            '{"provider":"openai.responses","version":1,"output":[NaN]}',
        )
        for state in malformed_states:
            with self.subTest(state=state[-8:]), self.assertRaises(
                ProviderProtocolError
            ):
                await model.complete(
                    (
                        Message("user", "first"),
                        Message("assistant", provider_state=state),
                    ),
                    (),
                )

        mismatch = Message(
            "assistant",
            tool_calls=(ToolCall("toolu_1", "lookup", {"city": "x"}),),
            provider_state=(
                '{"provider":"anthropic.messages","version":1,'
                '"content":[{"type":"tool_use","id":"toolu_1",'
                '"name":"lookup","input":{"city":"x"}}]}'
            ),
        )
        with self.assertRaises(ProviderProtocolError):
            await model.complete((Message("user", "first"), mismatch), (LOOKUP,))

        openai_projection_mismatch = Message(
            "assistant",
            content="VISIBLE",
            provider_state=json.dumps(
                {
                    "provider": "openai.responses",
                    "version": 1,
                    "output": openai_text("HIDDEN")["output"],
                }
            ),
        )
        with self.assertRaises(ProviderProtocolError):
            await model.complete(
                (Message("user", "first"), openai_projection_mismatch), ()
            )

        anthropic_projection_mismatch = Message(
            "assistant",
            content="VISIBLE",
            provider_state=json.dumps(
                {
                    "provider": "anthropic.messages",
                    "version": 1,
                    "content": [{"type": "text", "text": "HIDDEN"}],
                }
            ),
        )
        with self.assertRaises(ProviderProtocolError):
            await self.anthropic().complete(
                (Message("user", "first"), anthropic_projection_mismatch), ()
            )
        self.assertEqual(self.server.requests, [])

    async def test_anthropic_nonleading_system_and_orphan_tool_result_are_rejected(self) -> None:
        model = self.anthropic()
        with self.assertRaises(ProviderProtocolError):
            await model.complete(
                (Message("user", "first"), Message("system", "late")), ()
            )
        with self.assertRaises(ProviderProtocolError):
            await model.complete(
                (
                    Message("user", "first"),
                    Message("tool", "orphan", tool_call_id="missing"),
                ),
                (),
            )
        self.assertEqual(self.server.requests, [])

    async def test_https_round_trip_and_cancellation_release_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cert_path = Path(directory) / "cert.pem"
            key_path = Path(directory) / "key.pem"
            cert_path.write_text(_TLS_CERT, encoding="ascii")
            key_path.write_text(_TLS_KEY, encoding="ascii")
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert_path, key_path)
            tls_server = LocalJSONServer(server_context)
            try:
                model = OpenAIResponsesModel(
                    "gpt-test",
                    api_key="local-openai-key",
                    base_url=tls_server.base_url + "/v1",
                    allow_localhost=True,
                    # This test validates TLS and cooperative cancellation, not the
                    # timeout boundary. Leave hosted Windows enough scheduling room;
                    # the cancellation and worker-release bounds below stay strict.
                    timeout=10,
                )
                client_context = ssl.create_default_context(cafile=str(cert_path))
                client_context.check_hostname = False
                original_connection = _ControlledHTTPSHandler._connection

                def local_tls_connection(handler, host, **options):
                    options["context"] = client_context
                    return original_connection(handler, host, **options)

                with mock.patch.object(
                    _ControlledHTTPSHandler,
                    "_connection",
                    local_tls_connection,
                ):
                    tls_server.queue(openai_text("secure"))
                    reply = await model.complete((Message("user", "TLS"),), ())
                    self.assertEqual(reply.content, "secure")
                    self.assertEqual(len(tls_server.requests), 1)

                    tls_server.headers_sent.clear()
                    tls_server.response_finished.clear()
                    body_gate = threading.Event()
                    worker_finished = threading.Event()
                    original_post = model._transport._post

                    def tracked_post(*args: object, **kwargs: object):
                        try:
                            return original_post(*args, **kwargs)
                        finally:
                            worker_finished.set()

                    tls_server.queue(
                        raw=b"{}",
                        headers={"Content-Length": "100"},
                        body_gate=body_gate,
                    )
                    with mock.patch.object(
                        model._transport, "_post", side_effect=tracked_post
                    ):
                        task = asyncio.create_task(
                            model.complete((Message("user", "cancel TLS"),), ())
                        )
                        self.assertTrue(
                            await asyncio.to_thread(tls_server.headers_sent.wait, 5)
                        )
                        started = time.monotonic()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        self.assertLess(time.monotonic() - started, 0.5)
                        self.assertTrue(
                            await asyncio.to_thread(worker_finished.wait, 0.5)
                        )
                    body_gate.set()
                    self.assertTrue(
                        await asyncio.to_thread(tls_server.response_finished.wait, 5)
                    )
            finally:
                tls_server.close()

    async def test_json_numbers_surrogates_and_http_framing_fail_closed(self) -> None:
        response_cases = (
            (
                b'{"status":"completed","output":[],"overflow":1e9999}',
                "overflow",
            ),
            (
                b'{"status":"completed","output":[{"type":"message",'
                b'"role":"assistant","status":"completed","content":['
                b'{"type":"output_text","text":"\\ud800"}]}]}',
                "surrogate",
            ),
        )
        for raw, name in response_cases:
            with self.subTest(boundary="response", name=name):
                self.server.queue(raw=raw)
                with self.assertRaises(ProviderProtocolError) as raised:
                    await self.openai().complete((Message("user", "bad"),), ())
                self.assertEqual(exception_chain(raised.exception), [raised.exception])

        calls = 0

        def guarded(city: str) -> str:
            nonlocal calls
            calls += 1
            return city

        for arguments, name in (
            ('{"city":1e9999}', "overflow"),
            ('{"city":"\\ud800"}', "surrogate"),
        ):
            with self.subTest(boundary="arguments", name=name):
                self.server.queue(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": f"call-{name}",
                                "name": "guarded",
                                "arguments": arguments,
                                "status": "completed",
                            }
                        ],
                    }
                )
                with self.assertRaises(ModelCallError):
                    await self._harness(
                        self.openai(),
                        (Tool("guarded", guarded, effect="read_only"),),
                    ).run((Message("user", "call"),), run_id=f"bad-{name}")
        self.assertEqual(calls, 0)

        before = len(self.server.requests)
        for state in (
            '{"provider":"openai.responses","version":1,"output":[1e9999]}',
            (
                '{"provider":"openai.responses","version":1,'
                '"output":["\\ud800"]}'
            ),
        ):
            with self.assertRaises(ProviderProtocolError) as raised:
                await self.openai().complete(
                    (
                        Message("user", "state"),
                        Message("assistant", provider_state=state),
                    ),
                    (),
                )
            self.assertEqual(exception_chain(raised.exception), [raised.exception])
        with self.assertRaises(ProviderConfigurationError):
            self.openai(extra_body={"metadata": {"bad": chr(0xD800)}})
        self.assertEqual(len(self.server.requests), before)

        valid = json.dumps(
            openai_text("accepted"), separators=(",", ":")
        ).encode("ascii")
        invalid_chunked = (
            f"{len(valid):X}\r\n".encode()
            + valid
            + b"\r\n8\r\nNOT-JSON\r\n0\r\n\r\n"
        )
        self.server.queue(
            raw=invalid_chunked,
            content_length=False,
            headers={
                "Content-Length": str(len(valid)),
                "Transfer-Encoding": "chunked",
            },
        )
        with self.assertRaises(ProviderProtocolError):
            await self.openai().complete((Message("user", "framing"),), ())

    async def test_invalid_tool_names_and_typed_state_mismatches_need_no_network(self) -> None:
        before = len(self.server.requests)
        invalid_tool = Tool([], lookup, effect="read_only")  # type: ignore[arg-type]
        for model in (self.openai(), self.anthropic()):
            with self.subTest(provider=type(model).__name__), self.assertRaises(
                ProviderConfigurationError
            ):
                await model.complete((Message("user", "tool"),), (invalid_tool,))

        def toggle(enabled: bool) -> bool:
            return enabled

        tool = Tool("toggle", toggle, effect="read_only")
        openai_state = Message(
            "assistant",
            tool_calls=(ToolCall("call-typed", "toggle", {"enabled": True}),),
            provider_state=(
                '{"provider":"openai.responses","version":1,"output":['
                '{"type":"function_call","call_id":"call-typed",'
                '"name":"toggle","arguments":"{\\"enabled\\":1}",'
                '"status":"completed"}]}'
            ),
        )
        with self.assertRaises(ProviderProtocolError):
            await self.openai().complete(
                (Message("user", "typed"), openai_state), (tool,)
            )

        anthropic_state = Message(
            "assistant",
            tool_calls=(ToolCall("toolu-typed", "toggle", {"enabled": True}),),
            provider_state=(
                '{"provider":"anthropic.messages","version":1,"content":['
                '{"type":"tool_use","id":"toolu-typed","name":"toggle",'
                '"input":{"enabled":1}}]}'
            ),
        )
        with self.assertRaises(ProviderProtocolError):
            await self.anthropic().complete(
                (Message("user", "typed"), anthropic_state), (tool,)
            )
        self.assertEqual(len(self.server.requests), before)

    async def test_malformed_openai_reasoning_never_reaches_tools(self) -> None:
        calls = 0

        def guarded(city: str) -> str:
            nonlocal calls
            calls += 1
            return city

        tool = Tool("guarded", guarded, effect="read_only")
        malformed = (
            {
                "type": "reasoning",
                "id": "r",
                "summary": [],
                "status": "incomplete",
            },
            {
                "type": "reasoning",
                "id": "r",
                "summary": [],
                "encrypted_content": 7,
            },
        )
        for ordinal, reasoning in enumerate(malformed):
            self.server.queue(
                {
                    "status": "completed",
                    "output": [
                        reasoning,
                        {
                            "type": "function_call",
                            "call_id": f"unsafe-{ordinal}",
                            "name": "guarded",
                            "arguments": '{"city":"x"}',
                            "status": "completed",
                        },
                    ],
                }
            )
            with self.subTest(ordinal=ordinal), self.assertRaises(ModelCallError):
                await self._harness(self.openai(), (tool,)).run(
                    (Message("user", "call"),), run_id=f"reasoning-{ordinal}"
                )
        self.assertEqual(calls, 0)

    async def test_malformed_optional_call_fields_never_reach_tools(self) -> None:
        calls = 0

        def guarded(city: str) -> str:
            nonlocal calls
            calls += 1
            return city

        tool = Tool("guarded", guarded, effect="read_only")
        openai_fields = (
            {"id": 7},
            {"namespace": []},
            {"caller": {"type": "program"}},
        )
        for ordinal, fields in enumerate(openai_fields):
            call = {
                "type": "function_call",
                "call_id": f"openai-{ordinal}",
                "name": "guarded",
                "arguments": '{"city":"x"}',
                "status": "completed",
                **fields,
            }
            self.server.queue({"status": "completed", "output": [call]})
            with self.subTest(
                provider="openai", ordinal=ordinal
            ), self.assertRaises(ModelCallError):
                await self._harness(self.openai(), (tool,)).run(
                    (Message("user", "call"),), run_id=f"openai-shape-{ordinal}"
                )

        anthropic_callers = (
            {"type": "direct", "unexpected": True},
            {"type": "code_execution_20250825"},
        )
        for ordinal, caller in enumerate(anthropic_callers):
            self.server.queue(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"anthropic-{ordinal}",
                            "name": "guarded",
                            "input": {"city": "x"},
                            "caller": caller,
                        }
                    ],
                    "stop_reason": "tool_use",
                }
            )
            with self.subTest(
                provider="anthropic", ordinal=ordinal
            ), self.assertRaises(ModelCallError):
                await self._harness(self.anthropic(), (tool,)).run(
                    (Message("user", "call"),), run_id=f"anthropic-shape-{ordinal}"
                )
        self.assertEqual(calls, 0)

    async def test_gated_connect_cancel_and_timeout_return_without_orphans(self) -> None:
        original_create_connection = socket.create_connection
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()

        async def exercise(*, cancel: bool) -> None:
            entered = threading.Event()
            release = threading.Event()

            def gated_create_connection(address, *args, **kwargs):
                if address[1] == self.server.server.server_port:
                    entered.set()
                    release.wait(2)
                return original_create_connection(address, *args, **kwargs)

            try:
                with mock.patch(
                    "socket.create_connection", side_effect=gated_create_connection
                ):
                    started = time.monotonic()
                    task = asyncio.create_task(
                        self.openai(timeout=0.5 if not cancel else 3).complete(
                            (Message("user", "gated"),), ()
                        )
                    )
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaises(ProviderTimeoutError):
                            await task
                    self.assertLess(time.monotonic() - started, 1.0)
            finally:
                release.set()
            await asyncio.sleep(0.2)

        loop.set_exception_handler(
            lambda current_loop, context: loop_errors.append(context)
        )
        try:
            await exercise(cancel=True)
            await exercise(cancel=False)
        finally:
            loop.set_exception_handler(previous_handler)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(loop_errors, [])


    async def test_openai_stream_uses_only_the_terminal_response(self) -> None:
        marker = "PRIVATE-DELTA-MUST-NOT-SURVIVE"
        raw_output = [
            {
                "type": "reasoning",
                "id": "rs-stream",
                "summary": [],
                "encrypted_content": "opaque",
            },
            {
                "type": "function_call",
                "call_id": "call-stream",
                "name": "lookup",
                "arguments": '{"city":"Shanghai"}',
                "status": "completed",
            },
        ]
        first = [
            (
                "response.created",
                {"type": "response.created", "sequence_number": 0},
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "delta": marker,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": 2,
                    "response": {"status": "completed", "output": raw_output},
                },
            ),
        ]
        self.server.queue_sse(
            first, newline=b"\r\n", bom=True, comment=True, chunk_size=1
        )
        self.server.queue_sse(
            [
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "sequence_number": 0,
                        "response": openai_text("Shanghai is 20 C."),
                    },
                )
            ]
        )

        harness = self._harness(self.openai(stream=True), (LOOKUP,))
        result = await harness.run(
            (Message("user", "Weather?"),), run_id="openai-stream"
        )

        self.assertEqual(result.final_message.content, "Shanghai is 20 C.")
        self.assertTrue(self.server.requests[0]["body"]["stream"])
        self.assertEqual(
            self.server.requests[0]["headers"].get("Accept"), "text/event-stream"
        )
        self.assertEqual(
            self.server.requests[1]["body"]["input"][1:3], raw_output
        )
        self.assertNotIn(
            marker,
            repr(result.events),
        )
        projected = tuple(
            event_projection(item) for item in harness.stored_events("openai-stream")
        )
        self.assertNotIn(marker, repr(projected))
        stored_state = harness.store.load("openai-stream").history[1].provider_state
        self.assertIsNotNone(stored_state)
        self.assertNotIn(marker, stored_state)
        self.assertNotIn("response.output_text.delta", stored_state)
        self.assertEqual(
            json.loads(stored_state)["output"], raw_output
        )

    async def test_openai_stream_fails_closed_on_protocol_and_terminal_errors(self) -> None:
        cases = (
            (
                [
                    (
                        "response.created",
                        {"type": "response.created", "sequence_number": 0},
                    )
                ],
                ProviderIncompleteError,
            ),
            (
                [
                    (
                        "response.created",
                        {"type": "response.created", "sequence_number": 2},
                    ),
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "sequence_number": 1,
                            "response": openai_text("hidden"),
                        },
                    ),
                ],
                ProviderProtocolError,
            ),
            (
                [
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "sequence_number": 0,
                            "response": {"status": "failed", "error": {}},
                        },
                    )
                ],
                ProviderProtocolError,
            ),
            (
                [
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": openai_text("missing sequence"),
                        },
                    )
                ],
                ProviderProtocolError,
            ),
            (
                [
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "sequence_number": True,
                            "response": openai_text("boolean sequence"),
                        },
                    )
                ],
                ProviderProtocolError,
            ),
            (
                [
                    (
                        "error",
                        {
                            "type": "error",
                            "sequence_number": 0,
                            "code": "server_error",
                        },
                    )
                ],
                ProviderResponseError,
            ),
        )
        for events, error_type in cases:
            with self.subTest(error=error_type.__name__):
                self.server.queue_sse(events)
                with self.assertRaises(error_type):
                    await self.openai(stream=True).complete(
                        (Message("user", "stream"),), ()
                    )

    async def test_openai_stream_malformed_tool_call_never_executes(self) -> None:
        called = 0

        def guarded(city: str) -> str:
            nonlocal called
            called += 1
            return city

        payload = {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "unsafe",
                    "name": "guarded",
                    "arguments": '{"city":"x"',
                    "status": "completed",
                }
            ],
        }
        self.server.queue_sse(
            [
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "sequence_number": 0,
                        "response": payload,
                    },
                )
            ]
        )
        with self.assertRaises(ModelCallError):
            await self._harness(
                self.openai(stream=True),
                (Tool("guarded", guarded, effect="read_only"),),
            ).run((Message("user", "call"),), run_id="bad-openai-stream")
        self.assertEqual(called, 0)

    async def test_openai_stream_parallel_or_duplicate_calls_never_execute(self) -> None:
        called = 0

        def guarded(city: str) -> str:
            nonlocal called
            called += 1
            return city

        tool = Tool("guarded", guarded, effect="read_only")
        for ordinal, call_ids in enumerate((("first", "second"), ("same", "same"))):
            payload = {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "guarded",
                        "arguments": '{"city":"x"}',
                        "status": "completed",
                    }
                    for call_id in call_ids
                ],
            }
            self.server.queue_sse(
                [
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "sequence_number": 0,
                            "response": payload,
                        },
                    )
                ]
            )
            run_id = f"bad-openai-calls-{ordinal}"
            harness = self._harness(self.openai(stream=True), (tool,))
            with self.subTest(call_ids=call_ids), self.assertRaises(ModelCallError):
                await harness.run((Message("user", "call"),), run_id=run_id)
            event_types = [item.event.type for item in harness.stored_events(run_id)]
            self.assertNotIn("model.completed", event_types)
            self.assertFalse(any(item.startswith("tool.") for item in event_types))
            self.assertEqual(event_types[-2:], ["model.failed", "run.failed"])
        self.assertEqual(called, 0)

    async def test_sse_truncated_declared_length_never_executes_terminal_tool(self) -> None:
        called = 0

        def guarded(city: str) -> str:
            nonlocal called
            called += 1
            return city

        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "truncated",
                        "name": "guarded",
                        "arguments": '{"city":"x"}',
                        "status": "completed",
                    }
                ],
            },
        }
        raw = (
            b"event: response.completed\n"
            + b"data: "
            + json.dumps(terminal, separators=(",", ":")).encode()
            + b"\n\n"
        )
        self.server.queue(
            raw=raw,
            headers={
                "Content-Type": "text/event-stream",
                "Content-Length": str(len(raw) + 100),
            },
            content_length=False,
        )
        with self.assertRaises(ModelCallError):
            await self._harness(
                self.openai(stream=True),
                (Tool("guarded", guarded, effect="read_only"),),
            ).run((Message("user", "call"),), run_id="truncated-stream")
        self.assertEqual(called, 0)

    async def test_anthropic_stream_rebuilds_thinking_and_tool_input(self) -> None:
        marker = "PARTIAL-INPUT-MUST-NOT-BE-PUBLIC"
        first = [
            ("ping", {"type": "ping"}),
            (
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
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "private"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "signature_delta",
                        "signature": "signed-thinking",
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu-stream",
                        "name": "lookup",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"city":',
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '"Shanghai"}',
                        "test_marker": marker,
                    },
                },
            ),
        ]
        # Keep the marker in an ignored ping instead; unsupported delta fields fail closed.
        first[-1][1]["delta"].pop("test_marker")
        first.append(("ping", {"type": "ping", "marker": marker}))
        first.extend(
            [
                (
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 1},
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )
        self.server.queue_sse(first, newline=b"\r", chunk_size=2)
        self.server.queue_sse(
            [
                (
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
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Shanghai is 20 C."},
                    },
                ),
                (
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )

        harness = self._harness(self.anthropic(stream=True), (LOOKUP,))
        result = await harness.run(
            (Message("user", "Weather?"),), run_id="anthropic-stream"
        )
        self.assertEqual(result.final_message.content, "Shanghai is 20 C.")
        self.assertTrue(self.server.requests[0]["body"]["stream"])
        expected_content = [
            {
                "type": "thinking",
                "thinking": "private",
                "signature": "signed-thinking",
            },
            {
                "type": "tool_use",
                "id": "toolu-stream",
                "name": "lookup",
                "input": {"city": "Shanghai"},
            },
        ]
        self.assertEqual(
            self.server.requests[1]["body"]["messages"][1]["content"],
            expected_content,
        )
        self.assertNotIn(
            marker,
            repr(result.events),
        )
        projected = tuple(
            event_projection(item)
            for item in harness.stored_events("anthropic-stream")
        )
        self.assertNotIn(marker, repr(projected))
        stored_state = harness.store.load("anthropic-stream").history[1].provider_state
        self.assertIsNotNone(stored_state)
        self.assertNotIn(marker, stored_state)
        self.assertNotIn("partial_json", stored_state)
        self.assertNotIn("input_json_delta", stored_state)
        self.assertEqual(
            json.loads(stored_state)["content"],
            expected_content,
        )

    async def test_anthropic_stream_fails_closed_on_block_and_terminal_errors(self) -> None:
        start = (
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
        cases = (
            (
                [
                    start,
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {"type": "text", "text": ""},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ],
                ProviderProtocolError,
            ),
            (
                [
                    start,
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "thinking", "thinking": "x"},
                        },
                    ),
                    (
                        "content_block_stop",
                        {"type": "content_block_stop", "index": 0},
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ],
                ProviderProtocolError,
            ),
            ([start], ProviderIncompleteError),
            (
                [
                    (
                        "error",
                        {
                            "type": "error",
                            "error": {"type": "overloaded_error"},
                        },
                    )
                ],
                ProviderResponseError,
            ),
        )
        for events, error_type in cases:
            with self.subTest(error=error_type.__name__):
                self.server.queue_sse(events)
                with self.assertRaises(error_type):
                    await self.anthropic(stream=True).complete(
                        (Message("user", "stream"),), ()
                    )

    async def test_anthropic_stream_invalid_complete_tools_never_execute(self) -> None:
        called = 0

        def guarded(city: str) -> str:
            nonlocal called
            called += 1
            return city

        tool = Tool("guarded", guarded, effect="read_only")
        start = (
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

        def tool_block(index: int, call_id: str, partial_json: str):
            return [
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
            ]

        ending = [
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        cases = (
            ("missing-stop", [start, *tool_block(0, "only", '{"city":"x"}'), ending[0]]),
            (
                "parallel",
                [
                    start,
                    *tool_block(0, "first", '{"city":"x"}'),
                    *tool_block(1, "second", '{"city":"y"}'),
                    *ending,
                ],
            ),
            (
                "duplicate-id",
                [
                    start,
                    *tool_block(0, "same", '{"city":"x"}'),
                    *tool_block(1, "same", '{"city":"y"}'),
                    *ending,
                ],
            ),
            (
                "duplicate-key",
                [start, *tool_block(0, "duplicate", '{"city":"x","city":"y"}'), *ending],
            ),
            ("non-object", [start, *tool_block(0, "array", "[]"), *ending]),
            ("schema", [start, *tool_block(0, "typed", '{"city":7}'), *ending]),
        )
        for name, events in cases:
            self.server.queue_sse(events)
            run_id = f"bad-anthropic-{name}"
            harness = self._harness(self.anthropic(stream=True), (tool,))
            with self.subTest(name=name), self.assertRaises(ModelCallError):
                await harness.run((Message("user", "call"),), run_id=run_id)
            event_types = [item.event.type for item in harness.stored_events(run_id)]
            self.assertNotIn("model.completed", event_types)
            self.assertFalse(any(item.startswith("tool.") for item in event_types))
            self.assertEqual(event_types[-2:], ["model.failed", "run.failed"])
        self.assertEqual(called, 0)

    async def test_sse_framing_and_limits_fail_closed(self) -> None:
        terminal = {
            "type": "response.completed",
            "sequence_number": 0,
            "response": openai_text("multi-line data"),
        }
        encoded = json.dumps(terminal, separators=(",", ":")).encode()
        split = encoded.index(b',"response"') + 1
        raw_terminal = (
            b"event: response.completed\n"
            + b"data: "
            + encoded[:split]
            + b"\n"
            + b"data: "
            + encoded[split:]
            + b"\n\n"
        )
        self.server.queue_sse(
            [],
            raw=raw_terminal,
            chunks=[raw_terminal, b": ignored after terminal\n\n"],
            chunk_delay=0.5,
        )
        reply = await asyncio.wait_for(
            self.openai(stream=True).complete((Message("user", "multi data"),), ()),
            0.3,
        )
        self.assertEqual(reply.content, "multi-line data")

        malformed = (
            b"event: response.completed\nevent: response.completed\ndata: {}\n\n",
            b"id: hidden\nevent: response.completed\ndata: {}\n\n",
            b"event: response.completed\ndata: {\"type\":\"response.completed\",\"type\":\"x\"}\n\n",
            b"event: response.completed\ndata: \xff\n\n",
            b"event: response.completed\ndata: {\"type\":\"response.completed\",\"value\":NaN}\n\n",
            b"event: response.completed\ndata: {\"type\":\"response.completed\",\"value\":\"\\ud800\"}\n\n",
        )
        for raw in malformed:
            with self.subTest(raw=raw[:20]):
                self.server.queue_sse([], raw=raw)
                with self.assertRaises(ProviderProtocolError):
                    await self.openai(stream=True).complete(
                        (Message("user", "bad framing"),), ()
                    )

        self.server.queue_sse(
            [],
            raw=(b":" + b"x" * 128 + b"\n\n"),
        )
        with self.assertRaises(ProviderProtocolError):
            await self.openai(stream=True, max_response_bytes=64).complete(
                (Message("user", "oversize"),), ()
            )

        self.server.queue(openai_text("wrong content type"))
        with self.assertRaises(ProviderProtocolError):
            await self.openai(stream=True).complete(
                (Message("user", "content type"),), ()
            )
        self.server.queue_sse(
            [],
            raw=raw_terminal,
            headers={
                "Content-Length": str(len(raw_terminal)),
                "Transfer-Encoding": "chunked",
            },
        )
        with self.assertRaises(ProviderProtocolError):
            await self.openai(stream=True).complete(
                (Message("user", "framing"),), ()
            )

    async def test_sse_timeout_and_cancellation_propagate(self) -> None:
        raw = (
            b"event: response.created\n"
            b'data: {"type":"response.created","sequence_number":0}\n\n'
        )
        self.server.queue_sse(
            [], raw=raw, chunks=[bytes((value,)) for value in raw], chunk_delay=0.02
        )
        with self.assertRaises(ProviderTimeoutError):
            await self.openai(stream=True, timeout=0.05).complete(
                (Message("user", "slow"),), ()
            )

        self.server.headers_sent.clear()
        self.server.response_finished.clear()
        gate = threading.Event()
        worker_finished = threading.Event()
        called = 0

        def guarded(city: str) -> str:
            nonlocal called
            called += 1
            return city

        model = self.openai(stream=True, timeout=2)
        original_post = model._transport._post

        def tracked_post(*args: object, **kwargs: object):
            try:
                return original_post(*args, **kwargs)
            finally:
                worker_finished.set()

        self.server.queue(
            raw=b"event: response.created\n",
            headers={"Content-Type": "text/event-stream"},
            content_length=False,
            body_gate=gate,
        )
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()
        harness = self._harness(
            model,
            (Tool("guarded", guarded, effect="read_only"),),
        )
        loop.set_exception_handler(
            lambda current_loop, context: loop_errors.append(context)
        )
        try:
            with mock.patch.object(model._transport, "_post", side_effect=tracked_post):
                task = asyncio.create_task(
                    harness.run(
                        (Message("user", "cancel"),), run_id="cancel-sse"
                    )
                )
                self.assertTrue(
                    await asyncio.to_thread(self.server.headers_sent.wait, 1)
                )
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(
                    await asyncio.to_thread(worker_finished.wait, 1)
                )
        finally:
            gate.set()
            loop.set_exception_handler(previous_handler)
        self.assertTrue(
            await asyncio.to_thread(self.server.response_finished.wait, 1)
        )
        await asyncio.sleep(0)
        projected = tuple(
            event_projection(item) for item in harness.stored_events("cancel-sse")
        )
        event_types = [item["event"]["type"] for item in projected]
        self.assertEqual(
            event_types, ["run.started", "model.started", "run.cancelled"]
        )
        self.assertEqual(harness.store.load("cancel-sse").status, "cancelled")
        self.assertEqual(called, 0)
        self.assertEqual(loop_errors, [])


if __name__ == "__main__":
    unittest.main()
