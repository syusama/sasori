import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, ModelReply, Tool, ToolCall  # noqa: E402
from sasori.cli import run_cli  # noqa: E402
from sasori_apps.workflow_incident import APP_ID, WORKFLOW_SPEC  # noqa: E402
from sasori_flow import json_sha256  # noqa: E402


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        return self.replies.pop(0)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "cli.sqlite3")
        self.module = types.ModuleType("sasori_cli_test_app")
        sys.modules[self.module.__name__] = self.module

    def tearDown(self):
        sys.modules.pop(self.module.__name__, None)
        self.temp.cleanup()

    def call(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_cli(("--db", self.db, "--json", *arguments))
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return code, lines, stderr.getvalue()

    def test_run_status_and_events_share_one_json_projection(self):
        self.module.create = lambda store: Harness(
            ScriptedModel(ModelReply(content="done", provider_state='{"private":true}')),
            store=store,
        )
        code, output, errors = self.call(
            "--app", "sasori_cli_test_app:create", "run", "hello", "--run-id", "cli-1"
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output[0]["state"], "completed")
        self.assertNotIn("private", json.dumps(output))
        self.assertNotIn("workflow", output[0])

        code, status, _ = self.call("status", "cli-1")
        self.assertEqual(code, 0)
        self.assertEqual(status[0], output[0])
        code, events, _ = self.call("events", "cli-1", "--after", "0")
        self.assertEqual(code, 0)
        self.assertEqual([item["seq"] for item in events], list(range(1, len(events) + 1)))

    def test_cli_ignores_legacy_projection_override(self):
        class LegacyHarness(Harness):
            def public_run_projection(self, run_id):
                return {
                    "run_id": "forged",
                    "app_id": "forged.app",
                    "state": "completed",
                }

        self.module.create = lambda store: LegacyHarness(
            ScriptedModel(ModelReply(content="done")), store=store
        )
        code, output, errors = self.call(
            "--app",
            "sasori_cli_test_app:create",
            "run",
            "hello",
            "--run-id",
            "cli-core-owned",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output[0]["run_id"], "cli-core-owned")
        self.assertEqual(output[0]["state"], "completed")
        self.assertNotIn("workflow", output[0])

    def test_cli_projection_extension_failure_is_stable_and_redacted(self):
        private = "private transcript and arguments"

        class MalformedHarness(Harness):
            def public_projection_extension(self, run_id):
                raise RuntimeError(private)

        self.module.create = lambda store: MalformedHarness(
            ScriptedModel(ModelReply(content="done")), store=store
        )
        code, output, errors = self.call(
            "--app",
            "sasori_cli_test_app:create",
            "run",
            "hello",
            "--run-id",
            "cli-projection-failed",
        )
        self.assertEqual((code, errors), (5, ""))
        self.assertEqual(
            output,
            [{
                "ok": False,
                "error": {
                    "code": "projection_integrity_failed",
                    "message": "public projection extension failed integrity validation",
                },
            }],
        )
        self.assertNotIn(private, json.dumps(output))

    def test_approval_is_explicit_and_resume_uses_the_same_harness(self):
        class ApprovalModel:
            async def complete(self, messages, tools):
                if messages[-1].role == "tool":
                    return ModelReply(content="written")
                return ModelReply(
                    tool_calls=(ToolCall("write-1", "write", {"value": 7}),)
                )

        def create(store):
            return Harness(
                ApprovalModel(),
                (Tool("write", lambda value: value, tool_revision="1"),),
                store=store,
            )

        self.module.create = create
        app = ("--app", "sasori_cli_test_app:create")
        code, paused, _ = self.call(*app, "run", "write", "--run-id", "cli-approval")
        self.assertEqual(code, 3)
        fingerprint = paused[0]["pending"]["fingerprint"]
        code, decided, _ = self.call(
            *app, "approval", "cli-approval", fingerprint, "--approve"
        )
        self.assertEqual(code, 0)
        self.assertEqual(decided[0]["detail"], "awaiting_resume")
        code, completed, _ = self.call(*app, "resume", "cli-approval")
        self.assertEqual(code, 0)
        self.assertEqual(completed[0]["final_message"]["content"], "written")

    def test_invalid_input_is_code_two_and_does_not_create_a_run(self):
        self.module.create = lambda store: Harness(
            ScriptedModel(ModelReply(content="unused")), store=store
        )
        code, output, _ = self.call(
            "--app", "sasori_cli_test_app:create", "run", "hello", "--run-id", "bad space"
        )
        self.assertEqual(code, 2)
        self.assertEqual(output[0]["error"]["code"], "invalid_input")

    def test_typed_workflow_approval_resume_and_reopen_do_not_replay(self):
        action_log = Path(self.temp.name) / "workflow-actions.jsonl"
        app = ("--app", "sasori_apps.workflow_incident:create_harness")

        with mock.patch.dict(
            os.environ, {"SASORI_ACTION_LOG": str(action_log)}, clear=False
        ):
            code, paused, errors = self.call(
                *app,
                "run",
                "checkout latency",
                "--run-id",
                "cli-workflow",
            )
            self.assertEqual((code, errors), (3, ""))
            self.assertEqual(paused[0]["state"], "paused")
            self.assertEqual(paused[0]["pause_reason"], "approval_required")
            self.assertEqual(paused[0]["detail"], "awaiting_approval")
            self.assertEqual(paused[0]["app_id"], APP_ID)
            self.assertEqual(paused[0]["input"], "checkout latency")
            self.assertEqual(
                [step["status"] for step in paused[0]["workflow"]["steps"]],
                ["completed", "approval_required"],
            )
            self.assertEqual(
                paused[0]["workflow"]["definition_sha256"], WORKFLOW_SPEC.digest
            )
            self.assertEqual(paused[0]["pending"]["effect"], "side_effecting")
            self.assertEqual(paused[0]["pending"]["arguments"]["step_id"], "record")
            self.assertEqual(
                paused[0]["pending"]["arguments"]["definition_sha256"],
                WORKFLOW_SPEC.digest,
            )
            self.assertEqual(
                json.loads(paused[0]["pending"]["arguments"]["payload_json"]),
                {"summary": "diagnostic captured for checkout latency"},
            )
            self.assertFalse(action_log.exists())

            fingerprint = paused[0]["pending"]["fingerprint"]
            code, decided, errors = self.call(
                *app,
                "approval",
                "cli-workflow",
                fingerprint,
                "--approve",
            )
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(decided[0]["state"], "paused")
            self.assertEqual(decided[0]["detail"], "awaiting_resume")
            self.assertEqual(decided[0]["pause_reason"], "resume_required")
            self.assertEqual(
                decided[0]["workflow"]["steps"][1]["status"], "resume_required"
            )
            self.assertFalse(action_log.exists())

            code, completed, errors = self.call(*app, "resume", "cli-workflow")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(completed[0]["state"], "completed")
            self.assertEqual(
                [step["status"] for step in completed[0]["workflow"]["steps"]],
                ["completed", "completed"],
            )
            self.assertIsNone(completed[0]["workflow"]["current_step_id"])
            final = json.loads(completed[0]["final_message"]["content"])
            self.assertEqual(final["workflow_id"], "incident-mechanism")
            self.assertEqual(final["workflow_version"], "1")
            self.assertEqual(final["definition_sha256"], WORKFLOW_SPEC.digest)
            self.assertEqual(final["status"], "succeeded")
            self.assertEqual(final["output"]["step_id"], "record")
            self.assertEqual(
                final["output"]["value"],
                "diagnostic captured for checkout latency",
            )
            self.assertEqual(
                final["output"]["value_sha256"],
                json_sha256(final["output"]["value"]),
            )

            actions = [
                json.loads(line)
                for line in action_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                actions,
                [{"summary": "diagnostic captured for checkout latency"}],
            )

            code, reopened, errors = self.call(*app, "resume", "cli-workflow")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(reopened[0], completed[0])
            self.assertEqual(
                action_log.read_text(encoding="utf-8").splitlines(),
                [json.dumps(actions[0], ensure_ascii=False)],
            )

            code, status, _ = self.call("status", "cli-workflow")
            self.assertEqual(code, 0)
            self.assertNotIn("workflow", status[0])
            self.assertEqual(
                status[0], {key: value for key, value in completed[0].items() if key != "workflow"}
            )
            code, exact_status, errors = self.call(*app, "status", "cli-workflow")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(exact_status[0], completed[0])
            code, events, _ = self.call("events", "cli-workflow")
            self.assertEqual(code, 0)
            self.assertEqual(
                [item["seq"] for item in events],
                list(range(1, completed[0]["latest_seq"] + 1)),
            )
            self.assertEqual(
                [item["event"]["type"] for item in events].count("run.completed"),
                1,
            )
            event_types = [item["event"]["type"] for item in events]
            for event_type, count in (
                ("run.started", 1),
                ("approval.requested", 1),
                ("approval.resolved", 1),
                ("tool.started", 2),
                ("tool.completed", 2),
            ):
                self.assertEqual(event_types.count(event_type), count)
            self.assertFalse(any(item.startswith("workflow.") for item in event_types))

            code, reopened_events, _ = self.call("events", "cli-workflow")
            self.assertEqual((code, reopened_events), (0, events))

    def test_typed_workflow_validation_is_invalid_input_and_creates_no_run(self):
        action_log = Path(self.temp.name) / "invalid-workflow-actions.jsonl"
        app = ("--app", "sasori_apps.workflow_incident:create_harness")
        with mock.patch.dict(
            os.environ, {"SASORI_ACTION_LOG": str(action_log)}, clear=False
        ):
            code, output, errors = self.call(
                *app,
                "run",
                "x" * (16 * 1024 + 1),
                "--run-id",
                "cli-workflow-invalid",
            )
            self.assertEqual((code, errors), (2, ""))
            self.assertEqual(output[0]["error"]["code"], "invalid_input")
            code, missing, errors = self.call("status", "cli-workflow-invalid")
            self.assertEqual((code, errors), (6, ""))
            self.assertEqual(missing[0]["error"]["code"], "runnotfound")
            self.assertFalse(action_log.exists())


if __name__ == "__main__":
    unittest.main()
