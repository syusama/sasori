from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from scripts.container_acceptance import (
        AcceptanceError,
        HTTPClient,
        SHA256,
        _canonical,
        _mapping,
        _read_token,
        _sha256,
        _strict_json,
    )
    from scripts.container_workflow_acceptance import (
        _catalog,
        _definition_from_catalog,
        _runtime_snapshot,
        _validate_runtime_snapshot,
    )
except ModuleNotFoundError:
    from container_acceptance import (  # type: ignore[no-redef]
        AcceptanceError,
        HTTPClient,
        SHA256,
        _canonical,
        _mapping,
        _read_token,
        _sha256,
        _strict_json,
    )
    from container_workflow_acceptance import (  # type: ignore[no-redef]
        _catalog,
        _definition_from_catalog,
        _runtime_snapshot,
        _validate_runtime_snapshot,
    )


EVIDENCE_KIND = "sasori.container-saved-workflow-acceptance"
EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_BYTES = 128 * 1024
CATALOG_ID = "wfcat_123e4567e89b42d3a456426614174000"
ETAG = re.compile(
    r'"sasori-wfcat-123e4567e89b42d3a456426614174000-r([12])-([0-9a-f]{64})"\Z'
)
RECORD_FIELDS = {
    "catalog_id",
    "catalog_revision",
    "current_contract",
    "definition",
    "definition_sha256",
    "head_revision",
    "is_head",
    "parent_revision",
    "saved_manifest",
}


