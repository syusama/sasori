from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit


EVIDENCE_KIND = "sasori.container-acceptance"
EVIDENCE_SCHEMA_VERSION = 2
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EXPECTED_EVENT_TYPES = (
    "run.started",
    "model.started",
    "model.completed",
    "tool.requested",
    "tool.started",
    "tool.completed",
    "model.started",
    "model.completed",
    "tool.requested",
    "approval.requested",
    "approval.resolved",
    "tool.started",
    "tool.completed",
    "model.started",
    "model.completed",
    "run.completed",
    "artifact.available",
)
INCIDENT_INPUT = "container acceptance incident"
EXPECTED_DIAGNOSTIC = f"diagnostic captured for {INCIDENT_INPUT}"
EXPECTED_ACTION_SUMMARY = f"Operator review: {EXPECTED_DIAGNOSTIC}"
EXPECTED_FINAL_CONTENT = f"Incident action recorded: {EXPECTED_ACTION_SUMMARY}"


class AcceptanceError(ValueError):
    pass


def _strict_json(value: bytes, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise AcceptanceError(f"{label} is not strict JSON") from None


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise AcceptanceError("acceptance value is not JSON-compatible") from None


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AcceptanceError(f"{label} is not a JSON object")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AcceptanceError(f"{label} is invalid")
    return value


def _read_token(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("token file could not be read") from None
    if not 16 <= len(raw) <= 4098:
        raise AcceptanceError("token file content is invalid")
    try:
        token = raw.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        raise AcceptanceError("token file content is invalid") from None
    if (
        not 16 <= len(token) <= 4096
        or token != token.strip()
        or any(not 0x21 <= ord(character) <= 0x7E for character in token)
    ):
        raise AcceptanceError("token file content is invalid")
    return token


class HTTPClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise AcceptanceError("base URL is invalid") from None
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise AcceptanceError("base URL must be an HTTP loopback origin")
        host = parsed.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise AcceptanceError("base URL must use a numeric loopback IP")
        self.host = host
        self.port = port or 80
        self.token = token
        self.timeout = timeout

    def _request_with_headers(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        expected_status: int,
        expected_content_type: str,
    ) -> tuple[bytes, dict[str, str]]:
        encoded = None if body is None else _canonical(body)
        request_headers = {
            "Accept": expected_content_type,
            "Authorization": f"Bearer {self.token}",
            **(headers or {}),
        }
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout
        )
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            content_type = response.getheader("Content-Type", "")
            status = response.status
            response_headers = {
                name.casefold(): value for name, value in response.getheaders()
            }
        except (OSError, TimeoutError, http.client.HTTPException, ValueError):
            raise AcceptanceError("HTTP acceptance request failed") from None
        finally:
            connection.close()
        if self.token.encode("ascii") in payload:
            raise AcceptanceError("server response disclosed the bearer token")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise AcceptanceError("HTTP acceptance response exceeded the size limit")
        if status != expected_status:
            raise AcceptanceError("HTTP acceptance response had an unexpected status")
        if not content_type.casefold().startswith(expected_content_type.casefold()):
            raise AcceptanceError("HTTP acceptance response had an unexpected content type")
        return payload, response_headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        expected_status: int,
        expected_content_type: str,
    ) -> bytes:
        return self._request_with_headers(
            method,
            path,
            body=body,
            headers=headers,
            expected_status=expected_status,
            expected_content_type=expected_content_type,
        )[0]

    def content(
        self,
        method: str,
        path: str,
        *,
        range_header: str | None = None,
        expected_status: int = 200,
    ) -> tuple[bytes, dict[str, str]]:
        headers = {"Range": range_header} if range_header is not None else None
        return self._request_with_headers(
            method,
            path,
            headers=headers,
            expected_status=expected_status,
            expected_content_type="text/plain",
        )

    def json(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        expected_status: int = 200,
    ) -> object:
        payload = self._request(
            method,
            path,
            body=body,
            expected_status=expected_status,
            expected_content_type="application/json",
        )
        return _strict_json(payload, "HTTP response")

    def sse(self, path: str, cursor: int) -> list[dict[str, object]]:
        payload = self._request(
            "GET",
            path,
            headers={"Last-Event-ID": str(cursor)},
            expected_status=200,
            expected_content_type="text/event-stream",
        )
        return _parse_sse(payload)


