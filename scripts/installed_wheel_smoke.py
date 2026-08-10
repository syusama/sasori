from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


PACKAGES = (
    "sasori",
    "sasori_apps",
    "sasori_artifacts",
    "sasori_context",
    "sasori_flow",
    "sasori_market",
    "sasori_memory",
    "sasori_plugins",
    "sasori_web",
)
SCRIPTS = ("sasori", "sasori-server", "sasori-catalog")
WEB_RESOURCES = (
    "index.html",
    "app.0.1.0.css",
    "artifacts.0.1.0.css",
    "app.0.1.1.js",
    "event-reducer.0.1.0.js",
    "app.0.1.2.js",
    "app.0.1.3.js",
    "app.0.1.4.js",
    "workflow.0.1.0.css",
    "workflow.0.1.0.js",
    "workflow.0.2.0.js",
    "mark.0.1.0.svg",
)


def _require_under_prefix(path: str | os.PathLike[str], prefix: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
        resolved_prefix = prefix.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"installed {label} path is missing or unreadable") from exc
    if not resolved.is_relative_to(resolved_prefix):
        raise RuntimeError(f"installed {label} resolved outside the consumer environment")
    return resolved


def main() -> int:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as stream:
        expected_version = tomllib.load(stream)["project"]["version"]
    distribution = importlib.metadata.distribution("sasori")
    if distribution.metadata["Name"] != "sasori" or distribution.version != expected_version:
        raise RuntimeError("installed Sasori identity is invalid")
    if distribution.requires:
        raise RuntimeError("installed Sasori unexpectedly declares runtime dependencies")

    prefix = Path(sys.prefix)
    _require_under_prefix(distribution.locate_file(""), prefix, "distribution")

    modules = {}
    for package in PACKAGES:
        module = importlib.import_module(package)
        if module.__file__ is None:
            raise RuntimeError(f"installed package has no origin: {package}")
        _require_under_prefix(module.__file__, prefix, f"package {package}")
        modules[package] = module
    workflow_app = importlib.import_module("sasori_apps.workflow_incident")
    if workflow_app.__file__ is None:
        raise RuntimeError("installed Workflow application has no origin")
    _require_under_prefix(
        workflow_app.__file__, prefix, "application sasori_apps.workflow_incident"
    )
    workflow_app_id = getattr(workflow_app, "APP_ID", None)
    workflow_spec = getattr(workflow_app, "WORKFLOW_SPEC", None)
    create_workflow_harness = getattr(workflow_app, "create_harness", None)
    derive_workflow_app_id = getattr(modules["sasori_flow"], "workflow_app_id", None)
    SerialWorkflowBuilder = getattr(
        modules["sasori_flow"], "SerialWorkflowBuilder", None
    )
    workflow_spec_from_data = getattr(
        modules["sasori_flow"], "workflow_spec_from_data", None
    )
    workflow_spec_from_json = getattr(
        modules["sasori_flow"], "workflow_spec_from_json", None
    )
    if (
        not isinstance(workflow_app_id, str)
        or workflow_spec is None
        or not callable(create_workflow_harness)
        or not callable(derive_workflow_app_id)
        or not callable(SerialWorkflowBuilder)
        or not callable(workflow_spec_from_data)
        or not callable(workflow_spec_from_json)
        or workflow_app_id != derive_workflow_app_id(workflow_spec)
    ):
        raise RuntimeError("installed Workflow application identity is invalid")
    workflow_document = json.dumps(
        workflow_spec.as_data(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    parsed_workflows = (
        workflow_spec_from_data(workflow_spec.as_data()),
        workflow_spec_from_json(workflow_document.encode("utf-8")),
    )
    if any(
        parsed.as_data() != workflow_spec.as_data()
        or parsed.digest != workflow_spec.digest
        or derive_workflow_app_id(parsed) != workflow_app_id
        for parsed in parsed_workflows
    ):
        raise RuntimeError("installed Workflow authoring identity is invalid")

    SQLiteStore = getattr(modules["sasori"], "SQLiteStore", None)
    RunPaused = getattr(modules["sasori"], "RunPaused", None)
    if not callable(SQLiteStore) or not isinstance(RunPaused, type):
        raise RuntimeError("installed Workflow runtime exports are incomplete")
    with tempfile.TemporaryDirectory(prefix="sasori-installed-workflow-") as directory:
        root = Path(directory)
        database = root / "runs.sqlite3"
        action_log = root / "actions.jsonl"
        previous_action_log = os.environ.get("SASORI_ACTION_LOG")
        os.environ["SASORI_ACTION_LOG"] = str(action_log)
        first_store = SQLiteStore(database)
        try:
            harness = create_workflow_harness(first_store, app_id=workflow_app_id)
            approval_request = None
            try:
                asyncio.run(
                    harness.run(
                        "installed wheel workflow",
                        run_id="InstalledWorkflow",
                        app_id=workflow_app_id,
                    )
                )
            except RunPaused as paused:
                if paused.reason != "approval_required" or paused.request is None:
                    raise RuntimeError("installed Workflow paused for the wrong reason")
                approval_request = paused.request
            else:
                raise RuntimeError("installed Workflow did not require approval")
            if action_log.exists():
                raise RuntimeError("installed Workflow executed before approval and resume")
            paused_projection = harness.public_run_projection("InstalledWorkflow")
            if (
                paused_projection.get("state") != "paused"
                or paused_projection.get("workflow", {}).get("definition_sha256")
                != workflow_spec.digest
                or [
                    step.get("status")
                    for step in paused_projection.get("workflow", {}).get("steps", ())
                ]
                != ["completed", "approval_required"]
            ):
                raise RuntimeError("installed Workflow paused projection is invalid")
            harness.resolve_approval(
                "InstalledWorkflow", approval_request.fingerprint, True
            )
            if action_log.exists():
                raise RuntimeError("installed Workflow approval executed the effect")
            approved_projection = harness.public_run_projection("InstalledWorkflow")
            if [
                step.get("status")
                for step in approved_projection.get("workflow", {}).get("steps", ())
            ] != ["completed", "resume_required"]:
                raise RuntimeError("installed Workflow approved projection is invalid")
            result = asyncio.run(harness.resume("InstalledWorkflow"))
            final_content = result.final_message.content
            final = json.loads(final_content)
            expected_value = "diagnostic captured for installed wheel workflow"
            if (
                final_content
                != json.dumps(
                    final,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                or final.get("version") != 1
                or final.get("workflow_id") != workflow_spec.workflow_id
                or final.get("workflow_version") != workflow_spec.version
                or final.get("definition_sha256") != workflow_spec.digest
                or final.get("status") != "succeeded"
                or final.get("output", {}).get("step_id") != "record"
                or final.get("output", {}).get("value") != expected_value
                or final.get("output", {}).get("value_sha256")
                != hashlib.sha256(
                    json.dumps(
                        expected_value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            ):
                raise RuntimeError("installed Workflow typed final is invalid")
            events = first_store.events("InstalledWorkflow")
            completed_projection = harness.public_run_projection("InstalledWorkflow")
            if (
                completed_projection.get("state") != "completed"
                or completed_projection.get("workflow", {}).get("current_step_id")
                is not None
                or [
                    step.get("status")
                    for step in completed_projection.get("workflow", {}).get("steps", ())
                ]
                != ["completed", "completed"]
            ):
                raise RuntimeError("installed Workflow completed projection is invalid")
        finally:
            first_store.close()

        second_store = SQLiteStore(database)
        try:
            reopened = create_workflow_harness(
                second_store, app_id=workflow_app_id
            )
            again = asyncio.run(reopened.resume("InstalledWorkflow"))
            if (
                again.final_message.content != final_content
                or second_store.events("InstalledWorkflow") != events
                or reopened.public_run_projection("InstalledWorkflow")
                != completed_projection
            ):
                raise RuntimeError("installed Workflow durable reopen changed the run")
        finally:
            second_store.close()
            if previous_action_log is None:
                os.environ.pop("SASORI_ACTION_LOG", None)
            else:
                os.environ["SASORI_ACTION_LOG"] = previous_action_log
        actions = [
            json.loads(line)
            for line in action_log.read_text(encoding="utf-8").splitlines()
        ]
        if actions != [{"summary": expected_value}]:
            raise RuntimeError("installed Workflow effect was replayed or changed")
    Message = getattr(modules["sasori"], "Message", None)
    ContextBudget = getattr(modules["sasori_context"], "ContextBudget", None)
    ContextProjector = getattr(modules["sasori_context"], "ContextProjector", None)
    default_message_units = getattr(
        modules["sasori_context"], "default_message_units", None
    )
    if not all(
        callable(item)
        for item in (Message, ContextBudget, ContextProjector, default_message_units)
    ):
        raise RuntimeError("installed sasori_context public exports are incomplete")
    context_messages = (Message("system", "installed"), Message("user", "wheel"))
    context_budget = sum(default_message_units(item) for item in context_messages)
    context_projection = ContextProjector(ContextBudget(context_budget)).project(
        context_messages
    )
    if context_projection.messages != context_messages or context_projection.compacted:
        raise RuntimeError("installed sasori_context projection contract is invalid")
    ModelReply = getattr(modules["sasori"], "ModelReply", None)
    SemanticCompactionModel = getattr(
        modules["sasori_context"], "SemanticCompactionModel", None
    )
    SemanticCompactionPolicy = getattr(
        modules["sasori_context"], "SemanticCompactionPolicy", None
    )
    if not all(
        callable(item)
        for item in (ModelReply, SemanticCompactionModel, SemanticCompactionPolicy)
    ):
        raise RuntimeError("installed semantic compaction exports are incomplete")

    class SummaryModel:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if tools:
                raise RuntimeError("installed summarizer received runtime tools")
            envelope = json.loads(messages[1].content)
            return ModelReply(
                content=json.dumps(
                    {
                        "version": 1,
                        "source_sha256": envelope["source_sha256"],
                        "summary": "Installed wheel summary is source bound.",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    class PrimaryModel:
        def __init__(self):
            self.messages = ()

        async def complete(self, messages, tools):
            self.messages = messages
            return ModelReply(content="installed semantic primary")

    summary_model = SummaryModel()
    primary_model = PrimaryModel()
    semantic = SemanticCompactionModel(
        primary_model,
        ContextProjector(ContextBudget(2000, hot_turns=1)),
        summary_model,
        summarizer_name="installed:summary-v1",
        policy=SemanticCompactionPolicy(cache_entries=0),
    )
    semantic_history = (
        Message("system", "installed policy"),
        Message("user", "old installed history " + "x" * 5000),
        Message("assistant", "old installed answer"),
        Message("user", "current installed request"),
    )
    semantic_reply = asyncio.run(semantic.complete(semantic_history, ()))
    semantic_records = semantic.diagnostics()
    if (
        semantic_reply.content != "installed semantic primary"
        or summary_model.calls != 1
        or len(semantic_records) != 1
        or semantic_records[0].outcome != "succeeded"
        or not any("derived history" in item.content for item in primary_model.messages)
        or any("x" * 100 in item.content for item in primary_model.messages)
    ):
        raise RuntimeError("installed semantic compaction contract is invalid")
    MemoryBinding = getattr(modules["sasori_memory"], "MemoryBinding", None)
    MemorySource = getattr(modules["sasori_memory"], "MemorySource", None)
    MemoryStore = getattr(modules["sasori_memory"], "MemoryStore", None)
    MemoryContextModel = getattr(
        modules["sasori_memory"], "MemoryContextModel", None
    )
    if not all(
        callable(item)
        for item in (MemoryBinding, MemorySource, MemoryStore, MemoryContextModel)
    ):
        raise RuntimeError("installed Memory public exports are incomplete")
    with tempfile.TemporaryDirectory(prefix="sasori-installed-memory-") as directory:
        memory = MemoryStore(Path(directory) / "memory.sqlite3")
        binding = MemoryBinding("local-owner", "research", "private", "default", 1)
        source = MemorySource(
            "harness-tool-call",
            "installed-run",
            "step-1",
            1,
            "installed-call",
            hashlib.sha256(b"installed-call").hexdigest(),
            "model-proposed-unverified",
            "explicit-tool-v1",
            hashlib.sha256(b"explicit-tool-v1").hexdigest(),
        )
        memory.bind_run("installed-run", binding)
        remembered = json.loads(
            memory.remember(
                binding,
                logical_key="installed.fact",
                kind="semantic",
                content="installed wheel Memory",
                priority=50,
                expected_revision=0,
                source=source,
                idempotency_key="installed-operation",
            )
        )
        recalled = json.loads(memory.search(binding, "installed", 1))
        if (
            remembered.get("revision") != 1
            or len(recalled.get("matches", ())) != 1
            or recalled["matches"][0].get("memory_id") != remembered.get("memory_id")
        ):
            raise RuntimeError("installed Memory durability/retrieval contract is invalid")
    Event = getattr(modules["sasori"], "Event", None)
    SQLiteStore = getattr(modules["sasori"], "SQLiteStore", None)
    ArtifactStore = getattr(modules["sasori_artifacts"], "ArtifactStore", None)
    if not all(callable(item) for item in (Event, SQLiteStore, ArtifactStore)):
        raise RuntimeError("installed artifact public exports are incomplete")
    with tempfile.TemporaryDirectory(prefix="sasori-installed-artifact-") as directory:
        root = Path(directory)
        store = SQLiteStore(root / "runs.sqlite3")
        artifacts = None
        try:
            store.start(
                "installed-artifact",
                (Message("user", "wheel artifact"),),
                Event("run.started", "installed-artifact", 0),
            )
            artifacts = ArtifactStore(store, root / "artifacts")
            ref = artifacts.put(
                "installed-artifact",
                b'{"wheel":true}',
                declared_filename="wheel.json",
                declared_media_type="application/json",
            )
            if (
                artifacts.list("installed-artifact") != (ref,)
                or artifacts.get("installed-artifact", ref.artifact_id).content
                != b'{"wheel":true}'
                or store.stored_events("installed-artifact")[-1].event.type
                != "artifact.available"
            ):
                raise RuntimeError("installed artifact publish/read contract is invalid")
        finally:
            if artifacts is not None:
                artifacts.close()
            store.close()
    resources = importlib.resources.files("sasori_web")
    if any(not (resources / name).is_file() or not (resources / name).read_bytes() for name in WEB_RESOURCES):
        raise RuntimeError("installed Workbench resources are missing or empty")

    executable_dir = os.fspath(os.path.dirname(sys.executable))
    for name in SCRIPTS:
        command = shutil.which(name, path=executable_dir)
        if command is None:
            raise RuntimeError(f"installed entry point is missing: {name}")
        completed = subprocess.run(
            [command, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"installed entry point failed: {name}")

    print("installed wheel smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
