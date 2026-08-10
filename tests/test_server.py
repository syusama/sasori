import asyncio
import concurrent.futures
import http.client
import inspect
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
    WorkflowPreflightRejected,
    _Owner,
    create_server,
)
from sasori_apps.workflow_incident import (  # noqa: E402
    APP_ID as WORKFLOW_INCIDENT_ID,
    APP_METADATA as WORKFLOW_INCIDENT_METADATA,
    WORKFLOW_SPEC as INCIDENT_WORKFLOW_SPEC,
)
from sasori_flow import (  # noqa: E402
    canonical_json,
    InputRef,
    InputSlot,
    ToolStep,
    WorkflowCompileError,
    WorkflowSpec,
    WorkflowValidationError,
    compile_workflow,
    preflight_workflow,
    workflow_app_id,
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

    def request(self, server, method, path, body=None, headers=None, *, timeout=5):
        connection = http.client.HTTPConnection(
            *server.server_address, timeout=timeout
        )
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

    def durable_snapshot(self, server):
        async def collect():
            store = server.owner._store
            self.assertIsNotNone(store)
            return {
                table: tuple(
                    tuple(row)
                    for row in store._db.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                )
                for table in (
                    "runs",
                    "events",
                    "checkpoints",
                    "accepted_replies",
                    "tool_calls",
                    "approvals",
                    "artifacts",
                )
            }

        return server.owner.call(collect(), 2)

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
        self.assertNotIn("workflow", paused)
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
        streamed_data = [
            json.loads(line[6:])
            for line in stream.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(streamed_data, events["events"][1:])

        status, resumed_stream, _ = self.request(
            server,
            "GET",
            "/v1/runs/http-1/events",
            headers={"Accept": "text/event-stream", "Last-Event-ID": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [
                int(line[4:])
                for line in resumed_stream.splitlines()
                if line.startswith("id: ")
            ],
            sequences[1:],
        )

        status, error, _ = self.request(
            server,
            "GET",
            f"/v1/runs/http-1/events?after_seq={events['latest_seq'] + 1}",
        )
        self.assertEqual((status, error["error"]["code"]), (409, "cursor_ahead"))

    def test_http_ignores_legacy_full_projection_override(self):
        class LegacyHarness(Harness):
            def public_run_projection(self, run_id):
                return {
                    "run_id": "forged",
                    "app_id": "forged.app",
                    "state": "completed",
                }

        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(
            app=lambda store: LegacyHarness(FinalModel(), store=store)
        )
        status, projected, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "http-core-owned", "input": "hello"},
        )
        self.assertEqual((status, projected["run_id"]), (200, "http-core-owned"))
        self.assertEqual(projected["state"], "completed")
        self.assertNotIn("workflow", projected)

    def test_http_projection_extension_failure_is_stable_and_redacted(self):
        private = "private transcript and arguments"

        class MalformedHarness(Harness):
            def public_projection_extension(self, run_id):
                raise RuntimeError(private)

        class FinalModel:
            async def complete(self, messages, tools):
                return ModelReply(content="done")

        server = self.start(
            app=lambda store: MalformedHarness(FinalModel(), store=store)
        )
        status, error, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "http-projection-failed", "input": "hello"},
        )
        self.assertEqual(
            (status, error["error"]),
            (
                502,
                {
                    "code": "projection_integrity_failed",
                    "message": "public projection extension failed integrity validation",
                    "retryable": False,
                },
            ),
        )
        self.assertNotIn(private, json.dumps(error))

    def test_active_workflow_status_projection_remains_cursor_coherent(self):
        started = threading.Event()
        release = threading.Event()

        async def slow(value: str) -> str:
            started.set()
            await asyncio.to_thread(release.wait)
            return value

        tool = Tool("slow_read", slow, effect="read_only")
        spec = WorkflowSpec(
            "http-active-projection",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "slow",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "slow",
        )
        app_id = workflow_app_id(spec)

        class UnusedModel:
            async def complete(self, messages, tools):
                raise AssertionError("compiled Workflow must replace the base model")

        self.module.create = lambda store: compile_workflow(
            spec,
            Harness(
                UnusedModel(),
                (tool,),
                store=store,
                model_timeout=5,
                tool_timeout=15,
            ),
        )
        server = create_server(
            "127.0.0.1",
            0,
            database=self.db,
            apps={app_id: "sasori_server_test_app:create"},
            trusted_loopback_no_auth=True,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.1,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.servers.append((server, server_thread))
        drive: dict[str, object] = {}

        def post_run() -> None:
            drive["response"] = self.request(
                server,
                "POST",
                "/v1/runs",
                {
                    "run_id": "HttpActiveWorkflow",
                    "app_id": app_id,
                    "input": "hold",
                },
                timeout=20,
            )

        drive_thread = threading.Thread(target=post_run, daemon=True)
        drive_thread.start()
        try:
            self.assertTrue(
                started.wait(10),
                "Workflow Tool did not enter its await boundary",
            )

            def read_status(_: int):
                return self.request(
                    server, "GET", "/v1/runs/HttpActiveWorkflow"
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(read_status, range(32)))
            cursors = []
            revisions = []
            for status, projected, _ in responses:
                self.assertEqual(status, 200)
                self.assertEqual(projected["state"], "running")
                self.assertEqual(
                    projected["workflow"]["latest_seq"], projected["latest_seq"]
                )
                self.assertEqual(projected["workflow"]["current_step_id"], "slow")
                self.assertEqual(
                    [step["status"] for step in projected["workflow"]["steps"]],
                    ["running"],
                )
                self.assertIsNotNone(
                    projected["workflow"]["steps"][0]["call_id"]
                )
                cursors.append(projected["latest_seq"])
                revisions.append(projected["revision"])
            self.assertTrue(all(cursor == cursors[0] for cursor in cursors))
            self.assertTrue(all(revision == revisions[0] for revision in revisions))
        finally:
            release.set()
            drive_thread.join(20)
        self.assertFalse(drive_thread.is_alive())
        status, completed, _ = drive["response"]
        self.assertEqual((status, completed["state"]), (200, "completed"))
        self.assertEqual(
            completed["workflow"]["latest_seq"], completed["latest_seq"]
        )
        self.assertEqual(
            [step["status"] for step in completed["workflow"]["steps"]],
            ["completed"],
        )

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
        unavailable_workflow = by_id[WORKFLOW_INCIDENT_ID]
        self.assertEqual(
            unavailable_workflow["availability"],
            {"status": "unavailable", "reason_code": "not_enabled"},
        )
        self.assertEqual(unavailable_workflow["tools"], [])
        self.assertEqual(
            unavailable_workflow["workflow"],
            WORKFLOW_INCIDENT_METADATA["workflow"],
        )
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

    def test_workflow_preflight_is_exact_detached_and_zero_execution(self):
        calls = {"model": 0, "tool": 0, "idempotency": 0}

        class UnusedModel:
            async def complete(self, messages, tools):
                calls["model"] += 1
                raise AssertionError("preflight must not execute a model")

        def inspect(summary: str, *, idempotency_key: str) -> str:
            calls["tool"] += 1
            raise AssertionError("preflight must not execute a Tool")

        def business_key(arguments):
            calls["idempotency"] += 1
            raise AssertionError("preflight must not reserve an idempotency key")

        tool = Tool(
            "inspect",
            inspect,
            effect="idempotent",
            tool_revision="contract-1",
            idempotency_key=business_key,
        )
        spec = WorkflowSpec(
            "http-preflight",
            "1",
            (InputSlot("incident", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"summary": InputRef("incident")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            app=lambda store: Harness(UnusedModel(), (tool,), store=store)
        )

        status, before, _ = self.request(server, "GET", "/v1/runs")
        self.assertEqual((status, before["items"]), (200, []))
        status, value, _ = self.request(
            server, "POST", "/v1/workflows/preflight", spec.as_data()
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(value), {"ok", "schema_version", "manifest"})
        self.assertEqual((value["ok"], value["schema_version"]), (True, 1))
        self.assertEqual(value["manifest"], preflight_workflow(spec, (tool,)))
        self.assertEqual(
            value["manifest"]["trust"],
            {"execution_mode": "trusted_installed_python", "sandboxed": False},
        )

        value["manifest"]["workflow_id"] = "client-tampered"
        status, repeated, _ = self.request(
            server, "POST", "/v1/workflows/preflight", spec.as_data()
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated["manifest"], preflight_workflow(spec, (tool,)))
        status, after, _ = self.request(server, "GET", "/v1/runs")
        self.assertEqual((status, after["items"]), (200, []))
        self.assertEqual(calls, {"model": 0, "tool": 0, "idempotency": 0})

    def test_workflow_preflight_preserves_all_durable_tables_and_external_spies(self):
        calls = {"model": 0, "tool": 0, "idempotency": 0, "fault": 0}

        class Model:
            async def complete(self, messages, tools):
                calls["model"] += 1
                return ModelReply(content="seeded")

        def inspect(value: str, *, idempotency_key: str) -> str:
            calls["tool"] += 1
            return value

        def business_key(arguments):
            calls["idempotency"] += 1
            return str(arguments["value"])

        def fault(point):
            calls["fault"] += 1

        tool = Tool(
            "inspect",
            inspect,
            effect="idempotent",
            tool_revision="contract-1",
            idempotency_key=business_key,
        )
        spec = WorkflowSpec(
            "deep-zero-preflight",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            app=lambda store: Harness(
                Model(), (tool,), store=store, fault_injector=fault
            )
        )
        status, seeded, _ = self.request(
            server,
            "POST",
            "/v1/runs",
            {"run_id": "preflight-existing", "input": "seed"},
        )
        self.assertEqual((status, seeded["state"]), (200, "completed"))
        calls.update({"model": 0, "tool": 0, "idempotency": 0, "fault": 0})
        before = self.durable_snapshot(server)
        drifted = spec.as_data()
        drifted["steps"][0]["schema_sha256"] = "0" * 64

        def forbidden(label):
            def fail(*args, **kwargs):
                raise AssertionError(f"preflight invoked forbidden {label}")

            return fail

        with (
            mock.patch("sasori.server.SQLiteStore", side_effect=forbidden("store")),
            mock.patch(
                "sasori.server.ArtifactStore",
                side_effect=forbidden("artifact store"),
            ),
            mock.patch("sasori.server.load_harness", side_effect=forbidden("loader")),
            mock.patch(
                "asyncio.create_subprocess_exec", side_effect=forbidden("process")
            ),
            mock.patch("asyncio.open_connection", side_effect=forbidden("network")),
            mock.patch("socket.create_connection", side_effect=forbidden("network")),
            mock.patch("subprocess.Popen", side_effect=forbidden("process")),
            mock.patch("subprocess.run", side_effect=forbidden("process")),
            mock.patch("urllib.request.urlopen", side_effect=forbidden("network")),
        ):
            accepted = server.owner.call(
                server.owner.workflow_preflight(spec.as_data()), 2
            )
            self.assertTrue(accepted["ok"])
            with self.assertRaises(WorkflowPreflightRejected):
                server.owner.call(server.owner.workflow_preflight(drifted), 2)

        after = self.durable_snapshot(server)
        self.assertEqual(after, before)
        self.assertTrue(before["runs"])
        self.assertTrue(before["events"])
        self.assertTrue(before["checkpoints"])
        self.assertTrue(before["accepted_replies"])
        self.assertEqual(calls, {"model": 0, "tool": 0, "idempotency": 0, "fault": 0})

    def test_workflow_preflight_error_taxonomy_is_exact_and_bounded(self):
        tool = Tool("inspect", lambda value: value, effect="read_only")
        spec = WorkflowSpec(
            "preflight-taxonomy",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            app=lambda store: Harness(object(), (tool,), store=store)
        )
        before = self.durable_snapshot(server)

        cases = (
            (
                WorkflowValidationError("manifest composition failed"),
                "manifest_rejected",
                "manifest composition failed",
            ),
            (
                WorkflowCompileError("x" * 513),
                "tool_contract_mismatch",
                "workflow Tool contract was rejected",
            ),
            (
                WorkflowCompileError("\ud800"),
                "tool_contract_mismatch",
                "workflow Tool contract was rejected",
            ),
        )
        for failure, reason_code, message in cases:
            with self.subTest(failure=type(failure).__name__, message=message):
                with mock.patch(
                    "sasori_flow.preflight_workflow", side_effect=failure
                ):
                    status, error, _ = self.request(
                        server,
                        "POST",
                        "/v1/workflows/preflight",
                        spec.as_data(),
                    )
                self.assertEqual(
                    (
                        status,
                        set(error),
                        set(error["error"]),
                        error["error"]["code"],
                        error["error"]["reason_code"],
                        error["error"]["retryable"],
                        error["error"]["message"],
                    ),
                    (
                        422,
                        {"ok", "error"},
                        {"code", "message", "retryable", "reason_code"},
                        "workflow_preflight_rejected",
                        reason_code,
                        False,
                        message,
                    ),
                )

        self.assertEqual(self.durable_snapshot(server), before)

    def test_workflow_preflight_timeout_is_retryable_and_non_mutating(self):
        tool = Tool("inspect", lambda value: value, effect="read_only")
        spec = WorkflowSpec(
            "preflight-timeout",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            app=lambda store: Harness(object(), (tool,), store=store)
        )
        before = self.durable_snapshot(server)
        original = server.owner.workflow_preflight
        cancelled = threading.Event()

        async def slow_preflight(definition):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        server.owner.workflow_preflight = slow_preflight
        started = time.monotonic()
        try:
            status, error, headers = self.request(
                server,
                "POST",
                "/v1/workflows/preflight",
                spec.as_data(),
                timeout=8,
            )
        finally:
            server.owner.workflow_preflight = original
        elapsed = time.monotonic() - started
        self.assertEqual(
            (
                status,
                error["error"]["code"],
                error["error"]["retryable"],
                headers["Retry-After"],
            ),
            (503, "runtime_busy", True, "1"),
        )
        self.assertGreaterEqual(elapsed, 4.5)
        self.assertLess(elapsed, 7.5)
        self.assertTrue(cancelled.wait(2), "timed-out owner operation was not cancelled")
        self.assertEqual(self.durable_snapshot(server), before)
        status, accepted, _ = self.request(
            server, "POST", "/v1/workflows/preflight", spec.as_data()
        )
        self.assertEqual((status, accepted["ok"]), (200, True))

    def test_workflow_preflight_rejects_strict_json_and_contract_drift(self):
        handler_calls = 0

        def inspect(summary: str) -> str:
            nonlocal handler_calls
            handler_calls += 1
            raise AssertionError("rejected preflight must not execute a Tool")

        tool = Tool("inspect", inspect, effect="read_only")
        spec = WorkflowSpec(
            "http-preflight-errors",
            "1",
            (InputSlot("incident", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"summary": InputRef("incident")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            app=lambda store: Harness(object(), (tool,), store=store)
        )

        invalid = spec.as_data()
        invalid["python_entrypoint"] = "unsafe.module:factory"
        status, error, _ = self.request(
            server, "POST", "/v1/workflows/preflight", invalid
        )
        self.assertEqual(
            (
                status,
                error["error"]["code"],
                error["error"]["reason_code"],
                error["error"]["retryable"],
            ),
            (422, "workflow_preflight_rejected", "invalid_definition", False),
        )
        self.assertNotIn("unsafe.module:factory", error["error"]["message"])

        drifted = spec.as_data()
        drifted["steps"][0]["schema_sha256"] = "0" * 64
        status, error, _ = self.request(
            server, "POST", "/v1/workflows/preflight", drifted
        )
        self.assertEqual(
            (
                status,
                error["error"]["code"],
                error["error"]["reason_code"],
            ),
            (
                422,
                "workflow_preflight_rejected",
                "tool_contract_mismatch",
            ),
        )
        self.assertLessEqual(len(error["error"]["message"].encode("utf-8")), 512)

        document = canonical_json(spec.as_data()).replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
            1,
        ).encode("utf-8")
        raw = (
            b"POST /v1/workflows/preflight HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{server.server_port}\r\n".encode("ascii")
            + b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(document)}\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
            + document
        )
        response, eof = self.raw_request(server, raw)
        self.assertTrue(eof)
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertIn(b'"code":"malformed_json"', response)

        status, error, _ = self.request(
            server, "POST", "/v1/workflows/preflight?mode=unsafe", spec.as_data()
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))
        status, error, _ = self.request(
            server, "GET", "/v1/workflows/preflight"
        )
        self.assertEqual(
            (status, error["error"]["code"]), (405, "method_not_allowed")
        )
        self.assertEqual(handler_calls, 0)

    def test_workflow_preflight_maps_repeated_signature_failure_to_contract_rejection(self):
        calls = {"handler": 0, "idempotency": 0}

        def stable(value: str, *, idempotency_key: str) -> str:
            return value

        def business_key(arguments):
            calls["idempotency"] += 1
            raise AssertionError("rejected preflight must not reserve a key")

        stable_tool = Tool(
            "changing",
            stable,
            effect="idempotent",
            idempotency_key=business_key,
            tool_revision="changing-v1",
        )
        spec = WorkflowSpec(
            "changing-signature",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "changing",
                    stable_tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "changing",
        )
        signature_reads = 0

        class ChangingSignature(type):
            @property
            def __signature__(cls):
                nonlocal signature_reads
                signature_reads += 1
                if signature_reads <= 2:
                    return inspect.signature(stable)
                raise ValueError("signature changed")

        class ChangingHandler(metaclass=ChangingSignature):
            def __new__(cls, value: str, *, idempotency_key: str) -> str:
                calls["handler"] += 1
                raise AssertionError("rejected preflight must not execute a Tool")

        changing_tool = Tool(
            "changing",
            ChangingHandler,
            effect="idempotent",
            idempotency_key=business_key,
            tool_revision="changing-v1",
        )
        server = self.start(
            app=lambda store: Harness(object(), (changing_tool,), store=store)
        )
        before = self.durable_snapshot(server)

        status, error, _ = self.request(
            server, "POST", "/v1/workflows/preflight", spec.as_data()
        )

        self.assertEqual(
            (
                status,
                error["error"]["code"],
                error["error"]["reason_code"],
                error["error"]["retryable"],
            ),
            (422, "workflow_preflight_rejected", "tool_contract_mismatch", False),
        )
        self.assertIn("tool schema cannot be inspected", error["error"]["message"])
        self.assertEqual(signature_reads, 3)
        self.assertEqual(calls, {"handler": 0, "idempotency": 0})
        self.assertEqual(self.durable_snapshot(server), before)

    def test_workflow_preflight_registry_excludes_ambiguous_and_wrapper_tools(self):
        class UnusedModel:
            async def complete(self, messages, tools):
                raise AssertionError("preflight must not execute a model")

        first_duplicate = Tool("duplicate", lambda value: value, effect="read_only")
        second_duplicate = Tool("duplicate", lambda value: value, effect="read_only")
        unique = Tool("unique", lambda value: value, effect="read_only")
        self.module.first = lambda store: Harness(
            UnusedModel(), (first_duplicate, unique), store=store
        )
        self.module.second = lambda store: Harness(
            UnusedModel(), (second_duplicate,), store=store
        )
        server = create_server(
            "127.0.0.1",
            0,
            database=self.db,
            apps={
                "incident": "sasori_server_test_app:first",
                "research": "sasori_server_test_app:second",
            },
            trusted_loopback_no_auth=True,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))

        def definition(workflow_id, tool):
            return WorkflowSpec(
                workflow_id,
                "1",
                (InputSlot("value", "string"),),
                (
                    ToolStep.from_tool(
                        "step",
                        tool,
                        {"value": InputRef("value")},
                        result_type="string",
                    ),
                ),
                "step",
            ).as_data()

        status, accepted, _ = self.request(
            server,
            "POST",
            "/v1/workflows/preflight",
            definition("unique-tool", unique),
        )
        self.assertEqual((status, accepted["ok"]), (200, True))
        status, rejected, _ = self.request(
            server,
            "POST",
            "/v1/workflows/preflight",
            definition("ambiguous-tool", first_duplicate),
        )
        self.assertEqual(
            (
                status,
                rejected["error"]["code"],
                rejected["error"]["reason_code"],
            ),
            (
                422,
                "workflow_preflight_rejected",
                "tool_contract_mismatch",
            ),
        )
        self.assertIn("unknown tool duplicate", rejected["error"]["message"])

        wrapper_spec = WorkflowSpec(
            "wrapper-only",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "step",
                    unique,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "step",
        )
        self.module.wrapper = lambda store: compile_workflow(
            wrapper_spec,
            Harness(UnusedModel(), (unique,), store=store),
        )
        wrapper_server = create_server(
            "127.0.0.1",
            0,
            database=str(Path(self.temp.name) / "wrapper.sqlite3"),
            app="sasori_server_test_app:wrapper",
            trusted_loopback_no_auth=True,
        )
        wrapper_thread = threading.Thread(
            target=wrapper_server.serve_forever, daemon=True
        )
        wrapper_thread.start()
        self.servers.append((wrapper_server, wrapper_thread))
        status, wrapper_rejected, _ = self.request(
            wrapper_server,
            "POST",
            "/v1/workflows/preflight",
            wrapper_spec.as_data(),
        )
        self.assertEqual(
            (
                status,
                wrapper_rejected["error"]["reason_code"],
            ),
            (422, "tool_contract_mismatch"),
        )
        self.assertIn("unknown tool unique", wrapper_rejected["error"]["message"])

    def test_workflow_preflight_auth_and_origin_fail_before_preflight(self):
        tool = Tool("inspect", lambda value: value, effect="read_only")
        spec = WorkflowSpec(
            "authorized-preflight",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        server = self.start(
            token="studio-secret",
            cors_origins=("https://studio.example",),
            app=lambda store: Harness(object(), (tool,), store=store),
        )
        calls = 0
        original = server.owner.workflow_preflight

        async def counted(definition):
            nonlocal calls
            calls += 1
            return await original(definition)

        server.owner.workflow_preflight = counted
        for headers, expected in (
            ({}, 401),
            ({"Authorization": "Bearer wrong"}, 401),
            (
                {
                    "Authorization": "Bearer studio-secret",
                    "Origin": "https://evil.example",
                },
                403,
            ),
        ):
            with self.subTest(headers=headers):
                status, _, _ = self.request(
                    server,
                    "POST",
                    "/v1/workflows/preflight",
                    spec.as_data(),
                    headers=headers,
                )
                self.assertEqual(status, expected)
                self.assertEqual(calls, 0)

        document = canonical_json(spec.as_data()).encode("utf-8")
        repeated_headers = (
            (
                b"Authorization: Bearer studio-secret\r\n"
                b"Authorization: Bearer studio-secret\r\n",
                b'"code":"invalid_header"',
            ),
            (
                b"Authorization: Bearer studio-secret\r\n"
                b"Origin: https://studio.example\r\n"
                b"Origin: https://studio.example\r\n",
                b'"code":"invalid_header"',
            ),
        )
        for repeated, expected_code in repeated_headers:
            raw = (
                b"POST /v1/workflows/preflight HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{server.server_port}\r\n".encode("ascii")
                + repeated
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(document)}\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
                + document
            )
            response, eof = self.raw_request(server, raw)
            self.assertTrue(eof)
            self.assertIn(b"HTTP/1.1 400", response)
            self.assertIn(expected_code, response)
            self.assertEqual(calls, 0)

        status, value, headers = self.request(
            server,
            "POST",
            "/v1/workflows/preflight",
            spec.as_data(),
            headers={
                "Authorization": "Bearer studio-secret",
                "Origin": "https://studio.example",
            },
        )
        self.assertEqual((status, value["ok"], calls), (200, True, 1))
        self.assertEqual(
            headers["Access-Control-Allow-Origin"], "https://studio.example"
        )

    def test_workflow_preflight_does_not_acquire_the_runtime_mutation_gate(self):
        entered = threading.Event()
        release = threading.Event()
        tool_calls = 0

        def slow(value: str) -> str:
            nonlocal tool_calls
            tool_calls += 1
            entered.set()
            if not release.wait(15):
                raise AssertionError("test did not release the controlled Tool")
            return value

        tool = Tool("slow", slow, effect="read_only")

        class Model:
            async def complete(self, messages, tools):
                if messages[-1].role == "tool":
                    return ModelReply(content="done")
                return ModelReply(
                    tool_calls=(ToolCall("slow-1", "slow", {"value": "x"}),)
                )

        spec = WorkflowSpec(
            "concurrent-preflight",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "slow",
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
            ),
            "slow",
        )
        server = self.start(app=lambda store: Harness(Model(), (tool,), store=store))
        drive = {}

        def run():
            drive["response"] = self.request(
                server,
                "POST",
                "/v1/runs",
                {"run_id": "preflight-gate", "input": "drive"},
                timeout=20,
            )

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(10), "Tool did not enter its await boundary")
            status, value, _ = self.request(
                server,
                "POST",
                "/v1/workflows/preflight",
                spec.as_data(),
                timeout=10,
            )
            self.assertEqual((status, value["ok"]), (200, True))
            self.assertEqual(tool_calls, 1)
        finally:
            release.set()
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(drive["response"][0], 200)
        self.assertEqual(tool_calls, 1)

    def test_first_party_workflow_uses_existing_http_approval_and_resume(self):
        action_log = Path(self.temp.name) / "workflow-actions.jsonl"
        with mock.patch.dict(
            "os.environ", {"SASORI_ACTION_LOG": str(action_log)}, clear=False
        ):
            server = create_server(
                "127.0.0.1",
                0,
                database=self.db,
                apps={
                    WORKFLOW_INCIDENT_ID: (
                        "sasori_apps.workflow_incident:create_harness"
                    )
                },
                trusted_loopback_no_auth=True,
                sse_max_seconds=2,
                sse_keepalive_seconds=0.1,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append((server, thread))

            status, catalog, _ = self.request(server, "GET", "/v1/apps")
            self.assertEqual(status, 200)
            app = next(
                item
                for item in catalog["apps"]
                if item["id"] == WORKFLOW_INCIDENT_ID
            )
            self.assertEqual(app["availability"]["status"], "ready")
            self.assertEqual(
                app["workflow"]["definition_sha256"],
                INCIDENT_WORKFLOW_SPEC.digest,
            )
            self.assertFalse(app["workflow"]["supports_parallel"])
            self.assertEqual(
                app["workflow"], WORKFLOW_INCIDENT_METADATA["workflow"]
            )
            self.assertTrue(
                all(tool["plugin_id"] == "com.sasori.flow" for tool in app["tools"])
            )
            dispatch_names = [tool["name"] for tool in app["tools"]]
            self.assertEqual(app["worker"]["tool_names"], dispatch_names)
            self.assertEqual(app["skills"][0]["tool_names"], dispatch_names)
            self.assertEqual(
                app["worker"]["logical_tool_names"],
                ["inspect_incident", "record_action"],
            )
            self.assertEqual(
                app["skills"][0]["logical_tool_names"],
                ["inspect_incident", "record_action"],
            )
            steps = app["workflow"]["steps"]
            self.assertEqual(
                [step["step_id"] for step in steps], ["inspect", "record"]
            )
            self.assertEqual(
                [step["logical_tool_name"] for step in steps],
                ["inspect_incident", "record_action"],
            )
            self.assertEqual(
                [step["dispatch_tool_name"] for step in steps], dispatch_names
            )
            self.assertEqual(
                [step["effect"] for step in steps],
                ["read_only", "side_effecting"],
            )
            self.assertEqual(
                [step["is_output"] for step in steps], [False, True]
            )
            self.assertTrue(
                all(
                    step["dispatch_schema_sha256"]
                    == app["tools"][index]["schema_sha256"]
                    for index, step in enumerate(steps)
                )
            )

            status, paused, _ = self.request(
                server,
                "POST",
                "/v1/runs",
                {
                    "run_id": "HttpTypedWorkflow",
                    "app_id": WORKFLOW_INCIDENT_ID,
                    "input": "checkout latency is high",
                },
            )
            self.assertEqual((status, paused["state"]), (202, "paused"))
            self.assertEqual(paused["input"], "checkout latency is high")
            self.assertEqual(
                paused["workflow"]["definition_sha256"],
                INCIDENT_WORKFLOW_SPEC.digest,
            )
            self.assertEqual(
                [step["status"] for step in paused["workflow"]["steps"]],
                ["completed", "approval_required"],
            )
            self.assertEqual(paused["pending"]["effect"], "side_effecting")
            self.assertEqual(
                paused["pending"]["arguments"]["step_id"], "record"
            )
            self.assertFalse(action_log.exists())
            fingerprint = paused["pending"]["fingerprint"]
            status, decided, _ = self.request(
                server,
                "POST",
                "/v1/runs/HttpTypedWorkflow/approval",
                {"fingerprint": fingerprint, "approved": True},
            )
            self.assertEqual((status, decided["detail"]), (200, "awaiting_resume"))
            self.assertEqual(
                decided["workflow"]["steps"][1]["status"], "resume_required"
            )
            self.assertFalse(action_log.exists())
            status, completed, _ = self.request(
                server, "POST", "/v1/runs/HttpTypedWorkflow/resume", {}
            )
            self.assertEqual((status, completed["state"]), (200, "completed"))
            self.assertEqual(
                [step["status"] for step in completed["workflow"]["steps"]],
                ["completed", "completed"],
            )
            self.assertIsNone(completed["workflow"]["current_step_id"])
            outcome = json.loads(completed["final_message"]["content"])
            self.assertEqual(outcome["status"], "succeeded")
            self.assertEqual(outcome["definition_sha256"], INCIDENT_WORKFLOW_SPEC.digest)
            self.assertEqual(len(action_log.read_text("utf-8").splitlines()), 1)
            status, reopened, _ = self.request(
                server, "GET", "/v1/runs/HttpTypedWorkflow"
            )
            self.assertEqual((status, reopened), (200, completed))
            status, history, _ = self.request(server, "GET", "/v1/runs?limit=10")
            self.assertEqual(status, 200)
            self.assertNotIn("workflow", history["items"][0])

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
        self.assertIn('id="artifacts-tab" role="tab"', page)
        self.assertIn('id="studio-button" type="button"', page)
        self.assertIn('id="workflow-studio" hidden', page)
        self.assertIn("DRAFT ONLY", page)
        self.assertIn("NO EXECUTION", page)
        self.assertIn('id="artifact-list" aria-live="polite"', page)
        self.assertIn('tabindex="-1"', page)
        self.assertIn('data-mobile-view="stage" class="active" aria-pressed="true"', page)

        assets = {}
        for path, content_type in (
            ("/assets/app.0.1.0.css", "text/css"),
            ("/assets/artifacts.0.1.0.css", "text/css"),
            ("/assets/app.0.1.1.js", "text/javascript"),
            ("/assets/event-reducer.0.1.0.js", "text/javascript"),
            ("/assets/app.0.1.2.js", "text/javascript"),
            ("/assets/app.0.1.3.js", "text/javascript"),
            ("/assets/app.0.1.4.js", "text/javascript"),
            ("/assets/workflow.0.1.0.css", "text/css"),
            ("/assets/workflow.0.1.0.js", "text/javascript"),
            ("/assets/workflow.0.2.0.js", "text/javascript"),
            ("/assets/workflow-manifest.0.1.0.js", "text/javascript"),
            ("/assets/workflow-studio.0.1.0.css", "text/css"),
            ("/assets/workflow-studio.0.1.0.js", "text/javascript"),
            ("/assets/mark.0.1.0.svg", "image/svg+xml"),
        ):
            status, body, asset_headers = self.request(server, "GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(asset_headers["Content-Type"].startswith(content_type))
            self.assertIn("immutable", asset_headers["Cache-Control"])
            self.assertTrue(body)
            assets[path] = body

        self.assertLess(
            page.index("/assets/event-reducer.0.1.0.js"),
            page.index("/assets/app.0.1.2.js"),
        )
        self.assertLess(
            page.index("/assets/app.0.1.2.js"),
            page.index("/assets/app.0.1.3.js"),
        )
        self.assertLess(
            page.index("/assets/app.0.1.3.js"),
            page.index("/assets/app.0.1.4.js"),
        )
        self.assertLess(
            page.index("/assets/app.0.1.4.js"),
            page.index("/assets/workflow.0.2.0.js"),
        )
        self.assertLess(
            page.index("/assets/workflow.0.2.0.js"),
            page.index("/assets/workflow-manifest.0.1.0.js"),
        )
        self.assertLess(
            page.index("/assets/workflow-manifest.0.1.0.js"),
            page.index("/assets/workflow-studio.0.1.0.js"),
        )
        reducer = assets["/assets/event-reducer.0.1.0.js"]
        self.assertIn("function reduceEvent(state, projected)", reducer)
        self.assertIn("function createRunGate()", reducer)
        script = assets["/assets/app.0.1.2.js"]
        self.assertIn("reduceEvent(state.eventState, projected)", script)
        self.assertIn("state.runGate.startWatcher(context)", script)
        self.assertNotIn("watchRun(context)", script)
        self.assertNotIn("state.eventState = createEventState(context.runId)", script)
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn('button.setAttribute("aria-pressed", String(active))', script)
        self.assertEqual(script.count('$$(".mobile-nav [data-mobile-view]")'), 2)
        self.assertNotIn('$$("[data-mobile-view]")', script)
        stylesheet = assets["/assets/app.0.1.0.css"]
        self.assertNotIn(".signal b { display: none; }", stylesheet)
        artifact_script = assets["/assets/app.0.1.3.js"]
        self.assertIn("contextIsActive(context)", artifact_script)
        self.assertIn("textContent = text", artifact_script)
        self.assertIn("URL.createObjectURL(blob)", artifact_script)
        self.assertIn("URL.revokeObjectURL(objectUrl)", artifact_script)
        self.assertNotIn("token=", artifact_script)
        self.assertNotIn("innerHTML", artifact_script)
        artifact_styles = assets["/assets/artifacts.0.1.0.css"]
        self.assertIn(".artifact-card", artifact_styles)
        recovery_script = assets["/assets/app.0.1.4.js"]
        self.assertIn("renderOperatorActionWithCancelledPolicy", recovery_script)
        self.assertIn("state.run.state !== \"cancelled\"", recovery_script)
        self.assertIn("retry.remove()", recovery_script)
        self.assertNotIn("innerHTML", recovery_script)
        workflow_script = assets["/assets/workflow.0.2.0.js"]
        self.assertIn("state.run.workflow", workflow_script)
        self.assertIn(
            "function workflowRunProjection(app, contract, run = state.run)",
            workflow_script,
        )
        self.assertIn("function renderWorkflowSurface(app)", workflow_script)
        self.assertIn("workflowRefreshInFlight", workflow_script)
        self.assertIn("workflowRefreshPendingContext", workflow_script)
        self.assertIn(
            "value.latest_seq !== Number(run.latest_seq || 0)", workflow_script
        )
        self.assertNotIn("state.eventState.events", workflow_script)
        self.assertNotIn("function workflowStepProjection", workflow_script)
        self.assertNotIn("reduceEvent(", workflow_script)
        self.assertNotIn("new Map", workflow_script)
        self.assertNotIn("innerHTML", workflow_script)
        workflow_manifest_script = assets[
            "/assets/workflow-manifest.0.1.0.js"
        ]
        self.assertIn("function workflowManifestContract(app)", workflow_manifest_script)
        self.assertIn("workflowContract = workflowManifestContract", workflow_manifest_script)
        self.assertIn("manual_effect_resolution_on_ambiguity", workflow_manifest_script)
        self.assertIn("trusted_installed_python", workflow_manifest_script)
        self.assertNotIn("innerHTML", workflow_manifest_script)
        workflow_studio_script = assets["/assets/workflow-studio.0.1.0.js"]
        self.assertIn('fetch("/v1/workflows/preflight"', workflow_studio_script)
        self.assertIn("request.editEpoch !== editEpoch", workflow_studio_script)
        self.assertIn("editor.value !== request.draft", workflow_studio_script)
        self.assertIn("workflowManifestContract(app)", workflow_studio_script)
        self.assertIn("activeRequest.controller.abort()", workflow_studio_script)
        self.assertNotIn("innerHTML", workflow_studio_script)
        self.assertNotIn("localStorage", workflow_studio_script)
        self.assertNotIn("/v1/runs", workflow_studio_script)
        workflow_studio_styles = assets["/assets/workflow-studio.0.1.0.css"]
        self.assertIn(".workflow-studio", workflow_studio_styles)
        self.assertIn("prefers-reduced-motion", workflow_studio_styles)
        workflow_styles = assets["/assets/workflow.0.1.0.css"]
        self.assertIn(".workflow-rail", workflow_styles)

        status, error, _ = self.request(server, "GET", "/assets/../README.md")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/app.0.1.2.js?v=1")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/app.0.1.3.js?v=1")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/app.0.1.4.js?v=1")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(server, "GET", "/assets/workflow.0.2.0.js?v=1")
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(
            server, "GET", "/assets/workflow-manifest.0.1.0.js?v=1"
        )
        self.assertEqual((status, error["error"]["code"]), (404, "not_found"))
        status, error, _ = self.request(
            server, "GET", "/assets/workflow-studio.0.1.0.js?v=1"
        )
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
