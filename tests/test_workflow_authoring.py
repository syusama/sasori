from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, SQLiteStore, Tool
from sasori_flow import (
    InputRef,
    InputSlot,
    Literal,
    SerialWorkflowBuilder,
    StepRef,
    ToolStep,
    WorkflowSpec,
    WorkflowValidationError,
    MAX_WORKFLOW_DEFINITION_BYTES,
    compile_workflow,
    workflow_app_id,
    workflow_spec_from_data,
    workflow_spec_from_json,
)


class _UnusedModel:
    async def complete(self, messages, tools):
        raise AssertionError("authoring validation must not execute the model")


class WorkflowAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        def inspect(summary: str) -> str:
            return f"checked:{summary}"

        def record(summary: str) -> str:
            return summary

        self.inspect = Tool("inspect", inspect, effect="read_only")
        self.record = Tool(
            "record", record, effect="side_effecting", tool_revision="1"
        )
        self.manual = WorkflowSpec(
            "authoring-parity",
            "1",
            (InputSlot("incident", "string", max_bytes=16 * 1024),),
            (
                ToolStep.from_tool(
                    "inspect",
                    self.inspect,
                    {"summary": InputRef("incident")},
                    result_type="string",
                    max_result_bytes=32 * 1024,
                ),
                ToolStep.from_tool(
                    "record",
                    self.record,
                    {"summary": StepRef("inspect")},
                    result_type="string",
                    max_result_bytes=32 * 1024,
                ),
            ),
            "record",
        )

    def test_data_json_and_builder_preserve_w0_identity(self) -> None:
        parsed_data = workflow_spec_from_data(self.manual.as_data())
        parsed_json = workflow_spec_from_json(
            json.dumps(self.manual.as_data(), ensure_ascii=False, indent=2).encode()
        )

        builder = SerialWorkflowBuilder("authoring-parity", version="1")
        incident = builder.input(
            "incident", value_type="string", max_bytes=16 * 1024
        )
        inspection = builder.step(
            "inspect",
            self.inspect,
            arguments={"summary": incident},
            result_type="string",
            max_result_bytes=32 * 1024,
        )
        record = builder.step(
            "record",
            self.record,
            arguments={"summary": inspection},
            result_type="string",
            max_result_bytes=32 * 1024,
        )
        built = builder.build(output=record)

        for candidate in (parsed_data, parsed_json, built):
            self.assertEqual(candidate.as_data(), self.manual.as_data())
            self.assertEqual(candidate.digest, self.manual.digest)
            self.assertEqual(workflow_app_id(candidate), workflow_app_id(self.manual))

        store = SQLiteStore()
        self.addCleanup(store.close)
        manual_runtime = compile_workflow(
            self.manual,
            Harness(_UnusedModel(), (self.inspect, self.record), store=store),
        )
        parsed_runtime = compile_workflow(
            parsed_json,
            Harness(_UnusedModel(), (self.inspect, self.record), store=store),
        )
        self.assertEqual(
            [tool.name for tool in parsed_runtime.tools],
            [tool.name for tool in manual_runtime.tools],
        )
        self.assertEqual(
            [tool.tool_revision for tool in parsed_runtime.tools],
            [tool.tool_revision for tool in manual_runtime.tools],
        )

    def test_strict_json_rejects_duplicate_unknown_and_nonstandard_values(self) -> None:
        document = json.dumps(self.manual.as_data(), separators=(",", ":"))
        duplicate = document.replace(
            '"schema_version":1', '"schema_version":1,"schema_version":1', 1
        )
        with self.assertRaisesRegex(WorkflowValidationError, "duplicate"):
            workflow_spec_from_json(duplicate)

        unknown = self.manual.as_data()
        unknown["handler"] = "unsafe.module:call"
        with self.assertRaisesRegex(WorkflowValidationError, "unknown"):
            workflow_spec_from_data(unknown)

        nested_unknown = self.manual.as_data()
        nested_unknown["steps"][0]["result"]["format"] = "template"
        with self.assertRaisesRegex(WorkflowValidationError, "unknown"):
            workflow_spec_from_data(nested_unknown)

        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                bad = document.replace('"output_step":"record"', f'"x":{token},"output_step":"record"')
                with self.assertRaises(WorkflowValidationError):
                    workflow_spec_from_json(bad)

    def test_strict_json_rejects_encoding_unicode_and_version_drift(self) -> None:
        with self.assertRaisesRegex(WorkflowValidationError, "UTF-8"):
            workflow_spec_from_json(b"\xff")
        with self.assertRaisesRegex(WorkflowValidationError, "Unicode"):
            workflow_spec_from_json(
                json.dumps(self.manual.as_data()).replace(
                    '"authoring-parity"', '"\\ud800"'
                )
            )
        with self.assertRaises(WorkflowValidationError):
            workflow_spec_from_json("\ufeff{}")
        with self.assertRaisesRegex(WorkflowValidationError, "size"):
            workflow_spec_from_json(" " * (MAX_WORKFLOW_DEFINITION_BYTES + 1))

        for key, value in (("schema_version", 2), ("execution", "dag-v1")):
            with self.subTest(key=key):
                changed = self.manual.as_data()
                changed[key] = value
                with self.assertRaises(WorkflowValidationError):
                    workflow_spec_from_data(changed)

        missing = self.manual.as_data()
        del missing["output_step"]
        with self.assertRaisesRegex(WorkflowValidationError, "missing"):
            workflow_spec_from_data(missing)

    def test_parser_reuses_existing_reference_and_definition_limits(self) -> None:
        deeply_nested: object = None
        for _ in range(1500):
            deeply_nested = [deeply_nested]
        excessive_depth = self.manual.as_data()
        excessive_depth["unexpected"] = deeply_nested
        with self.assertRaisesRegex(WorkflowValidationError, "nesting depth"):
            workflow_spec_from_data(excessive_depth)

        forward = self.manual.as_data()
        forward["steps"][0]["arguments"]["summary"] = {
            "kind": "step_output",
            "step_id": "record",
        }
        with self.assertRaisesRegex(WorkflowValidationError, "earlier"):
            workflow_spec_from_data(forward)

        missing = self.manual.as_data()
        missing["output_step"] = "missing"
        with self.assertRaisesRegex(WorkflowValidationError, "output_step"):
            workflow_spec_from_data(missing)

        reserved = self.manual.as_data()
        reserved["steps"][1]["arguments"]["idempotency_key"] = {
            "kind": "literal",
            "value": "forged",
        }
        parsed = workflow_spec_from_data(reserved)
        store = SQLiteStore()
        self.addCleanup(store.close)
        with self.assertRaisesRegex(Exception, "arguments"):
            compile_workflow(
                parsed,
                Harness(_UnusedModel(), (self.inspect, self.record), store=store),
            )

    def test_builder_rejects_duplicates_invalid_output_and_post_build_mutation(self) -> None:
        builder = SerialWorkflowBuilder("builder-guards", version="1")
        reference = builder.input("value", value_type="string")
        with self.assertRaisesRegex(WorkflowValidationError, "duplicate"):
            builder.input("value", value_type="string")
        step = builder.step(
            "inspect",
            self.inspect,
            arguments={"summary": reference},
            result_type="string",
        )
        with self.assertRaisesRegex(WorkflowValidationError, "duplicate"):
            builder.step(
                "inspect",
                self.inspect,
                arguments={"summary": Literal("x")},
                result_type="string",
            )
        with self.assertRaisesRegex(WorkflowValidationError, "output"):
            builder.build(output=StepRef("missing"))

        spec = builder.build(output=step)
        self.assertEqual(spec.output_step, "inspect")
        for operation in (
            lambda: builder.input("later", value_type="string"),
            lambda: builder.step(
                "later",
                self.inspect,
                arguments={"summary": Literal("x")},
                result_type="string",
            ),
            lambda: builder.build(output="inspect"),
        ):
            with self.assertRaisesRegex(WorkflowValidationError, "built"):
                operation()


if __name__ == "__main__":
    unittest.main()