def _parse_sse(payload: bytes) -> list[dict[str, object]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise AcceptanceError("SSE response is not UTF-8") from None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    frames = normalized.split("\n\n")
    events: list[dict[str, object]] = []
    for frame in frames:
        if not frame:
            continue
        event_id: str | None = None
        event_type: str | None = None
        data: list[str] = []
        only_control = True
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "retry":
                if not value.isdigit():
                    raise AcceptanceError("SSE retry field is invalid")
                continue
            only_control = False
            if field == "id" and event_id is None:
                event_id = value
            elif field == "event" and event_type is None:
                event_type = value
            elif field == "data":
                data.append(value)
            else:
                raise AcceptanceError("SSE frame contains unsupported or duplicate fields")
        if only_control:
            continue
        if event_id is None or event_type is None or not data or not event_id.isdigit():
            raise AcceptanceError("SSE event frame is incomplete")
        projected = _mapping(
            _strict_json("\n".join(data).encode("utf-8"), "SSE data"),
            "SSE data",
        )
        sequence = _integer(projected.get("seq"), "SSE sequence", minimum=1)
        event = _mapping(projected.get("event"), "SSE event")
        if sequence != int(event_id) or event.get("type") != event_type:
            raise AcceptanceError("SSE envelope fields disagree")
        events.append(projected)
    return events


def _projection(
    value: object,
    *,
    label: str,
    run_id: str,
    state: str,
    pause_reason: str | None,
    detail: str,
) -> dict[str, object]:
    projected = _mapping(value, label)
    expected = {
        "run_id": run_id,
        "app_id": "incident",
        "state": state,
        "pause_reason": pause_reason,
        "detail": detail,
    }
    if any(projected.get(key) != item for key, item in expected.items()):
        raise AcceptanceError(f"{label} did not match the expected public state")
    _integer(projected.get("latest_seq"), f"{label} latest sequence")
    return projected


def _event_list(value: object, run_id: str) -> tuple[list[dict[str, object]], int]:
    envelope = _mapping(value, "JSON events response")
    if envelope.get("run_id") != run_id or envelope.get("after_seq") != 0:
        raise AcceptanceError("JSON events response identity is invalid")
    latest = _integer(envelope.get("latest_seq"), "JSON events latest sequence")
    raw_events = envelope.get("events")
    if not isinstance(raw_events, list):
        raise AcceptanceError("JSON events response has no event list")
    events: list[dict[str, object]] = []
    for expected_sequence, raw in enumerate(raw_events, 1):
        projected = _mapping(raw, "JSON event")
        if projected.get("seq") != expected_sequence:
            raise AcceptanceError("JSON event sequence is not contiguous")
        event = _mapping(projected.get("event"), "JSON event envelope")
        if (
            event.get("run_id") != run_id
            or event.get("version") != 1
            or not isinstance(event.get("type"), str)
        ):
            raise AcceptanceError("JSON event envelope is invalid")
        events.append(projected)
    if latest != len(events):
        raise AcceptanceError("JSON events latest sequence disagrees with the event list")
    return events, latest


def _event_types(events: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(str(item["event"]["type"]) for item in events)


def _effect_count(events: list[dict[str, object]]) -> int:
    completed = [
        item
        for item in events
        if item["event"].get("type") == "tool.completed"
        and item["event"].get("tool_name") == "record_action"
    ]
    for item in completed:
        data = _mapping(item["event"].get("data"), "record_action completion data")
        if data.get("output") != EXPECTED_ACTION_SUMMARY:
            raise AcceptanceError("record_action completed with an unexpected summary")
    return len(completed)


def _pending_fingerprint(value: object) -> str:
    pending = _mapping(value, "initial pending approval")
    fingerprint = pending.get("fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not fingerprint
        or pending.get("tool_name") != "record_action"
        or pending.get("effect") != "side_effecting"
        or pending.get("arguments") != {"summary": EXPECTED_ACTION_SUMMARY}
    ):
        raise AcceptanceError("initial pending approval payload is unexpected")
    return fingerprint


def _final_message(value: object, label: str) -> dict[str, object]:
    final_message = _mapping(value, label)
    if final_message != {
        "role": "assistant",
        "content": EXPECTED_FINAL_CONTENT,
    }:
        raise AcceptanceError(f"{label} does not match the expected Incident final")
    return final_message


def _events(client: HTTPClient, run_id: str) -> tuple[list[dict[str, object]], int]:
    encoded_run_id = quote(run_id, safe="")
    value = client.json("GET", f"/v1/runs/{encoded_run_id}/events?after_seq=0")
    return _event_list(value, run_id)


def _artifact_evidence(client: HTTPClient, run_id: str) -> dict[str, object]:
    encoded_run_id = quote(run_id, safe="")
    listed = _mapping(
        client.json("GET", f"/v1/runs/{encoded_run_id}/artifacts"),
        "artifact list",
    )
    if set(listed) != {"run_id", "artifacts"} or listed.get("run_id") != run_id:
        raise AcceptanceError("artifact list identity is invalid")
    values = listed.get("artifacts")
    if not isinstance(values, list) or len(values) != 1:
        raise AcceptanceError("completed container run must expose one artifact")
    ref = _mapping(values[0], "artifact reference")
    expected_keys = {
        "version",
        "artifact_id",
        "run_id",
        "content_sha256",
        "size_bytes",
        "filename",
        "media_type",
        "created_seq",
    }
    artifact_id = ref.get("artifact_id")
    digest = ref.get("content_sha256")
    size = _integer(ref.get("size_bytes"), "artifact size")
    created_seq = _integer(ref.get("created_seq"), "artifact event sequence", minimum=1)
    if (
        set(ref) != expected_keys
        or ref.get("version") != 1
        or ref.get("run_id") != run_id
        or not isinstance(artifact_id, str)
        or not artifact_id
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
        or not isinstance(ref.get("filename"), str)
        or not ref["filename"].endswith("-result.md")
        or ref.get("media_type") != "text/plain; charset=utf-8"
    ):
        raise AcceptanceError("artifact reference is invalid")
    encoded_artifact_id = quote(artifact_id, safe="")
    path = (
        f"/v1/runs/{encoded_run_id}/artifacts/{encoded_artifact_id}/content"
    )
    content, headers = client.content("GET", path)
    if (
        len(content) != size
        or hashlib.sha256(content).hexdigest() != digest
        or EXPECTED_FINAL_CONTENT.encode("utf-8") not in content
        or headers.get("etag") != f'"sha256-{digest}"'
        or headers.get("x-sasori-content-sha256") != digest
        or headers.get("cache-control") != "private, no-store"
        or not headers.get("content-disposition", "").startswith("attachment;")
        or headers.get("content-length") != str(size)
    ):
        raise AcceptanceError("artifact full download contract is invalid")
    head, head_headers = client.content("HEAD", path)
    if head or head_headers.get("content-length") != str(size):
        raise AcceptanceError("artifact HEAD contract is invalid")
    end = min(size - 1, 31)
    ranged, range_headers = client.content(
        "GET", path, range_header=f"bytes=0-{end}", expected_status=206
    )
    if (
        ranged != content[: end + 1]
        or range_headers.get("content-range") != f"bytes 0-{end}/{size}"
    ):
        raise AcceptanceError("artifact range contract is invalid")
    return {
        "artifact_id": artifact_id,
        "content_sha256": digest,
        "size_bytes": size,
        "filename": ref["filename"],
        "media_type": ref["media_type"],
        "created_seq": created_seq,
    }


def run_prepare(client: HTTPClient, run_id: str) -> dict[str, object]:
    encoded_run_id = quote(run_id, safe="")
    paused = _projection(
        client.json(
            "POST",
            "/v1/runs",
            body={
                "run_id": run_id,
                "app_id": "incident",
                "input": INCIDENT_INPUT,
            },
            expected_status=202,
        ),
        label="initial run projection",
        run_id=run_id,
        state="paused",
        pause_reason="approval_required",
        detail="awaiting_approval",
    )
    fingerprint = _pending_fingerprint(paused.get("pending"))
    reconnect_cursor = _integer(
        paused.get("latest_seq"), "approval-required cursor", minimum=1
    )
    decided = _projection(
        client.json(
            "POST",
            f"/v1/runs/{encoded_run_id}/approval",
            body={"fingerprint": fingerprint, "approved": True},
        ),
        label="approval decision projection",
        run_id=run_id,
        state="paused",
        pause_reason="resume_required",
        detail="awaiting_resume",
    )
    durable = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        label="durable resume-required projection",
        run_id=run_id,
        state="paused",
        pause_reason="resume_required",
        detail="awaiting_resume",
    )
    if durable != decided:
        raise AcceptanceError("durable projection differs from the approval response")
    before_resume, latest = _events(client, run_id)
    if _effect_count(before_resume) != 0:
        raise AcceptanceError("approval executed the side effect before explicit resume")
    types = _event_types(before_resume)
    if types != EXPECTED_EVENT_TYPES[:11]:
        raise AcceptanceError("prepared Incident event semantics are unexpected")
    if latest != durable.get("latest_seq"):
        raise AcceptanceError("prepared projection and JSON event cursors disagree")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "prepare",
        "run_id": run_id,
        "app_id": "incident",
        "workflow": {
            "initial_pause_reason": paused["pause_reason"],
            "decision_pause_reason": decided["pause_reason"],
            "explicit_resume": False,
        },
        "reconnect_after_seq": reconnect_cursor,
        "latest_seq": latest,
        "event_count": len(before_resume),
        "event_types": list(types),
        "events_sha256": _sha256(before_resume),
        "projection_sha256": _sha256(durable),
        "effect": {"tool_name": "record_action", "completed_count": 0},
    }


