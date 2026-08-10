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
sys.modules.setdefault("container_acceptance", container_acceptance)
WORKFLOW_SPEC = importlib.util.spec_from_file_location(
    "sasori_container_workflow_acceptance",
    ROOT / "scripts" / "container_workflow_acceptance.py",
)
container_workflow_acceptance = importlib.util.module_from_spec(WORKFLOW_SPEC)
sys.modules[WORKFLOW_SPEC.name] = container_workflow_acceptance
WORKFLOW_SPEC.loader.exec_module(container_workflow_acceptance)
MEMORY_SPEC = importlib.util.spec_from_file_location(
    "sasori_container_memory_acceptance",
    ROOT / "scripts" / "container_memory_acceptance.py",
)
container_memory_acceptance = importlib.util.module_from_spec(MEMORY_SPEC)
sys.modules[MEMORY_SPEC.name] = container_memory_acceptance
MEMORY_SPEC.loader.exec_module(container_memory_acceptance)

from sasori.server import create_server  # noqa: E402


class ContainerAcceptanceTests(unittest.TestCase):
    TOKEN = "container-acceptance-secret-32-bytes"

    def test_compose_secret_is_group_scoped_for_the_non_root_runtime(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'group_add:\n      - "${SASORI_TOKEN_GID:-10001}"', compose
        )
        self.assertIn("SASORI_ARTIFACT_ROOT: /data/artifacts", compose)
        self.assertIn('SASORI_PUBLISH_FINAL_ARTIFACT: "1"', compose)
        self.assertIn("os.O_WRONLY | os.O_CREAT | os.O_EXCL", workflow)
        self.assertIn(
            "os.O_WRONLY | os.O_CREAT | os.O_EXCL,\n              0o600,",
            workflow,
        )
        self.assertIn("os.fchmod(stream.fileno(), 0o640)", workflow)
        self.assertIn("stat.S_IMODE(token_stat.st_mode) != 0o640", workflow)
        self.assertIn('"SASORI_TOKEN_GID": str(token_stat.st_gid)', workflow)
        self.assertNotIn('write_text(token + "\\n"', workflow)
        self.assertNotIn("0o644", workflow)
        self.assertNotIn("os.getgid()", workflow)
        self.assertIn(
            "container could not read the group-scoped bearer-token secret",
            workflow,
        )
        self.assertIn('Path("/data/artifacts/blobs/sha256")', workflow)
        self.assertIn("tampered = bytes([content[0] ^ 1]) + content[1:]", workflow)
        self.assertIn("scripts/container_acceptance.py tamper-check", workflow)
        self.assertIn("scripts/container_memory_acceptance.py", workflow)
        self.assertIn('python - prepare \\', workflow)
        self.assertIn('python - after-restart \\', workflow)
        self.assertIn("container Memory restart evidence is inconsistent", workflow)
        self.assertIn("sasori-memory-prepared-${{ github.run_id }}", workflow)
        self.assertIn("sasori-memory-restarted-${{ github.run_id }}", workflow)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            '"--app", "sasori_apps.workflow_incident:create_harness"',
            dockerfile,
        )
        self.assertNotRegex(
            dockerfile,
            r"flow\.incident-mechanism\.[0-9a-f]{12}",
        )

    def test_ci_runs_workflow_acceptance_before_shared_restart_gates(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for name in (
            "WORKFLOW_EVIDENCE_FILE",
            "WORKFLOW_RESTARTED_FILE",
            "WORKFLOW_ACTION_PAUSED_FILE",
            "WORKFLOW_ACTION_COMPLETE_FILE",
            "WORKFLOW_ACTION_RESTARTED_FILE",
            "SASORI_WORKFLOW_RUN_ID",
        ):
            self.assertIn(name, workflow)
        self.assertIn(
            'action_snapshot 1 0 "$WORKFLOW_ACTION_PAUSED_FILE"', workflow
        )
        self.assertIn(
            'action_snapshot 1 1 "$WORKFLOW_ACTION_COMPLETE_FILE"', workflow
        )
        self.assertIn(
            'action_snapshot 1 1 "$WORKFLOW_ACTION_RESTARTED_FILE"', workflow
        )
        self.assertIn('> "$WORKFLOW_RESTARTED_FILE"', workflow)
        self.assertIn('if [[ ! -f "$path" ]]', workflow)
        for stem in (
            "sasori-workflow-evidence-${{ github.run_id }}",
            "sasori-workflow-restarted-${{ github.run_id }}",
            "sasori-workflow-action-paused-${{ github.run_id }}",
            "sasori-workflow-action-complete-${{ github.run_id }}",
            "sasori-workflow-action-restarted-${{ github.run_id }}",
        ):
            self.assertIn(stem, workflow)

        incident_complete = workflow.index(
            "python scripts/container_acceptance.py complete"
        )
        workflow_prepare = workflow.index(
            "python scripts/container_workflow_acceptance.py prepare"
        )
        workflow_complete = workflow.index(
            "python scripts/container_workflow_acceptance.py complete"
        )
        memory_prepare = workflow.index(
            "< scripts/container_memory_acceptance.py", workflow_complete
        )
        restart = workflow.index(
            'docker compose -p "$COMPOSE_PROJECT_NAME" restart sasori'
        )
        incident_restarted = workflow.index(
            "python scripts/container_acceptance.py after-restart", restart
        )
        workflow_restarted = workflow.index(
            "python scripts/container_workflow_acceptance.py after-restart",
            incident_restarted,
        )
        memory_restarted = workflow.index(
            "< scripts/container_memory_acceptance.py", workflow_restarted
        )
        second_owner = workflow.index(
            "second owner unexpectedly acquired", memory_restarted
        )
        tamper = workflow.index("tampered = bytes", second_owner)
        sbom = workflow.index("Generate and bind the final image SBOM", tamper)
        audit = workflow.index("Audit logs and generated reports", sbom)
        cleanup = workflow.index(
            "Stop containers and remove sensitive local files", audit
        )
        upload = workflow.index("Upload audited container evidence", cleanup)
        self.assertEqual(
            [
                incident_complete,
                workflow_prepare,
                workflow_complete,
                memory_prepare,
                restart,
                incident_restarted,
                workflow_restarted,
                memory_restarted,
                second_owner,
                tamper,
                sbom,
                audit,
                cleanup,
                upload,
            ],
            sorted(
                [
                    incident_complete,
                    workflow_prepare,
                    workflow_complete,
                    memory_prepare,
                    restart,
                    incident_restarted,
                    workflow_restarted,
                    memory_restarted,
                    second_owner,
                    tamper,
                    sbom,
                    audit,
                    cleanup,
                    upload,
                ]
            ),
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "sasori.sqlite3"
        self.action_log = self.root / "incident-actions.jsonl"
        self.artifact_root = self.root / "artifacts"
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
            artifact_root=self.artifact_root,
            apps={"incident": "sasori_apps.incident:create_harness"},
            token=self.TOKEN,
            publish_final_artifact=True,
            sse_max_seconds=2,
            sse_keepalive_seconds=0.05,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server, f"http://127.0.0.1:{server.server_port}"

    def _start_workflow_sasori(self) -> tuple[object, str]:
        from sasori_apps.workflow_incident import APP_ID

        server = create_server(
            "127.0.0.1",
            0,
            database=self.database,
            artifact_root=self.artifact_root,
            apps={APP_ID: "sasori_apps.workflow_incident:create_harness"},
            token=self.TOKEN,
            publish_final_artifact=True,
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

    def _memory_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = container_memory_acceptance.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def _workflow_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = container_workflow_acceptance.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_workflow_acceptance_approval_resume_and_restart(self) -> None:
        evidence = self.root / "workflow-evidence.json"
        run_id = "ContainerWorkflowAcceptance"
        server, base_url = self._start_workflow_sasori()
        common = [
            "--base-url",
            base_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(evidence),
        ]

        code, stdout, stderr = self._workflow_main(
            ["prepare", *common, "--run-id", run_id]
        )
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(stdout)
        self.assertEqual(prepared["phase"], "prepare")
        self.assertFalse(self.action_log.exists())

        code, stdout, stderr = self._workflow_main(["complete", *common])
        self.assertEqual((code, stderr), (0, ""))
        completed = json.loads(stdout)
        self.assertEqual(completed["phase"], "complete")
        self.assertEqual(
            [
                json.loads(line)
                for line in self.action_log.read_text(encoding="utf-8").splitlines()
            ],
            [
                {
                    "summary": (
                        "diagnostic captured for container typed workflow incident"
                    )
                }
            ],
        )

        self._stop_sasori(server)
        _, restarted_url = self._start_workflow_sasori()
        restarted_common = [
            "--base-url",
            restarted_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(evidence),
        ]
        code, stdout, stderr = self._workflow_main(
            ["after-restart", *restarted_common]
        )
        self.assertEqual((code, stderr), (0, ""))
        restarted = json.loads(stdout)
        self.assertTrue(restarted["verified"])
        self.assertEqual(restarted["events_sha256"], completed["events_sha256"])
        self.assertEqual(len(self.action_log.read_text("utf-8").splitlines()), 1)

    def test_memory_acceptance_write_search_and_fresh_store_restart(self) -> None:
        database = self.root / "memory-acceptance.sqlite3"
        evidence = self.root / "memory-acceptance-evidence.json"
        common = ["--database", str(database), "--evidence", str(evidence)]
        with mock.patch.object(
            container_memory_acceptance.importlib.metadata,
            "version",
            return_value="0.1.0.dev0",
        ):
            code, stdout, stderr = self._memory_main(["prepare", *common])
        self.assertEqual((code, stderr), (0, ""))
        prepared = json.loads(stdout)
        self.assertEqual(prepared["phase"], "prepare")
        self.assertEqual(prepared["revision"], 1)
        self.assertEqual(prepared["run_id"], "RunABC_Container")
        self.assertEqual(prepared["source_call_id"], "call_AbC123_X")
        self.assertEqual(len(prepared["memory_id"]), 64)

        code, stdout, stderr = self._memory_main(["after-restart", *common])
        self.assertEqual((code, stderr), (0, ""))
        verified = json.loads(stdout)
        self.assertTrue(verified["verified"])
        self.assertTrue(verified["run_binding_reloaded"])
        self.assertEqual(verified["memory_id"], prepared["memory_id"])
        self.assertEqual(verified["revision"], prepared["revision"])
        self.assertEqual(
            verified["collection_revision"], prepared["collection_revision"]
        )

    def test_memory_acceptance_evidence_tamper_fails_closed(self) -> None:
        database = self.root / "memory-tamper.sqlite3"
        evidence = self.root / "memory-tamper-evidence.json"
        common = ["--database", str(database), "--evidence", str(evidence)]
        with mock.patch.object(
            container_memory_acceptance.importlib.metadata,
            "version",
            return_value="0.1.0.dev0",
        ):
            self.assertEqual(self._memory_main(["prepare", *common])[0], 0)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["collection_revision"] = 999_999
        evidence.write_bytes(
            (
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        )
        code, stdout, stderr = self._memory_main(["after-restart", *common])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("changed across restart", stderr)

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
        self.assertEqual(stored["event_count"], 17)
        self.assertEqual(stored["latest_seq"], 17)
        self.assertEqual(stored["artifact"]["created_seq"], 17)
        self.assertEqual(stored["effect"]["completed_count"], 1)
        self.assertEqual(
            stored["reconnect"],
            {
                "after_seq": 10,
                "event_count": 7,
                "events_sha256": stored["reconnect"]["events_sha256"],
                "first_seq": 11,
                "last_seq": 17,
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
                "schema_version": 2,
                "kind": "sasori.container-acceptance",
                "phase": "after-restart",
                "run_id": "container-acceptance-1",
                "verified": True,
                "latest_seq": 17,
                "event_count": 17,
                "effect_count": 1,
                "artifact": stored["artifact"],
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

        artifact = stored["artifact"]
        digest = artifact["content_sha256"]
        blob = self.artifact_root / "blobs" / "sha256" / digest[:2] / digest
        content = blob.read_bytes()
        self.assertGreater(len(content), 0)
        blob.chmod(0o600)
        blob.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        tamper = [
            "tamper-check",
            "--base-url",
            restarted_url,
            "--token-file",
            str(self.token_file),
            "--evidence",
            str(self.evidence),
        ]
        code, stdout, stderr = self._main(tamper)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout),
            {
                "schema_version": 2,
                "kind": "sasori.container-acceptance",
                "phase": "tamper-check",
                "run_id": "container-acceptance-1",
                "verified": True,
                "artifact_id": artifact["artifact_id"],
                "content_sha256": digest,
                "size_bytes": artifact["size_bytes"],
                "status": 503,
                "error_code": "artifact_integrity_failed",
            },
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
            "schema_version": 2,
            "kind": "sasori.container-acceptance",
            "phase": "complete",
            "run_id": "valid-run",
            "app_id": "incident",
            "workflow": {
                "initial_pause_reason": "approval_required",
                "decision_pause_reason": "resume_required",
                "explicit_resume": True,
            },
            "latest_seq": 17,
            "event_count": 17,
            "event_types": list(container_acceptance.EXPECTED_EVENT_TYPES),
            "events_sha256": "0" * 64,
            "projection_sha256": "1" * 64,
            "final_message": {
                "role": "assistant",
                "content": container_acceptance.EXPECTED_FINAL_CONTENT,
            },
            "effect": {"tool_name": "record_action", "completed_count": 2},
            "artifact": {
                "artifact_id": "artifact-valid",
                "content_sha256": "3" * 64,
                "size_bytes": 42,
                "filename": "valid-run-result.md",
                "media_type": "text/plain; charset=utf-8",
                "created_seq": 17,
            },
            "reconnect": {
                "after_seq": 10,
                "event_count": 7,
                "first_seq": 11,
                "last_seq": 17,
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
            "schema_version": 2,
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
