from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (
    Harness,
    InjectedFault,
    ModelCallError,
    RunPaused,
    SQLiteStore,
    Tool,
)
from sasori_flow import (
    InputRef,
    InputSlot,
    StepRef,
    ToolStep,
    WorkflowSpec,
    compile_workflow,
)


class _UnusedModel:
    async def complete(self, messages, tools):
        raise AssertionError("the Workflow compiler must replace the base model")


def _base(store: SQLiteStore, tools: tuple[Tool, ...], fault=None) -> Harness:
    return Harness(
        _UnusedModel(),
        tools,
        store=store,
        fault_injector=fault,
        model_timeout=2,
        tool_timeout=2,
    )


class WorkflowPublicProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = SQLiteStore()
        self.addCleanup(self.store.close)
        self.actions: list[str] = []

        def inspect(summary: str) -> str:
            return f"secret-diagnostic:{summary}"

        def record(summary: str) -> str:
            self.actions.append(summary)
            return f"secret-record:{summary}"

        self.inspect = Tool("inspect", inspect, effect="read_only")
        self.record = Tool(
            "record", record, effect="side_effecting", tool_revision="1"
        )
        self.spec = WorkflowSpec(
            "public-projection",
            "1",
            (InputSlot("incident", "string"),),
            (
                ToolStep.from_tool(
                    "inspect",
                    self.inspect,
                    {"summary": InputRef("incident")},
                    result_type="string",
                ),
                ToolStep.from_tool(
                    "record",
                    self.record,
                    {"summary": StepRef("inspect")},
                    result_type="string",
                ),
            ),
            "record",
        )
        self.workflow = compile_workflow(
            self.spec, _base(self.store, (self.inspect, self.record))
        )

    async def test_public_projection_tracks_approval_resume_and_completion(self) -> None:
        with self.assertRaises(RunPaused) as paused:
            await self.workflow.run(
                {"incident": "private-incident"}, run_id="PublicProjection"
            )
        request = paused.exception.request
        assert request is not None

        before_snapshot = self.store.load("PublicProjection")
        before_events = self.store.events("PublicProjection")
        projected = self.workflow.public_run_projection("PublicProjection")
        after_snapshot = self.store.load("PublicProjection")
        self.assertEqual(after_snapshot.revision, before_snapshot.revision)
        self.assertEqual(after_snapshot.generation, before_snapshot.generation)
        self.assertEqual(self.store.events("PublicProjection"), before_events)
        self.assertEqual(projected["state"], "paused")
        public = projected["workflow"]
        self.assertEqual(public["schema_version"], 1)
        self.assertEqual(public["definition_sha256"], self.spec.digest)
        self.assertEqual(public["latest_seq"], projected["latest_seq"])
        self.assertEqual(public["current_step_id"], "record")
        self.assertEqual(
            [step["status"] for step in public["steps"]],
            ["completed", "approval_required"],
        )

        encoded = json.dumps(public, sort_keys=True, separators=(",", ":"))
        for forbidden in (
            "private-incident",
            "secret-diagnostic",
            "secret-record",
            "fingerprint",
            "idempotency_key",
            "provider_state",
        ):
            self.assertNotIn(forbidden, encoded)
        for step in public["steps"]:
            self.assertTrue(
                set(step).isdisjoint(
                    {"arguments", "output", "fingerprint", "idempotency_key"}
                )
            )

        self.workflow.resolve_approval(
            "PublicProjection", request.fingerprint, True
        )
        approved = self.workflow.public_run_projection("PublicProjection")
        self.assertEqual(self.actions, [])
        self.assertEqual(
            approved["workflow"]["steps"][1]["status"], "resume_required"
        )

        await self.workflow.resume("PublicProjection")
        completed = self.workflow.public_run_projection("PublicProjection")
        self.assertEqual(self.actions, ["secret-diagnostic:private-incident"])
        self.assertEqual(completed["state"], "completed")
        self.assertIsNone(completed["workflow"]["current_step_id"])
        self.assertEqual(
            [step["status"] for step in completed["workflow"]["steps"]],
            ["completed", "completed"],
        )
        events_before_reopen = self.store.events("PublicProjection")
        again = await self.workflow.resume("PublicProjection")
        self.assertEqual(again.final_message, self.store.load("PublicProjection").final_message)
        self.assertEqual(self.actions, ["secret-diagnostic:private-incident"])
        self.assertEqual(self.store.events("PublicProjection"), events_before_reopen)

    async def test_pending_requested_and_running_are_durable_step_states(self) -> None:
        def read(value: str) -> str:
            return value

        tool = Tool("read", read, effect="read_only")
        spec = WorkflowSpec(
            "projection-boundaries",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "first", tool, {"value": InputRef("value")}, result_type="string"
                ),
                ToolStep.from_tool(
                    "second", tool, {"value": StepRef("first")}, result_type="string"
                ),
            ),
            "second",
        )

        fired = False

        def after_reply(point: str) -> None:
            nonlocal fired
            if point == "after_model_reply_commit" and not fired:
                fired = True
                raise InjectedFault(point)

        requested = compile_workflow(spec, _base(self.store, (tool,), after_reply))
        with self.assertRaises(InjectedFault):
            await requested.run({"value": "x"}, run_id="RequestedBoundary")
        projection = requested.public_projection("RequestedBoundary")
        self.assertEqual(
            [step["status"] for step in projection["steps"]],
            ["requested", "pending"],
        )

        second_store = SQLiteStore()
        self.addCleanup(second_store.close)
        fired = False

        def after_dispatch(point: str) -> None:
            nonlocal fired
            if point == "after_tool_dispatch" and not fired:
                fired = True
                raise InjectedFault(point)

        running = compile_workflow(spec, _base(second_store, (tool,), after_dispatch))
        with self.assertRaises(InjectedFault):
            await running.run({"value": "x"}, run_id="RunningBoundary")
        projection = running.public_projection("RunningBoundary")
        self.assertEqual(
            [step["status"] for step in projection["steps"]],
            ["running", "pending"],
        )

    async def test_effect_unknown_failed_and_stopped_never_look_completed(self) -> None:
        fired = False

        def fault(point: str) -> None:
            nonlocal fired
            if point == "after_tool_return" and self.actions and not fired:
                fired = True
                raise InjectedFault(point)

        workflow = compile_workflow(
            self.spec, _base(self.store, (self.inspect, self.record), fault)
        )
        with self.assertRaises(RunPaused) as paused:
            await workflow.run({"incident": "ambiguous"}, run_id="PublicUnknown")
        request = paused.exception.request
        assert request is not None
        workflow.resolve_approval("PublicUnknown", request.fingerprint, True)
        with self.assertRaises(InjectedFault):
            await workflow.resume("PublicUnknown")
        with self.assertRaises(RunPaused):
            await workflow.resume("PublicUnknown")
        unknown = workflow.public_projection("PublicUnknown")
        self.assertEqual(unknown["steps"][1]["status"], "effect_unknown")
        self.assertNotEqual(unknown["steps"][1]["status"], "completed")

        def bad(value: str) -> int:
            return 7

        bad_tool = Tool("bad", bad, effect="read_only")
        never = Tool("never", lambda value: value, effect="read_only")
        failed_spec = WorkflowSpec(
            "projection-failure",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "bad", bad_tool, {"value": InputRef("value")}, result_type="string"
                ),
                ToolStep.from_tool(
                    "never", never, {"value": StepRef("bad")}, result_type="string"
                ),
            ),
            "never",
        )
        failed_store = SQLiteStore()
        self.addCleanup(failed_store.close)
        failed = compile_workflow(
            failed_spec, _base(failed_store, (bad_tool, never))
        )
        with self.assertRaises(ModelCallError):
            await failed.run({"value": "x"}, run_id="PublicFailed")
        projection = failed.public_projection("PublicFailed")
        self.assertEqual(
            [step["status"] for step in projection["steps"]],
            ["failed", "stopped"],
        )

    async def test_cancelled_read_only_inflight_and_downstream_steps_are_stopped(
        self,
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(value: str) -> str:
            started.set()
            await release.wait()
            return value

        slow_tool = Tool("slow", slow, effect="read_only")
        next_tool = Tool("next", lambda value: value, effect="read_only")
        spec = WorkflowSpec(
            "projection-cancelled",
            "1",
            (InputSlot("value", "string"),),
            (
                ToolStep.from_tool(
                    "slow",
                    slow_tool,
                    {"value": InputRef("value")},
                    result_type="string",
                ),
                ToolStep.from_tool(
                    "next",
                    next_tool,
                    {"value": StepRef("slow")},
                    result_type="string",
                ),
            ),
            "next",
        )
        store = SQLiteStore()
        self.addCleanup(store.close)
        workflow = compile_workflow(spec, _base(store, (slow_tool, next_tool)))
        task = asyncio.create_task(
            workflow.run({"value": "x"}, run_id="PublicCancelled")
        )
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        public_run = workflow.public_run_projection("PublicCancelled")
        projected = public_run["workflow"]
        self.assertEqual(public_run["state"], "cancelled")
        self.assertIsNone(public_run["pause_reason"])
        self.assertIsNone(public_run["pending"])
        self.assertEqual(
            [step["status"] for step in projected["steps"]],
            ["stopped", "stopped"],
        )
        self.assertIsNone(projected["current_step_id"])

    async def test_cancelled_mutable_inflight_preserves_effect_unknown(self) -> None:
        for effect in ("side_effecting", "idempotent"):
            with self.subTest(effect=effect):
                started = asyncio.Event()
                release = asyncio.Event()

                if effect == "idempotent":

                    async def slow(value: str, *, idempotency_key: str) -> str:
                        self.assertEqual(idempotency_key, f"slow:{value}")
                        started.set()
                        await release.wait()
                        return value

                    slow_tool = Tool(
                        f"slow_{effect}",
                        slow,
                        effect=effect,
                        idempotency_key=lambda arguments: f"slow:{arguments['value']}",
                        tool_revision="1",
                    )
                else:

                    async def slow(value: str) -> str:
                        started.set()
                        await release.wait()
                        return value

                    slow_tool = Tool(
                        f"slow_{effect}", slow, effect=effect, tool_revision="1"
                    )
                next_tool = Tool("next", lambda value: value, effect="read_only")
                spec = WorkflowSpec(
                    f"projection-cancelled-{effect}",
                    "1",
                    (InputSlot("value", "string"),),
                    (
                        ToolStep.from_tool(
                            "slow",
                            slow_tool,
                            {"value": InputRef("value")},
                            result_type="string",
                        ),
                        ToolStep.from_tool(
                            "next",
                            next_tool,
                            {"value": StepRef("slow")},
                            result_type="string",
                        ),
                    ),
                    "next",
                )
                store = SQLiteStore()
                self.addCleanup(store.close)
                workflow = compile_workflow(
                    spec, _base(store, (slow_tool, next_tool))
                )
                run_id = f"PublicCancelled{effect.title().replace('_', '')}"
                with self.assertRaises(RunPaused) as paused:
                    await workflow.run({"value": "x"}, run_id=run_id)
                request = paused.exception.request
                assert request is not None
                workflow.resolve_approval(run_id, request.fingerprint, True)
                task = asyncio.create_task(workflow.resume(run_id))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()

                public_run = workflow.public_run_projection(run_id)
                projected = public_run["workflow"]
                self.assertEqual(store.load(run_id).status, "cancelled")
                self.assertEqual(public_run["state"], "cancelled")
                self.assertEqual(public_run["pause_reason"], "effect_unknown")
                self.assertEqual(
                    public_run["pending"]["fingerprint"], request.fingerprint
                )
                self.assertEqual(
                    [step["status"] for step in projected["steps"]],
                    ["effect_unknown", "stopped"],
                )
                self.assertIsNotNone(projected["steps"][0]["call_id"])
                self.assertIsNone(projected["steps"][1]["call_id"])
                self.assertIsNone(projected["current_step_id"])
                with self.assertRaisesRegex(ValueError, "cancelled run cannot retry"):
                    workflow.resolve_effect(
                        run_id,
                        request.fingerprint,
                        "retry",
                        reason="retry must stay forbidden",
                    )
                workflow.resolve_effect(
                    run_id,
                    request.fingerprint,
                    "fail",
                    reason="operator could not verify the external outcome",
                )
                resolved = workflow.public_run_projection(run_id)
                self.assertEqual(resolved["state"], "cancelled")
                self.assertIsNone(resolved["pause_reason"])
                self.assertIsNone(resolved["pending"])
                self.assertEqual(
                    [step["status"] for step in resolved["workflow"]["steps"]],
                    ["failed", "stopped"],
                )


if __name__ == "__main__":
    unittest.main()
