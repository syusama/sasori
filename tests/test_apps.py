import asyncio
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
from sasori_context import BoundedContextModel, SemanticCompactionModel  # noqa: E402
from sasori_apps._shared import (  # noqa: E402
    AppConfigurationError,
    PromptedModel,
    configured_model,
    configured_model_and_timeout,
)
from sasori_apps.developer import (  # noqa: E402
    SYSTEM_PROMPT as DEVELOPER_PROMPT,
    create_harness as create_developer_harness,
    developer_harness,
)
from sasori_apps.registry import application_catalog  # noqa: E402
from sasori_apps.research import (  # noqa: E402
    SYSTEM_PROMPT as RESEARCH_PROMPT,
    research_harness,
)
from sasori_apps.workflow_incident import (  # noqa: E402
    APP_ID as WORKFLOW_INCIDENT_ID,
    APP_METADATA as WORKFLOW_INCIDENT_METADATA,
    WORKFLOW_SPEC as INCIDENT_WORKFLOW_SPEC,
    create_harness as create_workflow_harness,
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
        self.assertEqual(
            [item["id"] for item in catalog],
            ["incident", "research", "developer", WORKFLOW_INCIDENT_ID],
        )
        for item in catalog:
            self.assertTrue(item["title"])
            self.assertTrue(item["description"])
            self.assertTrue(item["worker"]["tool_names"])
            self.assertTrue(item["skills"])
        workflow = next(item for item in catalog if item["id"] == WORKFLOW_INCIDENT_ID)
        self.assertEqual(workflow["workflow"], WORKFLOW_INCIDENT_METADATA["workflow"])
        self.assertEqual(
            [step["recovery_policy"] for step in workflow["workflow"]["steps"]],
            [
                "read_only_replay_allowed",
                "manual_effect_resolution_on_ambiguity",
            ],
        )
        catalog[0]["title"] = "changed"
        self.assertEqual(application_catalog()[0]["title"], "Incident Chamber")

    async def test_typed_incident_workflow_uses_the_adapter_run_path(self):
        action_log = self.root / "workflow-actions.jsonl"
        with patch.dict(
            os.environ, {"SASORI_ACTION_LOG": str(action_log)}, clear=False
        ):
            harness = create_workflow_harness(
                self.store("typed-workflow.sqlite3"), app_id=WORKFLOW_INCIDENT_ID
            )
            with self.assertRaises(RunPaused) as paused:
                await harness.run(
                    (Message("user", "checkout latency is high"),),
                    run_id="TypedWorkflowApp",
                    app_id=WORKFLOW_INCIDENT_ID,
                )
            request = paused.exception.request
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.arguments["step_id"], "record")
            self.assertEqual(
                request.arguments["definition_sha256"],
                INCIDENT_WORKFLOW_SPEC.digest,
            )
            self.assertFalse(action_log.exists())
            harness.resolve_approval(
                "TypedWorkflowApp", request.fingerprint, True
            )
            self.assertFalse(action_log.exists())
            result = await harness.resume("TypedWorkflowApp")
            self.assertEqual(
                json.loads(result.final_message.content)["status"], "succeeded"
            )
            self.assertEqual(len(action_log.read_text("utf-8").splitlines()), 1)
            projection = harness.projection("TypedWorkflowApp")
            self.assertEqual(
                [step["status"] for step in projection["steps"]],
                ["completed", "completed"],
            )

    def test_provider_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AppConfigurationError):
                configured_model()

    def test_provider_application_context_configuration_is_explicit(self):
        primary = RecordingModel()
        with patch.dict(
            os.environ,
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_CONTEXT_MAX_UNITS": "1000",
                "SASORI_CONTEXT_RESERVE_UNITS": "100",
                "SASORI_CONTEXT_HOT_TURNS": "3",
            },
            clear=True,
        ), patch(
            "sasori_apps._shared.OpenAIResponsesModel", return_value=primary
        ):
            configured, harness_timeout = configured_model_and_timeout()

        self.assertIsInstance(configured, BoundedContextModel)
        self.assertIs(configured.model, primary)
        self.assertEqual(configured.projector.budget.max_units, 1000)
        self.assertEqual(configured.projector.budget.reserve_units, 100)
        self.assertEqual(configured.projector.budget.hot_turns, 3)
        self.assertEqual(harness_timeout, 65)

    def test_provider_application_semantic_compaction_uses_named_model(self):
        primary = RecordingModel()
        summarizer = RecordingModel()
        with patch.dict(
            os.environ,
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_PROVIDER_BASE_URL": "https://gateway.example/v1",
                "SASORI_PROVIDER_TIMEOUT": "40",
                "SASORI_CONTEXT_MAX_UNITS": "10000",
                "SASORI_COMPACTION_MODEL": "summary-model",
                "SASORI_COMPACTION_TIMEOUT": "12",
                "SASORI_COMPACTION_MAX_SOURCE_BYTES": "20000",
                "SASORI_COMPACTION_MAX_SUMMARY_BYTES": "2000",
                "SASORI_COMPACTION_CACHE_ENTRIES": "7",
                "SASORI_COMPACTION_DIAGNOSTIC_ENTRIES": "9",
            },
            clear=True,
        ), patch(
            "sasori_apps._shared.OpenAIResponsesModel",
            side_effect=(primary, summarizer),
        ) as factory:
            configured, harness_timeout = configured_model_and_timeout()

        self.assertIsInstance(configured, SemanticCompactionModel)
        self.assertIs(configured.model, primary)
        self.assertIs(configured.summarizer, summarizer)
        self.assertRegex(configured.summarizer_name, r"^openai:[0-9a-f]{64}$")
        self.assertNotIn("summary-model", configured.summarizer_name)
        self.assertNotIn("gateway.example", configured.summarizer_name)
        self.assertEqual(configured.policy.timeout_seconds, 12)
        self.assertEqual(configured.policy.max_source_bytes, 20000)
        self.assertEqual(configured.policy.max_summary_bytes, 2000)
        self.assertEqual(configured.policy.cache_entries, 7)
        self.assertEqual(configured.policy.diagnostic_entries, 9)
        self.assertEqual(harness_timeout, 57)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(
            factory.call_args_list[1].kwargs,
            {
                "timeout": 13.2,
                "allow_localhost": False,
                "base_url": "https://gateway.example/v1",
            },
        )

    def test_compaction_endpoint_changes_summarizer_cache_identity(self):
        identities = []
        for endpoint in ("https://one.example/v1", "https://two.example/v1"):
            with patch.dict(
                os.environ,
                {
                    "SASORI_PROVIDER": "openai",
                    "SASORI_MODEL": "runtime-model",
                    "SASORI_CONTEXT_MAX_UNITS": "1000",
                    "SASORI_COMPACTION_MODEL": "summary-model",
                    "SASORI_COMPACTION_BASE_URL": endpoint,
                },
                clear=True,
            ), patch(
                "sasori_apps._shared.OpenAIResponsesModel",
                side_effect=(RecordingModel(), RecordingModel()),
            ):
                configured = configured_model()
            self.assertIsInstance(configured, SemanticCompactionModel)
            identities.append(configured.summarizer_name)

        self.assertNotEqual(identities[0], identities[1])

    async def test_first_party_create_harness_reserves_both_model_stages(self):
        class DelayedSummarizer:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                await asyncio.sleep(0.03)
                envelope = json.loads(messages[1].content)
                return ModelReply(
                    content=json.dumps(
                        {
                            "version": 1,
                            "source_sha256": envelope["source_sha256"],
                            "summary": "first-party delayed summary",
                        }
                    )
                )

        class DelayedPrimary:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                await asyncio.sleep(0.03)
                return ModelReply(content="both model stages completed")

        primary = DelayedPrimary()
        summarizer = DelayedSummarizer()
        with patch.dict(
            os.environ,
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_PROVIDER_TIMEOUT": "0.08",
                "SASORI_CONTEXT_MAX_UNITS": "3000",
                "SASORI_CONTEXT_HOT_TURNS": "1",
                "SASORI_COMPACTION_MODEL": "summary-model",
                "SASORI_COMPACTION_TIMEOUT": "0.08",
                "SASORI_WORKSPACE_ROOT": str(Path.cwd()),
                "SASORI_GIT_ROOT": str(Path.cwd()),
                "PATH": os.environ.get("PATH", ""),
            },
            clear=True,
        ), patch(
            "sasori_apps._shared.OpenAIResponsesModel",
            side_effect=(primary, summarizer),
        ):
            harness = create_developer_harness(self.store("deadline.sqlite3"))

        self.assertAlmostEqual(harness.model_timeout, 5.16)
        result = await harness.run(
            (
                Message("user", "old context " + "x" * 5000),
                Message("assistant", "old answer"),
                Message("user", "current request"),
            ),
            run_id="first-party-deadline",
        )

        self.assertEqual(result.final_message.content, "both model stages completed")
        self.assertEqual((summarizer.calls, primary.calls), (1, 1))
        self.assertEqual(
            [event.type for event in result.events],
            ["run.started", "model.started", "model.completed", "run.completed"],
        )

    def test_orphan_or_invalid_context_configuration_fails_closed(self):
        cases = (
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_COMPACTION_MODEL": "summary-model",
            },
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_CONTEXT_MAX_UNITS": "100",
                "SASORI_CONTEXT_RESERVE_UNITS": "100",
            },
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_CONTEXT_MAX_UNITS": "100",
                "SASORI_COMPACTION_PROVIDER": "anthropic",
            },
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_CONTEXT_MAX_UNITS": "100",
                "SASORI_COMPACTION_TIMEOUT": "10",
            },
            {
                "SASORI_PROVIDER": "openai",
                "SASORI_MODEL": "runtime-model",
                "SASORI_CONTEXT_MAX_UNITS": "100",
                "SASORI_COMPACTION_MODEL": "summary-model",
                "SASORI_COMPACTION_PROVIDER": "unknown",
            },
        )
        for environment in cases:
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ), patch(
                "sasori_apps._shared.OpenAIResponsesModel",
                return_value=RecordingModel(),
            ):
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