def _json_with_headers(
    client: HTTPClient,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> tuple[dict[str, object], dict[str, str]]:
    payload, response_headers = client._request_with_headers(
        method,
        path,
        body=body,
        headers=headers,
        expected_status=expected_status,
        expected_content_type="application/json",
    )
    return (
        _mapping(_strict_json(payload, "saved Workflow HTTP response"), "saved Workflow response"),
        response_headers,
    )


def _record(
    value: object,
    headers: dict[str, str],
    definition: dict[str, object],
    *,
    revision: int,
    head_revision: int,
) -> dict[str, object]:
    envelope = _mapping(value, "saved Workflow envelope")
    record = _mapping(envelope.get("record"), "saved Workflow record")
    contract = _mapping(record.get("current_contract"), "saved Workflow current contract")
    manifest = _mapping(record.get("saved_manifest"), "saved Workflow manifest")
    digest = _sha256(definition)
    expected_etag = f'"sasori-wfcat-{CATALOG_ID[6:]}-r{revision}-{digest}"'
    if (
        set(envelope) != {"ok", "schema_version", "record"}
        or envelope.get("ok") is not True
        or envelope.get("schema_version") != 1
        or set(record) != RECORD_FIELDS
        or record.get("catalog_id") != CATALOG_ID
        or record.get("catalog_revision") != revision
        or record.get("parent_revision") != (None if revision == 1 else revision - 1)
        or record.get("head_revision") != head_revision
        or record.get("is_head") is not (revision == head_revision)
        or record.get("definition") != definition
        or record.get("definition_sha256") != digest
        or contract != {"status": "compatible", "reason_code": None}
        or manifest.get("schema_version") != 1
        or manifest.get("workflow_id") != definition.get("workflow_id")
        or manifest.get("version") != definition.get("version")
        or manifest.get("definition_sha256") != digest
        or headers.get("etag") != expected_etag
    ):
        raise AcceptanceError("saved Workflow record contract is invalid")
    return record


def _list(client: HTTPClient) -> dict[str, object]:
    page = _mapping(
        client.json("GET", "/v1/workflows?limit=100"),
        "saved Workflow list",
    )
    items = page.get("items")
    if (
        set(page) != {"ok", "schema_version", "items", "next_before"}
        or page.get("ok") is not True
        or page.get("schema_version") != 1
        or page.get("next_before") is not None
        or not isinstance(items, list)
        or len(items) != 1
    ):
        raise AcceptanceError("saved Workflow list contract is invalid")
    item = _mapping(items[0], "saved Workflow list item")
    if (
        set(item)
        != {
            "catalog_id",
            "catalog_revision",
            "definition_sha256",
            "definition_version",
            "workflow_id",
        }
        or item.get("catalog_id") != CATALOG_ID
        or item.get("catalog_revision") != 2
        or item.get("definition_version") != "container-2"
        or not isinstance(item.get("definition_sha256"), str)
        or SHA256.fullmatch(str(item["definition_sha256"])) is None
    ):
        raise AcceptanceError("saved Workflow list item is invalid")
    return page


def _stale_error(value: object) -> dict[str, object]:
    envelope = _mapping(value, "stale Workflow response")
    error = _mapping(envelope.get("error"), "stale Workflow error")
    if (
        set(envelope) != {"ok", "error"}
        or envelope.get("ok") is not False
        or set(error) != {"code", "message", "retryable"}
        or error.get("code") != "workflow_catalog_revision_mismatch"
        or error.get("message")
        != "saved Workflow precondition did not match durable state"
        or error.get("retryable") is not False
    ):
        raise AcceptanceError("stale Workflow error contract is invalid")
    return {
        "status": 412,
        "code": error["code"],
        "retryable": error["retryable"],
    }


def _observable_state(
    client: HTTPClient,
    first: dict[str, object],
    second: dict[str, object],
) -> dict[str, object]:
    current_value, current_headers = _json_with_headers(
        client, "GET", f"/v1/workflows/{quote(CATALOG_ID, safe='')}"
    )
    current = _record(current_value, current_headers, second, revision=2, head_revision=2)
    historical_value, historical_headers = _json_with_headers(
        client,
        "GET",
        f"/v1/workflows/{quote(CATALOG_ID, safe='')}?revision=1",
    )
    historical = _record(
        historical_value,
        historical_headers,
        first,
        revision=1,
        head_revision=2,
    )
    page = _list(client)
    return {
        "current_sha256": _sha256(current),
        "historical_sha256": _sha256(historical),
        "list_sha256": _sha256(page),
        "first_etag": historical_headers["etag"],
        "second_etag": current_headers["etag"],
        "first_definition_sha256": _sha256(first),
        "second_definition_sha256": _sha256(second),
    }


def _validate_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "saved Workflow evidence")
    required = {
        "schema_version",
        "kind",
        "phase",
        "catalog_id",
        "workflow_id",
        "apps_sha256",
        "runtime_before",
        "runtime_after",
        "current_sha256",
        "historical_sha256",
        "list_sha256",
        "first_etag",
        "second_etag",
        "first_definition_sha256",
        "second_definition_sha256",
        "stale_writer",
    }
    before = _validate_runtime_snapshot(evidence.get("runtime_before"))
    after = _validate_runtime_snapshot(evidence.get("runtime_after"))
    stale = _mapping(evidence.get("stale_writer"), "saved Workflow stale evidence")
    digests = (
        "apps_sha256",
        "current_sha256",
        "historical_sha256",
        "list_sha256",
        "first_definition_sha256",
        "second_definition_sha256",
    )
    if (
        set(evidence) != required
        or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("phase") != "prepare"
        or evidence.get("catalog_id") != CATALOG_ID
        or evidence.get("workflow_id") != "incident-mechanism"
        or before != after
        or any(
            not isinstance(evidence.get(name), str)
            or SHA256.fullmatch(str(evidence[name])) is None
            for name in digests
        )
        or not isinstance(evidence.get("first_etag"), str)
        or not isinstance(evidence.get("second_etag"), str)
        or ETAG.fullmatch(str(evidence["first_etag"])) is None
        or ETAG.fullmatch(str(evidence["second_etag"])) is None
        or evidence.get("first_etag") == evidence.get("second_etag")
        or stale
        != {
            "status": 412,
            "code": "workflow_catalog_revision_mismatch",
            "retryable": False,
        }
    ):
        raise AcceptanceError("saved Workflow evidence contract is invalid")
    return evidence


