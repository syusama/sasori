from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from sasori_memory import MemoryBinding, MemorySource, MemoryStore


SCHEMA_VERSION = 1
KIND = "sasori.container-memory-acceptance"
DEFAULT_DATABASE = Path("/data/memory-acceptance.sqlite3")
DEFAULT_EVIDENCE = Path("/data/memory-acceptance-evidence.json")
RUN_ID = "RunABC_Container"
SOURCE_CALL_ID = "call_AbC123_X"
CONTENT = "container durable memory survives restart"
CONTENT_SHA256 = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()
SOURCE_SHA256 = hashlib.sha256(b"container-memory-source-v1").hexdigest()
POLICY_SHA256 = hashlib.sha256(b"explicit-tool-v1").hexdigest()
MAX_EVIDENCE_BYTES = 64 * 1024


class AcceptanceError(ValueError):
    pass


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError):
        raise AcceptanceError("acceptance value is not canonical JSON data") from None


def _strict_json(value: str, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise AcceptanceError(f"{label} is not strict JSON") from None
    if not isinstance(parsed, dict) or _canonical(parsed) != value:
        raise AcceptanceError(f"{label} is not canonical JSON")
    return parsed


def _binding() -> MemoryBinding:
    return MemoryBinding("local-owner", "research", "container", "restart", 1)


def _source() -> MemorySource:
    return MemorySource(
        source_kind="harness_tool_call",
        source_run_id=RUN_ID,
        source_turn_id="turn-Container_1",
        source_step=1,
        source_call_id=SOURCE_CALL_ID,
        source_sha256=SOURCE_SHA256,
        source_authority="model_proposed_unverified",
        extractor_identity="explicit-tool-v1",
        policy_digest=POLICY_SHA256,
    )


def _write_evidence(path: Path, value: dict[str, object]) -> None:
    encoded = (_canonical(value) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise AcceptanceError("Memory acceptance evidence is too large")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise AcceptanceError("Memory acceptance evidence already exists") from None
    except OSError:
        raise AcceptanceError("Memory acceptance evidence could not be written") from None


def _read_evidence(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("Memory acceptance evidence could not be read") from None
    if not raw or len(raw) > MAX_EVIDENCE_BYTES or not raw.endswith(b"\n"):
        raise AcceptanceError("Memory acceptance evidence is invalid")
    try:
        text = raw[:-1].decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise AcceptanceError("Memory acceptance evidence is not UTF-8") from None
    return _strict_json(text, "Memory acceptance evidence")


def _match(store: MemoryStore, binding: MemoryBinding) -> tuple[dict[str, object], dict[str, object]]:
    result = _strict_json(
        store.search(binding, "container durable", 8), "Memory search result"
    )
    matches = result.get("matches")
    if not isinstance(matches, list) or len(matches) != 1 or not isinstance(matches[0], dict):
        raise AcceptanceError("Memory acceptance search did not return one record")
    match = matches[0]
    if (
        match.get("revision") != 1
        or match.get("content") != CONTENT
        or match.get("content_sha256") != CONTENT_SHA256
        or match.get("source_run_id") != RUN_ID
        or match.get("source_call_id") != SOURCE_CALL_ID
    ):
        raise AcceptanceError("Memory acceptance record changed")
    return result, match


def prepare(database: Path, evidence: Path) -> dict[str, object]:
    binding = _binding()
    store = MemoryStore(database)
    store.bind_run(RUN_ID, binding)
    remembered = _strict_json(
        store.remember(
            binding,
            logical_key="container.restart.fact",
            kind="semantic",
            content=CONTENT,
            priority=100,
            expected_revision=0,
            source=_source(),
            idempotency_key="container-memory-operation-v1",
        ),
        "Memory remember result",
    )
    result, match = _match(store, binding)
    if (
        match.get("memory_id") != remembered.get("memory_id")
        or match.get("revision") != remembered.get("revision")
    ):
        raise AcceptanceError("Memory remember and search identities disagree")
    value = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "phase": "prepare",
        "package_version": importlib.metadata.version("sasori"),
        "run_id": RUN_ID,
        "source_call_id": SOURCE_CALL_ID,
        "binding_sha256": binding.digest,
        "memory_id": match["memory_id"],
        "revision": match["revision"],
        "content_sha256": CONTENT_SHA256,
        "collection_revision": result["collection_revision"],
        "generation": result["generation"],
    }
    _write_evidence(evidence, value)
    return value


def after_restart(database: Path, evidence: Path) -> dict[str, object]:
    expected = _read_evidence(evidence)
    required = {
        "schema_version",
        "kind",
        "phase",
        "package_version",
        "run_id",
        "source_call_id",
        "binding_sha256",
        "memory_id",
        "revision",
        "content_sha256",
        "collection_revision",
        "generation",
    }
    if (
        set(expected) != required
        or expected.get("schema_version") != SCHEMA_VERSION
        or expected.get("kind") != KIND
        or expected.get("phase") != "prepare"
        or expected.get("run_id") != RUN_ID
        or expected.get("source_call_id") != SOURCE_CALL_ID
        or expected.get("revision") != 1
        or expected.get("content_sha256") != CONTENT_SHA256
    ):
        raise AcceptanceError("Memory acceptance evidence contract changed")
    binding = _binding()
    store = MemoryStore(database)
    if store.binding_for_run(RUN_ID) != binding or expected["binding_sha256"] != binding.digest:
        raise AcceptanceError("Memory run binding did not survive restart")
    result, match = _match(store, binding)
    if (
        match.get("memory_id") != expected["memory_id"]
        or match.get("revision") != expected["revision"]
        or result.get("collection_revision") != expected["collection_revision"]
        or result.get("generation") != expected["generation"]
    ):
        raise AcceptanceError("Memory identity or collection changed across restart")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "phase": "after-restart",
        "verified": True,
        "package_version": expected["package_version"],
        "run_id": RUN_ID,
        "source_call_id": SOURCE_CALL_ID,
        "memory_id": match["memory_id"],
        "revision": match["revision"],
        "content_sha256": match["content_sha256"],
        "collection_revision": result["collection_revision"],
        "generation": result["generation"],
        "run_binding_reloaded": True,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed-container Memory durability across restart."
    )
    parser.add_argument("phase", choices=("prepare", "after-restart"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    options = parser.parse_args(arguments)
    try:
        value = (
            prepare(options.database, options.evidence)
            if options.phase == "prepare"
            else after_restart(options.database, options.evidence)
        )
        print(_canonical(value))
        return 0
    except AcceptanceError as error:
        print(f"container Memory acceptance failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            "container Memory acceptance failed: " + type(error).__name__,
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
