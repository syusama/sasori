from __future__ import annotations

from sasori import Harness, ModelReply, SQLiteStore
from sasori_flow import (
    InputRef,
    InputSlot,
    StepRef,
    ToolStep,
    WorkflowSpec,
    compile_workflow,
    workflow_app_id,
)

from .incident import incident_tools


class _UnusedModel:
    async def complete(self, messages, tools) -> ModelReply:
        raise AssertionError("the workflow compiler must replace the base model")


_TOOLS = incident_tools()
WORKFLOW_SPEC = WorkflowSpec(
    workflow_id="incident-mechanism",
    version="1",
    inputs=(InputSlot("incident", "string", max_bytes=16 * 1024),),
    steps=(
        ToolStep.from_tool(
            "inspect",
            _TOOLS[0],
            {"summary": InputRef("incident")},
            result_type="string",
            max_result_bytes=32 * 1024,
        ),
        ToolStep.from_tool(
            "record",
            _TOOLS[1],
            {"summary": StepRef("inspect")},
            result_type="string",
            max_result_bytes=32 * 1024,
        ),
    ),
    output_step="record",
)
APP_ID = workflow_app_id(WORKFLOW_SPEC)

APP_METADATA = {
    "id": APP_ID,
    "title": "Incident Mechanism",
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
    "workflow": {
        "schema_version": 1,
        "workflow_id": WORKFLOW_SPEC.workflow_id,
        "version": WORKFLOW_SPEC.version,
        "definition_sha256": WORKFLOW_SPEC.digest,
        "execution": "single-harness-ordered-tools-v1",
        "step_count": len(WORKFLOW_SPEC.steps),
        "supports_parallel": False,
        "supports_branches": False,
        "supports_agent_nodes": False,
        "steps": [
            {
                "position": position,
                "step_id": step.step_id,
                "logical_tool_name": step.tool_name,
                "dispatch_tool_name": None,
                "effect": step.effect,
                "logical_tool_revision": step.tool_revision,
                "dispatch_tool_revision": None,
                "logical_schema_sha256": step.schema_sha256,
                "dispatch_schema_sha256": None,
                "result_type": step.result_type,
                "max_result_bytes": step.max_result_bytes,
                "is_output": step.step_id == WORKFLOW_SPEC.output_step,
            }
            for position, step in enumerate(WORKFLOW_SPEC.steps, start=1)
        ],
    },
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