def run_complete(
    client: HTTPClient, prepared: dict[str, object]
) -> dict[str, object]:
    prepared = _validated_prepared_evidence(prepared)
    run_id = str(prepared["run_id"])
    encoded_run_id = quote(run_id, safe="")
    before_projection = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        label="pre-resume projection",
        run_id=run_id,
        state="paused",
        pause_reason="resume_required",
        detail="awaiting_resume",
    )
    before_events, before_latest = _events(client, run_id)
    if (
        _sha256(before_projection) != prepared["projection_sha256"]
        or _sha256(before_events) != prepared["events_sha256"]
        or before_latest != prepared["latest_seq"]
        or len(before_events) != prepared["event_count"]
        or list(_event_types(before_events)) != prepared["event_types"]
        or _effect_count(before_events) != 0
    ):
        raise AcceptanceError("prepared durable evidence changed before explicit resume")
    completed = _projection(
        client.json(
            "POST", f"/v1/runs/{encoded_run_id}/resume", body={}
        ),
        label="explicit resume projection",
        run_id=run_id,
        state="completed",
        pause_reason=None,
        detail="completed",
    )
    final_message = _final_message(completed.get("final_message"), "final message")
    durable = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        label="durable completed projection",
        run_id=run_id,
        state="completed",
        pause_reason=None,
        detail="completed",
    )
    if durable != completed:
        raise AcceptanceError("durable projection differs from the resume response")
    events, latest = _events(client, run_id)
    types = _event_types(events)
    if types != EXPECTED_EVENT_TYPES:
        raise AcceptanceError("Incident durable event semantics are unexpected")
    effect_count = _effect_count(events)
    if effect_count != 1:
        raise AcceptanceError("Incident did not complete exactly one approved effect")
    if latest != durable.get("latest_seq"):
        raise AcceptanceError("projection and JSON event cursors disagree")
    artifact = _artifact_evidence(client, run_id)
    if artifact["created_seq"] != latest:
        raise AcceptanceError("artifact sequence is not the latest durable event")
    streamed = client.sse(
        f"/v1/runs/{encoded_run_id}/events", prepared["reconnect_after_seq"]
    )
    expected_tail = [
        item for item in events if item["seq"] > prepared["reconnect_after_seq"]
    ]
    if streamed != expected_tail or not streamed:
        raise AcceptanceError("SSE reconnect did not reproduce the durable event tail")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "complete",
        "run_id": run_id,
        "app_id": "incident",
        "workflow": {
            "initial_pause_reason": "approval_required",
            "decision_pause_reason": "resume_required",
            "explicit_resume": True,
        },
        "latest_seq": latest,
        "event_count": len(events),
        "event_types": list(types),
        "events_sha256": _sha256(events),
        "projection_sha256": _sha256(durable),
        "final_message": final_message,
        "effect": {"tool_name": "record_action", "completed_count": effect_count},
        "artifact": artifact,
        "reconnect": {
            "after_seq": prepared["reconnect_after_seq"],
            "event_count": len(streamed),
            "first_seq": streamed[0]["seq"],
            "last_seq": streamed[-1]["seq"],
            "events_sha256": _sha256(streamed),
        },
    }


