from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

try:
    from scripts.container_acceptance import (
        AcceptanceError,
        HTTPClient,
        RUN_ID,
        SHA256,
        _canonical,
        _event_types,
        _events,
        _integer,
        _mapping,
        _read_token,
        _sha256,
        _strict_json,
    )
except ModuleNotFoundError:
    from container_acceptance import (  # type: ignore[no-redef]
        AcceptanceError,
        HTTPClient,
        RUN_ID,
        SHA256,
        _canonical,
        _event_types,
        _events,
        _integer,
        _mapping,
        _read_token,
        _sha256,
        _strict_json,
    )


EVIDENCE_KIND = "sasori.container-workflow-acceptance"
EVIDENCE_SCHEMA_VERSION = 2
MAX_EVIDENCE_BYTES = 128 * 1024
WORKFLOW_ID = "incident-mechanism"
WORKFLOW_INPUT = "container typed workflow incident"
EXPECTED_VALUE = f"diagnostic captured for {WORKFLOW_INPUT}"
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


def _catalog(client: HTTPClient) -> dict[str, object]:
    catalog = _mapping(client.json("GET", "/v1/apps"), "application catalog")
    apps = catalog.get("apps")
    if catalog.get("schema_version") != 1 or not isinstance(apps, list):
        raise AcceptanceError("application catalog envelope is invalid")
    matches = [
        _mapping(item, "Workflow application")
        for item in apps
        if isinstance(item, dict)
        and isinstance(item.get("workflow"), dict)
        and item["workflow"].get("workflow_id") == WORKFLOW_ID
    ]
    if len(matches) != 1:
        raise AcceptanceError("exactly one Incident Workflow must be enabled")
    app = matches[0]
    app_id = app.get("id")
    workflow = _mapping(app.get("workflow"), "Workflow metadata")
    availability = _mapping(app.get("availability"), "Workflow availability")
    digest = workflow.get("definition_sha256")
    if (
        set(workflow)
        != {
            "schema_version",
            "workflow_id",
            "version",
            "definition_sha256",
            "app_id",
            "execution",
            "output_step",
            "step_count",
            "supports_parallel",
            "supports_branches",
            "supports_agent_nodes",
            "trust",
            "inputs",
            "steps",
        }
        or workflow.get("schema_version") != 1
        or not isinstance(app_id, str)
        or re.fullmatch(r"flow\.incident-mechanism\.[0-9a-f]{12}", app_id) is None
        or workflow.get("app_id") != app_id
        or availability.get("status") != "ready"
        or workflow.get("version") != "1"
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
        or not app_id.endswith(digest[:12])
        or workflow.get("execution") != "single-harness-ordered-tools-v1"
        or workflow.get("output_step") != "record"
        or workflow.get("step_count") != 2
        or workflow.get("supports_parallel") is not False
        or workflow.get("supports_branches") is not False
        or workflow.get("supports_agent_nodes") is not False
        or workflow.get("trust")
        != {"execution_mode": "trusted_installed_python", "sandboxed": False}
        or workflow.get("inputs")
        != [
            {
                "key": "incident",
                "type": "string",
                "required": True,
                "max_bytes": 16 * 1024,
            }
        ]
    ):
        raise AcceptanceError("Workflow catalog identity or capability is invalid")
    tools = app.get("tools")
    if not isinstance(tools, list) or len(tools) != 2:
        raise AcceptanceError("Workflow catalog must expose two wrapper Tools")
    wrappers = [_mapping(item, "Workflow wrapper Tool") for item in tools]
    names = [str(item.get("name")) for item in wrappers]
    effects = [item.get("effect") for item in wrappers]
    if (
        len(set(names)) != 2
        or any(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None for name in names)
        or effects != ["read_only", "side_effecting"]
        or any(item.get("plugin_id") != "com.sasori.flow" for item in wrappers)
        or wrappers[0].get("tool_revision") is not None
        or not isinstance(wrappers[1].get("tool_revision"), str)
        or any(
            not isinstance(item.get("schema_sha256"), str)
            or SHA256.fullmatch(str(item["schema_sha256"])) is None
            for item in wrappers
        )
    ):
        raise AcceptanceError("Workflow wrapper Tool contracts are invalid")
    worker = _mapping(app.get("worker"), "Workflow worker")
    skills = app.get("skills")
    if not isinstance(skills, list) or len(skills) != 1:
        raise AcceptanceError("Workflow skill metadata is invalid")
    skill = _mapping(skills[0], "Workflow skill")
    logical = ["inspect_incident", "record_action"]
    if (
        worker.get("tool_names") != names
        or skill.get("tool_names") != names
        or worker.get("logical_tool_names") != logical
        or skill.get("logical_tool_names") != logical
    ):
        raise AcceptanceError("Workflow logical and dispatch Tool metadata disagree")
    steps = workflow.get("steps")
    if not isinstance(steps, list) or len(steps) != 2:
        raise AcceptanceError("Workflow ordered step metadata is invalid")
    projected_steps = [_mapping(item, "Workflow ordered step") for item in steps]
    step_fields = {
        "position",
        "step_id",
        "depends_on",
        "argument_sources",
        "logical_tool_name",
        "dispatch_tool_name",
        "effect",
        "requires_approval",
        "recovery_policy",
        "logical_tool_revision",
        "dispatch_tool_revision",
        "logical_schema_sha256",
        "dispatch_schema_sha256",
        "result_type",
        "max_result_bytes",
        "is_output",
    }
    if (
        any(set(item) != step_fields for item in projected_steps)
        or [item.get("position") for item in projected_steps] != [1, 2]
        or [item.get("step_id") for item in projected_steps] != ["inspect", "record"]
        or [item.get("logical_tool_name") for item in projected_steps] != logical
        or [item.get("dispatch_tool_name") for item in projected_steps] != names
        or [item.get("effect") for item in projected_steps]
        != ["read_only", "side_effecting"]
        or [item.get("depends_on") for item in projected_steps]
        != [[], ["inspect"]]
        or [item.get("argument_sources") for item in projected_steps]
        != [
            [{"name": "summary", "kind": "input", "ref": "incident"}],
            [{"name": "summary", "kind": "step", "ref": "inspect"}],
        ]
        or [item.get("requires_approval") for item in projected_steps]
        != [False, True]
        or [item.get("recovery_policy") for item in projected_steps]
        != [
            "read_only_replay_allowed",
            "manual_effect_resolution_on_ambiguity",
        ]
        or [item.get("is_output") for item in projected_steps] != [False, True]
        or [item.get("result_type") for item in projected_steps]
        != ["string", "string"]
        or [item.get("max_result_bytes") for item in projected_steps]
        != [32 * 1024, 32 * 1024]
        or any(
            not isinstance(item.get("logical_schema_sha256"), str)
            or SHA256.fullmatch(str(item["logical_schema_sha256"])) is None
            or item.get("dispatch_schema_sha256") != wrappers[index]["schema_sha256"]
            or item.get("dispatch_tool_revision") != wrappers[index]["tool_revision"]
            for index, item in enumerate(projected_steps)
        )
    ):
        raise AcceptanceError("Workflow ordered step contracts are inconsistent")
    return {
        "app_id": app_id,
        "definition_sha256": digest,
        "manifest": workflow,
        "wrapper_names": names,
        "wrapper_revisions": [item.get("tool_revision") for item in wrappers],
        "wrapper_schema_sha256": [item.get("schema_sha256") for item in wrappers],
        "steps": projected_steps,
    }


