from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from sasori import MAX_TOOL_CALL_ID_BYTES, is_valid_app_id


_APPLICATION_ID = 0x53534D31
_SCHEMA_VERSION = 1
_RECORD_SCHEMA_VERSION = 1
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TERM = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)


class MemoryError(Exception):
    pass


class MemoryValidationError(MemoryError):
    pass


class MemoryConfigurationError(MemoryError):
    pass


class MemoryDatabaseError(MemoryError):
    pass


class MemoryCorruptionError(MemoryError):
    pass


class MemoryBindingError(MemoryError):
    pass


class MemoryRevisionConflict(MemoryError):
    pass


class MemoryIdempotencyConflict(MemoryError):
    pass


class MemorySuppressedError(MemoryError):
    pass


class MemoryScopeSuppressed(MemorySuppressedError):
    pass


class MemoryRebuildError(MemoryError):
    pass


def _utf8(value: object, name: str, *, minimum: int = 1, maximum: int) -> str:
    if not isinstance(value, str):
        raise MemoryValidationError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise MemoryValidationError(f"{name} must contain valid Unicode") from None
    if b"\x00" in encoded or not minimum <= len(encoded) <= maximum:
        raise MemoryValidationError(f"{name} has an invalid UTF-8 length")
    return value


def _identifier(value: object, name: str) -> str:
    value = _utf8(value, name, maximum=64)
    if _IDENTIFIER.fullmatch(value) is None:
        raise MemoryValidationError(f"{name} must be a lowercase ASCII identifier")
    return value


