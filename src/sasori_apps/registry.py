from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sasori import FULL_HOST_PROCESS_PRIVILEGES, Harness, Tool
from sasori._provider_common import compile_tool_schema
from sasori.plugins import PluginManifest, tool_schema_sha256
from sasori_plugins.git import git_manifest
from sasori_plugins.rag_sqlite import rag_sqlite_manifest
from sasori_plugins.web_fetch import web_fetch_manifest
from sasori_plugins.workspace import workspace_manifest
from sasori_flow import WorkflowHarness

from .developer import APP_METADATA as DEVELOPER
from .research import APP_METADATA as RESEARCH
from .workflow_incident import APP_ID as WORKFLOW_INCIDENT_ID
from .workflow_incident import APP_METADATA as WORKFLOW_INCIDENT


INCIDENT = {
    "id": "incident",
    "title": "Incident Chamber",
    "description": "Deterministic diagnosis and an operator-approved audit action.",
    "worker": {
        "id": "incident-responder",
        "title": "Incident Responder",
        "model_slot": "deterministic-demo",
        "tool_names": ["inspect_incident", "record_action"],
    },
    "skills": [
        {
            "id": "incident-triage",
            "title": "Incident triage",
            "description": "Capture a diagnostic summary and approve one auditable local action.",
            "tool_names": ["inspect_incident", "record_action"],
        }
    ],
}

_APPLICATIONS = (INCIDENT, RESEARCH, DEVELOPER, WORKFLOW_INCIDENT)
_APP_SPECS = {
    "incident": "sasori_apps.incident:create_harness",
    "research": "sasori_apps.research:create_harness",
    "developer": "sasori_apps.developer:create_harness",
    WORKFLOW_INCIDENT_ID: "sasori_apps.workflow_incident:create_harness",
}
_TOOL_ORIGINS = {
    "fetch_url": "com.sasori.web-fetch",
    "index_text": "com.sasori.rag-sqlite",
    "search_documents": "com.sasori.rag-sqlite",
    "list_files": "com.sasori.workspace",
    "read_text": "com.sasori.workspace",
    "search_text": "com.sasori.workspace",
    "write_text": "com.sasori.workspace",
    "git_status": "com.sasori.git",
    "git_diff": "com.sasori.git",
    "git_log": "com.sasori.git",
    "git_show": "com.sasori.git",
    "git_stage": "com.sasori.git",
    "git_commit": "com.sasori.git",
    "search_memory": "com.sasori.memory",
    "remember_memory": "com.sasori.memory",
    "forget_memory": "com.sasori.memory",
}


