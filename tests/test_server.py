import asyncio
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    ConcurrentRunError,
    Harness,
    Message,
    ModelReply,
    SQLiteStore,
    Tool,
    ToolCall,
)
from sasori.server import (  # noqa: E402
    ServerConfigurationError,
    ServerShutdownIncomplete,
    ServerShuttingDown,
    _Owner,
    create_server,
)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "server.sqlite3")
        self.module = types.ModuleType("sasori_server_test_app")
        sys.modules[self.module.__name__] = self.module
        self.servers = []

    def tearDown(self):
        failure = None
        for server, thread in reversed(self.servers):
            try:
                server.shutdown()
                server.server_close()
                thread.join(5)
            except BaseException as exc:
                failure = failure or exc
        try:
            sys.modules.pop(self.module.__name__, None)
            self.temp.cleanup()
        except BaseException as exc:
            failure = failure or exc
        if failure is not None:
            raise failure

    def start(self, *, token=None, app=None, cors_origins=()):
        if app is not None:
            self.module.create = app
        server = create_server(
            "127.0.0.1",
            0,
            database=self.db,
            app="sasori_server_test_app:create",
            token=token,
            trusted_loopback_no_auth=token is None,
            cors_origins=cors_origins,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def raw_request(self, server, request: bytes, *, shutdown_write: bool = False):
        chunks = []
        eof = False
        with socket.create_connection(server.server_address, timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(request)
            if shutdown_write:
                connection.shutdown(socket.SHUT_WR)
            while True:
                try:
                    chunk = connection.recv(64 * 1024)
                except TimeoutError:
                    break
                if not chunk:
                    eof = True
                    break
                chunks.append(chunk)
        return b"".join(chunks), eof

    def request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        if response_headers.get("Content-Type", "").startswith("application/json"):
            return status, json.loads(payload), response_headers
        return status, payload.decode("utf-8"), response_headers

    def test_http_approval_resume_status_events_and_sse_use_durable_projection(self):
        class Model:
            async def complete(self, messages, tools):
                if messages[-1].role == "tool":
                    return ModelReply(content="written")
                if messages[-1].content == "write":
                    return ModelReply(
                        tool_calls=(ToolCall("write-1", "write", {"value": 7}),)
                    )
                return ModelReply(content="plain")

        server = self.start(
            app=lambda store: Harness(
                Model(),
                (Tool("write", lambda value: value, tool_revision="1"),),
                store=store,
            )
        )
        status, ready, _ = self.request(server, "GET", "/readyz")
        self.assertEqual((status, ready["status"]), (200, "ready"))

        status, paused, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "http-1", "input": "write"},
        )
        self.assertEqual((status, paused["state"]), (202, "paused"))
        fingerprint = paused["pending"]["fingerprint"]
        status, decided, _ = self.request(
            server,
            "POST",
            "/v1/runs/http-1/approval",
            {"fingerprint": fingerprint, "approved": True},
        )
        self.assertEqual((status, decided["detail"]), (200, "awaiting_resume"))
        status, completed, _ = self.request(
            server, "POST", "/v1/runs/http-1/resume", {}
        )
        self.assertEqual((status, completed["state"]), (200, "completed"))
        self.assertEqual(completed["final_message"]["content"], "written")

        status, events, _ = self.request(
            server, "GET", "/v1/runs/http-1/events?after_seq=0"
        )
        self.assertEqual(status, 200)
        sequences = [item["seq"] for item in events["events"]]
        self.assertEqual(sequences, list(range(1, events["latest_seq"] + 1)))

        status, stream, headers = self.request(
            server,
            "GET",
            "/v1/runs/http-1/events?after_seq=1",
            headers={"Accept": "text/event-stream"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        streamed = [int(line[4:]) for line in stream.splitlines() if line.startswith("id: ")]
        self.assertEqual(streamed, sequences[1:])

    def test_multi_app_binding_catalog_and_history_are_durable(self):
        class NamedModel:
            def __init__(self, name):
                self.name = name

            async def complete(self, messages, tools):
                return ModelReply(content=f"{self.name}:{messages[-1].content}")

        self.module.incident = lambda store: Harness(
            NamedModel("incident"), store=store
        )
        self.module.research = lambda store: Harness(
            NamedModel("research"), store=store
        )
        server = create_server(
            "127.0.0.1",
            0,
            database=self.db,
            apps={
                "incident": "sasori_server_test_app:incident",
                "research": "sasori_server_test_app:research",
            },
            trusted_loopback_no_auth=True,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))

        status, catalog, _ = self.request(server, "GET", "/v1/apps")
        self.assertEqual((status, catalog["schema_version"]), (200, 1))
        by_id = {item["id"]: item for item in catalog["apps"]}
        self.assertEqual(by_id["incident"]["availability"]["status"], "ready")
        self.assertEqual(by_id["research"]["availability"]["status"], "ready")
        self.assertEqual(by_id["developer"]["availability"]["reason_code"], "not_enabled")
        self.assertNotIn("system_prompt", json.dumps(catalog))

        status, error, _ = self.request(
            server, "POST", "/v1/runs", {"run_id": "missing-app", "input": "x"}
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))
        status, error, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "unknown-app", "app_id": "developer", "input": "x"},
        )
        self.assertEqual((status, error["error"]["code"]), (404, "app_not_found"))

        for run_id, app_id in (("multi-1", "incident"), ("multi-2", "research")):
            status, value, _ = self.request(
                server,
                "POST",
                "/v1/runs",
                {"run_id": run_id, "app_id": app_id, "input": run_id},
            )
            self.assertEqual(status, 200)
            self.assertEqual(value["app_id"], app_id)
            self.assertEqual(value["input"], run_id)
            self.assertEqual(value["final_message"]["content"], f"{app_id}:{run_id}")

        status, first, _ = self.request(server, "GET", "/v1/runs?limit=1")
        self.assertEqual((status, first["items"][0]["run_id"]), (200, "multi-2"))
        self.assertIsNotNone(first["next_before"])
        status, second, _ = self.request(
            server, "GET", f"/v1/runs?limit=1&before={first['next_before']}"
        )
        self.assertEqual((status, second["items"][0]["run_id"]), (200, "multi-1"))
        status, filtered, _ = self.request(
            server, "GET", "/v1/runs?app_id=incident"
        )
        self.assertEqual([item["run_id"] for item in filtered["items"]], ["multi-1"])
        self.assertNotIn("arguments", json.dumps(filtered))

        server.shutdown()
        server.server_close()
        thread.join(5)
        self.servers.remove((server, thread))

        legacy_store = SQLiteStore(self.db)
        legacy = Harness(NamedModel("legacy"), store=legacy_store)
        asyncio.run(
            legacy.run(
                (Message("user", "legacy"),), run_id="legacy-unbound"
            )
        )
        legacy_store.close()

        restarted = create_server(
            "127.0.0.1",
            0,
            database=self.db,
            apps={
                "incident": "sasori_server_test_app:incident",
                "research": "sasori_server_test_app:research",
            },
            trusted_loopback_no_auth=True,
        )
        restarted_thread = threading.Thread(
            target=restarted.serve_forever, daemon=True
        )
        restarted_thread.start()
        self.servers.append((restarted, restarted_thread))
        status, durable, _ = self.request(restarted, "GET", "/v1/runs/multi-2")
        self.assertEqual((status, durable["app_id"]), (200, "research"))
        status, error, _ = self.request(
            restarted, "POST", "/v1/runs/legacy-unbound/resume", {}
        )
        self.assertEqual(
            (status, error["error"]["code"]), (409, "app_binding_missing")
        )

    def test_auth_cursor_and_request_boundary_fail_closed(self):
        server = self.start(
            token="test-token",
            app=lambda store: Harness(
                type(
                    "Model",
                    (),
                    {"complete": lambda self, messages, tools: None},
                )(),
                store=store,
            ),
        )
        status, error, headers = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "never", "input": "secret"},
        )
        self.assertEqual((status, error["error"]["code"]), (401, "unauthorized"))
        self.assertIn("Bearer", headers["WWW-Authenticate"])
        auth = {"Authorization": "Bearer test-token"}
        status, error, _ = self.request(
            server,
            "GET",
            "/v1/runs/missing/events?after_seq=-1",
            headers=auth,
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))
        status, error, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"input": "x", "unknown": True},
            headers=auth,
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))

        status, error, _ = self.request(
            server,
            "GET",
            "/v1/runs/bad%20id",
            headers=auth,
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))
        status, error, _ = self.request(
            server,
            "GET",
            "/v1/runs/missing/events?after_seq=1",
            headers={**auth, "Last-Event-ID": "2"},
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))

    def test_keep_alive_resets_response_state_for_each_request(self):
        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(app=lambda store: Harness(FinalModel(), store=store))
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request("GET", "/healthz")
            first = connection.getresponse()
            self.assertEqual(first.status, 200)
            first.read()
            active_socket = connection.sock
            self.assertIsNotNone(active_socket)

            connection.request("GET", "/v1/runs/bad%20id")
            second = connection.getresponse()
            error = json.loads(second.read())
            self.assertIs(connection.sock, active_socket)
            self.assertEqual(
                (second.status, error["error"]["code"]),
                (422, "invalid_request"),
            )
        finally:
            connection.close()

    def test_workbench_assets_are_exact_and_security_hardened(self):
        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(app=lambda store: Harness(FinalModel(), store=store))
        status, page, headers = self.request(server, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("SASORI", page)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn('id="settings-button" type="button" aria-label=', page)
        self.assertIn('id="connection-signal" data-state="idle" role="status"', page)
        self.assertIn('id="surface-tab" role="tab"', page)
        self.assertIn('tabindex="-1"', page)
        self.assertIn('data-mobile-view="stage" class="active" aria-pressed="true"', page)

        assets = {}
        for path, content_type in (
            ("/assets/app.0.1.0.css", "text/css"),
            ("/assets/app.0.1.1.js", "text/javascript"),
            ("/assets/mark.0.1.0.svg", "image/svg+xml"),
        ):
            status, body, asset_headers = self.request(server, "GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(asset_headers["Content-Type"].startswith(content_type))
            self.assertIn("immutable", asset_headers["Cache-Control"])
            self.assertTrue(body)
            assets[path] = body

        script = assets["/assets/app.0.1.1.js"]
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn('button.setAttribute("aria-pressed", String(active))', script)
        self.assertEqual(script.count('$$(".mobile-nav [data-mobile-view]")'), 2)
        self.assertNotIn('$$("[data-mobile-view]")', script)
        stylesheet = assets["/assets/app.0.1.0.css"]
        self.assertNotIn(".signal b { display: none; }", stylesheet)

        status, error, _ = self.request(server, "GET", "/assets/../README.md")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/app.0.1.1.js?v=1")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/app.0.1.0.js")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))

        origin = f"http://127.0.0.1:{server.server_port}"
        status, value, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "same-origin", "input": "browser"},
            headers={"Origin": origin},
        )
        self.assertEqual((status, value["state"]), (200, "completed"))

    def test_explicit_conflict_and_transition_taxonomy(self):
        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(
            app=lambda store: Harness(FinalModel(), store=store)
        )
        body = {"run_id": "taxonomy", "input": "finish"}
        status, _, _ = self.request(server, "POST", "/v1/runs", body)
        self.assertEqual(status, 200)
        status, error, _ = self.request(server, "POST", "/v1/runs", body)
        self.assertEqual(
            (status, error["error"]["code"]), (409, "run_already_exists")
        )
        status, error, _ = self.request(
            server, "POST", "/v1/runs/taxonomy/resume", {}
        )
        self.assertEqual(
            (status, error["error"]["code"]), (409, "invalid_transition")
        )

    def test_raw_http_rejections_close_without_parsing_a_second_request(self):
        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(
            token="test-token",
            cors_origins=("https://allowed.example",),
            app=lambda store: Harness(FinalModel(), store=store),
        )
        host = f"Host: 127.0.0.1:{server.server_port}\r\n"
        auth = "Authorization: Bearer test-token\r\n"
        body = b'{"input":"secret"}'
        unauthorized = (
            f"POST /v1/runs HTTP/1.1\r\n{host}"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body + (
            f"GET /healthz HTTP/1.1\r\n{host}\r\n"
        ).encode("ascii")
        forbidden = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            f"Origin: https://forbidden.example\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        duplicate_length = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}"
        ).encode("ascii")
        duplicate_authorization = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}{auth}"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        ).encode("ascii")
        duplicate_origin = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Origin: https://allowed.example\r\n"
            "Origin: https://allowed.example\r\n"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        ).encode("ascii")
        transfer_encoding = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
        ).encode("ascii")
        oversized = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Content-Type: application/json\r\nContent-Length: 1048577\r\n\r\n"
        ).encode("ascii")
        malformed_url = (
            f"GET http://[::1 HTTP/1.1\r\n{host}\r\n"
        ).encode("ascii")
        absolute_protected = (
            f"GET http://example.invalid/v1/runs/missing HTTP/1.1\r\n{host}\r\n"
        ).encode("ascii")
        absolute_preflight = (
            f"OPTIONS http://example.invalid/v1/runs HTTP/1.1\r\n{host}"
            "Origin: https://allowed.example\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        cases = (
            (unauthorized, 401, b'"code":"unauthorized"'),
            (absolute_protected, 401, b'"code":"unauthorized"'),
            (forbidden, 403, b'"code":"origin_forbidden"'),
            (duplicate_length, 400, b'"code":"invalid_length"'),
            (
                duplicate_authorization,
                400,
                b'"code":"invalid_header"',
            ),
            (duplicate_origin, 400, b'"code":"invalid_header"'),
            (transfer_encoding, 411, b'"code":"length_required"'),
            (oversized, 413, b'"code":"body_too_large"'),
            (malformed_url, 422, b'"code":"invalid_request"'),
        )
        for request, status, code in cases:
            with self.subTest(code=code):
                response, eof = self.raw_request(server, request)
                self.assertTrue(eof)
                self.assertEqual(response.count(b"HTTP/1.1"), 1)
                self.assertTrue(
                    response.startswith(f"HTTP/1.1 {status} ".encode("ascii"))
                )
                self.assertIn(code, response)
                self.assertIn(b"Connection: close", response)

        response, eof = self.raw_request(server, absolute_preflight)
        self.assertTrue(eof)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertTrue(response.startswith(b"HTTP/1.1 204 "))
        self.assertIn(b"Access-Control-Allow-Origin: https://allowed.example", response)

        incomplete = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Content-Type: application/json\r\nContent-Length: 10\r\n\r\n{}"
        ).encode("ascii")
        response, eof = self.raw_request(server, incomplete, shutdown_write=True)
        self.assertTrue(eof)
        self.assertIn(b'"code":"incomplete_body"', response)

        server.request_timeout_seconds = 0.1
        timed_out = (
            f"POST /v1/runs HTTP/1.1\r\n{host}{auth}"
            "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n"
        ).encode("ascii")
        response, eof = self.raw_request(server, timed_out)
        self.assertTrue(eof)
        self.assertIn(b"408 Request Timeout", response)
        self.assertIn(b'"code":"request_timeout"', response)

    def test_second_drive_is_busy_not_queued(self):
        started = threading.Event()
        release = threading.Event()

        class SlowModel:
            async def complete(self, messages, tools):
                started.set()
                await __import__("asyncio").to_thread(release.wait)
                return ModelReply(content="done")

        server = self.start(app=lambda store: Harness(SlowModel(), store=store))
        first = {}

        def run_first():
            first["response"] = self.request(
                server,
                "POST",
                "/v1/runs",
                {"run_id": "slow-1", "input": "wait"},
            )

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(started.wait(2))
        status, error, headers = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "slow-2", "input": "wait"},
        )
        self.assertEqual((status, error["error"]["code"]), (503, "runtime_busy"))
        self.assertEqual(headers["Retry-After"], "1")
        release.set()
        thread.join(5)
        self.assertEqual(first["response"][0], 200)

    def test_factory_failure_and_start_timeout_release_owner_resources(self):
        def failed_factory(store):
            raise RuntimeError("factory failed")

        self.module.create = failed_factory
        with self.assertRaisesRegex(ServerConfigurationError, "factory failed"):
            create_server(
                "127.0.0.1",
                0,
                database=self.db,
                app="sasori_server_test_app:create",
                trusted_loopback_no_auth=True,
            )
        with SQLiteStore(self.db) as reopened:
            self.assertFalse(reopened.closed)

        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(
            app=lambda store: Harness(FinalModel(), store=store)
        )
        self.assertEqual(self.request(server, "GET", "/readyz")[0], 200)
        server.shutdown()
        server.server_close()

        release = threading.Event()

        def slow_factory(store):
            release.wait(2)
            return Harness(FinalModel(), store=store)

        self.module.create = slow_factory
        owner = _Owner(self.db, "sasori_server_test_app:create")
        try:
            with self.assertRaisesRegex(
                ServerConfigurationError, "runtime owner did not start"
            ):
                owner.start(0.05)
        finally:
            release.set()
        owner._thread.join(2)
        self.assertEqual(owner.state, "CLOSED")
        self.assertFalse(owner._thread.is_alive())
        with SQLiteStore(self.db):
            pass

    def test_shutdown_cancels_active_drive_and_durably_settles_callers(self):
        started = threading.Event()
        cancelled = threading.Event()

        class SlowModel:
            async def complete(self, messages, tools):
                started.set()
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        server = self.start(app=lambda store: Harness(SlowModel(), store=store))
        outcome = {}

        def request():
            outcome["response"] = self.request(
                server,
                "POST",
                "/v1/runs",
                {"run_id": "shutdown-run", "input": "wait"},
            )

        caller = threading.Thread(target=request)
        caller.start()
        self.assertTrue(started.wait(2))
        server.shutdown()
        original_close = server.owner.close
        with mock.patch.object(
            server.owner,
            "close",
            side_effect=lambda: original_close(0.05, 2),
        ):
            server.server_close()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            (outcome["response"][0], outcome["response"][1]["error"]["code"]),
            (503, "server_shutting_down"),
        )
        with SQLiteStore(self.db) as store:
            self.assertEqual(store.load("shutdown-run").status, "cancelled")
            self.assertIn(
                "run.cancelled",
                [event.type for event in store.events("shutdown-run")],
            )
        with server._handlers:
            self.assertEqual(server._active_handlers, 0)

    def test_shutdown_during_side_effect_records_unknown_before_store_close(self):
        effect_started = threading.Event()
        release = threading.Event()

        def write(value):
            effect_started.set()
            release.wait(2)
            return value

        class WriteModel:
            async def complete(self, messages, tools):
                if messages[-1].role == "tool":
                    return ModelReply(content="written")
                return ModelReply(
                    tool_calls=(ToolCall("write-1", "write", {"value": 7}),)
                )

        server = self.start(
            app=lambda store: Harness(
                WriteModel(),
                (Tool("write", write, tool_revision="1"),),
                store=store,
            )
        )
        try:
            status, paused, _ = self.request(
                server,
                "POST",
                "/v1/runs",
                {"run_id": "shutdown-effect", "input": "write"},
            )
            self.assertEqual(status, 202)
            self.request(
                server,
                "POST",
                "/v1/runs/shutdown-effect/approval",
                {
                    "fingerprint": paused["pending"]["fingerprint"],
                    "approved": True,
                },
            )
            outcome = {}

            def resume():
                outcome["response"] = self.request(
                    server, "POST", "/v1/runs/shutdown-effect/resume", {}
                )

            caller = threading.Thread(target=resume)
            caller.start()
            self.assertTrue(effect_started.wait(2))
            server.shutdown()
            original_close = server.owner.close
            with mock.patch.object(
                server.owner,
                "close",
                side_effect=lambda: original_close(0.01, 2),
            ):
                server.server_close()
            caller.join(2)
            self.assertFalse(caller.is_alive())
            self.assertEqual(outcome["response"][0], 503)
            with SQLiteStore(self.db) as store:
                snapshot = store.load("shutdown-effect")
                self.assertEqual(snapshot.status, "cancelled")
                calls = store.calls("shutdown-effect", snapshot.step)
                self.assertEqual(calls[0].status, "effect_unknown")
                failed = [
                    event
                    for event in store.events("shutdown-effect")
                    if event.type == "tool.failed"
                ]
                self.assertTrue(failed[-1].data["effect_unknown"])
        finally:
            release.set()

    def test_cancellation_swallowing_hits_hard_deadline_without_false_close(self):
        started = threading.Event()
        swallowed = threading.Event()
        release = threading.Event()

        class DefiantModel:
            async def complete(self, messages, tools):
                started.set()
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        swallowed.set()
                return ModelReply(content="late")

        self.module.create = lambda store: Harness(DefiantModel(), store=store)
        owner = _Owner(self.db, "sasori_server_test_app:create")
        owner.start()
        outcome = []

        def call():
            try:
                owner.call(owner.run("wait", "defiant"))
            except Exception as exc:
                outcome.append(exc)

        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(started.wait(2))
        try:
            with self.assertRaisesRegex(
                ServerShutdownIncomplete, "hard deadline"
            ):
                owner.close(0.01, 0.15)
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertIsInstance(outcome[0], ServerShuttingDown)
            self.assertTrue(swallowed.is_set())
            self.assertEqual(owner.state, "CLOSING")
            self.assertTrue(owner._thread.is_alive())
            with self.assertRaises(ConcurrentRunError):
                SQLiteStore(self.db)
        finally:
            release.set()
        owner._thread.join(2)
        self.assertEqual(owner.state, "CLOSED")
        with SQLiteStore(self.db):
            pass

    def test_active_sse_ends_once_and_handlers_drain_on_shutdown(self):
        class PauseModel:
            async def complete(self, messages, tools):
                return ModelReply(tool_calls=(ToolCall("write-1", "write"),))

        server = self.start(
            app=lambda store: Harness(
                PauseModel(),
                (Tool("write", lambda: None, tool_revision="1"),),
                store=store,
            )
        )
        status, _, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "sse-shutdown", "input": "pause"},
        )
        self.assertEqual(status, 202)
        stream = socket.create_connection(server.server_address, timeout=3)
        stream.settimeout(3)
        request = (
            f"GET /v1/runs/sse-shutdown/events HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_port}\r\n"
            "Accept: text/event-stream\r\n\r\n"
        ).encode("ascii")
        try:
            stream.sendall(request)
            response = b""
            while b"retry: 1000\n\n" not in response:
                response += stream.recv(4096)
            server.shutdown()
            server.server_close()
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            stream.close()
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertIn(b"200 OK", response)
        self.assertNotIn(b'"code":"server_shutting_down"', response)
        with server._handlers:
            self.assertEqual(server._active_handlers, 0)

    def test_non_loopback_or_implicit_no_auth_is_rejected(self):
        self.module.create = lambda store: Harness(object(), store=store)
        with self.assertRaises(ServerConfigurationError):
            create_server(
                "127.0.0.1",
                0,
                database=self.db,
                app="sasori_server_test_app:create",
            )


if __name__ == "__main__":
    unittest.main()
