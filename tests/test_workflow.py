from __future__ import annotations

import asyncio
import ast
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sasori import (
    Harness,
    InjectedFault,
    Message,
    ModelCallError,
    ModelReply,
    RunCancelled,
    RunPaused,
    SQLiteStore,
    Tool,
    ToolCall,
)
from sasori_flow import (
    InputRef,
    InputSlot,
    Literal,
    MAX_WORKFLOW_DEFINITION_BYTES,
    MAX_WORKFLOW_INPUT_SLOTS,
    MAX_WORKFLOW_PAYLOAD_BYTES,
    MAX_WORKFLOW_STEP_ARGUMENTS,
    StepRef,
    ToolStep,
    WorkflowCompileError,
    WorkflowIntegrityError,
    WorkflowSpec,
    WorkflowValidationError,
    compile_workflow,
    json_sha256,
    resolve_arguments,
    workflow_app_id,
)
from sasori._provider_common import compile_tool_schema


class UnusedModel:
    async def complete(self, messages, tools):
        raise AssertionError("the base Harness model must not execute")


def _base_harness(
    store: SQLiteStore,
    tools: tuple[Tool, ...],
    *,
    fault_injector=None,
) -> Harness:
    return Harness(
        UnusedModel(),
        tools,
        store=store,
        fault_injector=fault_injector,
        model_timeout=2,
        tool_timeout=2,
    )


def _incident_spec(inspect_tool: Tool, record_tool: Tool) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="incident-response",
        version="1",
        inputs=(InputSlot("incident", "string"),),
        steps=(
            ToolStep.from_tool(
                "inspect",
                inspect_tool,
                {"summary": InputRef("incident")},
                result_type="string",
            ),
            ToolStep.from_tool(
                "record",
                record_tool,
                {"summary": StepRef("inspect")},
                result_type="string",
            ),
        ),
        output_step="record",
    )


class WorkflowSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        def inspect(summary: str) -> str:
            return summary

        def record(summary: str) -> str:
            return summary

        self.inspect = Tool("inspect", inspect, effect="read_only")
        self.record = Tool(
            "record", record, effect="side_effecting", tool_revision="1"
        )

    def test_definition_digest_is_canonical_and_versioned(self):
        first = WorkflowSpec(
            "canonical-flow",
            "1",
            (InputSlot("a", "string"), InputSlot("b", "integer")),
            (
                ToolStep.from_tool(
                    "inspect",
                    self.inspect,
                    {"summary": Literal({"b": 2, "a": 1})},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        second = WorkflowSpec(
            "canonical-flow",
            "1",
            (InputSlot("a", "string"), InputSlot("b", "integer")),
            (
                ToolStep.from_tool(
                    "inspect",
                    self.inspect,
                    {"summary": Literal({"a": 1, "b": 2})},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(len(first.digest), 64)
        self.assertNotEqual(first.digest, replace(first, version="2").digest)
        self.assertTrue(workflow_app_id(first).endswith(first.digest[:12]))

    def test_spec_rejects_forward_refs_duplicates_and_non_json_literals(self):
        with self.assertRaisesRegex(WorkflowValidationError, "earlier"):
            WorkflowSpec(
                "bad-forward",
                "1",
                (),
                (
                    ToolStep.from_tool(
                        "first",
                        self.inspect,
                        {"summary": StepRef("later")},
                        result_type="string",
                    ),
                    ToolStep.from_tool(
                        "later",
                        self.inspect,
                        {"summary": Literal("x")},
                        result_type="string",
                    ),
                ),
                "later",
            )
        duplicate = ToolStep.from_tool(
            "same",
            self.inspect,
            {"summary": Literal("x")},
            result_type="string",
        )
        with self.assertRaisesRegex(WorkflowValidationError, "unique"):
            WorkflowSpec("duplicates", "1", (), (duplicate, duplicate), "same")
        with self.assertRaises(WorkflowValidationError):
            Literal(float("nan"))
        with self.assertRaises(WorkflowValidationError):
            Literal(object())
        with self.assertRaisesRegex(WorkflowValidationError, "Unicode"):
            Literal("\ud800")

    def test_definition_argument_and_payload_bounds_are_global(self):
        step = ToolStep.from_tool(
            "inspect",
            self.inspect,
            {"summary": Literal("ok")},
            result_type="string",
        )
        with self.assertRaisesRegex(WorkflowValidationError, "input slots"):
            WorkflowSpec(
                "too-many-inputs",
                "1",
                tuple(
                    InputSlot(f"slot-{index}", "string")
                    for index in range(MAX_WORKFLOW_INPUT_SLOTS + 1)
                ),
                (step,),
                "inspect",
            )
        with self.assertRaisesRegex(WorkflowValidationError, "step arguments"):
            ToolStep.from_tool(
                "too-many-arguments",
                self.inspect,
                {
                    f"argument-{index}": Literal("x")
                    for index in range(MAX_WORKFLOW_STEP_ARGUMENTS + 1)
                },
                result_type="string",
            )

        large_steps = tuple(
            ToolStep.from_tool(
                f"large-{index}",
                self.inspect,
                {"summary": Literal("x" * 120_000)},
                result_type="string",
            )
            for index in range(MAX_WORKFLOW_DEFINITION_BYTES // 120_000 + 2)
        )
        with self.assertRaisesRegex(WorkflowValidationError, "definition"):
            WorkflowSpec("large-definition", "1", (), large_steps, large_steps[-1].step_id)

        def pair(left: str, right: str) -> str:
            return left + right

        pair_tool = Tool("pair", pair, effect="read_only")
        payload_step = ToolStep.from_tool(
            "pair",
            pair_tool,
            {
                "left": Literal("x" * 131_070),
                "right": Literal("y" * 131_070),
            },
            result_type="string",
        )
        self.assertLess(131_070, MAX_WORKFLOW_PAYLOAD_BYTES)
        with self.assertRaisesRegex(WorkflowValidationError, "resolved"):
            resolve_arguments(payload_step, {}, {})

    def test_architecture_keeps_one_core_loop_store_and_public_boundary(self):
        source_root = Path(__file__).parents[1] / "src"
        core_files = tuple((source_root / "sasori").rglob("*.py"))
        flow_files = tuple((source_root / "sasori_flow").rglob("*.py"))

        for path in core_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            imported.update(
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            )
            self.assertFalse(
                any(name == "sasori_flow" or name.startswith("sasori_flow.") for name in imported),
                f"core must not depend on sasori_flow: {path}",
            )

        flow_trees = {
            path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for path in flow_files
        }
        classes = {
            node.name
            for tree in flow_trees.values()
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        self.assertFalse(
            any(name.endswith("Store") for name in classes),
            "sasori_flow must not introduce a second durable store authority",
        )
        self.assertFalse(
            any(
                isinstance(node, ast.While)
                or (
                    isinstance(node, ast.Attribute)
                    and node.attr == "create_task"
                )
                for tree in flow_trees.values()
                for node in ast.walk(tree)
            ),
            "sasori_flow must not introduce a scheduler or background loop",
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and node.attr in {"_drive", "_process_call", "_invoke"}
                for tree in flow_trees.values()
                for node in ast.walk(tree)
            ),
            "sasori_flow must not call private core execution APIs",
        )

        runtime_tree = flow_trees[source_root / "sasori_flow" / "runtime.py"]
        model_class = next(
            node
            for node in runtime_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WorkflowModel"
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute) and node.attr == "handler"
                for node in ast.walk(model_class)
            ),
            "WorkflowModel must only propose ToolCalls, never execute handlers",
        )

    def test_compiled_workflow_emits_one_bounded_call_per_model_step(self):
        first = Tool("first", lambda value: value + ":first", effect="read_only")
        second = Tool("second", lambda value: value + ":second", effect="read_only")
        spec = WorkflowSpec(
            "bounded-flow",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "first",
                    first,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
                ToolStep.from_tool(
                    "second",
                    second,
                    {"value": StepRef("first")},
                    result_type="string",
                ),
            ),
            "second",
        )
        store = SQLiteStore()
        self.addCleanup(store.close)
        workflow = compile_workflow(spec, _base_harness(store, (first, second)))
        result = asyncio.run(workflow.run({"value": "input"}, run_id="bounded"))

        self.assertEqual(workflow.max_steps, len(spec.steps) + 1)
        assistant_calls = [
            len(message.tool_calls)
            for message in store.load(result.run_id).history
            if message.role == "assistant"
        ]
        self.assertEqual(assistant_calls, [1, 1, 0])
        self.assertTrue(all(len(tool.name.encode("utf-8")) <= 64 for tool in workflow.tools))
        self.assertTrue(all(compile_tool_schema(tool)["type"] == "object" for tool in workflow.tools))

        call_ids = [step["call_id"] for step in workflow.projection(result.run_id)["steps"]]
        self.assertEqual(len(call_ids), len(set(call_ids)))
        self.assertTrue(
            all(call_id and "\x00" not in call_id and len(call_id.encode("utf-8")) <= 256 for call_id in call_ids)
        )

    def test_input_schema_is_strict_bounded_and_boolean_is_not_integer(self):
        spec = WorkflowSpec(
            "strict-input",
            "1",
            (
                InputSlot("count", "integer", max_bytes=8),
                InputSlot("note", "string", required=False, max_bytes=8),
            ),
            (
                ToolStep.from_tool(
                    "inspect",
                    self.inspect,
                    {"summary": Literal("ok")},
                    result_type="string",
                ),
            ),
            "inspect",
        )
        self.assertEqual(dict(spec.validate_input({"count": 2})), {"count": 2})
        for invalid in (
            {"count": True},
            {"count": 2, "extra": 1},
            {},
            {"count": 2, "note": "too-long-note"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkflowValidationError):
                    spec.validate_input(invalid)

    def test_compile_fails_closed_on_tool_contract_drift(self):
        spec = _incident_spec(self.inspect, self.record)
        changed_effect = Tool(
            "record", self.record.handler, effect="read_only"
        )
        changed_revision = Tool(
            "record",
            self.record.handler,
            effect="side_effecting",
            tool_revision="2",
        )

        def changed_schema(summary: str, extra: str = "") -> str:
            return summary + extra

        schema_tool = Tool(
            "record",
            changed_schema,
            effect="side_effecting",
            tool_revision="1",
        )
        for replacement in (changed_effect, changed_revision, schema_tool):
            with self.subTest(tool=replacement):
                store = SQLiteStore()
                self.addCleanup(store.close)
                with self.assertRaises(WorkflowCompileError):
                    compile_workflow(
                        spec, _base_harness(store, (self.inspect, replacement))
                    )

        missing_argument = replace(
            spec,
            steps=(
                spec.steps[0],
                replace(spec.steps[1], arguments={}),
            ),
        )
        store = SQLiteStore()
        self.addCleanup(store.close)
        with self.assertRaisesRegex(WorkflowCompileError, "arguments"):
            compile_workflow(
                missing_argument, _base_harness(store, (self.inspect, self.record))
            )

        def remember(text: str, *, idempotency_key: str) -> str:
            return text

        idempotent = Tool(
            "remember",
            remember,
            effect="idempotent",
            idempotency_key=lambda arguments: str(arguments["text"]),
            tool_revision="1",
        )
        reserved = WorkflowSpec(
            "reserved-argument",
            "1",
            (),
            (
                ToolStep.from_tool(
                    "remember",
                    idempotent,
                    {
                        "text": Literal("x"),
                        "idempotency_key": Literal("forged"),
                    },
                    result_type="string",
                ),
            ),
            "remember",
        )
        with self.assertRaisesRegex(WorkflowCompileError, "arguments"):
            compile_workflow(reserved, _base_harness(store, (idempotent,)))

        inspectable = Tool("inspectable", lambda value: value, effect="read_only")
        unreadable_spec = WorkflowSpec(
            "unreadable-schema",
            "1",
            (),
            (
                ToolStep.from_tool(
                    "inspect",
                    inspectable,
                    {"value": Literal("x")},
                    result_type="string",
                ),
            ),
            "inspect",
        )

        def unreadable(value):
            return value

        unreadable.__annotations__["value"] = object()
        unreadable_tool = Tool("inspectable", unreadable, effect="read_only")
        unreadable_store = SQLiteStore()
        self.addCleanup(unreadable_store.close)
        with self.assertRaisesRegex(
            WorkflowCompileError, "tool schema cannot be inspected"
        ):
            compile_workflow(
                unreadable_spec,
                _base_harness(unreadable_store, (unreadable_tool,)),
            )

        signature_reads = 0

        class ChangingSignature(type):
            @property
            def __signature__(cls):
                nonlocal signature_reads
                signature_reads += 1
                if signature_reads == 1:
                    return inspect.signature(inspectable.handler)
                raise ValueError("signature changed")

        class ChangingHandler(metaclass=ChangingSignature):
            def __new__(cls, value: str) -> str:
                raise AssertionError("rejected Workflow Tool must not execute")

        changing_tool = Tool(
            "inspectable", ChangingHandler, effect="read_only"
        )
        changing_store = SQLiteStore()
        self.addCleanup(changing_store.close)
        with self.assertRaisesRegex(
            WorkflowCompileError, "tool schema cannot be inspected"
        ):
            compile_workflow(
                unreadable_spec,
                _base_harness(changing_store, (changing_tool,)),
            )
        self.assertEqual(signature_reads, 2)

    def test_compile_rejects_non_keyword_handlers_before_any_run(self):
        effects: list[object] = []

        def positional_only(value: str, /) -> str:
            effects.append(value)
            return value

        def var_positional(*values: str) -> str:
            effects.extend(values)
            return "".join(values)

        def var_keyword(**values: str) -> str:
            effects.append(values)
            return "ok"

        def reserved_key(*, idempotency_key: str) -> str:
            effects.append(idempotency_key)
            return idempotency_key

        cases = (
            ("positional-only", positional_only, {"value": Literal("x")}, "keywords"),
            ("var-positional", var_positional, {"values": Literal([])}, "keywords"),
            ("var-keyword", var_keyword, {"values": Literal({})}, "keywords"),
            ("reserved-key", reserved_key, {}, "reserved"),
        )
        for name, handler, arguments, error in cases:
            with self.subTest(name=name):
                tool = Tool(
                    name,
                    handler,
                    effect="side_effecting",
                    tool_revision="1",
                )
                spec = WorkflowSpec(
                    f"reject-{name}",
                    "1",
                    (),
                    (
                        ToolStep.from_tool(
                            "mutate",
                            tool,
                            arguments,
                            result_type="string",
                        ),
                    ),
                    "mutate",
                )
                store = SQLiteStore()
                self.addCleanup(store.close)
                with self.assertRaisesRegex(WorkflowCompileError, error):
                    compile_workflow(spec, _base_harness(store, (tool,)))
                self.assertEqual(store.list_runs(limit=1), ())
                self.assertEqual(effects, [])


class WorkflowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = SQLiteStore()
        self.addCleanup(self.store.close)
        self.inspections: list[str] = []
        self.actions: list[str] = []

        def inspect(summary: str) -> str:
            self.inspections.append(summary)
            return f"diagnostic:{summary}"

        def record(summary: str) -> str:
            self.actions.append(summary)
            return f"recorded:{summary}"

        self.inspect_tool = Tool("inspect_incident", inspect, effect="read_only")
        self.record_tool = Tool(
            "record_action",
            record,
            effect="side_effecting",
            tool_revision="1",
        )
        self.spec = _incident_spec(self.inspect_tool, self.record_tool)
        self.workflow = compile_workflow(
            self.spec,
            _base_harness(self.store, (self.inspect_tool, self.record_tool)),
        )

    async def test_read_then_mutate_uses_one_harness_approval_path(self):
        with self.assertRaises(RunPaused) as paused:
            await self.workflow.run(
                {"incident": "checkout latency"}, run_id="WorkflowApproval"
            )
        self.assertEqual(paused.exception.reason, "approval_required")
        self.assertEqual(self.inspections, ["checkout latency"])
        self.assertEqual(self.actions, [])
        approval = paused.exception.request
        self.assertIsNotNone(approval)
        assert approval is not None
        self.assertEqual(approval.effect, "side_effecting")
        self.assertEqual(approval.arguments["step_id"], "record")
        self.assertEqual(
            approval.arguments["definition_sha256"], self.spec.digest
        )
        self.assertEqual(
            json.loads(str(approval.arguments["payload_json"])),
            {"summary": "diagnostic:checkout latency"},
        )

        projected = self.workflow.projection("WorkflowApproval")
        self.assertEqual(
            [step["status"] for step in projected["steps"]],
            ["completed", "paused"],
        )
        self.workflow.resolve_approval(
            "WorkflowApproval", approval.fingerprint, True
        )
        self.assertEqual(self.actions, [], "approval must not execute the tool")

        result = await self.workflow.resume("WorkflowApproval")
        self.assertEqual(len(self.actions), 1)
        outcome = json.loads(result.final_message.content)
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(outcome["definition_sha256"], self.spec.digest)
        self.assertEqual(
            outcome["output"]["value"],
            "recorded:diagnostic:checkout latency",
        )
        again = await self.workflow.resume("WorkflowApproval")
        self.assertEqual(again.final_message, result.final_message)
        self.assertEqual(len(self.actions), 1)
        self.assertFalse(
            any(
                item.event.type.startswith("workflow.")
                for item in self.workflow.stored_events("WorkflowApproval")
            )
        )

    async def test_denial_executes_no_mutable_tool_and_fails_before_downstream(self):
        with self.assertRaises(RunPaused) as paused:
            await self.workflow.run({"incident": "denied"}, run_id="WorkflowDenied")
        approval = paused.exception.request
        assert approval is not None
        self.workflow.resolve_approval("WorkflowDenied", approval.fingerprint, False)
        with self.assertRaises(ModelCallError):
            await self.workflow.resume("WorkflowDenied")
        self.assertEqual(self.actions, [])
        events = [
            item.event.type for item in self.workflow.stored_events("WorkflowDenied")
        ]
        self.assertIn("tool.failed", events)
        self.assertIn("run.failed", events)
        self.assertNotIn("run.completed", events)

    async def test_completed_run_reopens_without_replaying_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "core.sqlite3"
            inspections: list[str] = []
            actions: list[str] = []

            def inspect(summary: str) -> str:
                inspections.append(summary)
                return f"checked:{summary}"

            def record(summary: str) -> str:
                actions.append(summary)
                return summary

            inspect_tool = Tool("inspect_incident", inspect, effect="read_only")
            record_tool = Tool(
                "record_action",
                record,
                effect="side_effecting",
                tool_revision="1",
            )
            spec = _incident_spec(inspect_tool, record_tool)
            first_store = SQLiteStore(database)
            first = compile_workflow(
                spec, _base_harness(first_store, (inspect_tool, record_tool))
            )
            with self.assertRaises(RunPaused) as paused:
                await first.run({"incident": "restart"}, run_id="WorkflowRestart")
            approval = paused.exception.request
            assert approval is not None
            first.resolve_approval("WorkflowRestart", approval.fingerprint, True)
            first_store.close()

            reopened_store = SQLiteStore(database)
            self.addCleanup(reopened_store.close)
            reopened = compile_workflow(
                spec, _base_harness(reopened_store, (inspect_tool, record_tool))
            )
            result = await reopened.resume("WorkflowRestart")
            self.assertEqual(result.steps, 3)
            self.assertEqual(inspections, ["restart"])
            self.assertEqual(actions, ["checked:restart"])
            reopened_store.close()

            final_store = SQLiteStore(database)
            self.addCleanup(final_store.close)
            final = compile_workflow(
                spec, _base_harness(final_store, (inspect_tool, record_tool))
            )
            again = await final.resume("WorkflowRestart")
            self.assertEqual(again.final_message, result.final_message)
            self.assertEqual(inspections, ["restart"])
            self.assertEqual(actions, ["checked:restart"])
            final_store.close()

    async def test_changed_definition_cannot_resume_old_app_binding(self):
        with self.assertRaises(RunPaused):
            await self.workflow.run({"incident": "drift"}, run_id="WorkflowDrift")
        changed = replace(self.spec, version="2")
        replacement = compile_workflow(
            changed,
            _base_harness(self.store, (self.inspect_tool, self.record_tool)),
        )
        self.assertNotEqual(replacement.app_id, self.workflow.app_id)
        with self.assertRaises(WorkflowIntegrityError):
            await replacement.resume("WorkflowDrift")
        self.assertEqual(self.actions, [])

    async def test_manual_effect_recovery_advances_from_committed_envelope_once(self):
        fired = False

        def fault(point: str) -> None:
            nonlocal fired
            if point == "after_tool_return" and len(self.actions) == 1 and not fired:
                fired = True
                raise InjectedFault(point)

        workflow = compile_workflow(
            self.spec,
            _base_harness(
                self.store,
                (self.inspect_tool, self.record_tool),
                fault_injector=fault,
            ),
        )
        with self.assertRaises(RunPaused) as paused:
            await workflow.run({"incident": "ambiguous"}, run_id="WorkflowUnknown")
        approval = paused.exception.request
        assert approval is not None
        workflow.resolve_approval("WorkflowUnknown", approval.fingerprint, True)
        with self.assertRaises(InjectedFault):
            await workflow.resume("WorkflowUnknown")
        self.assertEqual(len(self.actions), 1)
        with self.assertRaises(RunPaused) as unknown:
            await workflow.resume("WorkflowUnknown")
        self.assertEqual(unknown.exception.reason, "effect_unknown")
        call = next(
            item
            for item in self.store.calls("WorkflowUnknown", 2)
            if item.status == "effect_unknown"
        )
        value = "recorded:diagnostic:ambiguous"
        envelope = {
            "version": 1,
            "definition_sha256": self.spec.digest,
            "step_id": "record",
            "value": value,
            "value_sha256": json_sha256(value),
        }
        events_before_invalid = self.store.events("WorkflowUnknown")
        with self.assertRaises(WorkflowIntegrityError):
            workflow.resolve_effect(
                "WorkflowUnknown",
                call.fingerprint,
                "record_result",
                reason="invalid operator result",
                result={**envelope, "value_sha256": "0" * 64},
            )
        self.assertEqual(self.store.events("WorkflowUnknown"), events_before_invalid)
        unchanged = self.store.load("WorkflowUnknown")
        self.assertEqual(unchanged.status, "effect_unknown")
        self.assertEqual(
            self.store.calls("WorkflowUnknown", 2)[0].status,
            "effect_unknown",
        )
        workflow.resolve_effect(
            "WorkflowUnknown",
            call.fingerprint,
            "record_result",
            reason="operator verified the append",
            result=envelope,
        )
        result = await workflow.resume("WorkflowUnknown")
        self.assertEqual(json.loads(result.final_message.content)["output"]["value"], value)
        self.assertEqual(len(self.actions), 1)

    async def test_idempotent_retry_reuses_the_same_wrapped_key(self):
        keys: list[str] = []
        effects: set[str] = set()
        fired = False

        def remember(text: str, *, idempotency_key: str) -> str:
            keys.append(idempotency_key)
            effects.add(idempotency_key)
            return f"remembered:{text}"

        tool = Tool(
            "remember",
            remember,
            effect="idempotent",
            idempotency_key=lambda arguments: f"business:{arguments['text']}",
            tool_revision="1",
        )
        spec = WorkflowSpec(
            "idempotent-flow",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "remember",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "remember",
        )

        def fault(point: str) -> None:
            nonlocal fired
            if point == "after_tool_return" and not fired:
                fired = True
                raise InjectedFault(point)

        workflow = compile_workflow(
            spec, _base_harness(self.store, (tool,), fault_injector=fault)
        )
        with self.assertRaises(RunPaused) as paused:
            await workflow.run({"text": "same"}, run_id="WorkflowIdempotent")
        approval = paused.exception.request
        assert approval is not None
        workflow.resolve_approval("WorkflowIdempotent", approval.fingerprint, True)
        with self.assertRaises(InjectedFault):
            await workflow.resume("WorkflowIdempotent")
        result = await workflow.resume("WorkflowIdempotent")
        self.assertEqual(json.loads(result.final_message.content)["status"], "succeeded")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], "business:same")
        self.assertEqual(len(effects), 1)

    async def test_invalid_mutable_result_enters_effect_unknown_not_success(self):
        effects: list[str] = []

        def mutate(text: str) -> int:
            effects.append(text)
            return 7

        tool = Tool(
            "mutate",
            mutate,
            effect="side_effecting",
            tool_revision="1",
        )
        spec = WorkflowSpec(
            "typed-output",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "mutate",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "mutate",
        )
        workflow = compile_workflow(spec, _base_harness(self.store, (tool,)))
        with self.assertRaises(RunPaused) as paused:
            await workflow.run({"text": "effect"}, run_id="WorkflowBadOutput")
        approval = paused.exception.request
        assert approval is not None
        workflow.resolve_approval("WorkflowBadOutput", approval.fingerprint, True)
        with self.assertRaises(RunPaused) as unknown:
            await workflow.resume("WorkflowBadOutput")
        self.assertEqual(unknown.exception.reason, "effect_unknown")
        self.assertEqual(effects, ["effect"])
        self.assertNotIn(
            "run.completed",
            [item.event.type for item in workflow.stored_events("WorkflowBadOutput")],
        )

    async def test_read_only_result_type_error_is_explicit_run_failure(self):
        calls: list[str] = []

        def read(text: str) -> int:
            calls.append(text)
            return 1

        tool = Tool("read", read, effect="read_only")
        spec = WorkflowSpec(
            "bad-read-output",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "read",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "read",
        )
        workflow = compile_workflow(spec, _base_harness(self.store, (tool,)))
        with self.assertRaises(ModelCallError):
            await workflow.run({"text": "bad"}, run_id="WorkflowBadRead")
        self.assertEqual(calls, ["bad"])
        events = [item.event.type for item in workflow.stored_events("WorkflowBadRead")]
        self.assertIn("tool.failed", events)
        self.assertIn("run.failed", events)
        self.assertNotIn("run.completed", events)

    async def test_cancellation_propagates_and_never_starts_the_next_step(self):
        started = asyncio.Event()
        release = asyncio.Event()
        writes: list[str] = []

        async def slow(text: str) -> str:
            started.set()
            await release.wait()
            return text

        def write(text: str) -> str:
            writes.append(text)
            return text

        slow_tool = Tool("slow", slow, effect="read_only")
        write_tool = Tool(
            "write", write, effect="side_effecting", tool_revision="1"
        )
        spec = WorkflowSpec(
            "cancel-flow",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "slow",
                    slow_tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
                ToolStep.from_tool(
                    "write",
                    write_tool,
                    {"text": StepRef("slow")},
                    result_type="string",
                ),
            ),
            "write",
        )
        workflow = compile_workflow(
            spec, _base_harness(self.store, (slow_tool, write_tool))
        )
        task = asyncio.create_task(
            workflow.run({"text": "cancel"}, run_id="WorkflowCancel")
        )
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(writes, [])
        self.assertEqual(self.store.calls("WorkflowCancel", 2), ())
        with self.assertRaises(RunCancelled):
            await workflow.resume("WorkflowCancel")


class WorkflowTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_pending_call_tampering_is_rejected_before_dispatch(self):
        effects: list[str] = []

        def write(text: str) -> str:
            effects.append(text)
            return text

        tool = Tool(
            "write",
            write,
            effect="side_effecting",
            tool_revision="1",
        )
        spec = WorkflowSpec(
            "durable-tamper-flow",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "write",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "write",
        )
        store = SQLiteStore()
        self.addCleanup(store.close)
        workflow = compile_workflow(spec, _base_harness(store, (tool,)))
        with self.assertRaises(RunPaused) as paused:
            await workflow.run({"text": "approved"}, run_id="DurableTamper")
        request = paused.exception.request
        assert request is not None
        workflow.resolve_approval("DurableTamper", request.fingerprint, True)

        call = store.calls("DurableTamper", 1)[0]
        arguments = dict(call.arguments)
        arguments["payload_json"] = json.dumps(
            {"text": "tampered"}, sort_keys=True, separators=(",", ":")
        )
        assert store._db is not None
        store._db.execute(
            """
            UPDATE tool_calls SET arguments_json = ?
            WHERE run_id = ? AND step = ? AND ordinal = ?
            """,
            (
                json.dumps(arguments, sort_keys=True, separators=(",", ":")),
                "DurableTamper",
                1,
                0,
            ),
        )
        events_before = store.events("DurableTamper")
        with self.assertRaises(WorkflowIntegrityError):
            await workflow.resume("DurableTamper")
        self.assertEqual(effects, [])
        self.assertEqual(store.events("DurableTamper"), events_before)
        event_types = [event.type for event in events_before]
        self.assertNotIn("tool.started", event_types)
        self.assertNotIn("tool.completed", event_types)
        self.assertNotIn("run.completed", event_types)

    async def test_transcript_tampering_is_rejected_before_any_dispatch(self):
        calls: list[str] = []

        def read(text: str) -> str:
            calls.append(text)
            return text

        tool = Tool("read", read, effect="read_only")
        spec = WorkflowSpec(
            "tamper-flow",
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "read",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "read",
        )
        store = SQLiteStore()
        self.addCleanup(store.close)
        workflow = compile_workflow(spec, _base_harness(store, (tool,)))
        model = workflow.harness.model
        initial = model.initial_messages({"text": "safe"})
        reply = await model.complete(initial, model.tools)
        expected = reply.tool_calls[0]

        wrong_id = Message(
            "assistant",
            tool_calls=(
                ToolCall("wrong", expected.name, expected.arguments, complete=True),
            ),
        )
        with self.assertRaises(WorkflowIntegrityError):
            await model.complete(initial + (wrong_id,), model.tools)

        injected_state = Message(
            "assistant",
            tool_calls=(expected,),
            provider_state="untrusted-provider-state",
        )
        with self.assertRaises(WorkflowIntegrityError):
            await model.complete(initial + (injected_state,), model.tools)

        accepted = Message("assistant", tool_calls=(expected,))
        forged = Message(
            "tool",
            json.dumps(
                {
                    "version": 1,
                    "definition_sha256": "0" * 64,
                    "step_id": "read",
                    "value": "forged",
                    "value_sha256": json_sha256("forged"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            tool_call_id=expected.id,
            tool_name=expected.name,
        )
        with self.assertRaises(WorkflowIntegrityError):
            await model.complete(initial + (accepted, forged), model.tools)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
