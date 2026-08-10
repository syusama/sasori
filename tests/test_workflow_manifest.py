from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, SQLiteStore, Tool  # noqa: E402
from sasori_flow import (  # noqa: E402
    InputRef,
    InputSlot,
    Literal,
    SerialWorkflowBuilder,
    StepRef,
    ToolStep,
    WorkflowCompileError,
    WorkflowSpec,
    WorkflowValidationError,
    canonical_json,
    compile_workflow,
    preflight_workflow,
    workflow_spec_from_data,
    workflow_spec_from_json,
)


class _UnusedModel:
    async def complete(self, messages, tools):
        raise AssertionError("manifest inspection must not execute the model")


class WorkflowManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler_calls = 0

        def inspect(summary: str) -> str:
            self.handler_calls += 1
            raise AssertionError("manifest inspection must not execute a Tool")

        def record(summary: str) -> str:
            self.handler_calls += 1
            raise AssertionError("manifest inspection must not execute a Tool")

        self.inspect = Tool("inspect", inspect, effect="read_only")
        self.record = Tool(
            "record", record, effect="side_effecting", tool_revision="1"
        )
        self.spec = WorkflowSpec(
            "manifest-parity",
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

    def _builder_spec(self) -> WorkflowSpec:
        builder = SerialWorkflowBuilder("manifest-parity", version="1")
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
        return builder.build(output=record)

    def test_authoring_paths_preflight_and_compiled_harness_share_one_manifest(
        self,
    ) -> None:
        definitions = (
            self.spec,
            workflow_spec_from_data(self.spec.as_data()),
            workflow_spec_from_json(
                json.dumps(self.spec.as_data(), ensure_ascii=False).encode("utf-8")
            ),
            self._builder_spec(),
        )
        manifests = [
            preflight_workflow(candidate, (self.inspect, self.record))
            for candidate in definitions
        ]
        self.assertTrue(
            all(canonical_json(item) == canonical_json(manifests[0]) for item in manifests)
        )

        store = SQLiteStore()
        self.addCleanup(store.close)
        workflow = compile_workflow(
            self.spec,
            Harness(_UnusedModel(), (self.inspect, self.record), store=store),
        )
        self.assertEqual(workflow.definition_manifest(), manifests[0])
        first = workflow.definition_manifest()
        first["steps"][0]["step_id"] = "tampered"
        self.assertEqual(workflow.definition_manifest(), manifests[0])
        self.assertEqual(self.handler_calls, 0)

    def test_manifest_schema_dependencies_and_effect_policy_are_exact(self) -> None:
        manifest = preflight_workflow(self.spec, (self.inspect, self.record))
        self.assertEqual(
            set(manifest),
            {
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
            },
        )
        self.assertEqual(
            manifest["trust"],
            {"execution_mode": "trusted_installed_python", "sandboxed": False},
        )
        self.assertEqual(
            manifest["inputs"],
            [
                {
                    "key": "incident",
                    "type": "string",
                    "required": True,
                    "max_bytes": 16 * 1024,
                }
            ],
        )
        self.assertEqual(
            [step["depends_on"] for step in manifest["steps"]],
            [[], ["inspect"]],
        )
        self.assertEqual(
            [step["argument_sources"] for step in manifest["steps"]],
            [
                [{"name": "summary", "kind": "input", "ref": "incident"}],
                [{"name": "summary", "kind": "step", "ref": "inspect"}],
            ],
        )
        self.assertEqual(
            [step["requires_approval"] for step in manifest["steps"]],
            [False, True],
        )
        self.assertEqual(
            [step["recovery_policy"] for step in manifest["steps"]],
            [
                "read_only_replay_allowed",
                "manual_effect_resolution_on_ambiguity",
            ],
        )
        self.assertEqual(
            set(manifest["steps"][0]),
            {
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
            },
        )

    def test_literal_is_redacted_to_type_size_and_digest(self) -> None:
        secret = "literal-secret-do-not-project"
        step = ToolStep.from_tool(
            "literal",
            self.inspect,
            {"summary": Literal(secret)},
            result_type="string",
        )
        spec = WorkflowSpec("literal-manifest", "1", (), (step,), "literal")
        manifest = preflight_workflow(spec, (self.inspect,))
        source = manifest["steps"][0]["argument_sources"][0]
        self.assertEqual(
            source,
            {
                "name": "summary",
                "kind": "literal",
                "value_type": "string",
                "canonical_bytes": len(canonical_json(secret).encode("utf-8")),
                "value_sha256": hashlib.sha256(
                    canonical_json(secret).encode("utf-8")
                ).hexdigest(),
            },
        )
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("call_id", encoded)
        self.assertNotIn("fingerprint", encoded)
        self.assertNotIn("idempotency_key", encoded)

    def test_all_effects_have_fixed_static_recovery_labels(self) -> None:
        def echo(value: str) -> str:
            return value

        def idempotent_echo(value: str, *, idempotency_key: str) -> str:
            return value

        read = Tool(
            "read",
            echo,
            effect="read_only",
            tool_revision="read-contract-v1",
        )
        idem = Tool(
            "idem",
            idempotent_echo,
            effect="idempotent",
            tool_revision="2",
            idempotency_key=lambda arguments: str(arguments["value"]),
        )
        write = Tool("write", echo, effect="side_effecting", tool_revision="3")
        spec = WorkflowSpec(
            "effect-manifest",
            "1",
            (InputSlot("value", "string"),),
            tuple(
                ToolStep.from_tool(
                    name,
                    tool,
                    {"value": InputRef("value")},
                    result_type="string",
                )
                for name, tool in (("read", read), ("idem", idem), ("write", write))
            ),
            "write",
        )
        manifest = preflight_workflow(spec, (read, idem, write))
        self.assertEqual(
            [
                (step["effect"], step["requires_approval"], step["recovery_policy"])
                for step in manifest["steps"]
            ],
            [
                ("read_only", False, "read_only_replay_allowed"),
                ("idempotent", True, "same_verified_business_key_only"),
                (
                    "side_effecting",
                    True,
                    "manual_effect_resolution_on_ambiguity",
                ),
            ],
        )
        self.assertEqual(
            (
                manifest["steps"][0]["logical_tool_revision"],
                manifest["steps"][0]["dispatch_tool_revision"],
            ),
            ("read-contract-v1", None),
        )
        self.assertTrue(
            all(
                isinstance(step["dispatch_tool_revision"], str)
                and step["dispatch_tool_revision"]
                for step in manifest["steps"][1:]
            )
        )

    def test_preflight_reuses_compile_drift_checks_without_store_or_execution(self) -> None:
        changed = Tool("record", lambda summary: summary, effect="read_only")
        with patch("sasori.runtime.SQLiteStore") as store_factory:
            with self.assertRaisesRegex(WorkflowCompileError, "effect"):
                preflight_workflow(self.spec, (self.inspect, changed))
        store_factory.assert_not_called()
        self.assertEqual(self.handler_calls, 0)

        duplicate = Tool("inspect", lambda summary: summary, effect="read_only")
        with patch("sasori.runtime.SQLiteStore") as store_factory:
            with self.assertRaisesRegex(WorkflowCompileError, "duplicate"):
                preflight_workflow(
                    self.spec,
                    (self.inspect, duplicate, self.record),
                )
        store_factory.assert_not_called()
        self.assertEqual(self.handler_calls, 0)

    def test_manifest_limit_fails_closed_without_truncation(self) -> None:
        with patch("sasori_flow.manifest.MAX_WORKFLOW_MANIFEST_BYTES", 32):
            with self.assertRaisesRegex(WorkflowValidationError, "manifest"):
                preflight_workflow(self.spec, (self.inspect, self.record))
        self.assertEqual(self.handler_calls, 0)


if __name__ == "__main__":
    unittest.main()