def _definition_from_catalog(catalog: dict[str, object]) -> dict[str, object]:
    manifest = _mapping(catalog.get("manifest"), "Workflow catalog manifest")
    inputs = manifest.get("inputs")
    steps = manifest.get("steps")
    if not isinstance(inputs, list) or not isinstance(steps, list) or not steps:
        raise AcceptanceError("Workflow catalog cannot reconstruct the definition")
    definition_steps: list[dict[str, object]] = []
    for raw_step in steps:
        step = _mapping(raw_step, "Workflow catalog definition step")
        raw_sources = step.get("argument_sources")
        if not isinstance(raw_sources, list):
            raise AcceptanceError("Workflow catalog argument sources are invalid")
        arguments: dict[str, object] = {}
        for raw_source in raw_sources:
            source = _mapping(raw_source, "Workflow catalog argument source")
            name = source.get("name")
            ref = source.get("ref")
            kind = source.get("kind")
            if not isinstance(name, str) or not isinstance(ref, str):
                raise AcceptanceError("Workflow catalog argument reference is invalid")
            if kind == "input" and set(source) == {"name", "kind", "ref"}:
                binding = {"kind": "input", "key": ref}
            elif kind == "step" and set(source) == {"name", "kind", "ref"}:
                binding = {"kind": "step_output", "step_id": ref}
            else:
                raise AcceptanceError(
                    "Workflow catalog contains a non-reconstructable argument"
                )
            if name in arguments:
                raise AcceptanceError("Workflow catalog argument name is duplicated")
            arguments[name] = binding
        definition_steps.append(
            {
                "step_id": step.get("step_id"),
                "kind": "tool",
                "tool_name": step.get("logical_tool_name"),
                "effect": step.get("effect"),
                "tool_revision": step.get("logical_tool_revision"),
                "schema_sha256": step.get("logical_schema_sha256"),
                "arguments": arguments,
                "result": {
                    "type": step.get("result_type"),
                    "max_bytes": step.get("max_result_bytes"),
                },
            }
        )
    definition = {
        "schema_version": 1,
        "workflow_id": manifest.get("workflow_id"),
        "version": manifest.get("version"),
        "execution": manifest.get("execution"),
        "inputs": inputs,
        "steps": definition_steps,
        "output_step": manifest.get("output_step"),
    }
    if _sha256(definition) != manifest.get("definition_sha256"):
        raise AcceptanceError("Workflow catalog definition digest is inconsistent")
    return definition


