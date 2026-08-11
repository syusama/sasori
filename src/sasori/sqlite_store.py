from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from sasori_core.contracts import Event, Message, ModelReply, ToolCall
from sasori_core.store import (
    ApprovalConflict,
    ApprovalMismatch,
    CallRecord,
    ConcurrentRunError,
    DuplicateCallIdError,
    RunAlreadyExists,
    RunNotFound,
    Snapshot,
    StoredEvent,
    StoreError,
)
from ._sqlite_lock import acquire_process_lock, release_process_lock


class SchemaVersionError(StoreError):
    pass


class ArtifactRegistrationConflict(StoreError):
    pass


class ArtifactLimitExceeded(StoreError):
    pass


def _acquire_process_lock(path: str):
    try:
        return acquire_process_lock(path)
    except (OSError, BlockingIOError) as exc:
        raise ConcurrentRunError(
            "SQLiteStore allows one file-backed owner process"
        ) from exc


def _release_process_lock(stream) -> None:
    release_process_lock(stream)


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    run_id: str
    content_sha256: str
    size: int
    declared_filename: str
    declared_media_type: str | None
    detected_media_type: str
    created_seq: int


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("mapping keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"value is not JSON-like: {type(value).__name__}")


def _dump(value: object) -> str:
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _tool_call_data(call: ToolCall) -> dict[str, object]:
    try:
        arguments = _plain(call.arguments)
    except (TypeError, ValueError):
        arguments = {"__invalid_arguments__": type(call.arguments).__name__}
    return {
        "id": call.id,
        "name": call.name,
        "arguments": arguments,
        "complete": call.complete,
    }


def _tool_call_from_data(data: Mapping[str, object]) -> ToolCall:
    return ToolCall(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        arguments=data.get("arguments", {}),
        complete=bool(data.get("complete", False)),
    )


def _message_data(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [_tool_call_data(call) for call in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "tool_name": message.tool_name,
        "error_code": message.error_code,
        "provider_state": message.provider_state,
    }


def _message_from_data(data: Mapping[str, object]) -> Message:
    calls = data.get("tool_calls", [])
    return Message(
        role=str(data["role"]),
        content=str(data.get("content", "")),
        tool_calls=tuple(
            _tool_call_from_data(call) for call in calls if isinstance(call, Mapping)
        ),
        tool_call_id=data.get("tool_call_id") if isinstance(data.get("tool_call_id"), str) else None,
        tool_name=data.get("tool_name") if isinstance(data.get("tool_name"), str) else None,
        error_code=data.get("error_code") if isinstance(data.get("error_code"), str) else None,
        provider_state=(
            data.get("provider_state")
            if isinstance(data.get("provider_state"), str)
            else None
        ),
    )


def _reply_data(reply: ModelReply) -> dict[str, object]:
    return {
        "content": reply.content,
        "tool_calls": [
            _tool_call_data(call)
            for call in reply.tool_calls
            if isinstance(call, ToolCall)
        ],
        "provider_state": reply.provider_state,
    }


def _reply_from_data(data: Mapping[str, object]) -> ModelReply:
    calls = data.get("tool_calls", [])
    return ModelReply(
        content=str(data.get("content", "")),
        tool_calls=tuple(
            _tool_call_from_data(call) for call in calls if isinstance(call, Mapping)
        ),
        provider_state=(
            data.get("provider_state")
            if isinstance(data.get("provider_state"), str)
            else None
        ),
    )


def _snapshot_data(snapshot: Snapshot) -> dict[str, object]:
    return {
        "app_id": snapshot.app_id,
        "status": snapshot.status,
        "step": snapshot.step,
        "history": [_message_data(message) for message in snapshot.history],
        "accepted_reply": (
            _reply_data(snapshot.accepted_reply) if snapshot.accepted_reply else None
        ),
        "final_message": (
            _message_data(snapshot.final_message) if snapshot.final_message else None
        ),
    }


def _stored_artifact(row: sqlite3.Row) -> StoredArtifact:
    return StoredArtifact(
        artifact_id=str(row["artifact_id"]),
        run_id=str(row["run_id"]),
        content_sha256=str(row["content_sha256"]),
        size=int(row["size"]),
        declared_filename=str(row["declared_filename"]),
        declared_media_type=(
            str(row["declared_media_type"])
            if row["declared_media_type"] is not None
            else None
        ),
        detected_media_type=str(row["detected_media_type"]),
        created_seq=int(row["created_seq"]),
    )


class SQLiteStore:
    """Single-writer SQLite state store; it does not provide a process lease."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._process_lock = (
            _acquire_process_lock(self.path) if self.path != ":memory:" else None
        )
        self._db = None
        try:
            self._db = sqlite3.connect(self.path, isolation_level=None, timeout=0)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA synchronous = FULL")
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, 2, 3, 4):
                raise SchemaVersionError(
                    f"unsupported SQLiteStore schema version: {version}"
                )
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
                self._db.execute("PRAGMA locking_mode = EXCLUSIVE")
            if version == 1:
                self._migrate_v1()
                version = 2
            if version == 2:
                self._migrate_v2()
                version = 3
            if version == 3:
                self._migrate_v3()
                version = 4
            self._create_schema()
            if version == 0:
                self._db.execute("PRAGMA user_version = 4")
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"SQLiteStore cleanup failed: {cleanup_error}")
            if isinstance(error, sqlite3.OperationalError) and (
                "locked" in str(error).lower() or "busy" in str(error).lower()
            ):
                raise ConcurrentRunError(
                    "SQLiteStore allows one file-backed writer connection"
                ) from error
            raise

    def close(self) -> None:
        database, self._db = self._db, None
        process_lock, self._process_lock = self._process_lock, None
        try:
            if database is not None:
                database.close()
        finally:
            if process_lock is not None:
                _release_process_lock(process_lock)

    @property
    def closed(self) -> bool:
        return self._db is None

    def __enter__(self) -> SQLiteStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._db is None:
            raise StoreError("store is closed")

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                app_id TEXT,
                revision INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                step INTEGER NOT NULL,
                history_json TEXT NOT NULL,
                accepted_reply_json TEXT,
                final_message_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                seq INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                generation INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                PRIMARY KEY (run_id, generation)
            );
            CREATE TABLE IF NOT EXISTS accepted_replies (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                step INTEGER NOT NULL,
                reply_json TEXT NOT NULL,
                PRIMARY KEY (run_id, step)
            );
            CREATE TABLE IF NOT EXISTS tool_calls (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                step INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                call_id TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                name TEXT,
                arguments_json TEXT NOT NULL,
                arguments_valid INTEGER NOT NULL,
                complete INTEGER NOT NULL,
                effect TEXT NOT NULL,
                idempotency_key TEXT,
                tool_revision TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                PRIMARY KEY (run_id, step, ordinal)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_run_call_id
                ON tool_calls(run_id, call_id)
                WHERE call_id IS NOT NULL AND call_id <> '';
            CREATE TABLE IF NOT EXISTS approvals (
                fingerprint TEXT PRIMARY KEY REFERENCES tool_calls(fingerprint),
                run_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                decision INTEGER
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                content_sha256 TEXT NOT NULL CHECK (
                    length(content_sha256) = 64
                    AND content_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                size INTEGER NOT NULL CHECK (size >= 0 AND size <= 16777216),
                declared_filename TEXT NOT NULL,
                declared_media_type TEXT,
                detected_media_type TEXT NOT NULL,
                created_seq INTEGER NOT NULL,
                UNIQUE (run_id, created_seq),
                FOREIGN KEY (run_id, created_seq) REFERENCES events(run_id, seq)
            );
            CREATE INDEX IF NOT EXISTS artifacts_run
                ON artifacts(run_id, created_seq);
            CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS checkpoints_no_update
                BEFORE UPDATE ON checkpoints BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete
                BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS replies_no_update
                BEFORE UPDATE ON accepted_replies BEGIN SELECT RAISE(ABORT, 'accepted replies are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS replies_no_delete
                BEFORE DELETE ON accepted_replies BEGIN SELECT RAISE(ABORT, 'accepted replies are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS artifacts_no_update
                BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS artifacts_no_delete
                BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
            """
        )

    def _migrate_v1(self) -> None:
        self._begin()
        try:
            columns = {
                row[1] for row in self._db.execute("PRAGMA table_info(tool_calls)")
            }
            if "tool_revision" not in columns:
                self._db.execute(
                    """
                    ALTER TABLE tool_calls ADD COLUMN tool_revision TEXT NOT NULL
                    DEFAULT 'legacy-unversioned'
                    """
                )
            self._db.execute("PRAGMA user_version = 2")
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _migrate_v2(self) -> None:
        self._begin()
        try:
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
            if "app_id" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN app_id TEXT")
            self._db.execute("PRAGMA user_version = 3")
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _migrate_v3(self) -> None:
        self._begin()
        try:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    content_sha256 TEXT NOT NULL CHECK (
                        length(content_sha256) = 64
                        AND content_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    size INTEGER NOT NULL CHECK (size >= 0 AND size <= 16777216),
                    declared_filename TEXT NOT NULL,
                    declared_media_type TEXT,
                    detected_media_type TEXT NOT NULL,
                    created_seq INTEGER NOT NULL,
                    UNIQUE (run_id, created_seq),
                    FOREIGN KEY (run_id, created_seq) REFERENCES events(run_id, seq)
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id, created_seq)"
            )
            self._db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS artifacts_no_update
                BEFORE UPDATE ON artifacts BEGIN
                    SELECT RAISE(ABORT, 'artifacts are immutable');
                END
                """
            )
            self._db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS artifacts_no_delete
                BEFORE DELETE ON artifacts BEGIN
                    SELECT RAISE(ABORT, 'artifacts are immutable');
                END
                """
            )
            self._db.execute("PRAGMA user_version = 4")
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _begin(self) -> None:
        self._ensure_open()
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise ConcurrentRunError("SQLiteStore is single-writer; database is busy") from error

    def _insert_events(self, run_id: str, events: Sequence[Event]) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        seq = int(row["seq"])
        for event in events:
            seq += 1
            self._db.execute(
                "INSERT INTO events(run_id, seq, event_json) VALUES (?, ?, ?)",
                (
                    run_id,
                    seq,
                    _dump(
                        {
                            "type": event.type,
                            "run_id": event.run_id,
                            "step": event.step,
                            "data": event.data,
                            "version": event.version,
                            "tool_name": event.tool_name,
                            "call_id": event.call_id,
                        }
                    ),
                ),
            )
        return seq

    def _commit(
        self,
        current: Snapshot,
        updated: Snapshot,
        events: Sequence[Event] = (),
        mutate: Callable[[], None] | None = None,
    ) -> Snapshot:
        revision = current.revision + 1
        generation = current.generation + 1
        durable = replace(updated, revision=revision, generation=generation)
        self._begin()
        try:
            if mutate is not None:
                mutate()
            cursor = self._db.execute(
                """
                UPDATE runs SET revision = ?, generation = ?, status = ?, step = ?,
                    history_json = ?, accepted_reply_json = ?, final_message_json = ?
                WHERE run_id = ? AND revision = ?
                """,
                (
                    revision,
                    generation,
                    durable.status,
                    durable.step,
                    _dump([_message_data(message) for message in durable.history]),
                    _dump(_reply_data(durable.accepted_reply)) if durable.accepted_reply else None,
                    _dump(_message_data(durable.final_message)) if durable.final_message else None,
                    durable.run_id,
                    current.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentRunError("run revision changed; concurrent driver rejected")
            self._db.execute(
                "INSERT INTO checkpoints(run_id, generation, snapshot_json) VALUES (?, ?, ?)",
                (durable.run_id, generation, _dump(_snapshot_data(durable))),
            )
            self._insert_events(durable.run_id, events)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return durable

    def start(
        self,
        run_id: str,
        messages: Sequence[Message],
        event: Event,
        *,
        app_id: str | None = None,
    ) -> Snapshot:
        snapshot = Snapshot(
            run_id, 1, 1, "ready_model", 0, tuple(messages), app_id=app_id
        )
        self._begin()
        try:
            self._db.execute(
                """
                INSERT INTO runs(
                    run_id, app_id, revision, generation, status, step,
                    history_json, accepted_reply_json, final_message_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    snapshot.app_id,
                    snapshot.revision,
                    snapshot.generation,
                    snapshot.status,
                    snapshot.step,
                    _dump([_message_data(message) for message in snapshot.history]),
                ),
            )
            self._db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?)",
                (run_id, 1, _dump(_snapshot_data(snapshot))),
            )
            self._insert_events(run_id, (event,))
            self._db.commit()
        except sqlite3.IntegrityError as error:
            self._db.rollback()
            raise RunAlreadyExists(run_id) from error
        except BaseException:
            self._db.rollback()
            raise
        return snapshot

    def load(self, run_id: str) -> Snapshot:
        self._ensure_open()
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        history_data = json.loads(row["history_json"])
        accepted = json.loads(row["accepted_reply_json"]) if row["accepted_reply_json"] else None
        final = json.loads(row["final_message_json"]) if row["final_message_json"] else None
        return Snapshot(
            run_id=run_id,
            revision=int(row["revision"]),
            generation=int(row["generation"]),
            status=str(row["status"]),
            step=int(row["step"]),
            history=tuple(_message_from_data(item) for item in history_data),
            accepted_reply=_reply_from_data(accepted) if isinstance(accepted, Mapping) else None,
            final_message=_message_from_data(final) if isinstance(final, Mapping) else None,
            app_id=row["app_id"],
        )

    def list_runs(
        self,
        *,
        limit: int,
        before: int | None = None,
        app_id: str | None = None,
    ) -> tuple[tuple[int, Snapshot], ...]:
        self._ensure_open()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("before must be a positive integer")
        clauses = []
        parameters: list[object] = []
        if before is not None:
            clauses.append("rowid < ?")
            parameters.append(before)
        if app_id is not None:
            clauses.append("app_id = ?")
            parameters.append(app_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._db.execute(
            f"SELECT rowid, run_id FROM runs{where} ORDER BY rowid DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return tuple((int(row["rowid"]), self.load(str(row["run_id"]))) for row in rows)

    def transition(
        self, current: Snapshot, updated: Snapshot, events: Sequence[Event] = ()
    ) -> Snapshot:
        return self._commit(current, updated, events)

    def accept_reply(
        self,
        current: Snapshot,
        updated: Snapshot,
        reply: ModelReply,
        calls: Sequence[CallRecord],
        events: Sequence[Event],
    ) -> Snapshot:
        def mutate() -> None:
            self._db.execute(
                "INSERT INTO accepted_replies VALUES (?, ?, ?)",
                (current.run_id, updated.step, _dump(_reply_data(reply))),
            )
            for call in calls:
                self._db.execute(
                    """
                    INSERT INTO tool_calls(
                        run_id, step, ordinal, call_id, fingerprint, name,
                        arguments_json, arguments_valid, complete, effect,
                        idempotency_key, tool_revision,
                        status, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        call.run_id,
                        call.step,
                        call.ordinal,
                        call.call_id,
                        call.fingerprint,
                        call.name,
                        _dump(call.arguments),
                        int(call.arguments_valid),
                        int(call.complete),
                        call.effect,
                        call.idempotency_key,
                        call.tool_revision,
                        call.status,
                    ),
                )

        try:
            return self._commit(current, updated, events, mutate)
        except sqlite3.IntegrityError as error:
            if "tool_calls.run_id, tool_calls.call_id" in str(error) or "UNIQUE" in str(error):
                raise DuplicateCallIdError("duplicate provider call id in one run") from error
            raise

    def calls(self, run_id: str, step: int) -> tuple[CallRecord, ...]:
        self._ensure_open()
        rows = self._db.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? AND step = ? ORDER BY ordinal",
            (run_id, step),
        ).fetchall()
        return tuple(self._call_from_row(row) for row in rows)

    def _call_from_row(self, row: sqlite3.Row) -> CallRecord:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return CallRecord(
            run_id=str(row["run_id"]),
            step=int(row["step"]),
            ordinal=int(row["ordinal"]),
            call_id=row["call_id"],
            fingerprint=str(row["fingerprint"]),
            name=row["name"],
            arguments=json.loads(row["arguments_json"]),
            arguments_valid=bool(row["arguments_valid"]),
            complete=bool(row["complete"]),
            effect=str(row["effect"]),
            idempotency_key=row["idempotency_key"],
            tool_revision=str(row["tool_revision"]),
            status=str(row["status"]),
            result=_message_from_data(result) if isinstance(result, Mapping) else None,
        )

    def update_call(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        status: str,
        events: Sequence[Event] = (),
        result: Message | None = None,
    ) -> Snapshot:
        def mutate() -> None:
            cursor = self._db.execute(
                """
                UPDATE tool_calls SET status = ?, result_json = ?
                WHERE run_id = ? AND step = ? AND ordinal = ? AND status = ?
                """,
                (
                    status,
                    _dump(_message_data(result)) if result else None,
                    call.run_id,
                    call.step,
                    call.ordinal,
                    call.status,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentRunError("tool call state changed; concurrent driver rejected")

        return self._commit(current, updated, events, mutate)

    def request_approval(
        self,
        current: Snapshot,
        updated: Snapshot,
        call: CallRecord,
        event: Event,
    ) -> Snapshot:
        def mutate() -> None:
            self._db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, NULL)",
                (call.fingerprint, call.run_id, call.step, call.ordinal),
            )
            self._db.execute(
                "UPDATE tool_calls SET status = 'awaiting_approval' WHERE fingerprint = ?",
                (call.fingerprint,),
            )

        return self._commit(current, updated, (event,), mutate)

    def approval(self, fingerprint: str) -> bool | None:
        self._ensure_open()
        row = self._db.execute(
            "SELECT decision FROM approvals WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None or row["decision"] is None:
            return None
        return bool(row["decision"])

    def resolve_approval(
        self, run_id: str, fingerprint: str, approved: bool, event: Event
    ) -> tuple[Snapshot, bool]:
        current = self.load(run_id)
        row = self._db.execute(
            "SELECT fingerprint, decision FROM approvals WHERE run_id = ? AND fingerprint = ?",
            (run_id, fingerprint),
        ).fetchone()
        if row is None:
            raise ApprovalMismatch("approval does not match the immutable call fingerprint")
        if row["decision"] is not None:
            if bool(row["decision"]) != approved:
                raise ApprovalConflict("approval was already resolved differently")
            return current, False

        updated = replace(current, status="awaiting_resume")

        def mutate() -> None:
            cursor = self._db.execute(
                "UPDATE approvals SET decision = ? WHERE fingerprint = ? AND decision IS NULL",
                (int(approved), fingerprint),
            )
            if cursor.rowcount != 1:
                raise ApprovalConflict("approval decision changed concurrently")
            self._db.execute(
                "UPDATE tool_calls SET status = ? WHERE fingerprint = ?",
                ("approved" if approved else "denied", fingerprint),
            )

        return self._commit(current, updated, (event,), mutate), True

    def register_artifact(
        self,
        run_id: str,
        *,
        artifact_id: str,
        content_sha256: str,
        size: int,
        declared_filename: str,
        declared_media_type: str | None,
        detected_media_type: str,
    ) -> StoredArtifact:
        current = self.load(run_id)
        supplied = (
            run_id,
            content_sha256,
            size,
            declared_filename,
            declared_media_type,
            detected_media_type,
        )
        self._begin()
        try:
            existing = self._db.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if existing is not None:
                stored = _stored_artifact(existing)
                actual = (
                    stored.run_id,
                    stored.content_sha256,
                    stored.size,
                    stored.declared_filename,
                    stored.declared_media_type,
                    stored.detected_media_type,
                )
                if actual != supplied:
                    raise ArtifactRegistrationConflict(
                        "artifact_id already names different immutable content"
                    )
                self._db.commit()
                return stored
            count = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            if count >= 128:
                raise ArtifactLimitExceeded(
                    "a run cannot register more than 128 artifacts"
                )
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            created_seq = int(row["seq"]) + 1
            public_ref = {
                "version": 1,
                "artifact_id": artifact_id,
                "run_id": run_id,
                "content_sha256": content_sha256,
                "size_bytes": size,
                "filename": declared_filename,
                "media_type": detected_media_type,
                "created_seq": created_seq,
            }
            event = Event(
                "artifact.available",
                run_id,
                current.step,
                data={"artifact": public_ref},
            )
            inserted_seq = self._insert_events(run_id, (event,))
            if inserted_seq != created_seq:
                raise ConcurrentRunError("artifact event sequence changed concurrently")
            self._db.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, run_id, content_sha256, size,
                    declared_filename, declared_media_type, detected_media_type,
                    created_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    content_sha256,
                    size,
                    declared_filename,
                    declared_media_type,
                    detected_media_type,
                    created_seq,
                ),
            )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return StoredArtifact(
            artifact_id=artifact_id,
            run_id=run_id,
            content_sha256=content_sha256,
            size=size,
            declared_filename=declared_filename,
            declared_media_type=declared_media_type,
            detected_media_type=detected_media_type,
            created_seq=created_seq,
        )

    def stored_artifacts(self, run_id: str) -> tuple[StoredArtifact, ...]:
        self.load(run_id)
        rows = self._db.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_seq",
            (run_id,),
        ).fetchall()
        return tuple(_stored_artifact(row) for row in rows)

    def stored_artifact(
        self, run_id: str, artifact_id: str
    ) -> StoredArtifact | None:
        self.load(run_id)
        row = self._db.execute(
            "SELECT * FROM artifacts WHERE run_id = ? AND artifact_id = ?",
            (run_id, artifact_id),
        ).fetchone()
        return _stored_artifact(row) if row is not None else None

    def stored_events(
        self, run_id: str, after_seq: int = 0
    ) -> tuple[StoredEvent, ...]:
        self._ensure_open()
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        rows = self._db.execute(
            """
            SELECT seq, event_json FROM events
            WHERE run_id = ? AND seq > ? ORDER BY seq
            """,
            (run_id, after_seq),
        ).fetchall()
        events = []
        for row in rows:
            data = json.loads(row["event_json"])
            events.append(
                StoredEvent(
                    seq=int(row["seq"]),
                    event=Event(
                        type=data["type"],
                        run_id=data["run_id"],
                        step=data["step"],
                        data=data["data"],
                        version=data["version"],
                        tool_name=data.get("tool_name"),
                        call_id=data.get("call_id"),
                    ),
                )
            )
        return tuple(events)

    def events(self, run_id: str) -> tuple[Event, ...]:
        return tuple(item.event for item in self.stored_events(run_id))

    def counts(self, run_id: str) -> Mapping[str, int]:
        self._ensure_open()
        return {
            "events": self._db.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()[0],
            "checkpoints": self._db.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()[0],
        }