class WorkflowPreflightFailure(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def application_catalog() -> tuple[dict[str, object], ...]:
    return tuple(json.loads(json.dumps(item, ensure_ascii=False)) for item in _APPLICATIONS)


def application_specs() -> dict[str, str]:
    return dict(_APP_SPECS)


def app_id_for_spec(spec: str) -> str | None:
    return next((app_id for app_id, value in _APP_SPECS.items() if value == spec), None)


def workflow_preflight_tools(
    harnesses: Mapping[str, Harness],
) -> tuple[Tool, ...]:
    candidates: dict[str, Tool] = {}
    ambiguous: set[str] = set()
    for harness in harnesses.values():
        if isinstance(harness, WorkflowHarness):
            continue
        for tool in harness.tools:
            if tool.name in candidates:
                ambiguous.add(tool.name)
            else:
                candidates[tool.name] = tool
    return tuple(
        candidates[name]
        for name in sorted(candidates)
        if name not in ambiguous
    )


def workflow_preflight_definition(
    definition: dict[str, object], tools: Sequence[Tool]
) -> dict[str, object]:
    from sasori_flow import (
        WorkflowCompileError,
        WorkflowValidationError,
        preflight_workflow,
        workflow_spec_from_data,
    )

    try:
        spec = workflow_spec_from_data(definition)
    except WorkflowValidationError as exc:
        raise WorkflowPreflightFailure("invalid_definition", str(exc)) from None
    try:
        return preflight_workflow(spec, tuple(tools))
    except WorkflowCompileError as exc:
        raise WorkflowPreflightFailure("tool_contract_mismatch", str(exc)) from None
    except WorkflowValidationError as exc:
        raise WorkflowPreflightFailure("manifest_rejected", str(exc)) from None


def _permissions(manifest: PluginManifest) -> dict[str, object]:
    requested = manifest.permissions
    return {
        "id": manifest.plugin_id,
        "name": manifest.name,
        "version": manifest.version,
        "capability_kind": "plugin",
        "transport_type": None,
        "execution_mode": manifest.execution.mode,
        "requested_permissions": {
            "filesystem_read": list(requested.filesystem_read),
            "filesystem_write": list(requested.filesystem_write),
            "network_egress": list(requested.network_egress),
            "host_process": list(requested.host_process),
            "secrets": list(requested.secrets),
        },
        "effective_access": FULL_HOST_PROCESS_PRIVILEGES,
        "enforced": False,
    }


def _plugins(app_id: str, tool_names: set[str]) -> list[dict[str, object]]:
    if app_id == "research":
        manifests = (web_fetch_manifest(), rag_sqlite_manifest())
    elif app_id == "developer":
        manifests = (workspace_manifest(), git_manifest())
    else:
        manifests = ()
    result = [_permissions(manifest) for manifest in manifests]
    if tool_names.intersection(
        {"search_memory", "remember_memory", "forget_memory"}
    ):
        result.append(
            {
                "id": "com.sasori.memory",
                "name": "Sasori Durable bounded Memory",
                "version": "0.1.0.dev0",
                "capability_kind": "plugin",
                "transport_type": None,
                "execution_mode": "trusted_process",
                "requested_permissions": {
                    "filesystem_read": ["configured:memory.sqlite3"],
                    "filesystem_write": ["configured:memory.sqlite3"],
                    "network_egress": [],
                    "host_process": [],
                    "secrets": [],
                },
                "effective_access": FULL_HOST_PROCESS_PRIVILEGES,
                "enforced": False,
            }
        )
    if app_id == "incident":
        result.append(
            {
                "id": "sasori_apps.incident",
                "name": "Sasori Incident App",
                "version": "0.1.0.dev0",
                "capability_kind": "plugin",
                "transport_type": None,
                "execution_mode": "trusted_process",
                "requested_permissions": {
                    "filesystem_read": [],
                    "filesystem_write": ["configured:incident-action-log"],
                    "network_egress": [],
                    "host_process": [],
                    "secrets": [],
                },
                "effective_access": FULL_HOST_PROCESS_PRIVILEGES,
                "enforced": False,
            }
        )
    if app_id == WORKFLOW_INCIDENT_ID:
        result.append(
            {
                "id": "com.sasori.flow",
                "name": "Sasori Typed Workflow",
                "version": "0.1.0.dev0",
                "capability_kind": "plugin",
                "transport_type": None,
                "execution_mode": "trusted_process",
                "requested_permissions": {
                    "filesystem_read": [],
                    "filesystem_write": ["configured:incident-action-log"],
                    "network_egress": [],
                    "host_process": [],
                    "secrets": [],
                },
                "effective_access": FULL_HOST_PROCESS_PRIVILEGES,
                "enforced": False,
            }
        )
    if any(name not in _TOOL_ORIGINS for name in tool_names) and app_id == "developer":
        result.append(
            {
                "id": "configured-mcp-stdio",
                "name": "Configured MCP stdio",
                "version": None,
                "capability_kind": "mcp_transport",
                "transport_type": "stdio",
                "execution_mode": "trusted_process",
                "requested_permissions": None,
                "effective_access": FULL_HOST_PROCESS_PRIVILEGES,
                "enforced": False,
            }
        )
    return result


def application_surface_catalog(
    harnesses: Mapping[str, Harness], unavailable: Mapping[str, str]
) -> dict[str, object]:
    apps = []
    for metadata in application_catalog():
        app_id = str(metadata["id"])
        harness = harnesses.get(app_id)
        reason = unavailable.get(app_id)
        tools = tuple(harness.tools) if harness is not None else ()
        tool_names = {tool.name for tool in tools}
        if harness is not None:
            metadata["worker"]["tool_names"] = [tool.name for tool in tools]
            if isinstance(harness, WorkflowHarness):
                for skill in metadata["skills"]:
                    skill["tool_names"] = [tool.name for tool in tools]
                metadata["workflow"] = harness.definition_manifest()
            known_skills = {str(skill["id"]) for skill in metadata["skills"]}
            metadata["skills"].extend(
                {
                    "id": skill.skill_id,
                    "version": skill.version,
                    "title": skill.title,
                    "description": skill.description,
                    "tool_names": list(skill.tool_names),
                    "content_sha256": skill.content_sha256,
                }
                for skill in harness.skills
                if skill.skill_id not in known_skills
            )
        metadata["availability"] = {
            "status": "ready" if harness is not None else "unavailable",
            "reason_code": None if harness is not None else reason or "not_enabled",
        }
        metadata["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "effect": tool.effect,
                "tool_revision": tool.tool_revision,
                "plugin_id": (
                    "com.sasori.flow"
                    if isinstance(harness, WorkflowHarness)
                    else _TOOL_ORIGINS.get(
                        tool.name,
                        "sasori_apps.incident"
                        if app_id == "incident"
                        else "configured-mcp-stdio",
                    )
                ),
                "input_schema": compile_tool_schema(tool),
                "schema_sha256": tool_schema_sha256(tool),
            }
            for tool in tools
        ]
        metadata["plugins"] = _plugins(app_id, tool_names)
        apps.append(metadata)
    return {"schema_version": 1, "apps": apps}


__all__ = [
    "WorkflowPreflightFailure",
    "app_id_for_spec",
    "application_catalog",
    "application_specs",
    "application_surface_catalog",
    "workflow_preflight_definition",
    "workflow_preflight_tools",
]