def _runtime_snapshot(client: HTTPClient) -> dict[str, object]:
    history = _mapping(
        client.json("GET", "/v1/runs?limit=100"),
        "Workflow preflight run history",
    )
    items = history.get("items")
    if (
        set(history) != {"items", "next_before"}
        or not isinstance(items, list)
        or history.get("next_before") is not None
    ):
        raise AcceptanceError("Workflow preflight history snapshot is incomplete")
    events_by_run: dict[str, object] = {}
    event_count = 0
    for raw_item in items:
        item = _mapping(raw_item, "Workflow preflight history item")
        run_id = item.get("run_id")
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise AcceptanceError("Workflow preflight history run ID is invalid")
        events, latest = _events(client, run_id)
        if item.get("latest_seq") != latest:
            raise AcceptanceError("Workflow preflight history cursor changed")
        events_by_run[run_id] = events
        event_count += len(events)
    return {
        "run_count": len(items),
        "event_count": event_count,
        "runs_sha256": _sha256(history),
        "events_sha256": _sha256(events_by_run),
    }


def _validate_runtime_snapshot(value: object) -> dict[str, object]:
    snapshot = _mapping(value, "Workflow preflight runtime snapshot")
    if (
        set(snapshot)
        != {"run_count", "event_count", "runs_sha256", "events_sha256"}
        or not isinstance(snapshot.get("runs_sha256"), str)
        or SHA256.fullmatch(str(snapshot["runs_sha256"])) is None
        or not isinstance(snapshot.get("events_sha256"), str)
        or SHA256.fullmatch(str(snapshot["events_sha256"])) is None
    ):
        raise AcceptanceError("Workflow preflight runtime snapshot is invalid")
    _integer(snapshot.get("run_count"), "Workflow preflight run count", minimum=0)
    _integer(snapshot.get("event_count"), "Workflow preflight event count", minimum=0)
    return snapshot


def _validate_preflight_evidence(value: object) -> dict[str, object]:
    evidence = _mapping(value, "Workflow preflight evidence")
    required = {
        "schema_version",
        "kind",
        "phase",
        "app_id",
        "definition_sha256",
        "definition_json_sha256",
        "manifest_sha256",
        "runtime_before",
        "runtime_after",
        "invalid_contract",
    }
    invalid = _mapping(evidence.get("invalid_contract"), "invalid preflight evidence")
    before = _validate_runtime_snapshot(evidence.get("runtime_before"))
    after = _validate_runtime_snapshot(evidence.get("runtime_after"))
    if (
        set(evidence) != required
        or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("phase") != "preflight"
        or not isinstance(evidence.get("app_id"), str)
        or not isinstance(evidence.get("definition_sha256"), str)
        or SHA256.fullmatch(str(evidence["definition_sha256"])) is None
        or not isinstance(evidence.get("definition_json_sha256"), str)
        or SHA256.fullmatch(str(evidence["definition_json_sha256"])) is None
        or evidence.get("definition_json_sha256") != evidence.get("definition_sha256")
        or not isinstance(evidence.get("manifest_sha256"), str)
        or SHA256.fullmatch(str(evidence["manifest_sha256"])) is None
        or before != after
        or invalid
        != {
            "status": 422,
            "code": "workflow_preflight_rejected",
            "reason_code": "tool_contract_mismatch",
            "retryable": False,
        }
    ):
        raise AcceptanceError("Workflow preflight evidence contract is invalid")
    return evidence


