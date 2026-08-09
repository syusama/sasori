from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from sasori.sqlite_store import (
    ArtifactLimitExceeded,
    ArtifactRegistrationConflict,
    SQLiteStore,
    StoredArtifact,
)


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_FILENAME_BYTES = 255
_MAX_MEDIA_TYPE_BYTES = 127
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[A-Za-z0-9 =._+-]+)?\Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_STORAGE_KEY = re.compile(r"sha256/[0-9a-f]{2}/[0-9a-f]{64}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PREVIEWABLE = frozenset(
    {
        "application/json",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain; charset=utf-8",
    }
)


class ArtifactError(Exception):
    """Base error for the immutable artifact adapter."""


class ArtifactInvalid(ArtifactError):
    """Raised before invalid artifact input can touch storage."""


class ArtifactNotFound(ArtifactError):
    """Raised when an artifact is not owned by the requested run."""


class ArtifactConflict(ArtifactError):
    """Raised when an immutable artifact ID is reused for different bytes."""


class ArtifactCorrupted(ArtifactError):
    """Raised when durable metadata or blob bytes fail integrity checks."""


class ArtifactLimit(ArtifactError):
    """Raised when one run reaches the bounded artifact association limit."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    run_id: str
    content_sha256: str
    size_bytes: int
    filename: str
    media_type: str
    created_seq: int
    version: int = 1

    @property
    def can_preview_text(self) -> bool:
        return self.media_type in {
            "application/json",
            "text/plain; charset=utf-8",
        }


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    ref: ArtifactRef
    content: bytes


def validate_artifact_id(value: object) -> str:
    if not isinstance(value, str) or _ARTIFACT_ID.fullmatch(value) is None:
        raise ArtifactInvalid(
            "artifact_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    return value


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise ArtifactInvalid(
            "run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
    return value


def _validate_filename(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > _MAX_FILENAME_BYTES
    ):
        raise ArtifactInvalid("declared_filename must be one bounded plain filename")
    return value


def _validate_declared_media_type(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_MEDIA_TYPE_BYTES
        or _MEDIA_TYPE.fullmatch(value) is None
    ):
        raise ArtifactInvalid("declared_media_type is invalid")
    return value


def _strict_json(value: str) -> object:
    def invalid_constant(token: str) -> object:
        raise ValueError(f"invalid JSON constant: {token}")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, parse_constant=invalid_constant, object_pairs_hook=pairs)


def _detect_media_type(content: bytes) -> str:
    if (
        len(content) >= 24
        and content.startswith(_PNG_SIGNATURE)
        and content[12:16] == b"IHDR"
    ):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if "\x00" in text:
        return "application/octet-stream"
    try:
        _strict_json(text)
    except (ValueError, json.JSONDecodeError):
        return "text/plain; charset=utf-8"
    return "application/json"


def artifact_projection(ref: ArtifactRef) -> dict[str, object]:
    if not isinstance(ref, ArtifactRef):
        raise TypeError("artifact projection requires ArtifactRef")
    return {
        "version": ref.version,
        "artifact_id": ref.artifact_id,
        "run_id": ref.run_id,
        "content_sha256": ref.content_sha256,
        "size_bytes": ref.size_bytes,
        "filename": ref.filename,
        "media_type": ref.media_type,
        "created_seq": ref.created_seq,
    }


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _require_plain_directory(path: Path, message: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ArtifactCorrupted(message) from error
    if not stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(path):
        raise ArtifactCorrupted(message)


def _acquire_process_lock(path: Path):
    stream = path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except (OSError, BlockingIOError) as error:
        stream.close()
        raise ArtifactConflict("artifact store already has an owner process") from error


def _release_process_lock(stream) -> None:
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


class ArtifactStore:
    """Blob adapter backed by the run store's atomic artifact/event catalog."""

    def __init__(self, run_store: SQLiteStore, root: str | Path) -> None:
        if not isinstance(run_store, SQLiteStore) or run_store.closed:
            raise ArtifactInvalid("run_store must be an open SQLiteStore")
        raw_root = Path(root)
        if raw_root.exists() and _is_link_or_reparse(raw_root):
            raise ArtifactInvalid("artifact root must not be a symbolic link")
        self.root = raw_root.resolve()
        self._run_store: SQLiteStore | None = run_store
        self._lock = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _require_plain_directory(self.root, "artifact root must be a real directory")
            self._blobs = self.root / "blobs"
            if self._blobs.exists() and _is_link_or_reparse(self._blobs):
                raise ArtifactInvalid("artifact blob root must not be a symbolic link")
            self._blobs.mkdir(exist_ok=True)
            _require_plain_directory(
                self._blobs, "artifact blob root must be a real directory"
            )
            lock_path = self.root / "owner.lock"
            if lock_path.exists() and _is_link_or_reparse(lock_path):
                raise ArtifactInvalid("artifact owner lock must not be a symbolic link")
            self._lock = _acquire_process_lock(lock_path)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ArtifactStore:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._run_store is None

    def close(self) -> None:
        self._run_store = None
        lock, self._lock = self._lock, None
        if lock is not None:
            _release_process_lock(lock)

    def _ensure_open(self) -> SQLiteStore:
        if self._run_store is None or self._run_store.closed:
            raise ArtifactError("artifact store is closed")
        return self._run_store

    @staticmethod
    def _stored_ref(row: StoredArtifact) -> ArtifactRef:
        try:
            artifact_id = validate_artifact_id(row.artifact_id)
            run_id = _validate_run_id(row.run_id)
            filename = _validate_filename(row.declared_filename)
            _validate_declared_media_type(row.declared_media_type)
        except ArtifactInvalid as error:
            raise ArtifactCorrupted("artifact metadata is invalid") from error
        if (
            _SHA256.fullmatch(row.content_sha256) is None
            or type(row.size) is not int
            or not 0 <= row.size <= MAX_ARTIFACT_BYTES
            or row.detected_media_type
            not in _PREVIEWABLE | {"application/octet-stream"}
            or type(row.created_seq) is not int
            or row.created_seq <= 0
        ):
            raise ArtifactCorrupted("artifact metadata is invalid")
        return ArtifactRef(
            artifact_id=artifact_id,
            run_id=run_id,
            content_sha256=row.content_sha256,
            size_bytes=row.size,
            filename=filename,
            media_type=row.detected_media_type,
            created_seq=row.created_seq,
        )

    def _blob_path(self, storage_key: str) -> Path:
        if _STORAGE_KEY.fullmatch(storage_key) is None:
            raise ArtifactCorrupted("artifact storage key is invalid")
        relative = Path(*storage_key.split("/"))
        path = self._blobs / relative
        try:
            path.relative_to(self._blobs)
        except ValueError as error:
            raise ArtifactCorrupted("artifact storage key escapes the blob root") from error
        return path

    @staticmethod
    def _read_path_verified(path: Path, size: int, digest: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(path):
                raise ArtifactCorrupted(
                    "artifact blob is missing or not a regular file"
                )
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise ArtifactCorrupted("artifact blob changed while it was opened")
            if opened.st_size != size or size > MAX_ARTIFACT_BYTES:
                raise ArtifactCorrupted("artifact blob size does not match metadata")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = None
                content = stream.read(MAX_ARTIFACT_BYTES + 1)
        except ArtifactCorrupted:
            raise
        except OSError as error:
            raise ArtifactCorrupted("artifact blob could not be read") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(content) != size:
            raise ArtifactCorrupted("artifact blob size does not match metadata")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactCorrupted("artifact blob digest does not match metadata")
        return content

    def _read_verified(self, ref: ArtifactRef) -> bytes:
        storage_key = f"sha256/{ref.content_sha256[:2]}/{ref.content_sha256}"
        return self._read_path_verified(
            self._blob_path(storage_key), ref.size_bytes, ref.content_sha256
        )

    def _write_blob(self, storage_key: str, content: bytes) -> None:
        path = self._blob_path(storage_key)
        parent = path.parent
        if parent.exists() and _is_link_or_reparse(parent):
            raise ArtifactCorrupted("artifact blob directory is a symbolic link")
        parent.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(self._blobs, "artifact blob root is invalid")
        _require_plain_directory(parent.parent, "artifact digest root is invalid")
        _require_plain_directory(parent, "artifact blob directory is invalid")
        if path.exists():
            self._read_path_verified(
                path, len(content), hashlib.sha256(content).hexdigest()
            )
            return
        temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        finalized = False
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                finalized = True
            except FileExistsError:
                self._read_path_verified(
                    path, len(content), hashlib.sha256(content).hexdigest()
                )
            if finalized:
                temporary.unlink()
            try:
                path.chmod(0o444)
            except OSError:
                pass
            if os.name != "nt":
                descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise ArtifactError(
                "artifact blob requires same-filesystem no-overwrite hard-link finalize"
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self._read_path_verified(path, len(content), hashlib.sha256(content).hexdigest())

    def put(
        self,
        run_id: str,
        content: bytes,
        *,
        declared_filename: str,
        declared_media_type: str | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        run_store = self._ensure_open()
        run_id = _validate_run_id(run_id)
        if not isinstance(content, bytes):
            raise ArtifactInvalid("artifact content must be bytes")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ArtifactInvalid("artifact content exceeds 16 MiB")
        filename = _validate_filename(declared_filename)
        declared = _validate_declared_media_type(declared_media_type)
        digest = hashlib.sha256(content).hexdigest()
        detected = _detect_media_type(content)
        if artifact_id is None:
            identity = json.dumps(
                [1, run_id, digest, filename, detected],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            artifact_id = f"artifact-{hashlib.sha256(identity).hexdigest()[:32]}"
        artifact_id = validate_artifact_id(artifact_id)
        storage_key = f"sha256/{digest[:2]}/{digest}"
        self._write_blob(storage_key, content)

        try:
            stored = run_store.register_artifact(
                run_id,
                artifact_id=artifact_id,
                content_sha256=digest,
                size=len(content),
                declared_filename=filename,
                declared_media_type=declared,
                detected_media_type=detected,
            )
        except ArtifactRegistrationConflict as error:
            raise ArtifactConflict(str(error)) from error
        except ArtifactLimitExceeded as error:
            raise ArtifactLimit(str(error)) from error
        ref = self._stored_ref(stored)
        self._read_verified(ref)
        return ref

    def list(self, run_id: str) -> tuple[ArtifactRef, ...]:
        run_store = self._ensure_open()
        run_id = _validate_run_id(run_id)
        return tuple(
            self._stored_ref(row) for row in run_store.stored_artifacts(run_id)
        )

    def get(self, run_id: str, artifact_id: str) -> ArtifactPayload:
        run_store = self._ensure_open()
        run_id = _validate_run_id(run_id)
        artifact_id = validate_artifact_id(artifact_id)
        stored = run_store.stored_artifact(run_id, artifact_id)
        if stored is None:
            raise ArtifactNotFound("artifact was not found for this run")
        ref = self._stored_ref(stored)
        return ArtifactPayload(ref, self._read_verified(ref))


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "ArtifactConflict",
    "ArtifactCorrupted",
    "ArtifactError",
    "ArtifactInvalid",
    "ArtifactLimit",
    "ArtifactNotFound",
    "ArtifactPayload",
    "ArtifactRef",
    "ArtifactStore",
    "artifact_projection",
    "validate_artifact_id",
]
