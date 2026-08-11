from __future__ import annotations

from sasori import Harness, ModelReply, SQLiteStore
from sasori_flow import (
    SerialWorkflowBuilder,
    compile_workflow,
    preflight_workflow,
    workflow_app_id,
)

from .incident import incident_tools


class _UnusedModel:
    async def complete(self, messages, tools) -> ModelReply:
        raise AssertionError("the workflow compiler must replace the base model")


_TOOLS = incident_tools()
_BUILDER = SerialWorkflowBuilder("incident-mechanism", version="1")
_INCIDENT = _BUILDER.input(
    "incident", value_type="string", max_bytes=16 * 1024
)
_INSPECTION = _BUILDER.step(
    "inspect",
    _TOOLS[0],
    arguments={"summary": _INCIDENT},
    result_type="string",
    max_result_bytes=32 * 1024,
)
_RECORD = _BUILDER.step(
    "record",
    _TOOLS[1],
    arguments={"summary": _INSPECTION},
    result_type="string",
    max_result_bytes=32 * 1024,
)
WORKFLOW_SPEC = _BUILDER.build(output=_RECORD)
APP_ID = workflow_app_id(WORKFLOW_SPEC)

APP_METADATA = {
    "id": APP_ID,
    "title": "Incident Workflow",
    "description": (
        "A version-bound typed tool sequence using the same Harness approval "
        "and recovery path."
    ),
    "worker": {
        "id": "incident-workflow-v1",
        "title": "Incident Workflow",
        "model_slot": "deterministic-workflow",
        "tool_names": ["inspect_incident", "record_action"],
        "logical_tool_names": ["inspect_incident", "record_action"],
    },
    "skills": [
        {
            "id": "typed-incident-workflow",
            "title": "Typed incident workflow",
            "description": (
                "Inspect one incident, then pause before one mutable audit action."
            ),
            "tool_names": ["inspect_incident", "record_action"],
            "logical_tool_names": ["inspect_incident", "record_action"],
        }
    ],
    "workflow": preflight_workflow(WORKFLOW_SPEC, _TOOLS),
}


def create_harness(
    store: SQLiteStore,
    *,
    app_id: str | None = None,
) -> Harness:
    if app_id is not None and app_id != APP_ID:
        raise ValueError("workflow app_id must match its immutable definition")
    base = Harness(_UnusedModel(), incident_tools(), store=store)
    return compile_workflow(WORKFLOW_SPEC, base)


__all__ = ["APP_ID", "APP_METADATA", "WORKFLOW_SPEC", "create_harness"]