def run_preflight(client: HTTPClient) -> dict[str, object]:
    catalog = _catalog(client)
    definition = _definition_from_catalog(catalog)
    before = _runtime_snapshot(client)
    response = _mapping(
        client.json("POST", "/v1/workflows/preflight", body=definition),
        "Workflow preflight success",
    )
    if (
        set(response) != {"ok", "schema_version", "manifest"}
        or response.get("ok") is not True
        or response.get("schema_version") != 1
        or response.get("manifest") != catalog.get("manifest")
    ):
        raise AcceptanceError("Workflow preflight manifest differs from the catalog")
    drifted = json.loads(_canonical(definition).decode("utf-8"))
    drifted["steps"][0]["schema_sha256"] = "0" * 64
    rejected = _mapping(
        client.json(
            "POST",
            "/v1/workflows/preflight",
            body=drifted,
            expected_status=422,
        ),
        "Workflow preflight rejection",
    )
    error = _mapping(rejected.get("error"), "Workflow preflight error")
    message = error.get("message")
    if (
        set(rejected) != {"ok", "error"}
        or rejected.get("ok") is not False
        or set(error) != {"code", "message", "retryable", "reason_code"}
        or error.get("code") != "workflow_preflight_rejected"
        or error.get("retryable") is not False
        or error.get("reason_code") != "tool_contract_mismatch"
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 512
    ):
        raise AcceptanceError("Workflow preflight rejection contract is invalid")
    after = _runtime_snapshot(client)
    if before != after:
        raise AcceptanceError("Workflow preflight mutated runs or events")
    return _validate_preflight_evidence(
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "kind": EVIDENCE_KIND,
            "phase": "preflight",
            "app_id": catalog["app_id"],
            "definition_sha256": catalog["definition_sha256"],
            "definition_json_sha256": _sha256(definition),
            "manifest_sha256": _sha256(response["manifest"]),
            "runtime_before": before,
            "runtime_after": after,
            "invalid_contract": {
                "status": 422,
                "code": error["code"],
                "reason_code": error["reason_code"],
                "retryable": error["retryable"],
            },
        }
    )


def _workflow_projection(
    value: object,
    *,
    catalog: dict[str, object],
    latest_seq: int,
    statuses: tuple[str, str],
    current_step_id: str | None,
) -> dict[str, object]:
    workflow = _mapping(value, "Workflow public projection")
    expected_fields = {
        "schema_version",
        "workflow_id",
        "version",
        "definition_sha256",
        "app_id",
        "execution",
        "output_step",
        "current_step_id",
        "latest_seq",
        "steps",
    }
    if (
        set(workflow) != expected_fields
        or workflow.get("schema_version") != 1
        or workflow.get("workflow_id") != WORKFLOW_ID
        or workflow.get("version") != "1"
        or workflow.get("definition_sha256") != catalog["definition_sha256"]
        or workflow.get("app_id") != catalog["app_id"]
        or workflow.get("execution") != "single-harness-ordered-tools-v1"
        or workflow.get("output_step") != "record"
        or workflow.get("current_step_id") != current_step_id
        or workflow.get("latest_seq") != latest_seq
    ):
        raise AcceptanceError("Workflow public projection identity is invalid")
    values = workflow.get("steps")
    contract_steps = catalog.get("steps")
    if (
        not isinstance(values, list)
        or len(values) != 2
        or not isinstance(contract_steps, list)
        or len(contract_steps) != 2
    ):
        raise AcceptanceError("Workflow public step projection is invalid")
    steps = [_mapping(item, "Workflow public step") for item in values]
    expected_step_fields = {
        "position",
        "step_id",
        "kind",
        "logical_tool_name",
        "dispatch_tool_name",
        "effect",
        "logical_tool_revision",
        "dispatch_tool_revision",
        "logical_schema_sha256",
        "dispatch_schema_sha256",
        "result_type",
        "max_result_bytes",
        "call_id",
        "status",
        "error_code",
    }
    call_ids = []
    for index, (step, raw_contract) in enumerate(
        zip(steps, contract_steps, strict=True)
    ):
        contract = _mapping(raw_contract, "Workflow catalog step")
        call_id = step.get("call_id")
        expected = {
            "position": contract.get("position"),
            "step_id": contract.get("step_id"),
            "kind": "tool",
            "logical_tool_name": contract.get("logical_tool_name"),
            "dispatch_tool_name": contract.get("dispatch_tool_name"),
            "effect": contract.get("effect"),
            "logical_tool_revision": contract.get("logical_tool_revision"),
            "dispatch_tool_revision": contract.get("dispatch_tool_revision"),
            "logical_schema_sha256": contract.get("logical_schema_sha256"),
            "dispatch_schema_sha256": contract.get("dispatch_schema_sha256"),
            "result_type": contract.get("result_type"),
            "max_result_bytes": contract.get("max_result_bytes"),
            "status": statuses[index],
            "error_code": None,
        }
        if (
            set(step) != expected_step_fields
            or any(step.get(key) != item for key, item in expected.items())
            or not isinstance(call_id, str)
            or not call_id
            or "\x00" in call_id
            or len(call_id.encode("utf-8")) > 256
        ):
            raise AcceptanceError("Workflow public step contract is inconsistent")
        call_ids.append(call_id)
    if len(set(call_ids)) != 2:
        raise AcceptanceError("Workflow public call identity is invalid")
    encoded = _canonical(workflow)
    if WORKFLOW_INPUT.encode("utf-8") in encoded or EXPECTED_VALUE.encode("utf-8") in encoded:
        raise AcceptanceError("Workflow public projection disclosed private values")
    return workflow


