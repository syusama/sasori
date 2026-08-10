import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, ModelReply, RunPaused, Tool, ToolCall  # noqa: E402
from sasori.projection import (  # noqa: E402
    ProjectionIntegrityError,
    compose_run_projection,
    event_projection,
    run_projection,
    validate_run_id,
)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        return self.replies.pop(0)


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    def _harness(self, *args, **kwargs):
        return self.enterContext(Harness(*args, **kwargs))

    async def test_projection_is_shared_json_safe_and_hides_provider_state(self):
        state = '{"provider":"test","version":1}'
        harness = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(ToolCall("call-1", "write", {"value": 3}),),
                    provider_state=state,
                ),
                ModelReply(content="done", provider_state=state),
            ),
            (Tool("write", lambda value: value, tool_revision="1"),),
        )
        with self.assertRaises(RunPaused):
            await harness.run((Message("user", "write"),), run_id="projection-1")

        projected = run_projection(harness.store, "projection-1")
        self.assertEqual(projected["state"], "paused")
        self.assertEqual(projected["pause_reason"], "approval_required")
        self.assertEqual(projected["pending"]["arguments"], {"value": 3})
        self.assertNotIn(state, json.dumps(projected))
        harness.resolve_approval(
            "projection-1", projected["pending"]["fingerprint"], True
        )
        await harness.resume("projection-1")
        completed = run_projection(harness.store, "projection-1")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["final_message"]["content"], "done")
        self.assertNotIn(state, json.dumps(completed))
        events = [event_projection(item) for item in harness.stored_events("projection-1")]
        self.assertEqual([item["seq"] for item in events], list(range(1, len(events) + 1)))
        json.dumps(events)

    def test_run_id_validation(self):
        self.assertEqual(validate_run_id("run-1.ok"), "run-1.ok")
        for invalid in ("", "-bad", "space bad", "a" * 65, None):
            with self.assertRaises(ValueError):
                validate_run_id(invalid)

    async def test_composer_ignores_legacy_full_projection_override(self):
        harness = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("projection-read", "read", {}),)),
                ModelReply(content="done"),
            ),
            (Tool("read", lambda: "ok", effect="read_only"),),
        )
        await harness.run((Message("user", "hello"),), run_id="projection-owned")

        class LegacyOverride:
            def public_run_projection(self, run_id):
                raise AssertionError("legacy full projection hook must be ignored")

        projected = compose_run_projection(
            harness.store, "projection-owned", LegacyOverride()
        )
        self.assertEqual(projected, run_projection(harness.store, "projection-owned"))

    async def test_composer_rejects_malformed_or_unbound_extensions(self):
        harness = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall("projection-call-1", "wf_projection_inspect", {}),
                    )
                ),
                ModelReply(content="done"),
            ),
            (
                Tool(
                    "wf_projection_inspect",
                    lambda: "ok",
                    effect="read_only",
                ),
            ),
        )
        await harness.run(
            (Message("user", "hello"),),
            run_id="projection-extension",
            app_id="projection.app",
        )

        private = "private projection failure detail"
        core = run_projection(harness.store, "projection-extension")
        valid = {
            "workflow": {
                "schema_version": 1,
                "workflow_id": "projection-extension",
                "version": "1",
                "definition_sha256": "0" * 64,
                "app_id": "projection.app",
                "execution": "single-harness-ordered-tools-v1",
                "output_step": "only-step",
                "current_step_id": None,
                "latest_seq": core["latest_seq"],
                "steps": [
                    {
                        "position": 1,
                        "step_id": "only-step",
                        "kind": "tool",
                        "logical_tool_name": "inspect",
                        "dispatch_tool_name": "wf_projection_inspect",
                        "effect": "read_only",
                        "logical_tool_revision": None,
                        "dispatch_tool_revision": None,
                        "logical_schema_sha256": "1" * 64,
                        "dispatch_schema_sha256": "2" * 64,
                        "result_type": "string",
                        "max_result_bytes": 1024,
                        "call_id": "projection-call-1",
                        "status": "completed",
                        "error_code": None,
                    }
                ],
            }
        }

        class Malformed:
            def __init__(self, value):
                self.value = value

            def public_projection_extension(self, run_id):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        invalid = (
            {"run_id": "forged"},
            {"workflow": []},
            {"workflow": {"schema_version": 2, "app_id": "projection.app", "latest_seq": 3}},
            {"workflow": {"schema_version": 1, "app_id": "other.app", "latest_seq": 3}},
            {"workflow": {"schema_version": 1, "app_id": "projection.app", "latest_seq": -1}},
            RuntimeError(private),
        )
        extra = copy.deepcopy(valid)
        extra["workflow"]["provider_state"] = private
        invalid += (extra,)
        missing = copy.deepcopy(valid)
        del missing["workflow"]["steps"]
        invalid += (missing,)
        nested_extra = copy.deepcopy(valid)
        nested_extra["workflow"]["steps"][0]["arguments"] = {"private": private}
        invalid += (nested_extra,)
        nested_missing = copy.deepcopy(valid)
        del nested_missing["workflow"]["steps"][0]["status"]
        invalid += (nested_missing,)
        bad_binding = copy.deepcopy(valid)
        bad_binding["workflow"]["steps"][0]["call_id"] = None
        invalid += (bad_binding,)
        contradictory = copy.deepcopy(valid)
        contradictory["workflow"]["steps"][0]["status"] = "failed"
        contradictory["workflow"]["steps"][0]["error_code"] = "forged_failure"
        invalid += (contradictory,)
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProjectionIntegrityError) as raised:
                    compose_run_projection(
                        harness.store, "projection-extension", Malformed(value)
                    )
                self.assertEqual(
                    str(raised.exception),
                    "public projection extension failed integrity validation",
                )
                self.assertNotIn(private, str(raised.exception))

        projected = compose_run_projection(
            harness.store, "projection-extension", Malformed(valid)
        )
        self.assertEqual(projected["workflow"], valid["workflow"])

        unbound = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("unbound-read", "read", {}),)),
                ModelReply(content="done"),
            ),
            (Tool("read", lambda: "ok", effect="read_only"),),
        )
        await unbound.run((Message("user", "hello"),), run_id="projection-unbound")
        unbound_extension = copy.deepcopy(valid)
        unbound_extension["workflow"]["app_id"] = None
        unbound_extension["workflow"]["latest_seq"] = run_projection(
            unbound.store, "projection-unbound"
        )["latest_seq"]
        with self.assertRaises(ProjectionIntegrityError):
            compose_run_projection(
                unbound.store, "projection-unbound", Malformed(unbound_extension)
            )

    async def test_composer_binds_running_workflow_to_the_durable_core_step(self):
        started = asyncio.Event()

        class BlockingModel:
            async def complete(self, messages, tools):
                started.set()
                await asyncio.Event().wait()

        harness = self._harness(BlockingModel())
        task = asyncio.create_task(
            harness.run(
                (Message("user", "hello"),),
                run_id="projection-running-step",
                app_id="projection.app",
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        core = run_projection(harness.store, "projection-running-step")
        self.assertEqual(
            (core["state"], core["detail"], core["step"]),
            ("running", "ready_model", 0),
        )

        extension = {
            "workflow": {
                "schema_version": 1,
                "workflow_id": "projection-running",
                "version": "1",
                "definition_sha256": "0" * 64,
                "app_id": "projection.app",
                "execution": "single-harness-ordered-tools-v1",
                "output_step": "only-step",
                "current_step_id": "only-step",
                "latest_seq": core["latest_seq"],
                "steps": [
                    {
                        "position": 1,
                        "step_id": "only-step",
                        "kind": "tool",
                        "logical_tool_name": "inspect",
                        "dispatch_tool_name": "wf_projection_inspect",
                        "effect": "read_only",
                        "logical_tool_revision": None,
                        "dispatch_tool_revision": None,
                        "logical_schema_sha256": "1" * 64,
                        "dispatch_schema_sha256": "2" * 64,
                        "result_type": "string",
                        "max_result_bytes": 1024,
                        "call_id": None,
                        "status": "pending",
                        "error_code": None,
                    }
                ],
            }
        }

        class Extension:
            def __init__(self, value):
                self.value = value

            def public_projection_extension(self, run_id):
                return self.value

        projected = compose_run_projection(
            harness.store, "projection-running-step", Extension(extension)
        )
        self.assertEqual(projected["workflow"], extension["workflow"])
        shifted = copy.deepcopy(extension)
        shifted["workflow"]["steps"][0]["call_id"] = "forged-call"
        shifted["workflow"]["steps"][0]["status"] = "running"
        with self.assertRaises(ProjectionIntegrityError):
            compose_run_projection(
                harness.store, "projection-running-step", Extension(shifted)
            )

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