def _validated_prepared_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "prepared evidence")
    expected_keys = {
        "schema_version",
        "kind",
        "phase",
        "run_id",
        "app_id",
        "workflow",
        "reconnect_after_seq",
        "latest_seq",
        "event_count",
        "event_types",
        "events_sha256",
        "projection_sha256",
        "effect",
    }
    if set(evidence) != expected_keys:
        raise AcceptanceError("prepared evidence fields are invalid")
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("phase") != "prepare"
        or evidence.get("app_id") != "incident"
        or not isinstance(evidence.get("run_id"), str)
        or RUN_ID.fullmatch(str(evidence["run_id"])) is None
        or not isinstance(evidence.get("events_sha256"), str)
        or SHA256.fullmatch(str(evidence["events_sha256"])) is None
        or not isinstance(evidence.get("projection_sha256"), str)
        or SHA256.fullmatch(str(evidence["projection_sha256"])) is None
    ):
        raise AcceptanceError("prepared evidence identity is invalid")
    latest = _integer(
        evidence.get("latest_seq"), "prepared evidence latest sequence", minimum=1
    )
    count = _integer(
        evidence.get("event_count"), "prepared evidence event count", minimum=1
    )
    reconnect = _integer(
        evidence.get("reconnect_after_seq"),
        "prepared evidence reconnect cursor",
        minimum=1,
    )
    if (
        latest != count
        or latest != len(EXPECTED_EVENT_TYPES[:11])
        or reconnect != len(EXPECTED_EVENT_TYPES[:10])
        or evidence.get("event_types") != list(EXPECTED_EVENT_TYPES[:11])
    ):
        raise AcceptanceError("prepared evidence event summary is invalid")
    workflow = _mapping(evidence.get("workflow"), "prepared evidence workflow")
    if workflow != {
        "initial_pause_reason": "approval_required",
        "decision_pause_reason": "resume_required",
        "explicit_resume": False,
    }:
        raise AcceptanceError("prepared evidence workflow is invalid")
    effect = _mapping(evidence.get("effect"), "prepared evidence effect")
    if effect != {"tool_name": "record_action", "completed_count": 0}:
        raise AcceptanceError("prepared evidence effect summary is invalid")
    return evidence


