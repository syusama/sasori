import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sasori import Tool, tool_schema_sha256  # noqa: E402
from sasori_flow import (  # noqa: E402
    InputRef,
    InputSlot,
    SavedWorkflowCatalog,
    ToolStep,
    WorkflowCatalogConfigurationError,
    WorkflowCatalogExists,
    WorkflowCatalogIntegrityError,
    WorkflowCatalogNotFound,
    WorkflowCatalogRevisionMismatch,
    WorkflowCatalogStore,
    WorkflowSpec,
    WorkflowValidationError,
    catalog_etag,
    validate_catalog_id,
)


def catalog_id() -> str:
    return f"wfcat_{uuid.uuid4().hex}"


def workflow(tool: Tool, *, version: str = "1", workflow_id: str = "saved-flow"):
    step = ToolStep(
        "inspect",
        tool.name,
        tool.effect,
        tool.tool_revision,
        tool_schema_sha256(tool),
        {"value": InputRef("value")},
        "string",
    )
    return WorkflowSpec(
        workflow_id,
        version,
        (InputSlot("value", "string"),),
        (step,),
        step.step_id,
    ).as_data()


class WorkflowCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "workflows.sqlite3"
        self.tool = Tool("inspect", lambda value: value, effect="read_only")
        self.store = WorkflowCatalogStore(self.path)
        self.catalog = SavedWorkflowCatalog(self.store, (self.tool,))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_catalog_id_is_a_version_four_uuid_not_a_workflow_name(self):
        value = catalog_id()
        self.assertEqual(validate_catalog_id(value), value)
        for invalid in (
            "saved-flow",
            "wfcat_" + "a" * 32,
            "wfcat_550e8400e29b11d4a716446655440000",
            "wfcat_550E8400E29B41D4A716446655440000",
            "wfcat_550e8400-e29b-41d4-a716-446655440000",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_catalog_id(invalid)

    def test_create_round_trips_canonical_definition_and_saved_manifest(self):
        identity = catalog_id()
        record = self.catalog.create(identity, workflow(self.tool))
        loaded = self.catalog.get(identity)

        self.assertEqual(record.catalog_id, identity)
        self.assertEqual(record.catalog_revision, 1)
        self.assertIsNone(record.parent_revision)
        self.assertEqual(loaded.record, record)
        self.assertEqual(loaded.current_contract, "compatible")
        self.assertIsNone(loaded.current_reason_code)
        self.assertEqual(record.definition["workflow_id"], "saved-flow")
        self.assertEqual(record.saved_manifest["definition_sha256"], record.definition_sha256)
        self.assertEqual(catalog_etag(record), catalog_etag(loaded.record))

    def test_store_rejects_non_exact_manifest_before_starting_a_transaction(self):
        definition, manifest = self.catalog._preflight(workflow(self.tool))
        manifest["schema_version"] = True
        with self.assertRaises(WorkflowValidationError):
            self.store.create(catalog_id(), definition, manifest)
        database = self.store._db
        self.assertIsNotNone(database)
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM workflow_heads").fetchone()[0],
            0,
        )
        self.assertFalse(database.in_transaction)

    def test_catalog_and_workflow_identity_are_independent(self):
        first = self.catalog.create(catalog_id(), workflow(self.tool))
        second = self.catalog.create(catalog_id(), workflow(self.tool))
        self.assertNotEqual(first.catalog_id, second.catalog_id)
        self.assertEqual(first.definition_sha256, second.definition_sha256)

    def test_create_collision_does_not_append_a_revision(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        with self.assertRaises(WorkflowCatalogExists):
            self.catalog.create(identity, workflow(self.tool, version="2"))
        self.assertEqual(self.store.revision_count(identity), 1)

    def test_update_appends_immutable_revision_and_old_revision_is_readable(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))
        second, changed = self.catalog.update(
            identity,
            first.catalog_revision,
            first.definition_sha256,
            workflow(self.tool, version="2"),
        )

        self.assertTrue(changed)
        self.assertEqual((second.catalog_revision, second.parent_revision), (2, 1))
        self.assertEqual(self.catalog.get(identity, revision=1).record, first)
        self.assertEqual(self.catalog.get(identity).record, second)
        self.assertEqual(self.store.revision_count(identity), 2)

    def test_current_identical_update_is_a_no_op(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))
        current, changed = self.catalog.update(
            identity,
            1,
            first.definition_sha256,
            workflow(self.tool),
        )
        self.assertFalse(changed)
        self.assertEqual(current, first)
        self.assertEqual(self.store.revision_count(identity), 1)

    def test_stale_identical_update_is_still_rejected(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))
        second, _ = self.catalog.update(
            identity,
            1,
            first.definition_sha256,
            workflow(self.tool, version="2"),
        )
        with self.assertRaises(WorkflowCatalogRevisionMismatch) as caught:
            self.catalog.update(
                identity,
                1,
                first.definition_sha256,
                workflow(self.tool, version="2"),
            )
        self.assertEqual(caught.exception.current_revision, 2)
        self.assertEqual(caught.exception.current_definition_sha256, second.definition_sha256)
        self.assertEqual(self.store.revision_count(identity), 2)

    def test_list_is_stable_bounded_and_never_returns_definition_or_manifest(self):
        identities = [catalog_id() for _ in range(3)]
        for identity in identities:
            self.catalog.create(identity, workflow(self.tool, workflow_id="same-name"))
        first = self.catalog.list(limit=2, before=None)
        second = self.catalog.list(limit=2, before=first.next_before)

        self.assertEqual([item.catalog_id for item in first.items], identities[:0:-1])
        self.assertEqual([item.catalog_id for item in second.items], identities[:1])
        self.assertIsNone(second.next_before)
        payload = json.dumps(first.as_data())
        self.assertNotIn('"definition"', payload)
        self.assertNotIn('"saved_manifest"', payload)

    def test_restart_preserves_head_and_history(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))
        second, _ = self.catalog.update(
            identity,
            1,
            first.definition_sha256,
            workflow(self.tool, version="2"),
        )
        self.store.close()
        self.store = WorkflowCatalogStore(self.path)
        self.catalog = SavedWorkflowCatalog(self.store, (self.tool,))
        self.assertEqual(self.catalog.get(identity).record, second)
        self.assertEqual(self.catalog.get(identity, revision=1).record, first)

    def test_tool_drift_is_readable_but_not_relabelled_as_current(self):
        identity = catalog_id()
        record = self.catalog.create(identity, workflow(self.tool))
        changed_tool = Tool("inspect", lambda changed: changed, effect="read_only")
        reopened = SavedWorkflowCatalog(self.store, (changed_tool,))
        detail = reopened.get(identity)
        self.assertEqual(detail.record, record)
        self.assertEqual(detail.current_contract, "incompatible")
        self.assertEqual(detail.current_reason_code, "tool_contract_mismatch")

    def test_foreign_application_or_newer_schema_fails_without_mutation(self):
        self.store.close()
        database = sqlite3.connect(self.path)
        try:
            database.execute("PRAGMA application_id = 1234")
            database.commit()
        finally:
            database.close()
        before = self.path.read_bytes()
        with self.assertRaises(WorkflowCatalogConfigurationError):
            WorkflowCatalogStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_tables_are_not_adopted_as_an_empty_catalog(self):
        other = Path(self.temp.name) / "foreign.sqlite3"
        database = sqlite3.connect(other)
        try:
            database.execute("CREATE TABLE foreign_data(secret TEXT)")
            database.execute("INSERT INTO foreign_data VALUES ('keep')")
            database.commit()
        finally:
            database.close()
        with self.assertRaises(WorkflowCatalogConfigurationError):
            WorkflowCatalogStore(other)
        database = sqlite3.connect(other)
        try:
            self.assertEqual(database.execute("SELECT secret FROM foreign_data").fetchone()[0], "keep")
        finally:
            database.close()

    def test_second_file_owner_is_rejected_and_close_releases_the_lock(self):
        with self.assertRaises(WorkflowCatalogConfigurationError):
            WorkflowCatalogStore(self.path)
        self.store.close()
        reopened = WorkflowCatalogStore(self.path)
        reopened.close()

    def test_fault_before_commit_rolls_back_snapshot_and_head(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))

        def fail(point):
            if point == "after_revision_insert":
                raise RuntimeError("injected before commit")

        self.store._fault_hook = fail
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.catalog.update(
                identity,
                1,
                first.definition_sha256,
                workflow(self.tool, version="2"),
            )
        self.store._fault_hook = None
        self.assertEqual(self.catalog.get(identity).record, first)
        self.assertEqual(self.store.revision_count(identity), 1)

    def test_fault_after_commit_is_recovered_by_known_catalog_identity(self):
        identity = catalog_id()

        def fail(point):
            if point == "after_commit":
                raise RuntimeError("response outcome unknown")

        self.store._fault_hook = fail
        with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
            self.catalog.create(identity, workflow(self.tool))
        self.store._fault_hook = None
        self.assertEqual(self.catalog.get(identity).record.catalog_revision, 1)
        with self.assertRaises(WorkflowCatalogExists):
            self.catalog.create(identity, workflow(self.tool))

    def test_child_process_crash_points_preserve_atomic_head_and_history(self):
        child = r'''
import os
import sys
from sasori import Tool, tool_schema_sha256
from sasori_flow import InputRef, InputSlot, SavedWorkflowCatalog, ToolStep, WorkflowCatalogStore, WorkflowSpec

path, identity, digest, point = sys.argv[1:]
tool = Tool("inspect", lambda value: value, effect="read_only")
step = ToolStep(
    "inspect", tool.name, tool.effect, tool.tool_revision,
    tool_schema_sha256(tool), {"value": InputRef("value")}, "string",
)
definition = WorkflowSpec(
    "saved-flow", "2", (InputSlot("value", "string"),), (step,), step.step_id,
).as_data()
store = WorkflowCatalogStore(path)
store._fault_hook = lambda reached: os._exit(91) if reached == point else None
SavedWorkflowCatalog(store, (tool,)).update(identity, 1, digest, definition)
os._exit(92)
'''
        expected = {
            "before_revision_insert": 1,
            "after_revision_insert": 1,
            "after_head_update": 1,
            "after_commit": 2,
        }
        for point, expected_revision in expected.items():
            with self.subTest(point=point):
                path = Path(self.temp.name) / f"crash-{point}.sqlite3"
                store = WorkflowCatalogStore(path)
                catalog = SavedWorkflowCatalog(store, (self.tool,))
                identity = catalog_id()
                first = catalog.create(identity, workflow(self.tool))
                store.close()
                environment = dict(os.environ)
                source_root = str(ROOT / "src")
                environment["PYTHONPATH"] = source_root + (
                    os.pathsep + environment["PYTHONPATH"]
                    if environment.get("PYTHONPATH") else ""
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child,
                        str(path),
                        identity,
                        first.definition_sha256,
                        point,
                    ],
                    env=environment,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    91,
                    completed.stderr.decode("utf-8", "replace"),
                )
                reopened = WorkflowCatalogStore(path)
                try:
                    recovered = SavedWorkflowCatalog(reopened, (self.tool,))
                    head = recovered.get(identity).record
                    self.assertEqual(head.catalog_revision, expected_revision)
                    self.assertEqual(reopened.revision_count(identity), expected_revision)
                    self.assertEqual(recovered.get(identity, revision=1).record, first)
                    if expected_revision == 1:
                        with self.assertRaises(WorkflowCatalogNotFound):
                            recovered.get(identity, revision=2)
                    else:
                        self.assertEqual(head.definition["version"], "2")
                        self.assertEqual(head.parent_revision, 1)
                finally:
                    reopened.close()

    def test_corrupt_definition_manifest_or_head_fails_closed(self):
        cases = (
            "UPDATE workflow_revisions SET definition_sha256 = ?",
            "UPDATE workflow_revisions SET manifest_sha256 = ?",
            "UPDATE workflow_heads SET current_definition_sha256 = ?",
        )
        for statement in cases:
            with self.subTest(statement=statement):
                other = Path(self.temp.name) / f"corrupt-{hashlib.sha256(statement.encode()).hexdigest()[:8]}.sqlite3"
                store = WorkflowCatalogStore(other)
                catalog = SavedWorkflowCatalog(store, (self.tool,))
                identity = catalog_id()
                catalog.create(identity, workflow(self.tool))
                store.close()
                database = sqlite3.connect(other)
                try:
                    database.execute("PRAGMA foreign_keys = OFF")
                    trigger_sql = database.execute(
                        "SELECT sql FROM sqlite_schema "
                        "WHERE type = 'trigger' "
                        "AND name = 'workflow_revisions_immutable_update'"
                    ).fetchone()[0]
                    database.execute("DROP TRIGGER workflow_revisions_immutable_update")
                    database.execute(statement, ("0" * 64,))
                    database.execute(trigger_sql)
                    database.commit()
                finally:
                    database.close()
                with self.assertRaises(WorkflowCatalogIntegrityError):
                    store = WorkflowCatalogStore(other)
                    try:
                        SavedWorkflowCatalog(store, (self.tool,)).get(identity)
                    finally:
                        store.close()

    def test_dangling_head_fails_current_history_and_list_through_one_validator(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute(
            "UPDATE workflow_heads SET current_revision = 999 WHERE catalog_id = ?",
            (identity,),
        )
        for operation in (
            lambda: self.catalog.get(identity),
            lambda: self.catalog.get(identity, revision=1),
            lambda: self.catalog.list(100, None),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorkflowCatalogIntegrityError, "head is dangling"
            ):
                operation()

    def test_stored_non_integer_head_is_integrity_failure_not_value_error(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute(
            "UPDATE workflow_heads SET current_revision = 'abc' WHERE catalog_id = ?",
            (identity,),
        )
        for operation in (
            lambda: self.catalog.get(identity),
            lambda: self.catalog.get(identity, revision=1),
            lambda: self.catalog.list(100, None),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorkflowCatalogIntegrityError, "stored Workflow head revision is invalid"
            ):
                operation()

    def test_stored_non_text_head_is_integrity_failure_on_every_read_path(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute(
            "UPDATE workflow_heads SET current_workflow_id = ? WHERE catalog_id = ?",
            (sqlite3.Binary(b"stored-secret-literal"), identity),
        )
        for operation in (
            lambda: self.catalog.get(identity),
            lambda: self.catalog.get(identity, revision=1),
            lambda: self.catalog.list(100, None),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorkflowCatalogIntegrityError, "stored Workflow head identity is invalid"
            ):
                operation()

    def test_exhausted_catalog_sequence_fails_closed_before_insert(self):
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute(
            "UPDATE workflow_catalog_meta SET next_catalog_seq = ? WHERE singleton = 1",
            (2**63 - 1,),
        )
        with self.assertRaisesRegex(
            WorkflowCatalogIntegrityError, "sequence is exhausted"
        ):
            self.catalog.create(catalog_id(), workflow(self.tool))
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM workflow_heads").fetchone()[0],
            0,
        )
        self.assertFalse(database.in_transaction)

    def test_list_validates_the_pagination_sentinel_before_using_its_cursor(self):
        older = catalog_id()
        self.catalog.create(older, workflow(self.tool, workflow_id="older"))
        self.catalog.create(catalog_id(), workflow(self.tool, workflow_id="newer"))
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute(
            "UPDATE workflow_heads SET current_revision = 999 WHERE catalog_id = ?",
            (older,),
        )
        with self.assertRaises(WorkflowCatalogIntegrityError):
            self.catalog.list(1, None)

    def test_revision_chain_gap_fails_current_history_and_list(self):
        identity = catalog_id()
        first = self.catalog.create(identity, workflow(self.tool))
        self.catalog.update(
            identity,
            first.catalog_revision,
            first.definition_sha256,
            workflow(self.tool, version="2"),
        )
        database = self.store._db
        self.assertIsNotNone(database)
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute("DROP TRIGGER workflow_revisions_immutable_delete")
        database.execute(
            "DELETE FROM workflow_revisions "
            "WHERE catalog_id = ? AND catalog_revision = 1",
            (identity,),
        )
        for operation in (
            lambda: self.catalog.get(identity),
            lambda: self.catalog.get(identity, revision=1),
            lambda: self.catalog.list(100, None),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorkflowCatalogIntegrityError, "revision chain is incomplete"
            ):
                operation()

    def test_intermediate_parent_anomaly_fails_every_head_read(self):
        identity = catalog_id()
        current = self.catalog.create(identity, workflow(self.tool))
        for version in ("2", "3"):
            current, _ = self.catalog.update(
                identity,
                current.catalog_revision,
                current.definition_sha256,
                workflow(self.tool, version=version),
            )
        database = self.store._db
        self.assertIsNotNone(database)
        trigger_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'workflow_revisions_immutable_update'"
        ).fetchone()[0]
        database.execute("PRAGMA ignore_check_constraints = ON")
        database.execute("DROP TRIGGER workflow_revisions_immutable_update")
        database.execute(
            "UPDATE workflow_revisions SET parent_revision = 99 "
            "WHERE catalog_id = ? AND catalog_revision = 2",
            (identity,),
        )
        database.execute(trigger_sql)
        database.execute("PRAGMA ignore_check_constraints = OFF")
        for operation in (
            lambda: self.catalog.get(identity),
            lambda: self.catalog.get(identity, revision=1),
            lambda: self.catalog.list(100, None),
        ):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                WorkflowCatalogIntegrityError, "revision chain is invalid"
            ):
                operation()

    def test_over_nested_stored_manifest_is_integrity_failure(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        document = (
            '{"x":' * 800 + '"stored-secret-literal"' + "}" * 800
        ).encode("utf-8")
        database = self.store._db
        self.assertIsNotNone(database)
        trigger_sql = database.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'workflow_revisions_immutable_update'"
        ).fetchone()[0]
        database.execute("DROP TRIGGER workflow_revisions_immutable_update")
        database.execute(
            "UPDATE workflow_revisions SET manifest_json = ?, manifest_sha256 = ? "
            "WHERE catalog_id = ?",
            (document, hashlib.sha256(document).hexdigest(), identity),
        )
        database.execute(trigger_sql)
        with self.assertRaisesRegex(
            WorkflowCatalogIntegrityError, "stored Workflow manifest is invalid"
        ):
            self.catalog.get(identity)

    def test_same_name_no_op_triggers_are_rejected_by_schema_fingerprint(self):
        for action in ("update", "delete"):
            with self.subTest(action=action):
                path = Path(self.temp.name) / f"no-op-{action}.sqlite3"
                store = WorkflowCatalogStore(path)
                store.close()
                database = sqlite3.connect(path)
                try:
                    database.executescript(
                        f"DROP TRIGGER workflow_revisions_immutable_{action};"
                        f"CREATE TRIGGER workflow_revisions_immutable_{action} "
                        f"BEFORE {action.upper()} ON workflow_revisions "
                        "BEGIN SELECT 1; END;"
                    )
                finally:
                    database.close()
                with self.assertRaisesRegex(
                    WorkflowCatalogConfigurationError, "schema objects"
                ):
                    WorkflowCatalogStore(path)

    def test_missing_composite_foreign_key_is_rejected_as_schema_drift(self):
        self.store.close()
        database = sqlite3.connect(self.path)
        try:
            database.executescript(
                "PRAGMA foreign_keys = OFF;"
                "ALTER TABLE workflow_heads RENAME TO workflow_heads_original;"
                "CREATE TABLE workflow_heads ("
                "catalog_id TEXT PRIMARY KEY,"
                "catalog_seq INTEGER NOT NULL UNIQUE CHECK (catalog_seq >= 1),"
                "current_revision INTEGER NOT NULL CHECK (current_revision >= 1),"
                "current_definition_sha256 TEXT NOT NULL,"
                "current_workflow_id TEXT NOT NULL,"
                "current_definition_version TEXT NOT NULL"
                ");"
                "INSERT INTO workflow_heads SELECT * FROM workflow_heads_original;"
                "DROP TABLE workflow_heads_original;"
            )
        finally:
            database.close()
        with self.assertRaisesRegex(
            WorkflowCatalogConfigurationError, "schema objects"
        ):
            WorkflowCatalogStore(self.path)

    def test_foreign_key_violation_is_rejected_during_reopen(self):
        identity = catalog_id()
        self.catalog.create(identity, workflow(self.tool))
        self.store.close()
        database = sqlite3.connect(self.path)
        try:
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute(
                "UPDATE workflow_heads SET current_revision = 999 WHERE catalog_id = ?",
                (identity,),
            )
            database.commit()
        finally:
            database.close()
        with self.assertRaisesRegex(
            WorkflowCatalogIntegrityError, "foreign-key integrity"
        ):
            WorkflowCatalogStore(self.path)

    def test_invalid_caller_values_remain_input_errors(self):
        identity = catalog_id()
        record = self.catalog.create(identity, workflow(self.tool))
        invalid_calls = (
            lambda: self.catalog.get("invalid"),
            lambda: self.catalog.get(identity, revision=2**63),
            lambda: self.catalog.list(0, None),
            lambda: self.catalog.list(101, None),
            lambda: self.catalog.list(1, 2**63),
            lambda: self.catalog.update(
                identity, 0, record.definition_sha256, workflow(self.tool)
            ),
            lambda: self.catalog.update(
                identity, 2**63, record.definition_sha256, workflow(self.tool)
            ),
            lambda: self.catalog.update(identity, 1, "bad", workflow(self.tool)),
        )
        for operation in invalid_calls:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()

    def test_missing_identity_or_revision_is_not_found(self):
        identity = catalog_id()
        with self.assertRaises(WorkflowCatalogNotFound):
            self.catalog.get(identity)
        self.catalog.create(identity, workflow(self.tool))
        with self.assertRaises(WorkflowCatalogNotFound):
            self.catalog.get(identity, revision=2)


if __name__ == "__main__":
    unittest.main()
