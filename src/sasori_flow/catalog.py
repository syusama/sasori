from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from sasori import Tool
from sasori._sqlite_lock import acquire_process_lock, release_process_lock

from .manifest import MAX_WORKFLOW_MANIFEST_BYTES, validate_workflow_manifest
from .runtime import WorkflowCompileError, preflight_workflow, workflow_app_id
from .spec import WorkflowSpec, WorkflowValidationError, canonical_json
from .authoring import workflow_spec_from_data, workflow_spec_from_json


_APPLICATION_ID = 0x53574631
_SCHEMA_VERSION = 1
_CATALOG_ID = re.compile(r"wfcat_([0-9a-f]{32})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SQLITE_INT_MAX = 2**63 - 1
_SCHEMA_V1 = (
    (
        "table",
        "workflow_catalog_meta",
        "workflow_catalog_meta",
        """CREATE TABLE workflow_catalog_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            owner_scope TEXT NOT NULL CHECK (owner_scope = 'deployment'),
            next_catalog_seq INTEGER NOT NULL CHECK (next_catalog_seq >= 1)
        )""",
    ),
    (
        "table",
        "workflow_revisions",
        "workflow_revisions",
        """CREATE TABLE workflow_revisions (
            catalog_id TEXT NOT NULL,
            catalog_revision INTEGER NOT NULL CHECK (catalog_revision >= 1),
            parent_revision INTEGER,
            workflow_id TEXT NOT NULL,
            definition_version TEXT NOT NULL,
            definition_sha256 TEXT NOT NULL CHECK (length(definition_sha256) = 64),
            definition_json BLOB NOT NULL CHECK (length(definition_json) <= 1048576),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            manifest_json BLOB NOT NULL CHECK (length(manifest_json) <= 4194304),
            PRIMARY KEY (catalog_id, catalog_revision),
            UNIQUE (
                catalog_id, catalog_revision, definition_sha256,
                workflow_id, definition_version
            ),
            CHECK (
                (catalog_revision = 1 AND parent_revision IS NULL) OR
                (catalog_revision > 1 AND parent_revision = catalog_revision - 1)
            )
        )""",
    ),
    (
        "table",
        "workflow_heads",
        "workflow_heads",
        """CREATE TABLE workflow_heads (
            catalog_id TEXT PRIMARY KEY,
            catalog_seq INTEGER NOT NULL UNIQUE CHECK (catalog_seq >= 1),
            current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
            current_definition_sha256 TEXT NOT NULL,
            current_workflow_id TEXT NOT NULL,
            current_definition_version TEXT NOT NULL,
            FOREIGN KEY (
                catalog_id, current_revision, current_definition_sha256,
                current_workflow_id, current_definition_version
            ) REFERENCES workflow_revisions (
                catalog_id, catalog_revision, definition_sha256,
                workflow_id, definition_version
            )
        )""",
    ),
    (
        "trigger",
        "workflow_revisions_immutable_update",
        "workflow_revisions",
        """CREATE TRIGGER workflow_revisions_immutable_update
        BEFORE UPDATE ON workflow_revisions BEGIN
            SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END""",
    ),
    (
        "trigger",
        "workflow_revisions_immutable_delete",
        "workflow_revisions",
        """CREATE TRIGGER workflow_revisions_immutable_delete
        BEFORE DELETE ON workflow_revisions BEGIN
            SELECT RAISE(ABORT, 'workflow revisions are immutable');
        END""",
    ),
)


class WorkflowCatalogError(Exception):
    pass


class WorkflowCatalogConfigurationError(WorkflowCatalogError):
    pass


class WorkflowCatalogIntegrityError(WorkflowCatalogError):
    pass


class WorkflowCatalogNotFound(WorkflowCatalogError):
    pass


class WorkflowCatalogExists(WorkflowCatalogError):
    pass


class WorkflowCatalogRevisionMismatch(WorkflowCatalogError):
    def __init__(self, revision: int, definition_sha256: str) -> None:
        self.current_revision = revision
        self.current_definition_sha256 = definition_sha256
        super().__init__("saved Workflow changed since it was loaded")


class WorkflowCatalogValidationError(WorkflowCatalogError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _normalize_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split())


def _stored_int(value: object, label: str, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= _SQLITE_INT_MAX:
        raise WorkflowCatalogIntegrityError(f"stored {label} is invalid")
    return value


def _stored_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise WorkflowCatalogIntegrityError(f"stored {label} is invalid")
    return value


def _stable_sqlite_errors(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except sqlite3.IntegrityError:
            raise WorkflowCatalogIntegrityError(
                "Workflow catalog SQLite integrity operation failed"
            ) from None
        except sqlite3.Error:
            raise WorkflowCatalogError(
                "Workflow catalog SQLite operation failed"
            ) from None

    return guarded


def validate_catalog_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("catalog_id must be a string")
    matched = _CATALOG_ID.fullmatch(value)
    if matched is None:
        raise ValueError("catalog_id must be wfcat_ plus a lowercase UUID")
    parsed = uuid.UUID(hex=matched.group(1))
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("catalog_id must contain a version-4 UUID")
    return value


@dataclass(frozen=True, slots=True)
class SavedWorkflowRecord:
    catalog_id: str
    catalog_revision: int
    parent_revision: int | None
    definition: dict[str, object]
    definition_sha256: str
    saved_manifest: dict[str, object]

    @property
    def workflow_id(self) -> str:
        return str(self.definition["workflow_id"])

    @property
    def definition_version(self) -> str:
        return str(self.definition["version"])

    def as_data(self) -> dict[str, object]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_revision": self.catalog_revision,
            "parent_revision": self.parent_revision,
            "definition_sha256": self.definition_sha256,
            "definition": self.definition,
            "saved_manifest": self.saved_manifest,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedHead:
    catalog_id: str
    catalog_seq: int
    current_revision: int
    current_record: SavedWorkflowRecord


@dataclass(frozen=True, slots=True)
class SavedWorkflowDetail:
    record: SavedWorkflowRecord
    head_revision: int
    current_contract: str
    current_reason_code: str | None

    def as_data(self) -> dict[str, object]:
        value = self.record.as_data()
        value.update(
            {
                "head_revision": self.head_revision,
                "is_head": self.record.catalog_revision == self.head_revision,
                "current_contract": {
                    "status": self.current_contract,
                    "reason_code": self.current_reason_code,
                },
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class SavedWorkflowSummary:
    catalog_id: str
    catalog_seq: int
    catalog_revision: int
    workflow_id: str
    definition_version: str
    definition_sha256: str

    def as_data(self) -> dict[str, object]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_revision": self.catalog_revision,
            "workflow_id": self.workflow_id,
            "definition_version": self.definition_version,
            "definition_sha256": self.definition_sha256,
        }


@dataclass(frozen=True, slots=True)
class SavedWorkflowPage:
    items: tuple[SavedWorkflowSummary, ...]
    next_before: int | None

    def as_data(self) -> dict[str, object]:
        return {
            "ok": True,
            "schema_version": 1,
            "items": [item.as_data() for item in self.items],
            "next_before": self.next_before,
        }


def catalog_etag(record: SavedWorkflowRecord) -> str:
    identity = validate_catalog_id(record.catalog_id)[6:]
    return (
        f'"sasori-wfcat-{identity}-r{record.catalog_revision}-'
        f'{record.definition_sha256}"'
    )


class WorkflowCatalogStore:
    @_stable_sqlite_errors
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._fault_hook: Callable[[str], None] | None = None
        self._process_lock = None
        self._db: sqlite3.Connection | None = None
        try:
            if self.path != ":memory:":
                self._process_lock = acquire_process_lock(self.path)
            self._db = sqlite3.connect(self.path, isolation_level=None, timeout=0)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA synchronous = FULL")
            application_id = int(
                self._db.execute("PRAGMA application_id").fetchone()[0]
            )
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
                self._db.execute("PRAGMA locking_mode = EXCLUSIVE")
            if application_id == 0 and version == 0:
                tables = self._user_tables()
                if tables:
                    raise WorkflowCatalogConfigurationError(
                        "Workflow catalog will not adopt a foreign SQLite schema"
                    )
                self._initialize()
            elif application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
                raise WorkflowCatalogConfigurationError(
                    "Workflow catalog SQLite identity or schema version is unsupported"
                )
            self._validate_schema()
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Workflow catalog cleanup failed: {cleanup_error}")
            if isinstance(error, (OSError, BlockingIOError)) or (
                isinstance(error, sqlite3.OperationalError)
                and any(word in str(error).lower() for word in ("locked", "busy"))
            ):
                raise WorkflowCatalogConfigurationError(
                    "Workflow catalog allows one file-backed owner process"
                ) from error
            raise

    def _user_tables(self) -> set[str]:
        assert self._db is not None
        return {
            str(row[0])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _initialize(self) -> None:
        assert self._db is not None
        try:
            statements = ";\n".join(item[3] for item in _SCHEMA_V1)
            self._db.executescript(
                f"BEGIN IMMEDIATE;\n{statements};\n"
                "INSERT INTO workflow_catalog_meta VALUES (1, 'deployment', 1);\n"
                f"PRAGMA application_id = {_APPLICATION_ID};\n"
                f"PRAGMA user_version = {_SCHEMA_VERSION};\nCOMMIT;"
            )
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _validate_schema(self) -> None:
        assert self._db is not None
        expected = tuple(
            sorted(
                (kind, name, table, _normalize_sql(sql))
                for kind, name, table, sql in _SCHEMA_V1
            )
        )
        actual = tuple(
            sorted(
                (
                    _stored_text(row["type"], "schema object type"),
                    _stored_text(row["name"], "schema object name"),
                    _stored_text(row["tbl_name"], "schema object table"),
                    _normalize_sql(_stored_text(row["sql"], "schema object SQL")),
                )
                for row in self._db.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "WHERE type IN ('table', 'trigger') "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
        )
        if actual != expected:
            raise WorkflowCatalogConfigurationError(
                "Workflow catalog schema objects do not match version 1"
            )
        foreign_keys = tuple(
            (
                _stored_int(row["seq"], "foreign-key sequence", 0),
                _stored_text(row["table"], "foreign-key table"),
                _stored_text(row["from"], "foreign-key source"),
                _stored_text(row["to"], "foreign-key target"),
                _stored_text(row["on_update"], "foreign-key update action"),
                _stored_text(row["on_delete"], "foreign-key delete action"),
                _stored_text(row["match"], "foreign-key match mode"),
            )
            for row in self._db.execute("PRAGMA foreign_key_list(workflow_heads)")
        )
        expected_foreign_keys = tuple(
            (
                index,
                "workflow_revisions",
                source,
                target,
                "NO ACTION",
                "NO ACTION",
                "NONE",
            )
            for index, (source, target) in enumerate(
                (
                    ("catalog_id", "catalog_id"),
                    ("current_revision", "catalog_revision"),
                    ("current_definition_sha256", "definition_sha256"),
                    ("current_workflow_id", "workflow_id"),
                    ("current_definition_version", "definition_version"),
                )
            )
        )
        foreign_keys_enabled = self._db.execute("PRAGMA foreign_keys").fetchone()
        if (
            foreign_keys_enabled is None
            or foreign_keys_enabled[0] != 1
            or foreign_keys != expected_foreign_keys
        ):
            raise WorkflowCatalogConfigurationError(
                "Workflow catalog foreign-key schema does not match version 1"
            )
        if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkflowCatalogIntegrityError(
                "Workflow catalog foreign-key integrity failed"
            )
        meta = self._db.execute(
            "SELECT owner_scope, next_catalog_seq FROM workflow_catalog_meta "
            "WHERE singleton = 1"
        ).fetchone()
        if (
            meta is None
            or _stored_text(meta["owner_scope"], "Workflow catalog owner scope")
            != "deployment"
        ):
            raise WorkflowCatalogIntegrityError("Workflow catalog metadata is invalid")
        _stored_int(meta["next_catalog_seq"], "Workflow catalog sequence")

    @_stable_sqlite_errors
    def close(self) -> None:
        database, self._db = self._db, None
        process_lock, self._process_lock = self._process_lock, None
        try:
            if database is not None:
                database.close()
        finally:
            if process_lock is not None:
                release_process_lock(process_lock)

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    @staticmethod
    def _documents(
        definition: Mapping[str, object], manifest: Mapping[str, object]
    ) -> tuple[WorkflowSpec, bytes, bytes, str]:
        spec = workflow_spec_from_data(definition)
        validate_workflow_manifest(spec, manifest, app_id=workflow_app_id(spec))
        definition_json = canonical_json(spec.as_data()).encode("utf-8")
        manifest_json = canonical_json(manifest).encode("utf-8")
        if len(manifest_json) > MAX_WORKFLOW_MANIFEST_BYTES:
            raise WorkflowValidationError("workflow manifest exceeds the size limit")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("workflow_id") != spec.workflow_id
            or manifest.get("version") != spec.version
            or manifest.get("definition_sha256") != spec.digest
        ):
            raise WorkflowValidationError("workflow manifest does not match the definition")
        manifest_sha256 = hashlib.sha256(manifest_json).hexdigest()
        return spec, definition_json, manifest_json, manifest_sha256

    @_stable_sqlite_errors
    def create(
        self,
        catalog_id: str,
        definition: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> SavedWorkflowRecord:
        assert self._db is not None
        identity = validate_catalog_id(catalog_id)
        spec, definition_json, manifest_json, manifest_sha256 = self._documents(
            definition, manifest
        )
        self._db.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            if self._db.execute(
                "SELECT 1 FROM workflow_heads WHERE catalog_id = ?", (identity,)
            ).fetchone() is not None:
                raise WorkflowCatalogExists("saved Workflow catalog identity exists")
            meta = self._db.execute(
                "SELECT next_catalog_seq FROM workflow_catalog_meta WHERE singleton = 1"
            ).fetchone()
            if meta is None:
                raise WorkflowCatalogIntegrityError("Workflow catalog metadata is missing")
            sequence = _stored_int(
                meta["next_catalog_seq"], "Workflow catalog sequence"
            )
            if sequence == _SQLITE_INT_MAX:
                raise WorkflowCatalogIntegrityError(
                    "Workflow catalog sequence is exhausted"
                )
            self._fault("before_revision_insert")
            self._db.execute(
                """
                INSERT INTO workflow_revisions VALUES (?, 1, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    spec.workflow_id,
                    spec.version,
                    spec.digest,
                    definition_json,
                    manifest_sha256,
                    manifest_json,
                ),
            )
            self._fault("after_revision_insert")
            self._db.execute(
                """
                INSERT INTO workflow_heads VALUES (?, ?, 1, ?, ?, ?)
                """,
                (identity, sequence, spec.digest, spec.workflow_id, spec.version),
            )
            self._db.execute(
                "UPDATE workflow_catalog_meta SET next_catalog_seq = ? WHERE singleton = 1",
                (sequence + 1,),
            )
            self._fault("after_head_insert")
            self._db.execute("COMMIT")
            committed = True
        finally:
            if not committed and self._db.in_transaction:
                self._db.execute("ROLLBACK")
        self._fault("after_commit")
        return self.load(identity)[0]

    @_stable_sqlite_errors
    def update(
        self,
        catalog_id: str,
        expected_revision: int,
        expected_definition_sha256: str,
        definition: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> tuple[SavedWorkflowRecord, bool]:
        assert self._db is not None
        identity = validate_catalog_id(catalog_id)
        if (
            type(expected_revision) is not int
            or not 1 <= expected_revision <= _SQLITE_INT_MAX
        ):
            raise ValueError("expected catalog revision must be positive")
        if not isinstance(expected_definition_sha256, str) or not _SHA256.fullmatch(
            expected_definition_sha256
        ):
            raise ValueError("expected definition digest is invalid")
        spec, definition_json, manifest_json, manifest_sha256 = self._documents(
            definition, manifest
        )
        self._db.execute("BEGIN IMMEDIATE")
        committed = False
        no_op = False
        try:
            head_row = self._db.execute(
                "SELECT * FROM workflow_heads WHERE catalog_id = ?",
                (identity,),
            ).fetchone()
            if head_row is None:
                raise WorkflowCatalogNotFound("saved Workflow was not found")
            head = self._validated_head(head_row)
            current_revision = head.current_revision
            current_digest = head.current_record.definition_sha256
            if (
                current_revision != expected_revision
                or current_digest != expected_definition_sha256
            ):
                raise WorkflowCatalogRevisionMismatch(current_revision, current_digest)
            current = self._db.execute(
                "SELECT manifest_sha256 FROM workflow_revisions "
                "WHERE catalog_id = ? AND catalog_revision = ?",
                (identity, current_revision),
            ).fetchone()
            if current is None:
                raise WorkflowCatalogIntegrityError("Workflow catalog head is dangling")
            if spec.digest == current_digest and str(current["manifest_sha256"]) == manifest_sha256:
                no_op = True
                self._db.execute("ROLLBACK")
                committed = True
            else:
                revision = current_revision + 1
                self._fault("before_revision_insert")
                self._db.execute(
                    """
                    INSERT INTO workflow_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity,
                        revision,
                        current_revision,
                        spec.workflow_id,
                        spec.version,
                        spec.digest,
                        definition_json,
                        manifest_sha256,
                        manifest_json,
                    ),
                )
                self._fault("after_revision_insert")
                cursor = self._db.execute(
                    """
                    UPDATE workflow_heads SET
                        current_revision = ?, current_definition_sha256 = ?,
                        current_workflow_id = ?, current_definition_version = ?
                    WHERE catalog_id = ? AND current_revision = ?
                        AND current_definition_sha256 = ?
                    """,
                    (
                        revision,
                        spec.digest,
                        spec.workflow_id,
                        spec.version,
                        identity,
                        current_revision,
                        current_digest,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WorkflowCatalogRevisionMismatch(current_revision, current_digest)
                self._fault("after_head_update")
                self._db.execute("COMMIT")
                committed = True
        finally:
            if not committed and self._db.in_transaction:
                self._db.execute("ROLLBACK")
        if not no_op:
            self._fault("after_commit")
        return self.load(identity)[0], not no_op

    @staticmethod
    def _strict_json(document: bytes, label: str) -> dict[str, object]:
        try:
            def constant(_token: str) -> object:
                raise ValueError

            def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
                value: dict[str, object] = {}
                for key, item in items:
                    if key in value:
                        raise ValueError
                    value[key] = item
                return value

            value = json.loads(
                document.decode("utf-8", "strict"),
                parse_constant=constant,
                object_pairs_hook=pairs,
            )
            canonical = (
                canonical_json(value).encode("utf-8")
                if isinstance(value, dict)
                else None
            )
        except (UnicodeError, ValueError, RecursionError, WorkflowValidationError):
            raise WorkflowCatalogIntegrityError(f"stored {label} is invalid") from None
        if canonical != document:
            raise WorkflowCatalogIntegrityError(f"stored {label} is not canonical")
        return value

    def _record(self, row: sqlite3.Row) -> SavedWorkflowRecord:
        identity = _stored_text(row["catalog_id"], "catalog identity")
        try:
            validate_catalog_id(identity)
        except ValueError:
            raise WorkflowCatalogIntegrityError("stored catalog identity is invalid") from None
        definition_json = row["definition_json"]
        manifest_json = row["manifest_json"]
        if not isinstance(definition_json, bytes) or not isinstance(manifest_json, bytes):
            raise WorkflowCatalogIntegrityError("stored Workflow documents are not bytes")
        try:
            spec = workflow_spec_from_json(definition_json)
        except WorkflowValidationError:
            raise WorkflowCatalogIntegrityError("stored Workflow definition is invalid") from None
        manifest = self._strict_json(manifest_json, "Workflow manifest")
        try:
            validate_workflow_manifest(
                spec, manifest, app_id=workflow_app_id(spec)
            )
        except (WorkflowCompileError, WorkflowValidationError):
            raise WorkflowCatalogIntegrityError(
                "stored Workflow manifest is invalid"
            ) from None
        definition_sha256 = _stored_text(
            row["definition_sha256"], "Workflow definition digest"
        )
        manifest_sha256 = _stored_text(
            row["manifest_sha256"], "Workflow manifest digest"
        )
        if (
            not _SHA256.fullmatch(definition_sha256)
            or not _SHA256.fullmatch(manifest_sha256)
            or spec.digest != definition_sha256
            or hashlib.sha256(manifest_json).hexdigest() != manifest_sha256
            or _stored_text(row["workflow_id"], "Workflow identity")
            != spec.workflow_id
            or _stored_text(row["definition_version"], "Workflow version")
            != spec.version
        ):
            raise WorkflowCatalogIntegrityError("stored Workflow relation is invalid")
        revision = _stored_int(row["catalog_revision"], "Workflow revision")
        parent = (
            None
            if row["parent_revision"] is None
            else _stored_int(row["parent_revision"], "Workflow parent revision")
        )
        if revision < 1 or (
            (revision == 1 and parent is not None)
            or (revision > 1 and parent != revision - 1)
        ):
            raise WorkflowCatalogIntegrityError("stored Workflow revision chain is invalid")
        return SavedWorkflowRecord(
            identity,
            revision,
            None if parent is None else int(parent),
            spec.as_data(),
            definition_sha256,
            manifest,
        )

    def _validated_head(self, row: sqlite3.Row) -> _ValidatedHead:
        assert self._db is not None
        identity = _stored_text(row["catalog_id"], "catalog identity")
        try:
            validate_catalog_id(identity)
        except ValueError:
            raise WorkflowCatalogIntegrityError(
                "stored catalog identity is invalid"
            ) from None
        sequence = _stored_int(row["catalog_seq"], "catalog sequence")
        revision = _stored_int(row["current_revision"], "Workflow head revision")
        current = self._db.execute(
            "SELECT * FROM workflow_revisions "
            "WHERE catalog_id = ? AND catalog_revision = ?",
            (identity, revision),
        ).fetchone()
        if current is None:
            raise WorkflowCatalogIntegrityError("Workflow catalog head is dangling")
        record = self._record(current)
        if (
            _stored_text(
                row["current_definition_sha256"], "Workflow head definition digest"
            )
            != record.definition_sha256
            or _stored_text(row["current_workflow_id"], "Workflow head identity")
            != record.workflow_id
            or _stored_text(
                row["current_definition_version"], "Workflow head version"
            )
            != record.definition_version
        ):
            raise WorkflowCatalogIntegrityError(
                "Workflow catalog head does not match its revision"
            )
        chain = self._db.execute(
            "SELECT COUNT(*), MIN(catalog_revision), MAX(catalog_revision) "
            "FROM workflow_revisions WHERE catalog_id = ?",
            (identity,),
        ).fetchone()
        if (
            chain is None
            or _stored_int(chain[0], "Workflow revision count") != revision
            or _stored_int(chain[1], "Workflow first revision") != 1
            or _stored_int(chain[2], "Workflow latest revision") != revision
        ):
            raise WorkflowCatalogIntegrityError(
                "Workflow catalog revision chain is incomplete"
            )
        anomaly = self._db.execute(
            "SELECT 1 FROM workflow_revisions WHERE catalog_id = ? AND ("
            "(catalog_revision = 1 AND parent_revision IS NOT NULL) OR "
            "(catalog_revision > 1 AND (parent_revision IS NULL OR "
            "parent_revision != catalog_revision - 1))) LIMIT 1",
            (identity,),
        ).fetchone()
        if anomaly is not None:
            raise WorkflowCatalogIntegrityError(
                "Workflow catalog revision chain is invalid"
            )
        return _ValidatedHead(identity, sequence, revision, record)

    @_stable_sqlite_errors
    def load(
        self, catalog_id: str, revision: int | None = None
    ) -> tuple[SavedWorkflowRecord, int]:
        assert self._db is not None
        identity = validate_catalog_id(catalog_id)
        if revision is not None and (
            type(revision) is not int or not 1 <= revision <= _SQLITE_INT_MAX
        ):
            raise ValueError("catalog revision must be positive")
        head = self._db.execute(
            "SELECT * FROM workflow_heads WHERE catalog_id = ?", (identity,)
        ).fetchone()
        if head is None:
            raise WorkflowCatalogNotFound("saved Workflow was not found")
        current = self._validated_head(head)
        if revision is None or revision == current.current_revision:
            return current.current_record, current.current_revision
        row = self._db.execute(
            "SELECT * FROM workflow_revisions "
            "WHERE catalog_id = ? AND catalog_revision = ?",
            (identity, revision),
        ).fetchone()
        if row is None:
            raise WorkflowCatalogNotFound("saved Workflow revision was not found")
        record = self._record(row)
        return record, current.current_revision

    @_stable_sqlite_errors
    def list(self, limit: int, before: int | None) -> SavedWorkflowPage:
        assert self._db is not None
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("catalog list limit must be between 1 and 100")
        if before is not None and (
            type(before) is not int or not 1 <= before <= _SQLITE_INT_MAX
        ):
            raise ValueError("catalog list cursor must be positive")
        clause = "" if before is None else "WHERE catalog_seq < ?"
        arguments: tuple[object, ...] = () if before is None else (before,)
        rows = self._db.execute(
            "SELECT * FROM workflow_heads " + clause + " ORDER BY catalog_seq DESC LIMIT ?",
            arguments + (limit + 1,),
        ).fetchall()
        heads = tuple(self._validated_head(row) for row in rows)
        visible = heads[:limit]
        items = []
        for head in visible:
            record = head.current_record
            items.append(
                SavedWorkflowSummary(
                    head.catalog_id,
                    head.catalog_seq,
                    head.current_revision,
                    record.workflow_id,
                    record.definition_version,
                    record.definition_sha256,
                )
            )
        next_before = items[-1].catalog_seq if len(heads) > limit and items else None
        return SavedWorkflowPage(tuple(items), next_before)

    @_stable_sqlite_errors
    def revision_count(self, catalog_id: str) -> int:
        assert self._db is not None
        identity = validate_catalog_id(catalog_id)
        return int(
            self._db.execute(
                "SELECT COUNT(*) FROM workflow_revisions WHERE catalog_id = ?",
                (identity,),
            ).fetchone()[0]
        )


class SavedWorkflowCatalog:
    def __init__(self, store: WorkflowCatalogStore, tools: Sequence[Tool]) -> None:
        if not isinstance(store, WorkflowCatalogStore):
            raise TypeError("saved Workflow catalog requires a WorkflowCatalogStore")
        if any(not isinstance(tool, Tool) for tool in tools):
            raise TypeError("saved Workflow catalog tools must be Sasori Tools")
        self.store = store
        self.tools = tuple(tools)

    def _preflight(
        self, definition: Mapping[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        try:
            spec = workflow_spec_from_data(definition)
        except WorkflowValidationError as exc:
            raise WorkflowCatalogValidationError("invalid_definition", str(exc)) from None
        try:
            manifest = preflight_workflow(spec, self.tools)
        except WorkflowCompileError as exc:
            raise WorkflowCatalogValidationError("tool_contract_mismatch", str(exc)) from None
        except WorkflowValidationError as exc:
            raise WorkflowCatalogValidationError("manifest_rejected", str(exc)) from None
        return spec.as_data(), manifest

    def create(
        self, catalog_id: str, definition: Mapping[str, object]
    ) -> SavedWorkflowRecord:
        exact, manifest = self._preflight(definition)
        return self.store.create(catalog_id, exact, manifest)

    def update(
        self,
        catalog_id: str,
        expected_revision: int,
        expected_definition_sha256: str,
        definition: Mapping[str, object],
    ) -> tuple[SavedWorkflowRecord, bool]:
        exact, manifest = self._preflight(definition)
        return self.store.update(
            catalog_id,
            expected_revision,
            expected_definition_sha256,
            exact,
            manifest,
        )

    def get(self, catalog_id: str, revision: int | None = None) -> SavedWorkflowDetail:
        record, head_revision = self.store.load(catalog_id, revision)
        try:
            current = preflight_workflow(
                workflow_spec_from_data(record.definition), self.tools
            )
            status = (
                "compatible"
                if canonical_json(current) == canonical_json(record.saved_manifest)
                else "incompatible"
            )
            reason = None if status == "compatible" else "manifest_rejected"
        except WorkflowCompileError:
            status, reason = "incompatible", "tool_contract_mismatch"
        except WorkflowValidationError:
            status, reason = "incompatible", "manifest_rejected"
        return SavedWorkflowDetail(record, head_revision, status, reason)

    def list(self, limit: int, before: int | None) -> SavedWorkflowPage:
        return self.store.list(limit, before)


__all__ = [
    "SavedWorkflowCatalog",
    "SavedWorkflowDetail",
    "SavedWorkflowPage",
    "SavedWorkflowRecord",
    "SavedWorkflowSummary",
    "WorkflowCatalogConfigurationError",
    "WorkflowCatalogError",
    "WorkflowCatalogExists",
    "WorkflowCatalogIntegrityError",
    "WorkflowCatalogNotFound",
    "WorkflowCatalogRevisionMismatch",
    "WorkflowCatalogStore",
    "WorkflowCatalogValidationError",
    "catalog_etag",
    "validate_catalog_id",
]