def _validated_completed_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "evidence")
    expected_keys = {
        "schema_version",
        "kind",
        "phase",
        "run_id",
        "app_id",
        "workflow",
        "latest_seq",
        "event_count",
        "event_types",
        "events_sha256",
        "projection_sha256",
        "final_message",
        "effect",
        "artifact",
        "reconnect",
    }
    if set(evidence) != expected_keys:
        raise AcceptanceError("evidence fields are invalid")
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("phase") != "complete"
        or evidence.get("app_id") != "incident"
        or not isinstance(evidence.get("run_id"), str)
        or RUN_ID.fullmatch(str(evidence["run_id"])) is None
        or not isinstance(evidence.get("events_sha256"), str)
        or SHA256.fullmatch(str(evidence["events_sha256"])) is None
        or not isinstance(evidence.get("projection_sha256"), str)
        or SHA256.fullmatch(str(evidence["projection_sha256"])) is None
    ):
        raise AcceptanceError("evidence identity is invalid")
    latest = _integer(evidence.get("latest_seq"), "evidence latest sequence", minimum=1)
    count = _integer(evidence.get("event_count"), "evidence event count", minimum=1)
    types = evidence.get("event_types")
    if latest != count or types != list(EXPECTED_EVENT_TYPES):
        raise AcceptanceError("evidence event summary is invalid")
    workflow = _mapping(evidence.get("workflow"), "evidence workflow")
    if workflow != {
        "initial_pause_reason": "approval_required",
        "decision_pause_reason": "resume_required",
        "explicit_resume": True,
    }:
        raise AcceptanceError("evidence workflow is invalid")
    _final_message(evidence.get("final_message"), "evidence final message")
    effect = _mapping(evidence.get("effect"), "evidence effect")
    artifact = _mapping(evidence.get("artifact"), "evidence artifact")
    reconnect = _mapping(evidence.get("reconnect"), "evidence reconnect")
    if effect != {"tool_name": "record_action", "completed_count": 1}:
        raise AcceptanceError("evidence effect summary is invalid")
    if (
        set(artifact)
        != {
            "artifact_id",
            "content_sha256",
            "size_bytes",
            "filename",
            "media_type",
            "created_seq",
        }
        or not isinstance(artifact.get("artifact_id"), str)
        or not isinstance(artifact.get("content_sha256"), str)
        or SHA256.fullmatch(str(artifact["content_sha256"])) is None
        or _integer(artifact.get("size_bytes"), "evidence artifact size") < 1
        or not isinstance(artifact.get("filename"), str)
        or artifact.get("media_type") != "text/plain; charset=utf-8"
        or _integer(
            artifact.get("created_seq"), "evidence artifact sequence", minimum=1
        )
        != latest
    ):
        raise AcceptanceError("evidence artifact summary is invalid")
    for field in ("after_seq", "event_count", "first_seq", "last_seq"):
        _integer(reconnect.get(field), f"evidence reconnect {field}", minimum=1)
    if (
        set(reconnect)
        != {"after_seq", "event_count", "first_seq", "last_seq", "events_sha256"}
        or reconnect["after_seq"] != len(EXPECTED_EVENT_TYPES[:10])
        or reconnect["first_seq"] != reconnect["after_seq"] + 1
        or reconnect["last_seq"] != latest
        or reconnect["event_count"] != reconnect["last_seq"] - reconnect["after_seq"]
        or not isinstance(reconnect.get("events_sha256"), str)
        or SHA256.fullmatch(str(reconnect["events_sha256"])) is None
    ):
        raise AcceptanceError("evidence reconnect digest is invalid")
    return evidence