def _sha256(value: object, name: str) -> str:
    value = _utf8(value, name, maximum=64)
    if _HEX_SHA256.fullmatch(value) is None:
        raise MemoryValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _run_id(value: object, name: str = "run_id") -> str:
    value = _utf8(value, name, maximum=64)
    if _RUN_ID.fullmatch(value) is None:
        raise MemoryValidationError(
            f"{name} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )
    return value


def _positive_integer(value: object, name: str, *, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise MemoryValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(
    value: object, name: str, *, maximum: int = 2**63 - 1
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise MemoryValidationError(f"{name} must be a non-negative integer")
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        raise MemoryValidationError("value is not canonical JSON data") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryBinding:
    owner_id: str
    app_id: str
    scope_id: str
    session_id: str
    binding_revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_id", _identifier(self.owner_id, "owner_id"))
        if not is_valid_app_id(self.app_id):
            raise MemoryValidationError("app_id must be a valid Sasori application ID")
        object.__setattr__(self, "scope_id", _identifier(self.scope_id, "scope_id"))
        object.__setattr__(
            self, "session_id", _identifier(self.session_id, "session_id")
        )
        object.__setattr__(
            self,
            "binding_revision",
            _positive_integer(self.binding_revision, "binding_revision"),
        )

    @property
    def digest(self) -> str:
        return _digest(asdict(self))

    @property
    def values(self) -> tuple[object, ...]:
        return (
            self.owner_id,
            self.app_id,
            self.scope_id,
            self.session_id,
            self.binding_revision,
        )


@dataclass(frozen=True, slots=True)
class MemorySource:
    source_kind: str
    source_run_id: str
    source_turn_id: str
    source_step: int
    source_call_id: str
    source_sha256: str
    source_authority: str
    extractor_identity: str
    policy_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "source_kind",
            "source_authority",
            "extractor_identity",
        ):
            object.__setattr__(
                self, field_name, _identifier(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self, "source_run_id", _run_id(self.source_run_id, "source_run_id")
        )
        object.__setattr__(
            self,
            "source_turn_id",
            _utf8(self.source_turn_id, "source_turn_id", maximum=256),
        )
        object.__setattr__(
            self,
            "source_call_id",
            _utf8(
                self.source_call_id,
                "source_call_id",
                maximum=MAX_TOOL_CALL_ID_BYTES,
            ),
        )
        object.__setattr__(
            self, "source_step", _positive_integer(self.source_step, "source_step")
        )
        object.__setattr__(
            self,
            "source_sha256",
            _sha256(self.source_sha256, "source_sha256"),
        )
        object.__setattr__(
            self, "policy_digest", _sha256(self.policy_digest, "policy_digest")
        )

    @property
    def identity(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    max_query_bytes: int = 2048
    max_terms: int = 16
    max_scope_records: int = 10_000
    max_candidates: int = 256
    max_top_k: int = 8
    max_record_bytes: int = 16 * 1024
    max_result_bytes: int = 64 * 1024
    max_injection_bytes: int = 72 * 1024
    retrieval_policy_version: str = "lexical-term-coverage-v1"

    def __post_init__(self) -> None:
        for name in (
            "max_query_bytes",
            "max_terms",
            "max_scope_records",
            "max_candidates",
            "max_top_k",
            "max_record_bytes",
            "max_result_bytes",
            "max_injection_bytes",
        ):
            object.__setattr__(
                self, name, _positive_integer(getattr(self, name), name, maximum=2**31 - 1)
            )
        if self.max_candidates < self.max_top_k:
            raise MemoryValidationError("max_candidates must cover max_top_k")
        if self.max_injection_bytes < self.max_result_bytes:
            raise MemoryValidationError("max_injection_bytes must cover max_result_bytes")
        object.__setattr__(
            self,
            "retrieval_policy_version",
            _identifier(self.retrieval_policy_version, "retrieval_policy_version"),
        )


_SCHEMA: Mapping[str, str] = {
    "memory_bindings": """
        CREATE TABLE memory_bindings (
            run_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            binding_digest TEXT NOT NULL
        )
    """,
    "memory_scopes": """
        CREATE TABLE memory_scopes (
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            collection_revision INTEGER NOT NULL,
            active_generation INTEGER NOT NULL,
            next_sequence INTEGER NOT NULL,
            scope_suppressed INTEGER NOT NULL CHECK(scope_suppressed IN (0, 1)),
            PRIMARY KEY(owner_id, app_id, scope_id, session_id, binding_revision)
        )
    """,
    "memory_versions": """
        CREATE TABLE memory_versions (
            schema_version INTEGER NOT NULL,
            memory_id TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            priority INTEGER NOT NULL,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            source_turn_id TEXT NOT NULL,
            source_step INTEGER NOT NULL,
            source_call_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_authority TEXT NOT NULL,
            extractor_identity TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            observation_identity TEXT NOT NULL,
            collection_revision INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'tombstone')),
            PRIMARY KEY(memory_id, revision)
        )
    """,
    "memory_heads": """
        CREATE TABLE memory_heads (
            memory_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            current_revision INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'tombstone'))
        )
    """,
    "memory_operations": """
        CREATE TABLE memory_operations (
            idempotency_key TEXT PRIMARY KEY,
            request_sha256 TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            result_json TEXT NOT NULL
        )
    """,
    "memory_delete_outcomes": """
        CREATE TABLE memory_delete_outcomes (
            operation_identity TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            operation_kind TEXT NOT NULL CHECK(
                operation_kind IN ('forget_exact', 'forget_source', 'forget_scope')
            ),
            memory_id TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            revision INTEGER NOT NULL,
            deleted_count INTEGER NOT NULL,
            collection_revision INTEGER NOT NULL,
            already_deleted INTEGER NOT NULL CHECK(already_deleted IN (0, 1)),
            status TEXT NOT NULL
        )
    """,
    "memory_observations": """
        CREATE TABLE memory_observations (
            observation_identity TEXT PRIMARY KEY,
            request_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL
        )
    """,
    "memory_suppressions": """
        CREATE TABLE memory_suppressions (
            memory_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            operation_identity TEXT NOT NULL
        )
    """,
    "memory_source_suppressions": """
        CREATE TABLE memory_source_suppressions (
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            source_identity TEXT NOT NULL,
            operation_identity TEXT NOT NULL,
            PRIMARY KEY(
                owner_id, app_id, scope_id, session_id,
                binding_revision, source_identity
            )
        )
    """,
    "memory_terms": """
        CREATE TABLE memory_terms (
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            term TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY(
                owner_id, app_id, scope_id, session_id, binding_revision,
                generation, term, memory_id
            )
        )
    """,
    "memory_audit": """
        CREATE TABLE memory_audit (
            operation_identity TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            operation_kind TEXT NOT NULL,
            outcome TEXT NOT NULL,
            result_sha256 TEXT NOT NULL
        )
    """,
    "memory_terms_lookup": """
        CREATE INDEX memory_terms_lookup ON memory_terms(
            owner_id, app_id, scope_id, session_id, binding_revision,
            generation, term
        )
    """,
    "memory_versions_source": """
        CREATE INDEX memory_versions_source ON memory_versions(
            owner_id, app_id, scope_id, session_id, binding_revision,
            source_identity, status
        )
    """,
}

_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "memory_bindings": (
        "run_id", "owner_id", "app_id", "scope_id", "session_id",
        "binding_revision", "binding_digest",
    ),
    "memory_scopes": (
        "owner_id", "app_id", "scope_id", "session_id", "binding_revision",
        "collection_revision", "active_generation", "next_sequence",
        "scope_suppressed",
    ),
    "memory_versions": (
        "schema_version", "memory_id", "logical_key", "revision", "kind",
        "content", "content_sha256", "priority", "owner_id", "app_id",
        "scope_id", "session_id", "binding_revision", "source_kind",
        "source_run_id", "source_turn_id", "source_step", "source_call_id",
        "source_sha256", "source_authority", "extractor_identity",
        "policy_digest", "source_identity", "observation_identity",
        "collection_revision", "sequence", "status",
    ),
    "memory_heads": (
        "memory_id", "owner_id", "app_id", "scope_id", "session_id",
        "binding_revision", "current_revision", "sequence", "status",
    ),
    "memory_operations": (
        "idempotency_key", "request_sha256", "operation_kind", "result_json",
    ),
    "memory_delete_outcomes": (
        "operation_identity", "owner_id", "app_id", "scope_id", "session_id",
        "binding_revision", "operation_kind", "memory_id", "source_identity",
        "revision", "deleted_count", "collection_revision", "already_deleted",
        "status",
    ),
    "memory_observations": (
        "observation_identity", "request_sha256", "result_json",
    ),
    "memory_suppressions": (
        "memory_id", "owner_id", "app_id", "scope_id", "session_id",
        "binding_revision", "operation_identity",
    ),
    "memory_source_suppressions": (
        "owner_id", "app_id", "scope_id", "session_id", "binding_revision",
        "source_identity", "operation_identity",
    ),
    "memory_terms": (
        "owner_id", "app_id", "scope_id", "session_id", "binding_revision",
        "generation", "term", "memory_id", "revision",
    ),
    "memory_audit": (
        "operation_identity", "owner_id", "app_id", "scope_id", "session_id",
        "binding_revision", "operation_kind", "outcome", "result_sha256",
    ),
}


def _canonical_schema(sql: str) -> str:
    return " ".join(sql.lower().split())


def _terms(value: str, maximum: int | None = None) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result = tuple(dict.fromkeys(match.group(0) for match in _TERM.finditer(normalized)))
    if maximum is not None and len(result) > maximum:
        raise MemoryValidationError("query contains too many normalized terms")
    return result


class MemoryStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        policy: MemoryPolicy | None = None,
        connect: Callable[..., sqlite3.Connection] | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(path, (str, os.PathLike)):
            raise MemoryConfigurationError("Memory database path is invalid")
        self.path = Path(path).expanduser().resolve()
        if self.path.exists() and not self.path.is_file():
            raise MemoryConfigurationError("Memory database path is not a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or MemoryPolicy()
        self.connect = connect
        self.fault_injector = fault_injector
        database = self._open()
        database.close()

    @staticmethod
    def _rollback(database: sqlite3.Connection) -> None:
        try:
            database.rollback()
        except sqlite3.Error:
            pass

    @staticmethod
    def _database_error(error: sqlite3.Error) -> MemoryDatabaseError:
        return MemoryDatabaseError("Memory SQLite operation failed")

    @staticmethod
    def _validate_schema(database: sqlite3.Connection) -> None:
        rows = database.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        objects = {
            str(row["name"]): (str(row["type"]), str(row["sql"] or ""))
            for row in rows
        }
        if set(objects) != set(_SCHEMA):
            raise MemoryConfigurationError("Memory database schema is incompatible")
        for name, expected_sql in _SCHEMA.items():
            expected_type = "index" if name.startswith("memory_terms_lookup") or name.startswith("memory_versions_source") else "table"
            kind, actual_sql = objects[name]
            if kind != expected_type or _canonical_schema(actual_sql) != _canonical_schema(expected_sql):
                raise MemoryConfigurationError("Memory database schema is incompatible")
        for name, expected in _TABLE_COLUMNS.items():
            columns = tuple(
                str(row["name"])
                for row in database.execute(f'PRAGMA table_info("{name}")').fetchall()
            )
            if columns != expected:
                raise MemoryConfigurationError("Memory database schema is incompatible")

    @classmethod
    def _validate_existing(cls, database: sqlite3.Connection) -> None:
        application_id = int(database.execute("PRAGMA application_id").fetchone()[0])
        version = int(database.execute("PRAGMA user_version").fetchone()[0])
        if application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
            raise MemoryConfigurationError("Memory database schema identity is invalid")
        cls._validate_schema(database)

    def _initialize(self, database: sqlite3.Connection) -> None:
        try:
            database.execute("BEGIN IMMEDIATE")
            application_id = int(database.execute("PRAGMA application_id").fetchone()[0])
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if application_id == 0 and version == 0:
                existing = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is not None:
                    raise MemoryConfigurationError(
                        "Memory path refers to a non-Memory database"
                    )
                for statement in _SCHEMA.values():
                    database.execute(statement)
                database.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                database.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._validate_schema(database)
            elif application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
                raise MemoryConfigurationError("Memory database schema identity is invalid")
            else:
                self._validate_schema(database)
            database.commit()
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise

    def _open(self, *, read_only: bool = False) -> sqlite3.Connection:
        connector = sqlite3.connect if self.connect is None else self.connect
        target = str(self.path)
        options: dict[str, object] = {"isolation_level": None, "timeout": 5.0}
        if read_only:
            target = f"{self.path.as_uri()}?mode=ro"
            options["uri"] = True
        try:
            database = connector(target, **options)
        except sqlite3.Error:
            raise MemoryDatabaseError("Memory SQLite database could not be opened") from None
        try:
            database.row_factory = sqlite3.Row
            if read_only:
                database.execute("PRAGMA query_only = ON")
                self._validate_existing(database)
            else:
                database.execute("PRAGMA foreign_keys = ON")
                database.execute("PRAGMA synchronous = FULL")
                self._initialize(database)
            return database
        except sqlite3.Error as error:
            database.close()
            raise self._database_error(error) from None
        except BaseException:
            database.close()
            raise

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    @staticmethod
    def _scope(database: sqlite3.Connection, binding: MemoryBinding) -> sqlite3.Row | None:
        row = database.execute(
            """
            SELECT collection_revision, active_generation, next_sequence,
                   scope_suppressed
            FROM memory_scopes
            WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
              AND binding_revision = ?
            """,
            binding.values,
        ).fetchone()
        if row is not None and (
            isinstance(row["collection_revision"], bool)
            or not isinstance(row["collection_revision"], int)
            or int(row["collection_revision"]) < 0
            or isinstance(row["active_generation"], bool)
            or not isinstance(row["active_generation"], int)
            or int(row["active_generation"]) < 1
            or isinstance(row["next_sequence"], bool)
            or not isinstance(row["next_sequence"], int)
            or int(row["next_sequence"]) < 1
            or row["scope_suppressed"] not in (0, 1)
        ):
            raise MemoryCorruptionError("Memory scope state is invalid")
        return row

    @classmethod
    def _ensure_scope(cls, database: sqlite3.Connection, binding: MemoryBinding) -> sqlite3.Row:
        database.execute(
            """
            INSERT OR IGNORE INTO memory_scopes(
                owner_id, app_id, scope_id, session_id, binding_revision,
                collection_revision, active_generation, next_sequence,
                scope_suppressed
            ) VALUES (?, ?, ?, ?, ?, 0, 1, 1, 0)
            """,
            binding.values,
        )
        row = cls._scope(database, binding)
        if row is None:
            raise MemoryCorruptionError("Memory scope could not be loaded")
        return row

    @staticmethod
    def _require_available(scope: sqlite3.Row) -> None:
        if int(scope["scope_suppressed"]) != 0:
            raise MemoryScopeSuppressed("Memory scope is suppressed")

    def bind_run(self, run_id: str, binding: MemoryBinding) -> None:
        run_id = _run_id(run_id)
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT * FROM memory_bindings WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is None:
                database.execute(
                    """
                    INSERT INTO memory_bindings(
                        run_id, owner_id, app_id, scope_id, session_id,
                        binding_revision, binding_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, *binding.values, binding.digest),
                )
                self._ensure_scope(database, binding)
            else:
                observed = MemoryBinding(
                    str(existing["owner_id"]),
                    str(existing["app_id"]),
                    str(existing["scope_id"]),
                    str(existing["session_id"]),
                    int(existing["binding_revision"]),
                )
                if observed != binding or str(existing["binding_digest"]) != binding.digest:
                    raise MemoryBindingError("run has a different durable Memory binding")
            database.commit()
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    def binding_for_run(self, run_id: str) -> MemoryBinding:
        run_id = _run_id(run_id)
        database = self._open(read_only=True)
        try:
            row = database.execute(
                "SELECT * FROM memory_bindings WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise MemoryBindingError("run has no durable Memory binding")
            binding = MemoryBinding(
                str(row["owner_id"]),
                str(row["app_id"]),
                str(row["scope_id"]),
                str(row["session_id"]),
                int(row["binding_revision"]),
            )
            if str(row["binding_digest"]) != binding.digest:
                raise MemoryCorruptionError("Memory binding digest is invalid")
            return binding
        finally:
            database.close()

    @staticmethod
    def _stored_json(value: object, label: str) -> str:
        result = str(value)
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, UnicodeError):
            raise MemoryCorruptionError(f"{label} is invalid") from None
        if len(result.encode("utf-8")) > 1024 * 1024 or _canonical(parsed) != result:
            raise MemoryCorruptionError(f"{label} is invalid")
        return result

    @staticmethod
    def _validated_replay_result(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        operation_identity: str,
        operation_kind: str,
        result: str,
        request_sha256: str,
        expected: Mapping[str, object],
    ) -> str:
        try:
            value = json.loads(result)
            if not isinstance(value, dict):
                raise MemoryValidationError("operation result must be an object")
            if operation_kind == "remember":
                memory_id = _sha256(value.get("memory_id"), "memory_id")
                revision = _positive_integer(value.get("revision"), "revision")
                row = database.execute(
                    "SELECT * FROM memory_versions WHERE memory_id = ? AND revision = ?",
                    (memory_id, revision),
                ).fetchone()
                if row is None:
                    raise MemoryValidationError("remember result has no durable record")
                MemoryStore._validate_version(row, binding)
                durable = {
                    "schema_version": 1,
                    "memory_id": str(row["memory_id"]),
                    "logical_key": str(row["logical_key"]),
                    "revision": int(row["revision"]),
                    "kind": str(row["kind"]),
                    "content_sha256": str(row["content_sha256"]),
                    "priority": int(row["priority"]),
                    "app_id": binding.app_id,
                    "scope_id": binding.scope_id,
                    "session_id": binding.session_id,
                    "binding_revision": binding.binding_revision,
                    "source_run_id": str(row["source_run_id"]),
                    "source_turn_id": str(row["source_turn_id"]),
                    "source_step": int(row["source_step"]),
                    "source_call_id": str(row["source_call_id"]),
                    "source_sha256": str(row["source_sha256"]),
                    "source_authority": str(row["source_authority"]),
                    "source_identity": str(row["source_identity"]),
                    "observation_identity": str(row["observation_identity"]),
                    "collection_revision": int(row["collection_revision"]),
                    "sequence": int(row["sequence"]),
                    "status": "active",
                }
                if value != durable or any(
                    durable.get(name) != item for name, item in expected.items()
                ):
                    raise MemoryValidationError("remember result changed")
                observation = database.execute(
                    "SELECT request_sha256, result_json FROM memory_observations "
                    "WHERE observation_identity = ?",
                    (durable["observation_identity"],),
                ).fetchone()
                if (
                    observation is None
                    or str(observation["request_sha256"]) != request_sha256
                    or MemoryStore._stored_json(
                        observation["result_json"], "Memory observation result"
                    )
                    != result
                ):
                    raise MemoryValidationError("remember observation changed")
                return result

            outcome = database.execute(
                "SELECT * FROM memory_delete_outcomes WHERE operation_identity = ?",
                (operation_identity,),
            ).fetchone()
            if (
                outcome is None
                or tuple(
                    outcome[name]
                    for name in (
                        "owner_id",
                        "app_id",
                        "scope_id",
                        "session_id",
                        "binding_revision",
                    )
                )
                != binding.values
                or str(outcome["operation_kind"]) != operation_kind
            ):
                raise MemoryValidationError("delete operation outcome changed")

            if operation_kind == "forget_exact":
                memory_id = _sha256(outcome["memory_id"], "memory_id")
                durable = {
                    "memory_id": memory_id,
                    "revision": _nonnegative_integer(
                        int(outcome["revision"]), "revision"
                    ),
                    "status": str(outcome["status"]),
                    "already_deleted": bool(
                        _nonnegative_integer(
                            int(outcome["already_deleted"]),
                            "already_deleted",
                            maximum=1,
                        )
                    ),
                }
                if not durable["already_deleted"]:
                    durable["collection_revision"] = _positive_integer(
                        int(outcome["collection_revision"]),
                        "collection_revision",
                    )
                if (
                    value != durable
                    or memory_id != expected.get("memory_id")
                    or durable["status"] != "tombstone"
                ):
                    raise MemoryValidationError("exact delete result changed")
                suppression = database.execute(
                    "SELECT * FROM memory_suppressions WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if suppression is None or tuple(
                    suppression[name]
                    for name in (
                        "owner_id",
                        "app_id",
                        "scope_id",
                        "session_id",
                        "binding_revision",
                    )
                ) != binding.values:
                    raise MemoryValidationError("exact delete suppression changed")
                head = database.execute(
                    "SELECT * FROM memory_heads WHERE memory_id = ?", (memory_id,)
                ).fetchone()
                durable_revision = 0 if head is None else int(head["current_revision"])
                if head is not None and (
                    tuple(
                        head[name]
                        for name in (
                            "owner_id",
                            "app_id",
                            "scope_id",
                            "session_id",
                            "binding_revision",
                        )
                    )
                    != binding.values
                    or str(head["status"]) != "tombstone"
                ):
                    raise MemoryValidationError("exact delete head changed")
                if durable["revision"] != durable_revision or (
                    not durable["already_deleted"]
                    and str(suppression["operation_identity"])
                    != operation_identity
                ):
                    raise MemoryValidationError("exact delete revision changed")
                return result

            if operation_kind == "forget_source":
                source_identity = _sha256(outcome["source_identity"], "source_identity")
                durable = {
                    "source_identity": source_identity,
                    "deleted_count": _nonnegative_integer(
                        int(outcome["deleted_count"]), "deleted_count"
                    ),
                    "collection_revision": _nonnegative_integer(
                        int(outcome["collection_revision"]), "collection_revision"
                    ),
                    "status": str(outcome["status"]),
                }
                if (
                    value != durable
                    or source_identity != expected.get("source_identity")
                    or durable["status"] != "suppressed"
                ):
                    raise MemoryValidationError("source delete result changed")
                suppression = database.execute(
                    "SELECT * FROM memory_source_suppressions "
                    "WHERE owner_id = ? AND app_id = ? AND scope_id = ? "
                    "AND session_id = ? AND binding_revision = ? "
                    "AND source_identity = ?",
                    (*binding.values, source_identity),
                ).fetchone()
                if suppression is None:
                    raise MemoryValidationError("source delete suppression changed")
                return result

            if operation_kind == "forget_scope":
                durable = {
                    "deleted_count": _nonnegative_integer(
                        int(outcome["deleted_count"]), "deleted_count"
                    ),
                    "collection_revision": _nonnegative_integer(
                        int(outcome["collection_revision"]), "collection_revision"
                    ),
                    "status": str(outcome["status"]),
                }
                scope = MemoryStore._scope(database, binding)
                if (
                    value != durable
                    or durable["status"] != "scope_suppressed"
                    or scope is None
                    or int(scope["scope_suppressed"]) != 1
                    or int(scope["collection_revision"])
                    != durable["collection_revision"]
                ):
                    raise MemoryValidationError("scope delete result changed")
                return result
            raise MemoryValidationError("operation kind is unknown")
        except MemoryCorruptionError:
            raise
        except (MemoryValidationError, TypeError, ValueError, OverflowError):
            raise MemoryCorruptionError(
                "Memory operation result is structurally invalid"
            ) from None

    @staticmethod
    def _operation(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        idempotency_key: str,
        request_sha256: str,
        operation_kind: str,
        expected: Mapping[str, object],
    ) -> str | None:
        row = database.execute(
            """
            SELECT o.request_sha256, o.operation_kind, o.result_json,
                   a.owner_id, a.app_id, a.scope_id, a.session_id,
                   a.binding_revision, a.operation_kind AS audit_operation_kind,
                   a.outcome, a.result_sha256
            FROM memory_operations o
            LEFT JOIN memory_audit a
              ON a.operation_identity = o.idempotency_key
            WHERE o.idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if _HEX_SHA256.fullmatch(str(row["request_sha256"])) is None:
            raise MemoryCorruptionError("Memory operation digest is invalid")
        if str(row["request_sha256"]) != request_sha256:
            raise MemoryIdempotencyConflict(
                "idempotency key was already used for another Memory request"
            )
        if (
            str(row["operation_kind"]) != operation_kind
            or tuple(
                row[name]
                for name in (
                    "owner_id",
                    "app_id",
                    "scope_id",
                    "session_id",
                    "binding_revision",
                )
            )
            != binding.values
            or str(row["audit_operation_kind"]) != operation_kind
            or str(row["outcome"]) != "committed"
            or _HEX_SHA256.fullmatch(str(row["result_sha256"])) is None
        ):
            raise MemoryCorruptionError("Memory operation audit is invalid")
        result = MemoryStore._stored_json(
            row["result_json"], "Memory operation result"
        )
        if hashlib.sha256(result.encode("utf-8")).hexdigest() != str(
            row["result_sha256"]
        ):
            raise MemoryCorruptionError("Memory operation result digest is invalid")
        return MemoryStore._validated_replay_result(
            database,
            binding,
            idempotency_key,
            operation_kind,
            result,
            request_sha256,
            expected,
        )

    @staticmethod
    def _record_operation(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        *,
        idempotency_key: str,
        request_sha256: str,
        operation_kind: str,
        result: str,
    ) -> None:
        database.execute(
            """
            INSERT INTO memory_operations(
                idempotency_key, request_sha256, operation_kind, result_json
            ) VALUES (?, ?, ?, ?)
            """,
            (idempotency_key, request_sha256, operation_kind, result),
        )
        database.execute(
            """
            INSERT INTO memory_audit(
                operation_identity, owner_id, app_id, scope_id, session_id,
                binding_revision, operation_kind, outcome, result_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?)
            """,
            (
                idempotency_key,
                *binding.values,
                operation_kind,
                hashlib.sha256(result.encode("utf-8")).hexdigest(),
            ),
        )

    @staticmethod
    def _record_delete_outcome(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        *,
        operation_identity: str,
        operation_kind: str,
        memory_id: str = "",
        source_identity: str = "",
        revision: int = 0,
        deleted_count: int = 0,
        collection_revision: int,
        already_deleted: bool = False,
        status: str,
    ) -> None:
        database.execute(
            """
            INSERT INTO memory_delete_outcomes(
                operation_identity, owner_id, app_id, scope_id, session_id,
                binding_revision, operation_kind, memory_id, source_identity,
                revision, deleted_count, collection_revision, already_deleted,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_identity,
                *binding.values,
                operation_kind,
                memory_id,
                source_identity,
                revision,
                deleted_count,
                collection_revision,
                int(already_deleted),
                status,
            ),
        )

    @staticmethod
    def _memory_id(binding: MemoryBinding, logical_key: str) -> str:
        return _digest({"binding": asdict(binding), "logical_key": logical_key})

    @staticmethod
    def _observation_identity(
        binding: MemoryBinding, memory_id: str, source: MemorySource
    ) -> str:
        return _digest(
            {
                "binding": asdict(binding),
                "memory_id": memory_id,
                "source": asdict(source),
            }
        )

    @staticmethod
    def _idempotency_key(value: object) -> str:
        return _utf8(value, "idempotency_key", maximum=256)

    @staticmethod
    def _insert_terms(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        generation: int,
        memory_id: str,
        revision: int,
        logical_key: str,
        content: str,
    ) -> None:
        for term in _terms(f"{logical_key} {content}"):
            database.execute(
                """
                INSERT INTO memory_terms(
                    owner_id, app_id, scope_id, session_id, binding_revision,
                    generation, term, memory_id, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*binding.values, generation, term, memory_id, revision),
            )

    @staticmethod
    def _delete_terms(
        database: sqlite3.Connection,
        binding: MemoryBinding,
        generation: int,
        memory_id: str,
    ) -> None:
        database.execute(
            """
            DELETE FROM memory_terms
            WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
              AND binding_revision = ? AND generation = ? AND memory_id = ?
            """,
            (*binding.values, generation, memory_id),
        )

    def remember(
        self,
        binding: MemoryBinding,
        *,
        logical_key: str,
        kind: str,
        content: str,
        priority: int,
        expected_revision: int,
        source: MemorySource,
        idempotency_key: str,
    ) -> str:
        logical_key = _identifier(logical_key, "logical_key")
        kind = _identifier(kind, "kind")
        if kind not in ("episodic", "semantic", "procedural"):
            raise MemoryValidationError("kind is not supported")
        content = _utf8(content, "content", maximum=self.policy.max_record_bytes)
        priority = _nonnegative_integer(priority, "priority", maximum=100)
        expected_revision = _nonnegative_integer(
            expected_revision, "expected_revision"
        )
        idempotency_key = self._idempotency_key(idempotency_key)
        memory_id = self._memory_id(binding, logical_key)
        observation_identity = self._observation_identity(binding, memory_id, source)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        request = {
            "operation": "remember",
            "binding": asdict(binding),
            "logical_key": logical_key,
            "kind": kind,
            "content": content,
            "priority": priority,
            "expected_revision": expected_revision,
            "source": asdict(source),
        }
        request_sha256 = _digest(request)
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = self._operation(
                database,
                binding,
                idempotency_key,
                request_sha256,
                "remember",
                {
                    "memory_id": memory_id,
                    "logical_key": logical_key,
                    "kind": kind,
                    "content_sha256": content_sha256,
                    "priority": priority,
                },
            )
            if existing is not None:
                database.commit()
                return existing
            scope = self._ensure_scope(database, binding)
            self._require_available(scope)
            if database.execute(
                "SELECT 1 FROM memory_suppressions WHERE memory_id = ?",
                (memory_id,),
            ).fetchone() is not None:
                raise MemorySuppressedError("Memory item is durably suppressed")
            if database.execute(
                """
                SELECT 1 FROM memory_source_suppressions
                WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                  AND binding_revision = ? AND source_identity = ?
                """,
                (*binding.values, source.identity),
            ).fetchone() is not None:
                raise MemorySuppressedError("Memory source is durably suppressed")
            observed = database.execute(
                "SELECT request_sha256, result_json FROM memory_observations WHERE observation_identity = ?",
                (observation_identity,),
            ).fetchone()
            if observed is not None:
                if _HEX_SHA256.fullmatch(str(observed["request_sha256"])) is None:
                    raise MemoryCorruptionError(
                        "Memory observation digest is invalid"
                    )
                if str(observed["request_sha256"]) != request_sha256:
                    raise MemoryIdempotencyConflict(
                        "Memory observation was reused with different bytes"
                    )
                result = self._stored_json(
                    observed["result_json"], "Memory observation result"
                )
                result = self._validated_replay_result(
                    database,
                    binding,
                    "remember",
                    result,
                    request_sha256,
                    {
                        "memory_id": memory_id,
                        "logical_key": logical_key,
                        "kind": kind,
                        "content_sha256": content_sha256,
                        "priority": priority,
                    },
                )
                self._record_operation(
                    database,
                    binding,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    operation_kind="remember",
                    result=result,
                )
                database.commit()
                return result

            head = database.execute(
                "SELECT * FROM memory_heads WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            current_revision = 0 if head is None else int(head["current_revision"])
            if current_revision != expected_revision:
                raise MemoryRevisionConflict("expected Memory revision is stale")
            if head is not None and tuple(head[name] for name in (
                "owner_id", "app_id", "scope_id", "session_id", "binding_revision"
            )) != binding.values:
                raise MemoryCorruptionError("Memory head crossed a binding boundary")
            revision = current_revision + 1
            sequence = int(scope["next_sequence"])
            collection_revision = int(scope["collection_revision"]) + 1
            values = (
                _RECORD_SCHEMA_VERSION,
                memory_id,
                logical_key,
                revision,
                kind,
                content,
                content_sha256,
                priority,
                *binding.values,
                source.source_kind,
                source.source_run_id,
                source.source_turn_id,
                source.source_step,
                source.source_call_id,
                source.source_sha256,
                source.source_authority,
                source.extractor_identity,
                source.policy_digest,
                source.identity,
                observation_identity,
                collection_revision,
                sequence,
                "active",
            )
            database.execute(
                f"INSERT INTO memory_versions VALUES ({','.join('?' for _ in values)})",
                values,
            )
            database.execute(
                """
                INSERT INTO memory_heads(
                    memory_id, owner_id, app_id, scope_id, session_id,
                    binding_revision, current_revision, sequence, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(memory_id) DO UPDATE SET
                    current_revision = excluded.current_revision,
                    sequence = excluded.sequence,
                    status = excluded.status
                """,
                (memory_id, *binding.values, revision, sequence),
            )
            generation = int(scope["active_generation"])
            self._delete_terms(database, binding, generation, memory_id)
            self._insert_terms(
                database,
                binding,
                generation,
                memory_id,
                revision,
                logical_key,
                content,
            )
            database.execute(
                """
                UPDATE memory_scopes SET collection_revision = ?, next_sequence = ?
                WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                  AND binding_revision = ?
                """,
                (collection_revision, sequence + 1, *binding.values),
            )
            result = _canonical(
                {
                    "schema_version": 1,
                    "memory_id": memory_id,
                    "logical_key": logical_key,
                    "revision": revision,
                    "kind": kind,
                    "content_sha256": content_sha256,
                    "priority": priority,
                    "app_id": binding.app_id,
                    "scope_id": binding.scope_id,
                    "session_id": binding.session_id,
                    "binding_revision": binding.binding_revision,
                    "source_run_id": source.source_run_id,
                    "source_turn_id": source.source_turn_id,
                    "source_step": source.source_step,
                    "source_call_id": source.source_call_id,
                    "source_sha256": source.source_sha256,
                    "source_authority": source.source_authority,
                    "source_identity": source.identity,
                    "observation_identity": observation_identity,
                    "collection_revision": collection_revision,
                    "sequence": sequence,
                    "status": "active",
                }
            )
            database.execute(
                "INSERT INTO memory_observations VALUES (?, ?, ?)",
                (observation_identity, request_sha256, result),
            )
            self._record_operation(
                database,
                binding,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                operation_kind="remember",
                result=result,
            )
            self._fault("before_remember_commit")
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    @staticmethod
    def _head(
        database: sqlite3.Connection, binding: MemoryBinding, memory_id: str
    ) -> sqlite3.Row | None:
        row = database.execute(
            "SELECT * FROM memory_heads WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is not None and tuple(row[name] for name in (
            "owner_id", "app_id", "scope_id", "session_id", "binding_revision"
        )) != binding.values:
            raise MemoryBindingError("Memory item belongs to another binding")
        return row

    @staticmethod
    def _version(
        database: sqlite3.Connection, memory_id: str, revision: int
    ) -> sqlite3.Row:
        row = database.execute(
            "SELECT * FROM memory_versions WHERE memory_id = ? AND revision = ?",
            (memory_id, revision),
        ).fetchone()
        if row is None:
            raise MemoryCorruptionError("Memory head has no matching version")
        return row

    @staticmethod
    def _validate_version(row: sqlite3.Row, binding: MemoryBinding) -> None:
        try:
            if int(row["schema_version"]) != _RECORD_SCHEMA_VERSION:
                raise MemoryCorruptionError("Memory record schema is unknown")
            if tuple(row[name] for name in (
                "owner_id", "app_id", "scope_id", "session_id", "binding_revision"
            )) != binding.values:
                raise MemoryCorruptionError("Memory record crossed a binding boundary")
            memory_id = _sha256(str(row["memory_id"]), "memory_id")
            logical_key = _identifier(str(row["logical_key"]), "logical_key")
            revision = _positive_integer(int(row["revision"]), "revision")
            kind = _identifier(str(row["kind"]), "kind")
            if kind not in ("episodic", "semantic", "procedural"):
                raise MemoryValidationError("kind is not supported")
            priority = _nonnegative_integer(
                int(row["priority"]), "priority", maximum=100
            )
            del revision, priority
            status = str(row["status"])
            if status not in ("active", "tombstone"):
                raise MemoryValidationError("status is invalid")
            content = str(row["content"])
            _utf8(
                content,
                "content",
                minimum=1 if status == "active" else 0,
                maximum=2**31 - 1,
            )
            if status == "tombstone" and content:
                raise MemoryValidationError("tombstone content must be empty")
            content_sha256 = _sha256(
                str(row["content_sha256"]), "content_sha256"
            )
            if content_sha256 != hashlib.sha256(content.encode("utf-8")).hexdigest():
                raise MemoryValidationError("content digest does not match")
            source = MemorySource(
                str(row["source_kind"]),
                str(row["source_run_id"]),
                str(row["source_turn_id"]),
                int(row["source_step"]),
                str(row["source_call_id"]),
                str(row["source_sha256"]),
                str(row["source_authority"]),
                str(row["extractor_identity"]),
                str(row["policy_digest"]),
            )
            if str(row["source_identity"]) != source.identity:
                raise MemoryValidationError("source identity does not match")
            expected_memory_id = _digest(
                {"binding": asdict(binding), "logical_key": logical_key}
            )
            if memory_id != expected_memory_id:
                raise MemoryValidationError("memory identity does not match")
            expected_observation = _digest(
                {
                    "binding": asdict(binding),
                    "memory_id": memory_id,
                    "source": asdict(source),
                }
            )
            if str(row["observation_identity"]) != expected_observation:
                raise MemoryValidationError("observation identity does not match")
            _positive_integer(
                int(row["collection_revision"]), "collection_revision"
            )
            _positive_integer(int(row["sequence"]), "sequence")
        except MemoryCorruptionError:
            raise
        except (MemoryValidationError, TypeError, ValueError, OverflowError):
            raise MemoryCorruptionError("Memory record is structurally invalid") from None

    def _tombstone(
        self,
        database: sqlite3.Connection,
        binding: MemoryBinding,
        head: sqlite3.Row,
        source: MemorySource,
        collection_revision: int,
        sequence: int,
    ) -> int:
        current_revision = int(head["current_revision"])
        current = self._version(database, str(head["memory_id"]), current_revision)
        self._validate_version(current, binding)
        revision = current_revision + 1
        empty_digest = hashlib.sha256(b"").hexdigest()
        observation_identity = self._observation_identity(
            binding, str(head["memory_id"]), source
        )
        values = (
            _RECORD_SCHEMA_VERSION,
            str(head["memory_id"]),
            str(current["logical_key"]),
            revision,
            str(current["kind"]),
            "",
            empty_digest,
            int(current["priority"]),
            *binding.values,
            source.source_kind,
            source.source_run_id,
            source.source_turn_id,
            source.source_step,
            source.source_call_id,
            source.source_sha256,
            source.source_authority,
            source.extractor_identity,
            source.policy_digest,
            source.identity,
            observation_identity,
            collection_revision,
            sequence,
            "tombstone",
        )
        database.execute(
            f"INSERT INTO memory_versions VALUES ({','.join('?' for _ in values)})",
            values,
        )
        database.execute(
            """
            UPDATE memory_heads
            SET current_revision = ?, sequence = ?, status = 'tombstone'
            WHERE memory_id = ?
            """,
            (revision, sequence, str(head["memory_id"])),
        )
        return revision

    def forget_exact(
        self,
        binding: MemoryBinding,
        memory_id: str,
        *,
        expected_revision: int,
        source: MemorySource,
        idempotency_key: str,
    ) -> str:
        memory_id = _sha256(memory_id, "memory_id")
        expected_revision = _nonnegative_integer(expected_revision, "expected_revision")
        idempotency_key = self._idempotency_key(idempotency_key)
        request = {
            "operation": "forget_exact",
            "binding": asdict(binding),
            "memory_id": memory_id,
            "expected_revision": expected_revision,
            "source": asdict(source),
        }
        request_sha256 = _digest(request)
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = self._operation(
                database,
                binding,
                idempotency_key,
                request_sha256,
                "forget_exact",
                {"memory_id": memory_id},
            )
            if existing is not None:
                database.commit()
                return existing
            scope = self._ensure_scope(database, binding)
            self._require_available(scope)
            suppression = database.execute(
                "SELECT 1 FROM memory_suppressions WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            head = self._head(database, binding, memory_id)
            if suppression is not None:
                revision = 0 if head is None else int(head["current_revision"])
                result = _canonical(
                    {"memory_id": memory_id, "revision": revision, "status": "tombstone", "already_deleted": True}
                )
            else:
                if head is None or str(head["status"]) != "active":
                    raise MemoryRevisionConflict("Memory item is not active")
                if int(head["current_revision"]) != expected_revision:
                    raise MemoryRevisionConflict("expected Memory revision is stale")
                collection_revision = int(scope["collection_revision"]) + 1
                sequence = int(scope["next_sequence"])
                revision = self._tombstone(
                    database,
                    binding,
                    head,
                    source,
                    collection_revision,
                    sequence,
                )
                database.execute(
                    "INSERT INTO memory_suppressions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, *binding.values, idempotency_key),
                )
                self._delete_terms(
                    database, binding, int(scope["active_generation"]), memory_id
                )
                database.execute(
                    """
                    UPDATE memory_scopes SET collection_revision = ?, next_sequence = ?
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ?
                    """,
                    (collection_revision, sequence + 1, *binding.values),
                )
                result = _canonical(
                    {
                        "memory_id": memory_id,
                        "revision": revision,
                        "collection_revision": collection_revision,
                        "status": "tombstone",
                        "already_deleted": False,
                    }
                )
            exact_outcome = json.loads(result)
            self._record_delete_outcome(
                database,
                binding,
                operation_identity=idempotency_key,
                operation_kind="forget_exact",
                memory_id=memory_id,
                revision=revision,
                collection_revision=int(
                    exact_outcome.get(
                        "collection_revision", scope["collection_revision"]
                    )
                ),
                already_deleted=bool(exact_outcome["already_deleted"]),
                status="tombstone",
            )
            self._record_operation(
                database,
                binding,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                operation_kind="forget_exact",
                result=result,
            )
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    def forget_source(
        self,
        binding: MemoryBinding,
        source_identity: str,
        *,
        source: MemorySource,
        idempotency_key: str,
    ) -> str:
        source_identity = _sha256(source_identity, "source_identity")
        idempotency_key = self._idempotency_key(idempotency_key)
        request = {
            "operation": "forget_source",
            "binding": asdict(binding),
            "source_identity": source_identity,
            "source": asdict(source),
        }
        request_sha256 = _digest(request)
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = self._operation(
                database,
                binding,
                idempotency_key,
                request_sha256,
                "forget_source",
                {"source_identity": source_identity},
            )
            if existing is not None:
                database.commit()
                return existing
            scope = self._ensure_scope(database, binding)
            self._require_available(scope)
            database.execute(
                """
                INSERT OR IGNORE INTO memory_source_suppressions(
                    owner_id, app_id, scope_id, session_id, binding_revision,
                    source_identity, operation_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*binding.values, source_identity, idempotency_key),
            )
            heads = database.execute(
                """
                SELECT h.* FROM memory_heads h
                JOIN memory_versions v ON v.memory_id = h.memory_id
                                      AND v.revision = h.current_revision
                WHERE h.owner_id = ? AND h.app_id = ? AND h.scope_id = ?
                  AND h.session_id = ? AND h.binding_revision = ?
                  AND h.status = 'active' AND v.source_identity = ?
                ORDER BY h.memory_id
                """,
                (*binding.values, source_identity),
            ).fetchall()
            collection_revision = int(scope["collection_revision"])
            sequence = int(scope["next_sequence"])
            for head in heads:
                collection_revision += 1
                self._tombstone(
                    database, binding, head, source, collection_revision, sequence
                )
                self._delete_terms(
                    database,
                    binding,
                    int(scope["active_generation"]),
                    str(head["memory_id"]),
                )
                sequence += 1
            if heads:
                database.execute(
                    """
                    UPDATE memory_scopes SET collection_revision = ?, next_sequence = ?
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ?
                    """,
                    (collection_revision, sequence, *binding.values),
                )
            result = _canonical(
                {
                    "source_identity": source_identity,
                    "deleted_count": len(heads),
                    "collection_revision": collection_revision,
                    "status": "suppressed",
                }
            )
            self._record_delete_outcome(
                database,
                binding,
                operation_identity=idempotency_key,
                operation_kind="forget_source",
                source_identity=source_identity,
                deleted_count=len(heads),
                collection_revision=collection_revision,
                status="suppressed",
            )
            self._record_operation(
                database,
                binding,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                operation_kind="forget_source",
                result=result,
            )
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    def forget_scope(
        self,
        binding: MemoryBinding,
        *,
        source: MemorySource,
        idempotency_key: str,
    ) -> str:
        idempotency_key = self._idempotency_key(idempotency_key)
        request = {
            "operation": "forget_scope",
            "binding": asdict(binding),
            "source": asdict(source),
        }
        request_sha256 = _digest(request)
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = self._operation(
                database,
                binding,
                idempotency_key,
                request_sha256,
                "forget_scope",
                {},
            )
            if existing is not None:
                database.commit()
                return existing
            scope = self._ensure_scope(database, binding)
            heads = database.execute(
                """
                SELECT * FROM memory_heads
                WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                  AND binding_revision = ? AND status = 'active'
                ORDER BY memory_id
                """,
                binding.values,
            ).fetchall()
            collection_revision = int(scope["collection_revision"])
            sequence = int(scope["next_sequence"])
            if int(scope["scope_suppressed"]) == 0:
                for head in heads:
                    collection_revision += 1
                    self._tombstone(
                        database, binding, head, source, collection_revision, sequence
                    )
                    sequence += 1
                database.execute(
                    """
                    DELETE FROM memory_terms
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ? AND generation = ?
                    """,
                    (*binding.values, int(scope["active_generation"])),
                )
                database.execute(
                    """
                    UPDATE memory_scopes SET collection_revision = ?, next_sequence = ?,
                                             scope_suppressed = 1
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ?
                    """,
                    (collection_revision, sequence, *binding.values),
                )
            result = _canonical(
                {
                    "deleted_count": len(heads),
                    "collection_revision": collection_revision,
                    "status": "scope_suppressed",
                }
            )
            self._record_delete_outcome(
                database,
                binding,
                operation_identity=idempotency_key,
                operation_kind="forget_scope",
                deleted_count=len(heads),
                collection_revision=collection_revision,
                status="scope_suppressed",
            )
            self._record_operation(
                database,
                binding,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                operation_kind="forget_scope",
                result=result,
            )
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    def _search_envelope(
        self,
        binding: MemoryBinding,
        normalized_query: str,
        terms: tuple[str, ...],
        collection_revision: int,
        generation: int,
        matches: list[dict[str, object]],
        *,
        truncated: bool,
        omitted_count: int,
        scope_status: str = "active",
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "app_id": binding.app_id,
            "scope_id": binding.scope_id,
            "session_id": binding.session_id,
            "binding_revision": binding.binding_revision,
            "query": normalized_query,
            "query_sha256": hashlib.sha256(normalized_query.encode("utf-8")).hexdigest(),
            "normalized_terms": list(terms),
            "collection_revision": collection_revision,
            "generation": generation,
            "scope_status": scope_status,
            "retrieval_policy_version": self.policy.retrieval_policy_version,
            "score_kind": "term_coverage_bps",
            "score_direction": "higher_is_better",
            "matches": matches,
            "truncated": truncated,
            "omitted_count": omitted_count,
        }

    def project_automatic_query(self, query: str) -> str:
        """Project an ordinary user turn into the explicit search contract.

        Explicit ``search`` remains strict. Automatic recall uses this bounded,
        deterministic UTF-8-safe projection so a valid long Agent input cannot
        fail merely because it contains more search terms than Memory accepts.
        """

        query = _utf8(query, "query", minimum=0, maximum=2**31 - 1)
        normalized = unicodedata.normalize("NFKC", query).casefold().strip()
        encoded = normalized.encode("utf-8")
        clipped = encoded[: self.policy.max_query_bytes]
        while True:
            try:
                bounded_text = clipped.decode("utf-8")
                break
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        all_terms = _terms(bounded_text)
        selected = all_terms[: self.policy.max_terms]
        projected = " ".join(selected)
        projected_bytes = projected.encode("utf-8")
        if len(projected_bytes) > self.policy.max_query_bytes:
            raise MemoryCorruptionError("automatic Memory query projection is invalid")
        return _canonical(
            {
                "projection_version": "automatic-lexical-v1",
                "source_sha256": hashlib.sha256(encoded).hexdigest(),
                "projected_sha256": hashlib.sha256(projected_bytes).hexdigest(),
                "query": projected,
                "truncated": (
                    len(encoded) > self.policy.max_query_bytes
                    or len(all_terms) > self.policy.max_terms
                    or projected != normalized
                ),
            }
        )

    def search(self, binding: MemoryBinding, query: str, top_k: int) -> str:
        query = _utf8(query, "query", minimum=0, maximum=self.policy.max_query_bytes)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= self.policy.max_top_k:
            raise MemoryValidationError(
                f"top_k must be an integer from 1 to {self.policy.max_top_k}"
            )
        normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
        terms = _terms(normalized_query, self.policy.max_terms)
        database = self._open(read_only=True)
        try:
            database.execute("BEGIN")
            scope = self._scope(database, binding)
            if scope is None:
                envelope = self._search_envelope(
                    binding, normalized_query, terms, 0, 1, [], truncated=False, omitted_count=0
                )
                database.commit()
                return _canonical(envelope)
            collection_revision = int(scope["collection_revision"])
            generation = int(scope["active_generation"])
            if int(scope["scope_suppressed"]) != 0:
                envelope = self._search_envelope(
                    binding,
                    normalized_query,
                    terms,
                    collection_revision,
                    generation,
                    [],
                    scope_status="suppressed",
                    truncated=False,
                    omitted_count=0,
                )
                database.commit()
                return _canonical(envelope)
            count = int(
                database.execute(
                    """
                    SELECT COUNT(*) FROM memory_heads
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ? AND status = 'active'
                    """,
                    binding.values,
                ).fetchone()[0]
            )
            if count > self.policy.max_scope_records:
                raise MemoryValidationError("Memory scope exceeds the retrieval record limit")
            if not terms:
                envelope = self._search_envelope(
                    binding,
                    normalized_query,
                    terms,
                    collection_revision,
                    generation,
                    [],
                    truncated=False,
                    omitted_count=0,
                )
                database.commit()
                return _canonical(envelope)
            placeholders = ",".join("?" for _ in terms)
            rows = database.execute(
                f"""
                SELECT v.*, h.sequence AS head_sequence,
                       COUNT(DISTINCT t.term) AS matched_terms
                FROM memory_terms t
                JOIN memory_heads h ON h.memory_id = t.memory_id
                                   AND h.current_revision = t.revision
                                   AND h.status = 'active'
                JOIN memory_versions v ON v.memory_id = h.memory_id
                                      AND v.revision = h.current_revision
                WHERE t.owner_id = ? AND t.app_id = ? AND t.scope_id = ?
                  AND t.session_id = ? AND t.binding_revision = ?
                  AND t.generation = ? AND t.term IN ({placeholders})
                  AND h.owner_id = ? AND h.app_id = ? AND h.scope_id = ?
                  AND h.session_id = ? AND h.binding_revision = ?
                  AND v.owner_id = ? AND v.app_id = ? AND v.scope_id = ?
                  AND v.session_id = ? AND v.binding_revision = ?
                  AND v.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_suppressions ms
                      WHERE ms.memory_id = v.memory_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_source_suppressions mss
                      WHERE mss.owner_id = v.owner_id AND mss.app_id = v.app_id
                        AND mss.scope_id = v.scope_id
                        AND mss.session_id = v.session_id
                        AND mss.binding_revision = v.binding_revision
                        AND mss.source_identity = v.source_identity
                  )
                GROUP BY t.memory_id, t.revision
                ORDER BY matched_terms DESC, v.priority DESC,
                         h.sequence DESC, t.memory_id ASC
                LIMIT ?
                """,
                (
                    *binding.values,
                    generation,
                    *terms,
                    *binding.values,
                    *binding.values,
                    self.policy.max_candidates + 1,
                ),
            ).fetchall()
            candidate_overflow = len(rows) > self.policy.max_candidates
            candidates = rows[: self.policy.max_candidates]
            matches: list[dict[str, object]] = []
            omitted = max(0, len(rows) - self.policy.max_candidates)
            for row in candidates:
                self._validate_version(row, binding)
                if int(row["head_sequence"]) != int(row["sequence"]):
                    raise MemoryCorruptionError("Memory head sequence is inconsistent")
                content = str(row["content"])
                if len(content.encode("utf-8")) > self.policy.max_record_bytes:
                    raise MemoryCorruptionError("Memory record exceeds the configured limit")
                item = {
                    "rank": len(matches) + 1,
                    "score": int(row["matched_terms"]) * 10_000 // len(terms),
                    "score_kind": "term_coverage_bps",
                    "score_direction": "higher_is_better",
                    "memory_id": str(row["memory_id"]),
                    "logical_key": str(row["logical_key"]),
                    "revision": int(row["revision"]),
                    "kind": str(row["kind"]),
                    "content": content,
                    "content_sha256": str(row["content_sha256"]),
                    "priority": int(row["priority"]),
                    "source_kind": str(row["source_kind"]),
                    "source_run_id": str(row["source_run_id"]),
                    "source_turn_id": str(row["source_turn_id"]),
                    "source_step": int(row["source_step"]),
                    "source_call_id": str(row["source_call_id"]),
                    "source_sha256": str(row["source_sha256"]),
                    "source_authority": str(row["source_authority"]),
                    "source_identity": str(row["source_identity"]),
                    "observation_identity": str(row["observation_identity"]),
                    "sequence": int(row["sequence"]),
                }
                tentative = matches + [item]
                envelope = self._search_envelope(
                    binding,
                    normalized_query,
                    terms,
                    collection_revision,
                    generation,
                    tentative,
                    truncated=True,
                    omitted_count=omitted + len(candidates) - len(tentative),
                )
                if len(_canonical(envelope).encode("utf-8")) > self.policy.max_result_bytes:
                    omitted += len(candidates) - len(matches)
                    break
                matches.append(item)
                if len(matches) == top_k:
                    omitted += len(candidates) - len(matches)
                    break
            truncated = candidate_overflow or omitted > 0
            envelope = self._search_envelope(
                binding,
                normalized_query,
                terms,
                collection_revision,
                generation,
                matches,
                truncated=truncated,
                omitted_count=omitted,
            )
            result = _canonical(envelope)
            if len(result.encode("utf-8")) > self.policy.max_result_bytes:
                raise MemoryCorruptionError("Memory result envelope exceeds its limit")
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except BaseException:
            self._rollback(database)
            raise
        finally:
            database.close()

    def rebuild(self, binding: MemoryBinding) -> str:
        database = self._open()
        try:
            database.execute("BEGIN IMMEDIATE")
            scope = self._ensure_scope(database, binding)
            self._require_available(scope)
            old_generation = int(scope["active_generation"])
            maximum = int(
                database.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0) FROM memory_terms
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ?
                    """,
                    binding.values,
                ).fetchone()[0]
            )
            new_generation = max(old_generation, maximum) + 1
            rows = database.execute(
                """
                SELECT v.* FROM memory_heads h
                JOIN memory_versions v ON v.memory_id = h.memory_id
                                      AND v.revision = h.current_revision
                WHERE h.owner_id = ? AND h.app_id = ? AND h.scope_id = ?
                  AND h.session_id = ? AND h.binding_revision = ?
                  AND h.status = 'active'
                ORDER BY h.memory_id
                """,
                binding.values,
            ).fetchall()
            for row in rows:
                self._validate_version(row, binding)
                source_suppressed = database.execute(
                    """
                    SELECT 1 FROM memory_source_suppressions
                    WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                      AND binding_revision = ? AND source_identity = ?
                    """,
                    (*binding.values, str(row["source_identity"])),
                ).fetchone()
                memory_suppressed = database.execute(
                    "SELECT 1 FROM memory_suppressions WHERE memory_id = ?",
                    (str(row["memory_id"]),),
                ).fetchone()
                if source_suppressed is not None or memory_suppressed is not None:
                    raise MemoryCorruptionError("suppressed Memory remained active")
                self._insert_terms(
                    database,
                    binding,
                    new_generation,
                    str(row["memory_id"]),
                    int(row["revision"]),
                    str(row["logical_key"]),
                    str(row["content"]),
                )
                self._fault("after_rebuild_record")
            self._fault("before_generation_switch")
            collection_revision = int(scope["collection_revision"]) + 1
            database.execute(
                """
                UPDATE memory_scopes SET active_generation = ?, collection_revision = ?
                WHERE owner_id = ? AND app_id = ? AND scope_id = ? AND session_id = ?
                  AND binding_revision = ?
                """,
                (new_generation, collection_revision, *binding.values),
            )
            result = _canonical(
                {
                    "old_generation": old_generation,
                    "generation": new_generation,
                    "record_count": len(rows),
                    "collection_revision": collection_revision,
                }
            )
            operation_identity = _digest(
                {
                    "operation": "rebuild",
                    "binding": asdict(binding),
                    "generation": new_generation,
                }
            )
            database.execute(
                "INSERT INTO memory_audit VALUES (?, ?, ?, ?, ?, ?, 'rebuild', 'committed', ?)",
                (
                    operation_identity,
                    *binding.values,
                    hashlib.sha256(result.encode("utf-8")).hexdigest(),
                ),
            )
            database.commit()
            return result
        except sqlite3.Error as error:
            self._rollback(database)
            raise self._database_error(error) from None
        except MemoryError:
            self._rollback(database)
            raise
        except BaseException as error:
            self._rollback(database)
            raise MemoryRebuildError("Memory rebuild did not commit") from error
        finally:
            database.close()

    def record_versions(self, binding: MemoryBinding, memory_id: str) -> str:
        memory_id = _sha256(memory_id, "memory_id")
        database = self._open(read_only=True)
        try:
            rows = database.execute(
                """
                SELECT * FROM memory_versions
                WHERE memory_id = ? AND owner_id = ? AND app_id = ? AND scope_id = ?
                  AND session_id = ? AND binding_revision = ?
                ORDER BY revision
                """,
                (memory_id, *binding.values),
            ).fetchall()
            versions = []
            for row in rows:
                self._validate_version(row, binding)
                versions.append(
                    {
                        "revision": int(row["revision"]),
                        "status": str(row["status"]),
                        "content": str(row["content"]),
                        "content_sha256": str(row["content_sha256"]),
                        "source_identity": str(row["source_identity"]),
                        "observation_identity": str(row["observation_identity"]),
                    }
                )
            return _canonical({"memory_id": memory_id, "versions": versions})
        finally:
            database.close()

    def operation_count(self) -> int:
        database = self._open(read_only=True)
        try:
            return int(database.execute("SELECT COUNT(*) FROM memory_operations").fetchone()[0])
        finally:
            database.close()


__all__ = [
    "MemoryBinding",
    "MemoryBindingError",
    "MemoryConfigurationError",
    "MemoryCorruptionError",
    "MemoryDatabaseError",
    "MemoryError",
    "MemoryIdempotencyConflict",
    "MemoryPolicy",
    "MemoryRebuildError",
    "MemoryRevisionConflict",
    "MemoryScopeSuppressed",
    "MemorySource",
    "MemoryStore",
    "MemorySuppressedError",
    "MemoryValidationError",
]
