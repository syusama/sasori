import asyncio
import hashlib
import http.client
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sasori import Harness, Tool, tool_schema_sha256  # noqa: E402
from sasori.server import ServerConfigurationError, create_server  # noqa: E402
from sasori_flow import InputRef, InputSlot, ToolStep, WorkflowSpec  # noqa: E402


class SavedWorkflowHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = str(root / "runs.sqlite3")
        self.workflow_database = str(root / "saved.sqlite3")
        self.module = types.ModuleType("sasori_saved_workflow_http_app")
        self.model_calls = 0
        self.tool_calls = 0

        owner = self

        class ForbiddenModel:
            async def complete(self, messages, tools):
                owner.model_calls += 1
                raise AssertionError("saved Workflow CRUD must not call the model")

        def inspect(value):
            owner.tool_calls += 1
            raise AssertionError("saved Workflow CRUD must not call a Tool")

        self.tool = Tool("inspect", inspect, effect="read_only")

        def create(store):
            return Harness(ForbiddenModel(), (self.tool,), store=store)

        self.module.create = create
        sys.modules[self.module.__name__] = self.module
        self.servers = []

    def tearDown(self):
        for server, thread in reversed(self.servers):
            try:
                server.shutdown()
                server.server_close()
            finally:
                thread.join(5)
        sys.modules.pop(self.module.__name__, None)
        self.temp.cleanup()

    def start(self, **overrides):
        options = {
            "database": self.database,
            "workflow_database": self.workflow_database,
            "artifact_root": Path(self.temp.name) / "artifacts",
            "app": f"{self.module.__name__}:create",
            "trusted_loopback_no_auth": True,
        }
        options.update(overrides)
        server = create_server("127.0.0.1", 0, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    @staticmethod
    def request(server, method, path, body=None, headers=None):
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
        return status, json.loads(payload), response_headers

    @staticmethod
    def request_bytes(server, method, path, body, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        request_headers = dict(headers or {})
        request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, json.loads(payload), dict(response.getheaders())
        connection.close()
        return result

    @staticmethod
    def raw_request(server, request):
        chunks = []
        with socket.create_connection(server.server_address, timeout=3) as connection:
            connection.settimeout(3)
            connection.sendall(request)
            while True:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def definition(self, *, version="1"):
        step = ToolStep(
            "inspect",
            self.tool.name,
            self.tool.effect,
            self.tool.tool_revision,
            tool_schema_sha256(self.tool),
            {"value": InputRef("value")},
            "string",
        )
        return WorkflowSpec(
            "saved-flow",
            version,
            (InputSlot("value", "string"),),
            (step,),
            step.step_id,
        ).as_data()

    @staticmethod
    def identity():
        return f"wfcat_{uuid.uuid4().hex}"

    def stop(self, server):
        index = next(i for i, item in enumerate(self.servers) if item[0] is server)
        _, thread = self.servers.pop(index)
        server.shutdown()
        server.server_close()
        thread.join(5)

    def run_rows(self, server):
        async def collect():
            database = server.owner._store._db
            return {
                table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "runs", "events", "checkpoints", "accepted_replies",
                    "tool_calls", "approvals", "artifacts",
                )
            }

        return server.owner.call(collect(), 2)

    def test_create_get_list_update_history_and_stale_cas(self):
        server = self.start()
        identity = self.identity()

        status, error, _ = self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition()
        )
        self.assertEqual((status, error["error"]["code"]), (428, "workflow_catalog_precondition_required"))

        status, created, headers = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["record"]["catalog_id"], identity)
        self.assertEqual(created["record"]["catalog_revision"], 1)
        self.assertEqual(created["record"]["current_contract"]["status"], "compatible")
        self.assertEqual(headers["Location"], f"/v1/workflows/{identity}")
        first_etag = headers["ETag"]

        status, listed, _ = self.request(server, "GET", "/v1/workflows?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual([item["catalog_id"] for item in listed["items"]], [identity])
        self.assertNotIn("definition", listed["items"][0])
        self.assertNotIn("saved_manifest", listed["items"][0])

        status, updated, headers = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(version="2"),
            {"If-Match": first_etag},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["record"]["catalog_revision"], 2)
        second_etag = headers["ETag"]

        status, conflict, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(version="2"),
            {"If-Match": first_etag},
        )
        self.assertEqual((status, conflict["error"]["code"]), (412, "workflow_catalog_revision_mismatch"))

        status, historical, headers = self.request(
            server, "GET", f"/v1/workflows/{identity}?revision=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(historical["record"]["definition"]["version"], "1")
        self.assertFalse(historical["record"]["is_head"])
        self.assertEqual(headers["ETag"], first_etag)

        status, current, headers = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(status, 200)
        self.assertEqual(current["record"]["definition"]["version"], "2")
        self.assertEqual(headers["ETag"], second_etag)
        self.assertEqual((self.model_calls, self.tool_calls), (0, 0))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_oversized_catalog_cursor_and_revision_are_invalid_requests(self):
        server = self.start()
        identity = self.identity()
        for path in (
            "/v1/workflows?before=9223372036854775808",
            f"/v1/workflows/{identity}?revision=9223372036854775808",
        ):
            with self.subTest(path=path):
                status, value, _ = self.request(server, "GET", path)
                self.assertEqual(
                    (status, value["error"]["code"]),
                    (422, "invalid_request"),
                )
                self.assertFalse(value["error"]["retryable"])
        self.assertEqual((self.model_calls, self.tool_calls), (0, 0))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_current_identical_put_is_no_op_and_create_replay_is_precondition_failure(self):
        server = self.start()
        identity = self.identity()
        status, created, headers = self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)
        etag = headers["ETag"]
        status, same, headers = self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition(),
            {"If-Match": etag},
        )
        self.assertEqual(status, 200)
        self.assertEqual(same["record"]["catalog_revision"], 1)
        self.assertEqual(headers["ETag"], etag)
        status, replay, _ = self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual((status, replay["error"]["code"]), (412, "workflow_catalog_revision_mismatch"))
        self.assertEqual(created["record"], same["record"])

    def test_conditional_header_and_path_contract_fail_closed(self):
        server = self.start()
        identity = self.identity()
        cases = (
            ({"If-None-Match": '"not-star"'}, 422),
            ({"If-Match": "*"}, 422),
            ({"If-Match": 'W/"weak"'}, 422),
            ({"If-Match": '"one", "two"'}, 422),
            ({"If-Match": '"bad"', "If-None-Match": "*"}, 422),
        )
        for headers, expected in cases:
            with self.subTest(headers=headers):
                status, error, _ = self.request(
                    server, "PUT", f"/v1/workflows/{identity}", self.definition(), headers
                )
                self.assertEqual((status, error["error"]["code"]), (expected, "invalid_request"))
        status, error, _ = self.request(
            server, "PUT", "/v1/workflows/saved-flow", self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual((status, error["error"]["code"]), (422, "invalid_request"))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_repeated_conditionals_fail_before_body_parse_or_catalog_mutation(self):
        server = self.start()
        identity = self.identity()
        calls = 0
        original = server.owner.saved_workflow_put

        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        server.owner.saved_workflow_put = counted
        repeated_headers = (
            b'If-Match: "one"\r\nIf-Match: "two"\r\n',
            b"If-None-Match: *\r\nIf-None-Match: *\r\n",
        )
        with mock.patch(
            "sasori.server._strict_json",
            side_effect=AssertionError("conditional rejection must precede JSON parsing"),
        ) as strict_json:
            for repeated in repeated_headers:
                with self.subTest(repeated=repeated):
                    raw = (
                        f"PUT /v1/workflows/{identity} HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{server.server_port}\r\n"
                    ).encode("ascii") + repeated + (
                        "Content-Type: application/json\r\n"
                        "Content-Length: 1\r\nConnection: close\r\n\r\n{"
                    ).encode("ascii")
                    response = self.raw_request(server, raw)
                    self.assertIn(b"HTTP/1.1 422", response)
                    self.assertIn(b'"code":"invalid_request"', response)
                    self.assertEqual(calls, 0)
            strict_json.assert_not_called()
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_auth_and_origin_fail_before_body_parse_or_catalog_mutation(self):
        server = self.start(
            token="catalog-secret",
            trusted_loopback_no_auth=False,
            cors_origins=("https://studio.example",),
        )
        identity = self.identity()
        calls = 0
        original = server.owner.saved_workflow_put

        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        server.owner.saved_workflow_put = counted
        with mock.patch(
            "sasori.server._strict_json",
            side_effect=AssertionError("authorization must precede JSON parsing"),
        ) as strict_json:
            status, value, _ = self.request_bytes(
                server,
                "PUT",
                f"/v1/workflows/{identity}",
                b"{",
                {"If-None-Match": "*"},
            )
            self.assertEqual((status, value["error"]["code"]), (401, "unauthorized"))
            status, value, _ = self.request_bytes(
                server,
                "PUT",
                f"/v1/workflows/{identity}",
                b"{",
                {
                    "Authorization": "Bearer catalog-secret",
                    "Origin": "https://evil.example",
                    "If-None-Match": "*",
                },
            )
            self.assertEqual((status, value["error"]["code"]), (403, "origin_forbidden"))
            strict_json.assert_not_called()
        self.assertEqual(calls, 0)
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_malformed_utf8_bom_and_duplicate_json_keys_never_reach_catalog(self):
        server = self.start()
        valid = json.dumps(self.definition(), separators=(",", ":")).encode("utf-8")
        bodies = (
            b'{"schema_version":"\xff"}',
            b"\xef\xbb\xbf" + valid,
            b'{"schema_version":1,"schema_version":1}',
        )
        for body in bodies:
            with self.subTest(body=body[:24]):
                status, value, _ = self.request_bytes(
                    server,
                    "PUT",
                    f"/v1/workflows/{self.identity()}",
                    body,
                    {"If-None-Match": "*"},
                )
                self.assertEqual((status, value["error"]["code"]), (400, "malformed_json"))
        status, page, _ = self.request(server, "GET", "/v1/workflows?limit=10")
        self.assertEqual((status, page["items"]), (200, []))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_timeout_after_commit_returns_exact_outcome_unknown_and_get_reconciles(self):
        server = self.start()
        identity = self.identity()
        committed = threading.Event()
        calls = 0
        original_put = server.owner.saved_workflow_put
        original_call = server.owner.call

        async def commit_then_wait(*args, **kwargs):
            nonlocal calls
            calls += 1
            value = await original_put(*args, **kwargs)
            committed.set()
            await asyncio.sleep(60)
            return value

        def short_call(operation, timeout=None):
            return original_call(operation, 0.05)

        server.owner.saved_workflow_put = commit_then_wait
        server.owner.call = short_call
        try:
            status, value, headers = self.request(
                server,
                "PUT",
                f"/v1/workflows/{identity}",
                self.definition(),
                {"If-None-Match": "*"},
            )
        finally:
            server.owner.call = original_call
            server.owner.saved_workflow_put = original_put
        self.assertTrue(committed.wait(1))
        self.assertEqual(status, 504)
        self.assertEqual(set(value), {"ok", "error"})
        self.assertEqual(
            value,
            {
                "ok": False,
                "error": {
                    "code": "workflow_catalog_outcome_unknown",
                    "message": (
                        "saved Workflow mutation outcome is unknown; "
                        "reconcile with a read-only GET"
                    ),
                    "retryable": False,
                    "catalog_id": identity,
                },
            },
        )
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertNotIn("Retry-After", headers)
        self.assertNotIn("ETag", headers)
        self.assertNotIn("Location", headers)
        self.assertEqual(calls, 1)

        status, recovered, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(status, 200)
        self.assertEqual(recovered["record"]["catalog_revision"], 1)
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_timeout_before_commit_is_outcome_unknown_and_get_establishes_absence(self):
        server = self.start()
        identity = self.identity()
        entered = threading.Event()
        calls = 0
        original_put = server.owner.saved_workflow_put
        original_call = server.owner.call

        async def wait_before_commit(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.sleep(60)
            return await original_put(*args, **kwargs)

        def short_call(operation, timeout=None):
            return original_call(operation, 0.05)

        server.owner.saved_workflow_put = wait_before_commit
        server.owner.call = short_call
        try:
            status, value, headers = self.request(
                server,
                "PUT",
                f"/v1/workflows/{identity}",
                self.definition(),
                {"If-None-Match": "*"},
            )
        finally:
            server.owner.call = original_call
            server.owner.saved_workflow_put = original_put
        self.assertTrue(entered.wait(1))
        self.assertEqual(
            (status, value["error"]["code"], value["error"]["retryable"]),
            (504, "workflow_catalog_outcome_unknown", False),
        )
        self.assertEqual(value["error"]["catalog_id"], identity)
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertNotIn("Retry-After", headers)
        self.assertEqual(calls, 1)

        status, missing, _ = self.request(
            server, "GET", f"/v1/workflows/{identity}"
        )
        self.assertEqual(
            (status, missing["error"]["code"]),
            (404, "workflow_catalog_not_found"),
        )
        status, page, _ = self.request(server, "GET", "/v1/workflows?limit=10")
        self.assertEqual((status, page["items"]), (200, []))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_catalog_crud_does_not_acquire_runtime_mutation_gate(self):
        server = self.start()
        entered = threading.Event()
        release = threading.Event()

        async def hold_gate():
            gate = server.owner._gate
            assert gate is not None
            await gate.acquire()
            entered.set()
            try:
                await asyncio.get_running_loop().run_in_executor(None, release.wait)
            finally:
                gate.release()

        future = asyncio.run_coroutine_threadsafe(hold_gate(), server.owner._loop)
        try:
            self.assertTrue(entered.wait(2), "runtime mutation gate was not acquired")
            identity = self.identity()
            status, created, headers = self.request(
                server,
                "PUT",
                f"/v1/workflows/{identity}",
                self.definition(),
                {"If-None-Match": "*"},
            )
            self.assertEqual((status, created["record"]["catalog_revision"]), (201, 1))
            status, current, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
            self.assertEqual((status, current["record"]["is_head"]), (200, True))
            status, page, _ = self.request(server, "GET", "/v1/workflows?limit=10")
            self.assertEqual((status, len(page["items"])), (200, 1))
            self.assertIn("ETag", headers)
        finally:
            release.set()
            future.result(5)
        self.assertEqual((self.model_calls, self.tool_calls), (0, 0))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_sqlite_runtime_failures_use_fixed_catalog_error_contracts(self):
        server = self.start()
        store = server.owner._workflow_store
        original = store._db

        class BrokenDatabase:
            def __init__(self, error):
                self.error = error

            def execute(self, *_args, **_kwargs):
                raise self.error

        cases = (
            (
                sqlite3.IntegrityError("raw check constraint prose"),
                "workflow_catalog_integrity_failed",
                "saved Workflow catalog integrity verification failed",
            ),
            (
                sqlite3.DatabaseError("raw database disk image prose"),
                "workflow_catalog_store_unavailable",
                "saved Workflow catalog is unavailable",
            ),
        )
        try:
            for error, code, message in cases:
                with self.subTest(code=code):
                    store._db = BrokenDatabase(error)
                    status, value, _ = self.request(
                        server, "GET", "/v1/workflows?limit=10"
                    )
                    self.assertEqual((status, value["error"]["code"]), (503, code))
                    self.assertEqual(value["error"]["message"], message)
                    self.assertNotIn("raw", json.dumps(value))
        finally:
            store._db = original
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_dangling_head_fails_current_history_and_list_with_fixed_http_taxonomy(self):
        server = self.start()
        identity = self.identity()
        status, _, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)

        async def tamper():
            database = server.owner._workflow_store._db
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute(
                "UPDATE workflow_heads SET current_revision = 999 "
                "WHERE catalog_id = ?",
                (identity,),
            )

        server.owner.call(tamper(), 2)
        for path in (
            f"/v1/workflows/{identity}",
            f"/v1/workflows/{identity}?revision=1",
            "/v1/workflows?limit=100",
        ):
            with self.subTest(path=path):
                status, value, _ = self.request(server, "GET", path)
                self.assertEqual(
                    (status, value),
                    (
                        503,
                        {
                            "ok": False,
                            "error": {
                                "code": "workflow_catalog_integrity_failed",
                                "message": (
                                    "saved Workflow catalog integrity verification failed"
                                ),
                                "retryable": False,
                            },
                        },
                    ),
                )
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_corrupt_stored_revision_never_escapes_as_generic_internal_error(self):
        server = self.start()
        identity = self.identity()
        status, _, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)

        async def tamper():
            database = server.owner._workflow_store._db
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute(
                "UPDATE workflow_heads SET current_revision = 'abc' "
                "WHERE catalog_id = ?",
                (identity,),
            )

        server.owner.call(tamper(), 2)
        status, value, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(
            (status, value["error"]["code"]),
            (503, "workflow_catalog_integrity_failed"),
        )
        self.assertNotIn("abc", json.dumps(value))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_non_text_stored_identity_uses_fixed_integrity_taxonomy(self):
        server = self.start()
        identity = self.identity()
        status, _, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)

        async def tamper():
            database = server.owner._workflow_store._db
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute(
                "UPDATE workflow_heads SET current_workflow_id = ? "
                "WHERE catalog_id = ?",
                (sqlite3.Binary(b"stored-secret-literal"), identity),
            )

        server.owner.call(tamper(), 2)
        status, value, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(
            (status, value["error"]["code"]),
            (503, "workflow_catalog_integrity_failed"),
        )
        self.assertNotIn("stored-secret-literal", json.dumps(value))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_exhausted_catalog_sequence_is_fixed_integrity_failure(self):
        server = self.start()

        async def exhaust():
            database = server.owner._workflow_store._db
            database.execute(
                "UPDATE workflow_catalog_meta SET next_catalog_seq = ? "
                "WHERE singleton = 1",
                (2**63 - 1,),
            )

        server.owner.call(exhaust(), 2)
        status, value, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{self.identity()}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(
            (status, value["error"]["code"]),
            (503, "workflow_catalog_integrity_failed"),
        )
        self.assertNotEqual(value["error"]["code"], "internal_error")
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_boolean_manifest_schema_version_is_not_equal_to_integer_one(self):
        server = self.start()
        identity = self.identity()
        status, _, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)

        async def tamper():
            database = server.owner._workflow_store._db
            row = database.execute(
                "SELECT manifest_json FROM workflow_revisions WHERE catalog_id = ?",
                (identity,),
            ).fetchone()
            manifest = json.loads(row[0])
            manifest["schema_version"] = True
            document = json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            trigger_sql = database.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
                "AND name = 'workflow_revisions_immutable_update'"
            ).fetchone()[0]
            database.execute("DROP TRIGGER workflow_revisions_immutable_update")
            database.execute(
                "UPDATE workflow_revisions SET manifest_json = ?, "
                "manifest_sha256 = ? WHERE catalog_id = ?",
                (document, hashlib.sha256(document).hexdigest(), identity),
            )
            database.execute(trigger_sql)

        server.owner.call(tamper(), 2)
        status, value, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(
            (status, value["error"]["code"]),
            (503, "workflow_catalog_integrity_failed"),
        )
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_over_nested_stored_manifest_is_fixed_integrity_failure(self):
        server = self.start()
        identity = self.identity()
        status, _, _ = self.request(
            server,
            "PUT",
            f"/v1/workflows/{identity}",
            self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)
        document = (
            '{"x":' * 800 + '"stored-secret-literal"' + "}" * 800
        ).encode("utf-8")

        async def tamper():
            database = server.owner._workflow_store._db
            trigger_sql = database.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
                "AND name = 'workflow_revisions_immutable_update'"
            ).fetchone()[0]
            database.execute("DROP TRIGGER workflow_revisions_immutable_update")
            database.execute(
                "UPDATE workflow_revisions SET manifest_json = ?, "
                "manifest_sha256 = ? WHERE catalog_id = ?",
                (document, hashlib.sha256(document).hexdigest(), identity),
            )
            database.execute(trigger_sql)

        server.owner.call(tamper(), 2)
        status, value, _ = self.request(server, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(
            (status, value["error"]["code"]),
            (503, "workflow_catalog_integrity_failed"),
        )
        self.assertNotIn("stored-secret-literal", json.dumps(value))
        self.assertEqual(set(self.run_rows(server).values()), {0})

    def test_restart_preserves_catalog_and_reports_tool_drift_without_mutation(self):
        server = self.start()
        identity = self.identity()
        status, _, headers = self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition(),
            {"If-None-Match": "*"},
        )
        self.assertEqual(status, 201)
        etag = headers["ETag"]
        self.stop(server)

        owner = self

        def changed(value, extra="drift"):
            owner.tool_calls += 1
            raise AssertionError("read-time compatibility must not call a Tool")

        changed_tool = Tool("inspect", changed, effect="read_only")

        def create_changed(store):
            class ForbiddenModel:
                async def complete(self, messages, tools):
                    owner.model_calls += 1
                    raise AssertionError("catalog read must not call the model")

            return Harness(ForbiddenModel(), (changed_tool,), store=store)

        self.module.create = create_changed
        restarted = self.start()
        status, value, headers = self.request(restarted, "GET", f"/v1/workflows/{identity}")
        self.assertEqual(status, 200)
        self.assertEqual(value["record"]["current_contract"], {
            "status": "incompatible", "reason_code": "tool_contract_mismatch"
        })
        self.assertEqual(headers["ETag"], etag)
        status, historical, historical_headers = self.request(
            restarted, "GET", f"/v1/workflows/{identity}?revision=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(historical["record"]["current_contract"]["status"], "incompatible")
        self.assertEqual(historical_headers["ETag"], etag)
        status, page, _ = self.request(restarted, "GET", "/v1/workflows?limit=10")
        self.assertEqual((status, len(page["items"])), (200, 1))
        self.assertEqual((self.model_calls, self.tool_calls), (0, 0))
        self.assertEqual(set(self.run_rows(restarted).values()), {0})

    def test_saved_identity_does_not_become_an_application(self):
        server = self.start()
        identity = self.identity()
        self.request(
            server, "PUT", f"/v1/workflows/{identity}", self.definition(),
            {"If-None-Match": "*"},
        )
        status, error, _ = self.request(
            server, "POST", "/v1/runs",
            {"run_id": "saved-cannot-run", "app_id": "saved-flow", "input": "x"},
        )
        self.assertEqual((status, error["error"]["code"]), (404, "app_not_found"))
        self.assertEqual((self.model_calls, self.tool_calls), (0, 0))

    def test_run_and_workflow_database_must_be_distinct(self):
        with self.assertRaisesRegex(ServerConfigurationError, "must be different"):
            create_server(
                "127.0.0.1",
                0,
                database=self.database,
                workflow_database=self.database,
                artifact_root=Path(self.temp.name) / "artifacts",
                app=f"{self.module.__name__}:create",
                trusted_loopback_no_auth=True,
            )


if __name__ == "__main__":
    unittest.main()