def _projection(
    value: object,
    *,
    run_id: str,
    catalog: dict[str, object],
    state: str,
    pause_reason: str | None,
    detail: str,
    statuses: tuple[str, str],
    current_step_id: str | None,
) -> dict[str, object]:
    projected = _mapping(value, "Workflow run projection")
    app_id = str(catalog["app_id"])
    expected = {
        "run_id": run_id,
        "app_id": app_id,
        "input": WORKFLOW_INPUT,
        "state": state,
        "pause_reason": pause_reason,
        "detail": detail,
    }
    if any(projected.get(key) != item for key, item in expected.items()):
        raise AcceptanceError("Workflow run projection is inconsistent")
    latest_seq = _integer(
        projected.get("latest_seq"), "Workflow latest sequence", minimum=1
    )
    _workflow_projection(
        projected.get("workflow"),
        catalog=catalog,
        latest_seq=latest_seq,
        statuses=statuses,
        current_step_id=current_step_id,
    )
    return projected


def _pending(
    value: object, *, definition_sha256: str, wrapper_name: str
) -> str:
    pending = _mapping(value, "Workflow pending approval")
    arguments = _mapping(pending.get("arguments"), "Workflow pending arguments")
    fingerprint = pending.get("fingerprint")
    payload_json = arguments.get("payload_json")
    if (
        not isinstance(fingerprint, str)
        or SHA256.fullmatch(fingerprint) is None
        or pending.get("tool_name") != wrapper_name
        or pending.get("effect") != "side_effecting"
        or set(arguments) != {"definition_sha256", "step_id", "payload_json"}
        or arguments.get("definition_sha256") != definition_sha256
        or arguments.get("step_id") != "record"
        or not isinstance(payload_json, str)
    ):
        raise AcceptanceError("Workflow approval envelope is invalid")
    payload = _mapping(
        _strict_json(payload_json.encode("utf-8"), "Workflow payload"),
        "Workflow payload",
    )
    if (
        payload != {"summary": EXPECTED_VALUE}
        or _canonical(payload).decode("utf-8") != payload_json
    ):
        raise AcceptanceError("Workflow resolved payload is not canonical or expected")
    return fingerprint


def _final(value: object, definition_sha256: str) -> dict[str, object]:
    message = _mapping(value, "Workflow final message")
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, str):
        raise AcceptanceError("Workflow final message envelope is invalid")
    outcome = _mapping(
        _strict_json(content.encode("utf-8"), "Workflow final content"),
        "Workflow final content",
    )
    output = _mapping(outcome.get("output"), "Workflow final output")
    if (
        _canonical(outcome).decode("utf-8") != content
        or outcome.get("version") != 1
        or outcome.get("workflow_id") != WORKFLOW_ID
        or outcome.get("workflow_version") != "1"
        or outcome.get("definition_sha256") != definition_sha256
        or outcome.get("status") != "succeeded"
        or output.get("step_id") != "record"
        or output.get("value") != EXPECTED_VALUE
        or output.get("value_sha256") != _sha256(EXPECTED_VALUE)
    ):
        raise AcceptanceError("Workflow typed final outcome is invalid")
    return message


def _validate_events(
    events: list[dict[str, object]], wrapper_names: list[str], *, completed: bool
) -> None:
    expected = EXPECTED_EVENT_TYPES if completed else EXPECTED_EVENT_TYPES[:11]
    if _event_types(events) != expected:
        raise AcceptanceError("Workflow durable event sequence is invalid")
    if any(str(item["event"]["type"]).startswith("workflow.") for item in events):
        raise AcceptanceError("Workflow introduced a second public event family")
    tool_events = [
        _mapping(item["event"], "Workflow tool event")
        for item in events
        if str(item["event"]["type"]).startswith("tool.")
    ]
    seen_names = []
    seen_call_ids = []
    for event in tool_events:
        name = event.get("tool_name")
        call_id = event.get("call_id")
        if name not in wrapper_names or not isinstance(call_id, str):
            raise AcceptanceError("Workflow tool event binding is invalid")
        if name not in seen_names:
            seen_names.append(name)
        if call_id not in seen_call_ids:
            seen_call_ids.append(call_id)
        if not call_id or "\x00" in call_id or len(call_id.encode("utf-8")) > 256:
            raise AcceptanceError("Workflow call ID violates the public bound")
    if seen_names != wrapper_names or len(seen_call_ids) != 2:
        raise AcceptanceError("Workflow step event order or call identity changed")


