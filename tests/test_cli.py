import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, ModelReply, Tool, ToolCall  # noqa: E402
from sasori.cli import run_cli  # noqa: E402


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

        code, status, _ = self.call("status", "cli-1")
        self.assertEqual(code, 0)
        self.assertEqual(status[0], output[0])
        code, events, _ = self.call("events", "cli-1", "--after", "0")
        self.assertEqual(code, 0)
        self.assertEqual([item["seq"] for item in events], list(range(1, len(events) + 1)))

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


if __name__ == "__main__":
    unittest.main()
