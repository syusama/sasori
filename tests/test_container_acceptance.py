from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location(
    "sasori_container_acceptance", ROOT / "scripts" / "container_acceptance.py"
)
container_acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = container_acceptance
SPEC.loader.exec_module(container_acceptance)

from sasori.server import create_server  # noqa: E402


class ContainerAcceptanceTests(unittest.TestCase):
    TOKEN = "container-acceptance-secret-32-bytes"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "sasori.sqlite3"
        self.action_log = self.root / "incident-actions.jsonl"
        self.token_file = self.root / "token"
        self.token_file.write_text(self.TOKEN + "\n", encoding="ascii")
        self.evidence = self.root / "evidence.json"
        self.servers: list[tuple[object, threading.Thread]] = []
        self.environment = mock.patch.dict(
            os.environ, {"SASORI_ACTION_LOG": str(self.action_log)}
        )
        self.environment.start()

    def tearDown(self) -> None:
        failure = None
        for server, thread in reversed(self.servers):
            try:
                server.shutdown()
                server.server_close()
                thread.join(5)
            except BaseException as error:
                failure = failure or error
        self.environment.stop()
        self.temp.cleanup()
        if failure is not None:
            raise failure

    def _start_sasori(self) -> tuple[object, str]:
        server = create_server(
            "127.0.0.1",
            0,
            database=self.database,
            apps={"incident": "sasori_apps.incident:create_harness"},
            token=self.TOKEN,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.05,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server, f"http://127.0.0.1:{server.server_port}"

    def _stop_sasori(self, server: object) -> None:
        index = next(
            index for index, (candidate, _) in enumerate(self.servers)
            if candidate is server
        )
        _, thread = self.servers.pop(index)
        server.shutdown()
        server.server_close()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def _main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = container_acceptance.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_real_incident_flow_and_restart_preserve_exactly_one_effect(self) -> None:
        server, base_url = self._start_sasori()
        prepare = [
            "prepare",
            "--base-url",
            base_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(self.evidence),
            "--run-id",
            "container-acceptance-1",
        ]
        code, stdout, stderr = self._main(prepare)
        self.assertEqual((code, stderr), (0, ""))
        output = json.loads(stdout)
        stored = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(output, stored)
        self.assertEqual(stored["phase"], "prepare")
        self.assertEqual(stored["event_count"], 11)
        self.assertEqual(stored["latest_seq"], 11)
        self.assertEqual(stored["effect"]["completed_count"], 0)
        self.assertFalse(stored["workflow"]["explicit_resume"])
        self.assertTrue(
            not self.action_log.exists()
            or not self.action_log.read_text(encoding="utf-8").splitlines()
        )
        client = container_acceptance.HTTPClient(base_url, self.TOKEN, 2)
        prepared_events, _ = container_acceptance._events(
            client, "container-acceptance-1"
        )
        self.assertFalse(
            any(
                item["event"].get("type") == "tool.completed"
                and item["event"].get("tool_name") == "record_action"
                for item in prepared_events
            )
        )
        self.assertNotIn(self.TOKEN, stdout)
        self.assertNotIn(self.TOKEN, self.evidence.read_text(encoding="utf-8"))

        complete = [
            "complete",
            "--base-url",
            base_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(self.evidence),
        ]
        code, stdout, stderr = self._main(complete)
        self.assertEqual((code, stderr), (0, ""))
        output = json.loads(stdout)
        stored = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(output, stored)
        self.assertEqual(stored["phase"], "complete")
        self.assertEqual(stored["event_count"], 16)
        self.assertEqual(stored["latest_seq"], 16)
        self.assertEqual(stored["effect"]["completed_count"], 1)
        self.assertEqual(
            stored["reconnect"],
            {
                "after_seq": 10,
                "event_count": 6,
                "events_sha256": stored["reconnect"]["events_sha256"],
                "first_seq": 11,
                "last_seq": 16,
            },
        )
        actions = [
            json.loads(line)
            for line in self.action_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            actions,
            [{"summary": container_acceptance.EXPECTED_ACTION_SUMMARY}],
        )
        self.assertNotIn(self.TOKEN, stdout)
        self.assertNotIn(self.TOKEN, self.evidence.read_text(encoding="utf-8"))

        self._stop_sasori(server)
        _, restarted_url = self._start_sasori()
        after = [
            "after-restart",
            "--base-url",
            restarted_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(self.evidence),
        ]
        code, stdout, stderr = self._main(after)
        self.assertEqual((code, stderr), (0, ""))
        verified = json.loads(stdout)
        self.assertEqual(
            verified,
            {
                "schema_version": 1,
                "kind": "sasori.container-acceptance",
                "phase": "after-restart",
                "run_id": "container-acceptance-1",
                "verified": True,
                "latest_seq": 16,
                "event_count": 16,
                "effect_count": 1,
            },
        )
        actions = [
            json.loads(line)
            for line in self.action_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            actions,
            [{"summary": container_acceptance.EXPECTED_ACTION_SUMMARY}],
        )
        self.assertNotIn(self.TOKEN, stdout + stderr)

    def test_server_error_echo_and_error_chain_never_disclose_token(self) -> None:
        token = self.TOKEN

        class EchoHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                raw = json.dumps(
                    {"authorization": self.headers.get("Authorization")}
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, stdout, stderr = self._main(
                [
                    "prepare",
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--token-file",
                    str(self.token_file),
                    "--evidence",
                    str(self.evidence),
                    "--run-id",
                    "error-echo",
                ]
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertFalse(self.evidence.exists())
        self.assertNotIn(token, stderr)
        self.assertIn("server response disclosed the bearer token", stderr)

        client = container_acceptance.HTTPClient(
            "http://127.0.0.1:1", token, 0.1
        )
        with self.assertRaises(container_acceptance.AcceptanceError) as raised:
            client.json("GET", "/v1/runs/missing")
        chain = []
        pending = [raised.exception]
        while pending:
            current = pending.pop()
            chain.append(current)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)
        self.assertNotIn(token, "\n".join(str(item) for item in chain))

    def test_strict_parsers_and_evidence_validation_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "numeric loopback"
        ):
            container_acceptance.HTTPClient(
                "http://localhost:8080", self.TOKEN, 1
            )

        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "pending approval payload"
        ):
            container_acceptance._pending_fingerprint(
                {
                    "fingerprint": "fingerprint",
                    "tool_name": "record_action",
                    "effect": "side_effecting",
                    "arguments": {"summary": "wrong summary"},
                }
            )
        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "expected Incident final"
        ):
            container_acceptance._final_message(
                {"role": "assistant", "content": "wrong final"}, "test final"
            )

        malformed_json = (
            b'{"seq":1,"seq":2}',
            b'{"value":NaN}',
            b'\xff',
        )
        for raw in malformed_json:
            with self.subTest(raw=raw), self.assertRaises(
                container_acceptance.AcceptanceError
            ):
                container_acceptance._strict_json(raw, "test")

        malformed_sse = (
            b"id: 1\nevent: run.completed\ndata: {}\nid: 1\n\n",
            b"id: x\nevent: run.completed\ndata: {}\n\n",
            b"id: 1\ndata: {}\n\n",
            b"retry: later\n\n",
        )
        for raw in malformed_sse:
            with self.subTest(raw=raw), self.assertRaises(
                container_acceptance.AcceptanceError
            ):
                container_acceptance._parse_sse(raw)

        evidence = {
            "schema_version": 1,
            "kind": "sasori.container-acceptance",
            "phase": "complete",
            "run_id": "valid-run",
            "app_id": "incident",
            "workflow": {
                "initial_pause_reason": "approval_required",
                "decision_pause_reason": "resume_required",
                "explicit_resume": True,
            },
            "latest_seq": 16,
            "event_count": 16,
            "event_types": list(container_acceptance.EXPECTED_EVENT_TYPES),
            "events_sha256": "0" * 64,
            "projection_sha256": "1" * 64,
            "final_message": {
                "role": "assistant",
                "content": container_acceptance.EXPECTED_FINAL_CONTENT,
            },
            "effect": {"tool_name": "record_action", "completed_count": 2},
            "reconnect": {
                "after_seq": 10,
                "event_count": 6,
                "first_seq": 11,
                "last_seq": 16,
                "events_sha256": "2" * 64,
            },
        }
        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "effect summary"
        ):
            container_acceptance._validated_completed_evidence(evidence)

        evidence["effect"]["completed_count"] = 1
        evidence["workflow"]["explicit_resume"] = False
        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "workflow"
        ):
            container_acceptance._validated_completed_evidence(evidence)

        prepared = {
            "schema_version": 1,
            "kind": "sasori.container-acceptance",
            "phase": "prepare",
            "run_id": "valid-run",
            "app_id": "incident",
            "workflow": {
                "initial_pause_reason": "approval_required",
                "decision_pause_reason": "resume_required",
                "explicit_resume": True,
            },
            "reconnect_after_seq": 10,
            "latest_seq": 11,
            "event_count": 11,
            "event_types": list(container_acceptance.EXPECTED_EVENT_TYPES[:11]),
            "events_sha256": "0" * 64,
            "projection_sha256": "1" * 64,
            "effect": {"tool_name": "record_action", "completed_count": 0},
        }
        with self.assertRaisesRegex(
            container_acceptance.AcceptanceError, "workflow"
        ):
            container_acceptance._validated_prepared_evidence(prepared)


if __name__ == "__main__":
    unittest.main()
