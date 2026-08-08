import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, ModelReply, RunPaused, ToolCall  # noqa: E402
from sasori.plugins import validate_registration  # noqa: E402
from sasori_plugins.git import (  # noqa: E402
    GitOutputLimitError,
    GitValidationError,
    git_manifest,
    git_registration,
)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class GitPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        executable = shutil.which("git")
        if executable is None:
            self.skipTest("Git is unavailable")
        self.git = executable
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_git("init", "--quiet")
        self.run_git("config", "user.name", "Sasori Test")
        self.run_git("config", "user.email", "sasori@example.invalid")
        (self.root / "tracked.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / "-danger.txt").write_text("safe\n", encoding="utf-8")
        self.run_git("add", "--all")
        self.run_git("commit", "--quiet", "-m", "initial")
        self.registration = git_registration(self.root)
        self.tools = {tool.name: tool for tool in self.registration.tools}

    def run_git(self, *arguments, check=True):
        return subprocess.run(
            (self.git, "-C", str(self.root), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def snapshot(self):
        return self.tools["git_status"].handler()["snapshot"]

    def test_manifest_registration_and_read_tools_are_exact(self):
        manifest = git_manifest()
        self.assertIs(validate_registration(manifest, self.registration), self.registration)
        self.assertEqual(manifest.execution.entry_point_value, "sasori_plugins.git:register")
        self.assertEqual(manifest.permissions.network_egress, ())
        self.assertEqual(manifest.permissions.secrets, ())
        self.assertEqual(manifest.permissions.host_process, ("git:local-repository",))
        self.assertEqual(
            {tool.name: (tool.effect, tool.tool_revision) for tool in self.registration.tools},
            {
                "git_status": ("read_only", None),
                "git_diff": ("read_only", None),
                "git_log": ("read_only", None),
                "git_show": ("read_only", None),
                "git_stage": ("side_effecting", "1"),
                "git_commit": ("side_effecting", "1"),
            },
        )

        (self.root / "tracked.txt").write_text("beta\n", encoding="utf-8")
        (self.root / "-danger.txt").write_text("changed\n", encoding="utf-8")
        status = self.tools["git_status"].handler()
        self.assertRegex(status["snapshot"], r"^[0-9a-f]{64}$")
        self.assertIn("tracked.txt", status["porcelain"])
        self.assertIn("beta", self.tools["git_diff"].handler("tracked.txt")["patch"])
        self.assertIn("changed", self.tools["git_diff"].handler("-danger.txt")["patch"])
        log = self.tools["git_log"].handler(1)["log"]
        head = self.run_git("rev-parse", "HEAD").stdout.decode().strip()
        self.assertIn(head, log)
        self.assertIn("alpha", self.tools["git_show"].handler(head, "tracked.txt")["content"])

    def test_paths_revisions_and_output_are_bounded(self):
        for value in ("../outside", "/absolute", "C:/drive", "a\\b", ".git/config", "a//b"):
            with self.subTest(value=value), self.assertRaises(GitValidationError):
                self.tools["git_diff"].handler(value)
        for value in ("HEAD", "-p", "A" * 40, "0" * 39):
            with self.subTest(value=value), self.assertRaises(GitValidationError):
                self.tools["git_show"].handler(value, "tracked.txt")
        with self.assertRaises(GitValidationError):
            self.tools["git_log"].handler(True)

        (self.root / "tracked.txt").write_text("x" * (600 * 1024), encoding="utf-8")
        with self.assertRaises(GitOutputLimitError):
            self.tools["git_diff"].handler("tracked.txt")

        (self.root / ".env").write_text("TEST_TOKEN=never-show\n", encoding="utf-8")
        with self.assertRaisesRegex(GitValidationError, "sensitive"):
            self.tools["git_diff"].handler(".env")

    async def test_stage_requires_approval_and_completed_resume_never_repeats(self):
        path = self.root / "new.txt"
        path.write_text("new\n", encoding="utf-8")
        expected = self.snapshot()
        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("stage-1", "git_stage", {"paths": ["new.txt"], "expected_snapshot": expected}),)),
            ModelReply(content="staged"),
        )
        harness = Harness(model, self.registration.tools)
        self.addCleanup(harness.close)
        with self.assertRaises(RunPaused) as caught:
            await harness.run((Message("user", "stage new.txt"),), run_id="git-stage")
        request = caught.exception.request
        self.assertIsNotNone(request)
        self.assertEqual(self.run_git("diff", "--cached", "--name-only").stdout, b"")

        harness.resolve_approval("git-stage", request.fingerprint, True)
        result = await harness.resume("git-stage")
        self.assertEqual(result.final_message.content, "staged")
        self.assertEqual(
            self.run_git("diff", "--cached", "--name-only").stdout.decode().strip(),
            "new.txt",
        )
        resumed = await harness.resume("git-stage")
        self.assertEqual(resumed.final_message, result.final_message)

    async def test_denial_and_stale_snapshot_never_stage(self):
        denied_path = self.root / "denied.txt"
        denied_path.write_text("no\n", encoding="utf-8")
        expected = self.snapshot()
        denied_model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("deny-1", "git_stage", {"paths": ["denied.txt"], "expected_snapshot": expected}),)),
            ModelReply(content="denied"),
        )
        denied = Harness(denied_model, self.registration.tools)
        self.addCleanup(denied.close)
        with self.assertRaises(RunPaused) as caught:
            await denied.run((Message("user", "do not stage"),), run_id="git-deny")
        request = caught.exception.request
        self.assertIsNotNone(request)
        denied.resolve_approval("git-deny", request.fingerprint, False)
        await denied.resume("git-deny")
        self.assertNotIn("denied.txt", self.run_git("diff", "--cached", "--name-only").stdout.decode())

        stale_path = self.root / "stale.txt"
        stale_path.write_text("first\n", encoding="utf-8")
        expected = self.snapshot()
        stale_model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("stale-1", "git_stage", {"paths": ["stale.txt"], "expected_snapshot": expected}),)),
            lambda messages: ModelReply(content=messages[-1].content),
        )
        stale = Harness(stale_model, self.registration.tools)
        self.addCleanup(stale.close)
        with self.assertRaises(RunPaused) as caught:
            await stale.run((Message("user", "stage stale"),), run_id="git-stale")
        request = caught.exception.request
        self.assertIsNotNone(request)
        stale_path.write_text("changed after approval request\n", encoding="utf-8")
        stale.resolve_approval("git-stale", request.fingerprint, True)
        result = await stale.resume("git-stale")
        self.assertEqual(json.loads(result.final_message.content)["outcome"], "stale_snapshot")
        self.assertNotIn("stale.txt", self.run_git("diff", "--cached", "--name-only").stdout.decode())

    async def test_commit_requires_approval_and_is_verified_once(self):
        marker = self.root / "hook-ran"
        hook = self.root / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\nprintf ran > '{marker.as_posix()}'\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.root / "tracked.txt").write_text("committed\n", encoding="utf-8")
        self.run_git("add", "--", "tracked.txt")
        expected = self.snapshot()
        before = self.run_git("rev-list", "--count", "HEAD").stdout
        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("commit-1", "git_commit", {"message": "sasori test commit", "expected_snapshot": expected}),)),
            ModelReply(content="committed"),
        )
        harness = Harness(model, self.registration.tools)
        self.addCleanup(harness.close)
        with self.assertRaises(RunPaused) as caught:
            await harness.run((Message("user", "commit"),), run_id="git-commit")
        request = caught.exception.request
        self.assertIsNotNone(request)
        self.assertEqual(self.run_git("rev-list", "--count", "HEAD").stdout, before)
        harness.resolve_approval("git-commit", request.fingerprint, True)
        await harness.resume("git-commit")
        after = self.run_git("rev-list", "--count", "HEAD").stdout
        self.assertEqual(int(after), int(before) + 1)
        await harness.resume("git-commit")
        self.assertEqual(self.run_git("rev-list", "--count", "HEAD").stdout, after)
        self.assertEqual(
            self.run_git("log", "-1", "--format=%s").stdout.decode().strip(),
            "sasori test commit",
        )
        self.assertFalse(marker.exists())

    def test_filters_and_sensitive_staged_files_fail_before_mutation(self):
        (self.root / ".gitattributes").write_text("filtered.txt filter=demo\n", encoding="utf-8")
        (self.root / "filtered.txt").write_text("filtered\n", encoding="utf-8")
        expected = self.snapshot()
        result = self.tools["git_stage"].handler(["filtered.txt"], expected)
        self.assertEqual(result["outcome"], "unsupported_filter")
        self.assertNotIn("filtered.txt", self.run_git("diff", "--cached", "--name-only").stdout.decode())

        (self.root / ".env").write_text("TEST_TOKEN=never-show\n", encoding="utf-8")
        self.run_git("add", "--", ".env")
        expected = self.snapshot()
        head = self.run_git("rev-parse", "HEAD").stdout
        result = self.tools["git_commit"].handler("must not commit secret", expected)
        self.assertEqual(result["outcome"], "sensitive_path")
        self.assertEqual(self.run_git("rev-parse", "HEAD").stdout, head)

    @unittest.skipIf(os.name == "nt", "Git executable-bit changes are POSIX-only")
    def test_executable_bit_change_invalidates_approved_snapshot(self):
        path = self.root / "tracked.txt"
        path.write_text("changed content\n", encoding="utf-8")
        expected = self.snapshot()
        path.chmod(path.stat().st_mode | 0o100)
        result = self.tools["git_stage"].handler(["tracked.txt"], expected)
        self.assertEqual(result["outcome"], "stale_snapshot")
        self.assertEqual(self.run_git("diff", "--cached", "--name-only").stdout, b"")


if __name__ == "__main__":
    unittest.main()