def run_prepare(client: HTTPClient) -> dict[str, object]:
    app_payload = client.json("GET", "/v1/apps")
    apps_sha256 = _sha256(app_payload)
    catalog = _catalog(client)
    first = _definition_from_catalog(catalog)
    second = json.loads(_canonical(first).decode("utf-8"))
    second["version"] = "container-2"
    runtime_before = _runtime_snapshot(client)
    path = f"/v1/workflows/{quote(CATALOG_ID, safe='')}"

    created_value, created_headers = _json_with_headers(
        client,
        "PUT",
        path,
        body=first,
        headers={"If-None-Match": "*"},
        expected_status=201,
    )
    _record(created_value, created_headers, first, revision=1, head_revision=1)
    if created_headers.get("location") != path:
        raise AcceptanceError("saved Workflow create Location is invalid")
    updated_value, updated_headers = _json_with_headers(
        client,
        "PUT",
        path,
        body=second,
        headers={"If-Match": created_headers["etag"]},
    )
    _record(updated_value, updated_headers, second, revision=2, head_revision=2)
    stale_value, _ = _json_with_headers(
        client,
        "PUT",
        path,
        body=second,
        headers={"If-Match": created_headers["etag"]},
        expected_status=412,
    )
    stale = _stale_error(stale_value)
    observable = _observable_state(client, first, second)
    runtime_after = _runtime_snapshot(client)
    if runtime_before != runtime_after or _sha256(client.json("GET", "/v1/apps")) != apps_sha256:
        raise AcceptanceError("saved Workflow CRUD changed applications, runs, or events")
    return _validate_evidence(
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "kind": EVIDENCE_KIND,
            "phase": "prepare",
            "catalog_id": CATALOG_ID,
            "workflow_id": first["workflow_id"],
            "apps_sha256": apps_sha256,
            "runtime_before": runtime_before,
            "runtime_after": runtime_after,
            **observable,
            "stale_writer": stale,
        }
    )


def run_after_restart(
    client: HTTPClient, prepared: dict[str, object]
) -> dict[str, object]:
    prepared = _validate_evidence(prepared)
    catalog = _catalog(client)
    first = _definition_from_catalog(catalog)
    second = json.loads(_canonical(first).decode("utf-8"))
    second["version"] = "container-2"
    runtime_before = _runtime_snapshot(client)
    observable = _observable_state(client, first, second)
    runtime_after = _runtime_snapshot(client)
    if (
        runtime_before != runtime_after
        or _sha256(client.json("GET", "/v1/apps")) != prepared["apps_sha256"]
        or any(prepared[name] != value for name, value in observable.items())
    ):
        raise AcceptanceError("saved Workflow durable evidence changed across restart")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "after-restart",
        "catalog_id": CATALOG_ID,
        "verified": True,
        "head_revision": 2,
        "runtime_unchanged": True,
        **observable,
    }


def _read_evidence(path: Path, token: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("saved Workflow evidence could not be read") from None
    if len(raw) > MAX_EVIDENCE_BYTES or token.encode("ascii") in raw:
        raise AcceptanceError("saved Workflow evidence is invalid")
    return _validate_evidence(_strict_json(raw, "saved Workflow evidence"))


def _write_evidence(path: Path, value: dict[str, object], token: str) -> None:
    encoded = _canonical(_validate_evidence(value)) + b"\n"
    if len(encoded) > MAX_EVIDENCE_BYTES or token.encode("ascii") in encoded:
        raise AcceptanceError("saved Workflow evidence is invalid")
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError:
        raise AcceptanceError("saved Workflow evidence already exists") from None
    except OSError:
        raise AcceptanceError("saved Workflow evidence could not be written") from None


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify durable saved Workflow Catalog behavior through a real server."
    )
    parser.add_argument("phase", choices=("prepare", "after-restart"))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    options = parser.parse_args(arguments)
    try:
        token = _read_token(options.token_file)
        client = HTTPClient(options.base_url, token, options.timeout)
        if options.phase == "prepare":
            if options.evidence.exists():
                raise AcceptanceError("saved Workflow evidence target already exists")
            evidence = run_prepare(client)
            _write_evidence(options.evidence, evidence, token)
        else:
            evidence = run_after_restart(
                client, _read_evidence(options.evidence, token)
            )
        print(_canonical(evidence).decode("utf-8"))
        return 0
    except AcceptanceError as error:
        print(f"acceptance failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
