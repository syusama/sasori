import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    Harness,
    Message,
    ModelReply,
    RunPaused,
    ToolCall,
)
from sasori.plugins import validate_registration  # noqa: E402
from sasori_plugins.rag_sqlite import (  # noqa: E402
    RAG_DB_ENV,
    RAGConfigurationError,
    RAGDatabaseError,
    RAGIdempotencyConflict,
    RAGUnsupportedError,
    RAGValidationError,
    rag_sqlite_manifest,
    rag_sqlite_registration,
    register,
)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class InjectedFailure(Exception):
    pass


class RAGSQLitePluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "rag.sqlite3"
        self.plugin_registration = rag_sqlite_registration(self.path)
        self.tools = {tool.name: tool for tool in self.plugin_registration.tools}

    def make_registration(self, path=None, **options):
        return rag_sqlite_registration(path or self.path, **options)

    @staticmethod
    def arguments(text="alpha beta gamma"):
        return {"collection": "docs", "source_id": "source-1", "text": text}

    @staticmethod
    def index_with(registration, arguments, *, key=None):
        tool = {tool.name: tool for tool in registration.tools}["index_text"]
        selected_key = tool.idempotency_key(arguments) if key is None else key
        return tool.handler(**arguments, idempotency_key=selected_key), selected_key

    @staticmethod
    def search_with(registration, query, limit=10, collection="docs"):
        tool = {tool.name: tool for tool in registration.tools}["search_documents"]
        return json.loads(tool.handler(collection, query, limit))

    def test_manifest_registration_skill_and_entrypoint_are_exact(self):
        manifest = rag_sqlite_manifest()
        self.assertIs(
            validate_registration(manifest, self.plugin_registration),
            self.plugin_registration,
        )
        self.assertEqual(
            manifest.execution.entry_point_value,
            "sasori_plugins.rag_sqlite:register",
        )
        self.assertEqual(
            manifest.permissions.filesystem_read,
            ("plugin_state:rag-sqlite.sqlite3",),
        )
        self.assertEqual(manifest.permissions.network_egress, ())
        self.assertEqual(manifest.permissions.secrets, ())
        self.assertEqual(self.tools["index_text"].effect, "idempotent")
        self.assertEqual(self.tools["index_text"].tool_revision, "1")
        self.assertEqual(self.tools["search_documents"].effect, "read_only")
        self.assertEqual(len(self.plugin_registration.skills), 1)
        skill = self.plugin_registration.skills[0]
        self.assertEqual(
            skill.skill_id, "com.sasori.rag-sqlite/grounded-search"
        )
        self.assertEqual(skill.tool_names, ("search_documents",))
        self.assertIn("source_sha256", skill.instructions)
        self.assertIn("chunk_sha256", skill.instructions)
        self.assertEqual(self.plugin_registration.workers, ())
        self.assertFalse(self.path.exists())

        configured_path = self.root / "configured.sqlite3"
        with patch.dict(os.environ, {RAG_DB_ENV: str(configured_path)}):
            configured = register()
        self.assertIs(validate_registration(manifest, configured), configured)
        self.assertFalse(configured_path.exists())

    async def test_index_waits_for_approval_and_handler_runs_once(self):
        arguments = self.arguments()
        original = self.tools["index_text"]
        calls = 0

        def tracked_index(
            collection: str,
            source_id: str,
            text: str,
            *,
            idempotency_key: str,
        ) -> str:
            nonlocal calls
            calls += 1
            return original.handler(
                collection,
                source_id,
                text,
                idempotency_key=idempotency_key,
            )

        tool = replace(original, handler=tracked_index)

        def final(messages):
            result = json.loads(messages[-1].content)
            self.assertEqual(result["collection_revision"], 1)
            return ModelReply(content="done")

        harness = self.enterContext(
            Harness(
                ScriptedModel(
                    ModelReply(
                        tool_calls=(
                            ToolCall("index-1", "index_text", arguments),
                        )
                    ),
                    final,
                ),
                (tool, self.tools["search_documents"]),
            )
        )
        with self.assertRaises(RunPaused) as paused:
            await harness.run((Message("user", "index"),), run_id="rag-approval")
        self.assertEqual(calls, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(paused.exception.request.effect, "idempotent")
        self.assertEqual(paused.exception.request.tool_revision, "1")
        self.assertTrue(paused.exception.request.idempotency_key.startswith("rag-sqlite-index-v1:"))

        harness.resolve_approval(
            "rag-approval", paused.exception.request.fingerprint, True
        )
        result = await harness.resume("rag-approval")
        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(calls, 1)
        repeated = await harness.resume("rag-approval")
        self.assertEqual(repeated.final_message.content, "done")
        self.assertEqual(calls, 1)

    async def test_search_is_read_only_and_never_creates_or_changes_database(self):
        def final(messages):
            result = json.loads(messages[-1].content)
            self.assertEqual(result["collection_revision"], 0)
            self.assertEqual(result["matches"], [])
            return ModelReply(content="done")

        harness = self.enterContext(
            Harness(
                ScriptedModel(
                    ModelReply(
                        tool_calls=(
                            ToolCall(
                                "search-1",
                                "search_documents",
                                {
                                    "collection": "docs",
                                    "query": "alpha",
                                    "limit": 10,
                                },
                            ),
                        )
                    ),
                    final,
                ),
                self.plugin_registration.tools,
            )
        )
        result = await harness.run(
            (Message("user", "search"),), run_id="rag-read-only"
        )
        self.assertEqual(result.final_message.content, "done")
        self.assertFalse(self.path.exists())

        self.index_with(self.plugin_registration, self.arguments())
        before = self.path.read_bytes()
        names = {path.name for path in self.root.iterdir()}
        self.assertEqual(
            len(self.search_with(self.plugin_registration, "alpha")["matches"]),
            1,
        )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({path.name for path in self.root.iterdir()}, names)

    def test_idempotency_deduplicates_conflicts_and_survives_restart(self):
        arguments = self.arguments()
        first, key = self.index_with(self.plugin_registration, arguments)
        repeated, repeated_key = self.index_with(
            self.plugin_registration, arguments
        )
        self.assertEqual(key, repeated_key)
        self.assertEqual(first, repeated)
        self.assertEqual(
            self.search_with(self.plugin_registration, "alpha")[
                "collection_revision"
            ],
            1,
        )

        changed = self.arguments("different text")
        with self.assertRaises(RAGIdempotencyConflict):
            self.index_with(self.plugin_registration, changed, key=key)

        restarted = self.make_registration(self.path)
        replayed, restarted_key = self.index_with(restarted, arguments)
        self.assertEqual(restarted_key, key)
        self.assertEqual(replayed, first)
        self.assertEqual(len(self.search_with(restarted, "alpha")["matches"]), 1)

    def test_superseded_idempotent_generation_fails_closed(self):
        original = self.arguments("alpha generation")
        first, first_key = self.index_with(self.plugin_registration, original)
        replacement = self.arguments("beta generation")
        second, _ = self.index_with(self.plugin_registration, replacement)
        self.assertEqual(json.loads(first)["source_revision"], 1)
        self.assertEqual(json.loads(second)["source_revision"], 2)

        with self.assertRaisesRegex(
            RAGIdempotencyConflict, "request was superseded"
        ):
            self.index_with(self.plugin_registration, original, key=first_key)

        self.assertEqual(
            self.search_with(self.plugin_registration, "alpha")["matches"], []
        )
        current = self.search_with(self.plugin_registration, "beta")
        self.assertEqual(current["collection_revision"], 2)
        self.assertEqual(current["matches"][0]["source_revision"], 2)

    def test_reindex_atomically_replaces_source_and_advances_revisions(self):
        old_result, old_key = self.index_with(
            self.plugin_registration,
            self.arguments("oldterm remains only in generation one"),
        )
        new_result, new_key = self.index_with(
            self.plugin_registration,
            self.arguments("newterm replaces the old generation"),
        )
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(json.loads(old_result)["source_revision"], 1)
        self.assertEqual(json.loads(new_result)["source_revision"], 2)
        self.assertEqual(json.loads(new_result)["collection_revision"], 2)
        self.assertEqual(
            self.search_with(self.plugin_registration, "oldterm")["matches"],
            [],
        )
        new_search = self.search_with(self.plugin_registration, "newterm")
        self.assertEqual(new_search["collection_revision"], 2)
        self.assertEqual(new_search["matches"][0]["source_revision"], 2)

    def test_transaction_failure_preserves_old_generation_and_retry(self):
        armed = False

        def fault(point):
            if armed and point == "after_chunks_replaced":
                raise InjectedFailure("stop before commit")

        registration = self.make_registration(_fault_injector=fault)
        self.index_with(registration, self.arguments("oldterm stable generation"))
        armed = True
        replacement = self.arguments("newterm uncommitted generation")
        with self.assertRaises(InjectedFailure):
            self.index_with(registration, replacement)
        old = self.search_with(registration, "oldterm")
        self.assertEqual(old["collection_revision"], 1)
        self.assertEqual(len(old["matches"]), 1)
        self.assertEqual(self.search_with(registration, "newterm")["matches"], [])

        armed = False
        committed, _ = self.index_with(registration, replacement)
        self.assertEqual(json.loads(committed)["collection_revision"], 2)
        self.assertEqual(self.search_with(registration, "oldterm")["matches"], [])
        self.assertEqual(len(self.search_with(registration, "newterm")["matches"]), 1)

    def test_missing_fts5_is_explicitly_unsupported(self):
        class NoFTSConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "CREATE VIRTUAL TABLE" in sql.upper() and "FTS5" in sql.upper():
                    raise sqlite3.OperationalError("no such module: fts5")
                return super().execute(sql, parameters)

        def connect(path, **options):
            return sqlite3.connect(path, factory=NoFTSConnection, **options)

        registration = self.make_registration(
            self.root / "no-fts.sqlite3", _connect=connect
        )
        with self.assertRaisesRegex(RAGUnsupportedError, "FTS5 is unavailable"):
            self.index_with(registration, self.arguments())

    def test_existing_schema_damage_fails_closed_without_repair(self):
        for table, operation in (
            (
                "idempotency_records",
                lambda registration: self.index_with(
                    registration, self.arguments()
                ),
            ),
            (
                "chunks_fts",
                lambda registration: self.search_with(registration, "alpha"),
            ),
        ):
            with self.subTest(table=table):
                path = self.root / f"damaged-{table}.sqlite3"
                registration = self.make_registration(path)
                self.index_with(registration, self.arguments())
                database = sqlite3.connect(path)
                try:
                    database.execute(f'DROP TABLE "{table}"')
                    database.commit()
                finally:
                    database.close()
                damaged = path.read_bytes()

                with self.assertRaisesRegex(
                    RAGConfigurationError, "schema is incomplete"
                ):
                    operation(registration)

                self.assertEqual(path.read_bytes(), damaged)
                database = sqlite3.connect(path)
                try:
                    revision = database.execute(
                        "SELECT revision FROM collections WHERE collection = ?",
                        ("docs",),
                    ).fetchone()[0]
                    exists = database.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)
                    ).fetchone()
                finally:
                    database.close()
                self.assertEqual(revision, 1)
                self.assertIsNone(exists)

    def test_incompatible_fts_and_triggers_fail_closed_for_read_and_write(self):
        damages = {
            "contentless-fts": (
                "DROP TABLE chunks_fts",
                """
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    collection UNINDEXED,
                    source_id UNINDEXED,
                    chunk_id UNINDEXED,
                    text,
                    content = '',
                    tokenize = 'unicode61 remove_diacritics 0'
                )
                """,
            ),
            "ledger-trigger": (
                """
                CREATE TRIGGER discard_idempotency
                AFTER INSERT ON idempotency_records
                BEGIN
                    DELETE FROM idempotency_records
                    WHERE idempotency_key = NEW.idempotency_key;
                END
                """,
            ),
        }
        for name, statements in damages.items():
            with self.subTest(damage=name):
                path = self.root / f"{name}.sqlite3"
                registration = self.make_registration(path)
                self.index_with(registration, self.arguments("alpha"))
                database = sqlite3.connect(path)
                try:
                    for statement in statements:
                        database.execute(statement)
                    database.commit()
                finally:
                    database.close()
                damaged = path.read_bytes()

                with self.assertRaises(RAGConfigurationError):
                    self.search_with(registration, "alpha")
                self.assertEqual(path.read_bytes(), damaged)
                with self.assertRaises(RAGConfigurationError):
                    self.index_with(registration, self.arguments("beta"))
                self.assertEqual(path.read_bytes(), damaged)

                database = sqlite3.connect(path)
                try:
                    revision = database.execute(
                        "SELECT revision FROM collections WHERE collection = ?",
                        ("docs",),
                    ).fetchone()[0]
                finally:
                    database.close()
                self.assertEqual(revision, 1)

    def test_malformed_and_oversized_input_fails_before_database_open(self):
        index = self.tools["index_text"].handler
        invalid_index = (
            {"collection": "Bad", "source_id": "source-1", "text": "text"},
            {"collection": "docs", "source_id": "../source", "text": "text"},
            {"collection": "docs", "source_id": "source-1", "text": ""},
            {"collection": "docs", "source_id": "source-1", "text": "bad\x00text"},
            {"collection": "docs", "source_id": "source-1", "text": "\ud800"},
            {
                "collection": "docs",
                "source_id": "source-1",
                "text": "x" * (1024 * 1024 + 1),
            },
        )
        for arguments in invalid_index:
            with self.subTest(arguments=str(arguments)[:60]):
                with self.assertRaises(RAGValidationError):
                    index(**arguments, idempotency_key="invalid")

        search = self.tools["search_documents"].handler
        invalid_search = (
            ("Bad", "alpha", 1),
            ("docs", "", 1),
            ("docs", "***", 1),
            ("docs", "bad\nquery", 1),
            ("docs", "x" * 65, 1),
            ("docs", " ".join(f"t{number}" for number in range(33)), 1),
            ("docs", "alpha", True),
            ("docs", "alpha", 0),
            ("docs", "alpha", 21),
        )
        for arguments in invalid_search:
            with self.subTest(arguments=arguments):
                with self.assertRaises(RAGValidationError):
                    search(*arguments)
        self.assertFalse(self.path.exists())

    def test_query_operators_and_sql_punctuation_are_literal_data(self):
        self.index_with(
            self.plugin_registration,
            self.arguments("alpha OR beta NEAR drop table documents"),
        )
        injected = self.search_with(
            self.plugin_registration,
            'alpha" OR * NOT (beta); DROP TABLE chunks;--',
        )
        self.assertEqual(injected["matches"], [])
        literal_operator = self.search_with(self.plugin_registration, "OR")
        self.assertEqual(len(literal_operator["matches"]), 1)
        normal = self.search_with(self.plugin_registration, "alpha")
        self.assertEqual(len(normal["matches"]), 1)

    def test_citation_offsets_hashes_and_normalization_are_deterministic(self):
        raw = "Cafe\u0301\r\n" + "a" * 900 + " citationneedle " + "b" * 500
        normalized = unicodedata.normalize("NFC", raw.replace("\r\n", "\n"))
        arguments = self.arguments(raw)
        first, first_key = self.index_with(self.plugin_registration, arguments)
        equivalent = self.arguments(normalized)
        replayed, replayed_key = self.index_with(
            self.plugin_registration, equivalent
        )
        self.assertEqual(first_key, replayed_key)
        self.assertEqual(first, replayed)

        result = self.search_with(self.plugin_registration, "citationneedle")
        self.assertEqual(result["collection_revision"], 1)
        self.assertGreaterEqual(len(result["matches"]), 1)
        source_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        for match in result["matches"]:
            expected_text = normalized[match["start"] : match["end"]]
            self.assertEqual(match["text"], expected_text)
            self.assertEqual(match["source_sha256"], source_sha256)
            self.assertEqual(
                match["chunk_sha256"],
                hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(len(match["chunk_id"]), 64)
            self.assertTrue(match["start"] <= normalized.index("citationneedle"))
            self.assertTrue(match["end"] > normalized.index("citationneedle"))
            self.assertIsInstance(match["score"], float)

    def test_database_identity_rejects_core_or_unrelated_sqlite(self):
        database = sqlite3.connect(self.path)
        try:
            database.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY)")
            database.execute("PRAGMA user_version = 2")
            database.commit()
        finally:
            database.close()
        with self.assertRaisesRegex(RAGConfigurationError, "schema identity"):
            self.search_with(self.plugin_registration, "alpha")
        database = sqlite3.connect(self.path)
        try:
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            database.close()
        self.assertEqual(tables, {"runs"})

        invalid = self.root / "not-sqlite.sqlite3"
        invalid.write_bytes(b"not a SQLite database")
        invalid_registration = self.make_registration(invalid)
        with self.assertRaises(RAGDatabaseError):
            self.search_with(invalid_registration, "alpha")
        self.assertEqual(invalid.read_bytes(), b"not a SQLite database")

    def test_every_tool_invocation_explicitly_closes_its_connection(self):
        closes = 0

        class TrackingConnection(sqlite3.Connection):
            def close(inner_self):
                nonlocal closes
                closes += 1
                super().close()

        def connect(path, **options):
            return sqlite3.connect(path, factory=TrackingConnection, **options)

        path = self.root / "tracked.sqlite3"
        registration = self.make_registration(path, _connect=connect)
        self.index_with(registration, self.arguments())
        self.search_with(registration, "alpha")
        self.assertEqual(closes, 2)

        database = sqlite3.connect(path)
        try:
            database.execute("DROP TABLE idempotency_records")
            database.commit()
        finally:
            database.close()
        with self.assertRaises(RAGConfigurationError):
            self.index_with(registration, self.arguments("replacement"))
        self.assertEqual(closes, 3)


if __name__ == "__main__":
    unittest.main()
