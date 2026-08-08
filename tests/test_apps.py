import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Message, ModelReply, RunPaused, SQLiteStore, ToolCall  # noqa: E402
from sasori.projection import event_projection  # noqa: E402
from sasori_apps._shared import (  # noqa: E402
    AppConfigurationError,
    PromptedModel,
    configured_model,
)
from sasori_apps.developer import (  # noqa: E402
    SYSTEM_PROMPT as DEVELOPER_PROMPT,
    developer_harness,
)
from sasori_apps.registry import application_catalog  # noqa: E402
from sasori_apps.research import (  # noqa: E402
    SYSTEM_PROMPT as RESEARCH_PROMPT,
    research_harness,
)


class RecordingModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tuple(tool.name for tool in tools)))
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self, name):
        store = SQLiteStore(self.root / name)
        self.addCleanup(store.close)
        return store

    @staticmethod
    def public_events(harness, run_id):
        return [event_projection(item) for item in harness.stored_events(run_id)]

    async def test_research_indexes_after_approval_then_returns_citations(self):
        arguments = {
            "collection": "docs",
            "source_id": "source-1",
            "text": "Sasori keeps its core loop small and auditable.",
        }

        def search_after_index(messages):
            indexed = json.loads(messages[-1].content)
            self.assertEqual(indexed["source_revision"], 1)
            return ModelReply(
                tool_calls=(
                    ToolCall(
                        "search-1",
                        "search_documents",
                        {"collection": "docs", "query": "auditable", "limit": 5},
                    ),
                )
            )

        def final_with_citations(messages):
            evidence = json.loads(messages[-1].content)
            match = evidence["matches"][0]
            citation = {
                key: match[key]
                for key in (
                    "source_id",
                    "chunk_id",
                    "start",
                    "end",
                    "source_revision",
                    "source_sha256",
                    "chunk_sha256",
                )
            }
            return ModelReply(
                content=json.dumps(
                    {"answer": match["text"], "citations": [citation]},
                    sort_keys=True,
                )
            )

        model = RecordingModel(
            ModelReply(
                tool_calls=(ToolCall("index-1", "index_text", arguments),)
            ),
            search_after_index,
            final_with_citations,
        )
        store = self.store("research-runs.sqlite3")
        rag_path = self.root / "research-rag.sqlite3"
        harness = research_harness(
            store,
            model,
            allowed_hosts=(),
            rag_database=rag_path,
        )
        self.assertIs(harness.store, store)

        with self.assertRaises(RunPaused) as paused:
            await harness.run(
                (Message("user", "Index and find the Sasori core claim."),),
                run_id="research-app",
            )
        self.assertEqual(paused.exception.request.tool_name, "index_text")
        self.assertEqual(paused.exception.request.effect, "idempotent")
        self.assertFalse(rag_path.exists())

        harness.resolve_approval(
            "research-app", paused.exception.request.fingerprint, True
        )
        result = await harness.resume("research-app")
        final = json.loads(result.final_message.content)
        self.assertIn("auditable", final["answer"])
        self.assertRegex(final["citations"][0]["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(final["citations"][0]["chunk_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            model.calls[0][1],
            ("fetch_url", "index_text", "search_documents"),
        )
        self.assertTrue(
            all(call[0][0] == Message("system", RESEARCH_PROMPT) for call in model.calls)
        )
        self.assertNotIn("system", [message.role for message in result.messages])
        public = json.dumps(self.public_events(harness, "research-app"))
        self.assertNotIn(RESEARCH_PROMPT, public)
        self.assertEqual(
            (await harness.resume("research-app")).final_message,
            result.final_message,
        )

    async def test_developer_reads_writes_after_approval_and_inspects_git(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is unavailable")
        workspace = self.root / "workspace"
        workspace.mkdir()
        note = workspace / "note.txt"
        note.write_bytes(b"before\n")

        def run_git(*arguments):
            return subprocess.run(
                (git, "-C", str(workspace), *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        run_git("init", "--quiet")
        run_git("config", "user.name", "Sasori Test")
        run_git("config", "user.email", "sasori@example.invalid")
        run_git("add", "--", "note.txt")
        run_git("commit", "--quiet", "-m", "initial")

        def request_write(messages):
            self.assertEqual(messages[-1].content, "before\n")
            return ModelReply(
                tool_calls=(
                    ToolCall(
                        "write-1",
                        "write_text",
                        {"path": "note.txt", "content": "after\n"},
                    ),
                )
            )

        def request_status(messages):
            self.assertEqual(json.loads(messages[-1].content)["path"], "note.txt")
            return ModelReply(
                tool_calls=(ToolCall("status-1", "git_status", {}),)
            )

        def finish(messages):
            status = json.loads(messages[-1].content)
            self.assertIn("note.txt", status["porcelain"])
            return ModelReply(content="Workspace updated and Git status inspected.")

        model = RecordingModel(
            ModelReply(
                tool_calls=(
                    ToolCall("read-1", "read_text", {"path": "note.txt"}),
                )
            ),
            request_write,
            request_status,
            finish,
        )
        store = self.store("developer-runs.sqlite3")
        harness = developer_harness(
            store,
            model,
            workspace_root=workspace,
            git_root=workspace,
        )
        self.assertIs(harness.store, store)

        with self.assertRaises(RunPaused) as paused:
            await harness.run(
                (Message("user", "Update note.txt and inspect the result."),),
                run_id="developer-app",
            )
        self.assertEqual(paused.exception.request.tool_name, "write_text")
        self.assertEqual(note.read_text("utf-8"), "before\n")

        harness.resolve_approval(
            "developer-app", paused.exception.request.fingerprint, True
        )
        result = await harness.resume("developer-app")
        self.assertEqual(note.read_text("utf-8"), "after\n")
        self.assertEqual(
            result.final_message.content,
            "Workspace updated and Git status inspected.",
        )
        self.assertTrue(
            all(call[0][0] == Message("system", DEVELOPER_PROMPT) for call in model.calls)
        )
        self.assertNotIn("system", [message.role for message in result.messages])
        self.assertNotIn(
            DEVELOPER_PROMPT,
            json.dumps(self.public_events(harness, "developer-app")),
        )
        await harness.resume("developer-app")
        self.assertEqual(note.read_text("utf-8"), "after\n")

    def test_catalog_is_json_safe_complete_and_detached(self):
        catalog = application_catalog()
        encoded = json.dumps(catalog, ensure_ascii=False)
        self.assertNotIn("system_prompt", encoded)
        self.assertEqual([item["id"] for item in catalog], ["incident", "research", "developer"])
        for item in catalog:
            self.assertTrue(item["title"])
            self.assertTrue(item["description"])
            self.assertTrue(item["worker"]["tool_names"])
            self.assertTrue(item["skills"])
        catalog[0]["title"] = "changed"
        self.assertEqual(application_catalog()[0]["title"], "Incident Chamber")

    def test_provider_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AppConfigurationError):
                configured_model()

    async def test_application_policy_precedes_a_caller_system_message(self):
        model = RecordingModel(ModelReply(content="done"))
        prompted = PromptedModel(model, "application policy")
        await prompted.complete(
            (Message("system", "caller policy"), Message("user", "hello")),
            (),
        )
        self.assertEqual(
            model.calls[0][0],
            (
                Message("system", "application policy"),
                Message("system", "caller policy"),
                Message("user", "hello"),
            ),
        )
        with patch.dict(
            os.environ,
            {"SASORI_PROVIDER": "openai", "SASORI_MODEL": "gpt-test", "SASORI_PROVIDER_TIMEOUT": "nan"},
            clear=True,
        ):
            with self.assertRaises(AppConfigurationError):
                configured_model()


if __name__ == "__main__":
    unittest.main()