def _artifact(client: HTTPClient, run_id: str, final: dict[str, object]) -> dict[str, object]:
    encoded_run_id = quote(run_id, safe="")
    listed = _mapping(
        client.json("GET", f"/v1/runs/{encoded_run_id}/artifacts"),
        "Workflow artifact list",
    )
    values = listed.get("artifacts")
    if listed.get("run_id") != run_id or not isinstance(values, list) or len(values) != 1:
        raise AcceptanceError("Workflow final artifact list is invalid")
    ref = _mapping(values[0], "Workflow artifact reference")
    artifact_id = ref.get("artifact_id")
    digest = ref.get("content_sha256")
    size = _integer(ref.get("size_bytes"), "Workflow artifact size", minimum=1)
    if (
        not isinstance(artifact_id, str)
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
    ):
        raise AcceptanceError("Workflow artifact identity is invalid")
    content, headers = client.content(
        "GET",
        f"/v1/runs/{encoded_run_id}/artifacts/{quote(artifact_id, safe='')}/content",
    )
    expected = str(final["content"]).encode("utf-8")
    if (
        expected not in content
        or len(content) != size
        or hashlib.sha256(content).hexdigest() != digest
        or headers.get("x-sasori-content-sha256") != digest
    ):
        raise AcceptanceError("Workflow final artifact content is invalid")
    return {
        "artifact_id": artifact_id,
        "content_sha256": digest,
        "size_bytes": size,
    }


def _validate_evidence(value: object, phase: str) -> dict[str, object]:
    evidence = _mapping(value, "Workflow evidence")
    required = {
        "schema_version",
        "kind",
        "phase",
        "run_id",
        "app_id",
        "definition_sha256",
        "wrapper_names",
        "latest_seq",
        "event_count",
        "events_sha256",
        "projection_sha256",
        "final_message",
        "artifact",
        "preflight",
    }
    if (
        set(evidence) != required
        or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("kind") != EVIDENCE_KIND
        or evidence.get("phase") != phase
        or not isinstance(evidence.get("run_id"), str)
        or RUN_ID.fullmatch(str(evidence["run_id"])) is None
        or not isinstance(evidence.get("app_id"), str)
        or not isinstance(evidence.get("definition_sha256"), str)
        or SHA256.fullmatch(str(evidence["definition_sha256"])) is None
        or not isinstance(evidence.get("wrapper_names"), list)
        or len(evidence["wrapper_names"]) != 2
        or not isinstance(evidence.get("events_sha256"), str)
        or SHA256.fullmatch(str(evidence["events_sha256"])) is None
        or not isinstance(evidence.get("projection_sha256"), str)
        or SHA256.fullmatch(str(evidence["projection_sha256"])) is None
    ):
        raise AcceptanceError("Workflow evidence contract is invalid")
    _integer(evidence.get("latest_seq"), "Workflow evidence cursor", minimum=1)
    _integer(evidence.get("event_count"), "Workflow evidence event count", minimum=1)
    preflight = _validate_preflight_evidence(evidence.get("preflight"))
    if (
        preflight.get("app_id") != evidence.get("app_id")
        or preflight.get("definition_sha256")
        != evidence.get("definition_sha256")
    ):
        raise AcceptanceError("Workflow preflight evidence identity changed")
    if phase == "prepare":
        if evidence.get("final_message") is not None or evidence.get("artifact") is not None:
            raise AcceptanceError("prepared Workflow evidence contains a final")
    else:
        _mapping(evidence.get("final_message"), "Workflow evidence final")
        _mapping(evidence.get("artifact"), "Workflow evidence artifact")
    return evidence