def run_after_restart(
    client: HTTPClient, evidence: dict[str, object]
) -> dict[str, object]:
    evidence = _validated_completed_evidence(evidence)
    run_id = str(evidence["run_id"])
    encoded_run_id = quote(run_id, safe="")
    projection = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        label="after-restart projection",
        run_id=run_id,
        state="completed",
        pause_reason=None,
        detail="completed",
    )
    events, latest = _events(client, run_id)
    final_message = _final_message(
        projection.get("final_message"), "after-restart final"
    )
    effect_count = _effect_count(events)
    artifact = _artifact_evidence(client, run_id)
    reconnect = evidence["reconnect"]
    streamed = client.sse(
        f"/v1/runs/{encoded_run_id}/events", reconnect["after_seq"]
    )
    expected_tail = [item for item in events if item["seq"] > reconnect["after_seq"]]
    if not streamed:
        raise AcceptanceError("after-restart SSE reconnect returned no durable events")
    checks = (
        _sha256(projection) == evidence["projection_sha256"],
        _sha256(events) == evidence["events_sha256"],
        final_message == evidence["final_message"],
        effect_count == evidence["effect"]["completed_count"],
        latest == evidence["latest_seq"],
        len(events) == evidence["event_count"],
        list(_event_types(events)) == evidence["event_types"],
        streamed == expected_tail,
        len(streamed) == reconnect["event_count"],
        streamed[0]["seq"] == reconnect["first_seq"],
        streamed[-1]["seq"] == reconnect["last_seq"],
        _sha256(streamed) == reconnect["events_sha256"],
        artifact == evidence["artifact"],
    )
    if not all(checks):
        raise AcceptanceError("after-restart durable evidence changed")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "after-restart",
        "run_id": run_id,
        "verified": True,
        "latest_seq": latest,
        "event_count": len(events),
        "effect_count": effect_count,
        "artifact": artifact,
    }