def _read_evidence(path: Path, token: str, phase: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("Workflow evidence file could not be read") from None
    if not raw or len(raw) > MAX_EVIDENCE_BYTES or token.encode("ascii") in raw:
        raise AcceptanceError("Workflow evidence file is invalid or contains the token")
    value = _strict_json(raw, "Workflow evidence file")
    if phase == "preflight":
        return _validate_preflight_evidence(value)
    return _validate_evidence(value, phase)


def _write_evidence(path: Path, value: dict[str, object], token: str, *, replace: bool) -> None:
    phase = str(value.get("phase"))
    validated = (
        _validate_preflight_evidence(value)
        if phase == "preflight"
        else _validate_evidence(value, phase)
    )
    encoded = json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if token.encode("ascii") in encoded:
        raise AcceptanceError("Workflow evidence would contain the bearer token")
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
        raise AcceptanceError("Workflow evidence file already exists") from None
    except OSError:
        raise AcceptanceError("Workflow evidence file could not be written") from None


def run_prepare(
    client: HTTPClient,
    run_id: str,
    preflight: dict[str, object],
) -> dict[str, object]:
    preflight = _validate_preflight_evidence(preflight)
    catalog = _catalog(client)
    app_id = str(catalog["app_id"])
    digest = str(catalog["definition_sha256"])
    wrappers = list(catalog["wrapper_names"])
    if (
        preflight["app_id"] != app_id
        or preflight["definition_sha256"] != digest
        or preflight["definition_json_sha256"]
        != _sha256(_definition_from_catalog(catalog))
        or preflight["manifest_sha256"] != _sha256(catalog["manifest"])
    ):
        raise AcceptanceError("Workflow preflight identity changed before run")
    encoded_run_id = quote(run_id, safe="")
    paused = _projection(
        client.json(
            "POST",
            "/v1/runs",
            body={"run_id": run_id, "app_id": app_id, "input": WORKFLOW_INPUT},
            expected_status=202,
        ),
        run_id=run_id,
        catalog=catalog,
        state="paused",
        pause_reason="approval_required",
        detail="awaiting_approval",
        statuses=("completed", "approval_required"),
        current_step_id="record",
    )
    fingerprint = _pending(
        paused.get("pending"),
        definition_sha256=digest,
        wrapper_name=wrappers[1],
    )
    decided = _projection(
        client.json(
            "POST",
            f"/v1/runs/{encoded_run_id}/approval",
            body={"fingerprint": fingerprint, "approved": True},
        ),
        run_id=run_id,
        catalog=catalog,
        state="paused",
        pause_reason="resume_required",
        detail="awaiting_resume",
        statuses=("completed", "resume_required"),
        current_step_id="record",
    )
    durable = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        run_id=run_id,
        catalog=catalog,
        state="paused",
        pause_reason="resume_required",
        detail="awaiting_resume",
        statuses=("completed", "resume_required"),
        current_step_id="record",
    )
    if durable != decided:
        raise AcceptanceError("Workflow approval response is not durable")
    events, latest = _events(client, run_id)
    _validate_events(events, wrappers, completed=False)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "prepare",
        "run_id": run_id,
        "app_id": app_id,
        "definition_sha256": digest,
        "wrapper_names": wrappers,
        "latest_seq": latest,
        "event_count": len(events),
        "events_sha256": _sha256(events),
        "projection_sha256": _sha256(durable),
        "final_message": None,
        "artifact": None,
        "preflight": preflight,
    }


def run_complete(client: HTTPClient, prepared: dict[str, object]) -> dict[str, object]:
    prepared = _validate_evidence(prepared, "prepare")
    run_id = str(prepared["run_id"])
    app_id = str(prepared["app_id"])
    digest = str(prepared["definition_sha256"])
    wrappers = list(prepared["wrapper_names"])
    catalog = _catalog(client)
    if (
        catalog["app_id"] != app_id
        or catalog["definition_sha256"] != digest
        or catalog["wrapper_names"] != wrappers
    ):
        raise AcceptanceError("Workflow catalog changed before resume")
    encoded_run_id = quote(run_id, safe="")
    completed = _projection(
        client.json("POST", f"/v1/runs/{encoded_run_id}/resume", body={}),
        run_id=run_id,
        catalog=catalog,
        state="completed",
        pause_reason=None,
        detail="completed",
        statuses=("completed", "completed"),
        current_step_id=None,
    )
    durable = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        run_id=run_id,
        catalog=catalog,
        state="completed",
        pause_reason=None,
        detail="completed",
        statuses=("completed", "completed"),
        current_step_id=None,
    )
    if durable != completed:
        raise AcceptanceError("completed Workflow projection is not durable")
    final = _final(durable.get("final_message"), digest)
    events, latest = _events(client, run_id)
    _validate_events(events, wrappers, completed=True)
    artifact = _artifact(client, run_id, final)
    return {
        **prepared,
        "phase": "complete",
        "latest_seq": latest,
        "event_count": len(events),
        "events_sha256": _sha256(events),
        "projection_sha256": _sha256(durable),
        "final_message": final,
        "artifact": artifact,
    }


def run_after_restart(
    client: HTTPClient, completed: dict[str, object]
) -> dict[str, object]:
    completed = _validate_evidence(completed, "complete")
    catalog = _catalog(client)
    run_id = str(completed["run_id"])
    app_id = str(completed["app_id"])
    digest = str(completed["definition_sha256"])
    wrappers = list(completed["wrapper_names"])
    if (
        catalog["app_id"] != app_id
        or catalog["definition_sha256"] != digest
        or catalog["wrapper_names"] != wrappers
    ):
        raise AcceptanceError("Workflow catalog changed across restart")
    restarted_preflight = run_preflight(client)
    stored_preflight = _validate_preflight_evidence(completed.get("preflight"))
    for field in (
        "app_id",
        "definition_sha256",
        "definition_json_sha256",
        "manifest_sha256",
        "invalid_contract",
    ):
        if restarted_preflight[field] != stored_preflight[field]:
            raise AcceptanceError("Workflow preflight changed across restart")
    encoded_run_id = quote(run_id, safe="")
    durable = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        run_id=run_id,
        catalog=catalog,
        state="completed",
        pause_reason=None,
        detail="completed",
        statuses=("completed", "completed"),
        current_step_id=None,
    )
    final = _final(durable.get("final_message"), digest)
    events, latest = _events(client, run_id)
    _validate_events(events, wrappers, completed=True)
    artifact = _artifact(client, run_id, final)
    if (
        _sha256(durable) != completed["projection_sha256"]
        or _sha256(events) != completed["events_sha256"]
        or latest != completed["latest_seq"]
        or len(events) != completed["event_count"]
        or final != completed["final_message"]
        or artifact != completed["artifact"]
    ):
        raise AcceptanceError("Workflow durable evidence changed across restart")
    conflict = _mapping(
        client.json(
            "POST",
            f"/v1/runs/{encoded_run_id}/resume",
            body={},
            expected_status=409,
        ),
        "completed Workflow resume conflict",
    )
    error = _mapping(conflict.get("error"), "completed Workflow resume error")
    if (
        conflict.get("ok") is not False
        or error.get("code") != "invalid_transition"
        or error.get("run_id") != run_id
        or error.get("retryable") is not False
        or error.get("message") != "run is completed and cannot resume"
    ):
        raise AcceptanceError("completed Workflow resume conflict is invalid")
    durable_after = _projection(
        client.json("GET", f"/v1/runs/{encoded_run_id}"),
        run_id=run_id,
        catalog=catalog,
        state="completed",
        pause_reason=None,
        detail="completed",
        statuses=("completed", "completed"),
        current_step_id=None,
    )
    events_after, latest_after = _events(client, run_id)
    artifact_after = _artifact(client, run_id, final)
    if (
        durable_after != durable
        or events_after != events
        or latest_after != latest
        or artifact_after != artifact
    ):
        raise AcceptanceError("completed Workflow resume replayed durable work")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "phase": "after-restart",
        "run_id": run_id,
        "app_id": app_id,
        "definition_sha256": digest,
        "verified": True,
        "latest_seq": latest,
        "event_count": len(events),
        "events_sha256": _sha256(events),
        "projection_sha256": _sha256(durable),
        "artifact": artifact,
        "preflight_manifest_sha256": restarted_preflight["manifest_sha256"],
        "preflight_runtime_unchanged": True,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the containerized Sasori typed Workflow lifecycle."
    )
    parser.add_argument(
        "phase", choices=("preflight", "prepare", "complete", "after-restart")
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
        if options.phase == "preflight":
            if options.run_id is not None or options.evidence.exists():
                raise AcceptanceError("preflight evidence target is invalid")
            evidence = run_preflight(client)
            _write_evidence(options.evidence, evidence, token, replace=False)
        elif options.phase == "prepare":
            run_id = options.run_id or f"workflow-{uuid.uuid4().hex}"
            if RUN_ID.fullmatch(run_id) is None:
                raise AcceptanceError("run ID is invalid")
            preflight = _read_evidence(options.evidence, token, "preflight")
            evidence = run_prepare(client, run_id, preflight)
            _write_evidence(options.evidence, evidence, token, replace=True)
        elif options.phase == "complete":
            if options.run_id is not None:
                raise AcceptanceError("run ID is read from prepared evidence")
            prepared = _read_evidence(options.evidence, token, "prepare")
            evidence = run_complete(client, prepared)
            _write_evidence(options.evidence, evidence, token, replace=True)
        else:
            if options.run_id is not None:
                raise AcceptanceError("run ID is read from completed evidence")
            completed = _read_evidence(options.evidence, token, "complete")
            evidence = run_after_restart(client, completed)
        encoded = _canonical(evidence)
        if token.encode("ascii") in encoded:
            raise AcceptanceError("Workflow acceptance output contains the token")
        print(encoded.decode("utf-8"))
        return 0
    except AcceptanceError as error:
        print(f"container Workflow acceptance failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "container Workflow acceptance failed: unexpected internal error",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