def run_tamper_check(
    client: HTTPClient, evidence: dict[str, object]
) -> dict[str, object]:
    evidence = _validated_completed_evidence(evidence)
    run_id = str(evidence["run_id"])
    artifact = _mapping(evidence["artifact"], "evidence artifact")
    artifact_id = str(artifact["artifact_id"])
    path = (
        f"/v1/runs/{quote(run_id, safe='')}/artifacts/"
        f"{quote(artifact_id, safe='')}/content"
    )
    response = _mapping(
        client.json("GET", path, expected_status=503),
        "artifact tamper response",
    )
    error = _mapping(response.get("error"), "artifact tamper error")
    if (
        set(response) != {"ok", "error"}
        or response.get("ok") is not False
        or set(error) != {"code", "message", "retryable", "run_id"}
        or error.get("code") != "artifact_integrity_failed"
        or error.get("message")
        != "artifact bytes failed durable integrity verification"
        or error.get("retryable") is not False
        or error.get("run_id") != run_id
    ):
        raise AcceptanceError("tampered artifact did not fail closed")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "tamper-check",
        "run_id": run_id,
        "verified": True,
        "artifact_id": artifact_id,
        "content_sha256": artifact["content_sha256"],
        "size_bytes": artifact["size_bytes"],
        "status": 503,
        "error_code": "artifact_integrity_failed",
    }


def _validate_phase_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "evidence")
    if evidence.get("phase") == "prepare":
        return _validated_prepared_evidence(evidence)
    if evidence.get("phase") == "complete":
        return _validated_completed_evidence(evidence)
    raise AcceptanceError("evidence phase is invalid")


def _read_evidence(path: Path, token: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("evidence file could not be read") from None
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise AcceptanceError("evidence file size is invalid")
    if token.encode("ascii") in raw:
        raise AcceptanceError("evidence file contains the bearer token")
    return _validate_phase_evidence(_strict_json(raw, "evidence file"))


def _write_evidence(
    path: Path,
    evidence: dict[str, object],
    token: str,
    *,
    replace: bool = False,
) -> None:
    encoded = json.dumps(
        _validate_phase_evidence(evidence),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if token.encode("ascii") in encoded:
        raise AcceptanceError("evidence would contain the bearer token")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not replace:
            with path.open("xb") as stream:
                stream.write(encoded)
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except FileExistsError:
        raise AcceptanceError("evidence file already exists") from None
    except OSError:
        raise AcceptanceError("evidence file could not be written") from None


def _print_json(value: object, token: str) -> None:
    encoded = _canonical(value)
    if token.encode("ascii") in encoded:
        raise AcceptanceError("acceptance output would contain the bearer token")
    print(encoded.decode("utf-8"))


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the containerized Sasori Incident HTTP workflow."
    )
    parser.add_argument(
        "phase", choices=("prepare", "complete", "after-restart", "tamper-check")
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--timeout", default=10.0, type=float)
    options = parser.parse_args(arguments)
    token: str | None = None
    try:
        if not math.isfinite(options.timeout) or not 0.1 <= options.timeout <= 60:
            raise AcceptanceError("timeout must be between 0.1 and 60 seconds")
        token = _read_token(options.token_file)
        client = HTTPClient(options.base_url, token, options.timeout)
        if options.phase == "prepare":
            run_id = options.run_id or f"container-{uuid.uuid4().hex}"
            if RUN_ID.fullmatch(run_id) is None:
                raise AcceptanceError("run ID is invalid")
            if options.evidence.exists():
                raise AcceptanceError("evidence file already exists")
            evidence = run_prepare(client, run_id)
            _write_evidence(options.evidence, evidence, token)
            _print_json(evidence, token)
        elif options.phase == "complete":
            if options.run_id is not None:
                raise AcceptanceError("run ID is read from prepared evidence")
            prepared = _read_evidence(options.evidence, token)
            completed = run_complete(client, prepared)
            _write_evidence(options.evidence, completed, token, replace=True)
            _print_json(completed, token)
        elif options.phase == "after-restart":
            if options.run_id is not None:
                raise AcceptanceError("run ID is read from completed evidence")
            completed = _read_evidence(options.evidence, token)
            _print_json(run_after_restart(client, completed), token)
        else:
            if options.run_id is not None:
                raise AcceptanceError("run ID is read from completed evidence")
            completed = _read_evidence(options.evidence, token)
            _print_json(run_tamper_check(client, completed), token)
        return 0
    except AcceptanceError as error:
        print(f"container acceptance failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("container acceptance failed: unexpected internal error", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
