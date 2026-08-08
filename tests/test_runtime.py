import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    Event,
    Harness,
    MaxStepsExceeded,
    Message,
    ModelReply,
    ModelTimeoutError,
    RunPaused,
    SQLiteStore,
    StoreError,
    Tool,
    ToolCall,
)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def complete(self, messages, tools):
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def _harness(self, *args, **kwargs):
        return self.enterContext(Harness(*args, **kwargs))

    async def test_harness_closes_only_its_owned_store(self):
        with Harness(ScriptedModel()) as owned:
            owned_store = owned.store
            self.assertFalse(owned_store.closed)
        self.assertTrue(owned_store.closed)
        owned.close()
        with self.assertRaisesRegex(StoreError, "^store is closed$"):
            await owned.run((Message("user", "closed"),))
        with self.assertRaisesRegex(StoreError, "^store is closed$"):
            await owned.resume("missing")

        borrowed_store = self.enterContext(SQLiteStore())
        with Harness(ScriptedModel(), store=borrowed_store) as borrowed:
            self.assertIs(borrowed.store, borrowed_store)
        borrowed.close()
        self.assertFalse(borrowed_store.closed)

        duplicate = Tool("duplicate", lambda: None, effect="read_only")
        with patch("sasori.runtime.SQLiteStore") as store_factory:
            with self.assertRaisesRegex(ValueError, "^duplicate tool name"):
                Harness(ScriptedModel(), (duplicate, duplicate))
        store_factory.assert_not_called()

    async def test_multi_turn_happy_path_has_stable_semantic_events(self):
        def after_add(messages):
            self.assertEqual(messages[-1].content, "5")
            return ModelReply(tool_calls=(ToolCall("c2", "double", {"value": 5}),))

        def final(messages):
            self.assertEqual(messages[-1].content, "10")
            return ModelReply(content="The answer is 10.")

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("c1", "add", {"left": 2, "right": 3}),)),
            after_add,
            final,
        )
        harness = self._harness(
            model,
            (
                Tool("add", lambda left, right: left + right, effect="read_only"),
                Tool("double", lambda value: value * 2, effect="read_only"),
            ),
        )

        result = await harness.run((Message("user", "Add and double."),), run_id="run-1")

        self.assertEqual(result.final_message.content, "The answer is 10.")
        self.assertEqual(result.steps, 3)
        self.assertEqual(
            [event.type for event in result.events],
            [
                "run.started",
                "model.started",
                "model.completed",
                "tool.requested",
                "tool.started",
                "tool.completed",
                "model.started",
                "model.completed",
                "tool.requested",
                "tool.started",
                "tool.completed",
                "model.started",
                "model.completed",
                "run.completed",
            ],
        )
        self.assertTrue(all(event.version == 1 for event in result.events))
        self.assertTrue(all(event.run_id == "run-1" for event in result.events))
        self.assertEqual(
            [(event.tool_name, event.call_id) for event in result.events if event.call_id],
            [("add", "c1")] * 3 + [("double", "c2")] * 3,
        )

    async def test_incomplete_and_structurally_invalid_calls_never_execute(self):
        calls = 0

        def guarded(value):
            nonlocal calls
            calls += 1
            return value

        def final(messages):
            errors = [message.error_code for message in messages if message.role == "tool"]
            self.assertEqual(errors, ["incomplete_tool_call", "malformed_arguments"])
            return ModelReply(content="Both unsafe calls were refused.")

        model = ScriptedModel(
            ModelReply(
                tool_calls=(
                    ToolCall("cut", "guarded", {"value": 1}, complete=False),
                    ToolCall("bad", "guarded", ["not", "a", "mapping"]),
                )
            ),
            final,
        )
        result = await self._harness(
            model, (Tool("guarded", guarded, effect="read_only"),)
        ).run(
            (Message("user", "Try unsafe calls."),)
        )

        self.assertEqual(calls, 0)
        self.assertEqual(result.final_message.content, "Both unsafe calls were refused.")
        self.assertNotIn("tool.started", [event.type for event in result.events])

    async def test_tool_failures_are_visible_and_run_can_continue(self):
        called = {"needs": 0, "boom": 0}

        def needs(value):
            called["needs"] += 1
            return value

        def boom():
            called["boom"] += 1
            raise RuntimeError("disk is read-only")

        def final(messages):
            errors = [message.error_code for message in messages if message.role == "tool"]
            self.assertEqual(
                errors, ["invalid_arguments", "unknown_tool", "tool_exception"]
            )
            return ModelReply(content="Recovered after three tool errors.")

        model = ScriptedModel(
            ModelReply(
                tool_calls=(
                    ToolCall("bad-args", "needs", {"wrong": 1}),
                    ToolCall("missing", "not_registered"),
                    ToolCall("raises", "boom"),
                )
            ),
            final,
        )
        result = await self._harness(
            model,
            (
                Tool("needs", needs, effect="read_only"),
                Tool("boom", boom, effect="read_only"),
            ),
        ).run((Message("user", "Exercise failures."),))

        self.assertEqual(called, {"needs": 0, "boom": 1})
        failed = [event for event in result.events if event.type == "tool.failed"]
        self.assertEqual(
            [event.data["error_code"] for event in failed],
            ["invalid_arguments", "unknown_tool", "tool_exception"],
        )
        self.assertEqual(result.events[-1].type, "run.completed")

    async def test_model_and_tool_timeouts_are_distinguishable(self):
        class SlowModel:
            async def complete(self, messages, tools):
                await asyncio.sleep(1)
                return ModelReply(content="late")

        model_events = []
        with self.assertRaises(ModelTimeoutError):
            await self._harness(
                SlowModel(), model_timeout=0.01, event_sink=model_events.append
            ).run((Message("user", "Wait."),))
        self.assertEqual(
            [event.data.get("error_code") for event in model_events if event.type.endswith("failed")],
            ["model_timeout", "model_timeout"],
        )

        async def slow_tool():
            await asyncio.sleep(1)

        def final(messages):
            self.assertEqual(messages[-1].error_code, "tool_timeout")
            return ModelReply(content="Tool timed out; run continued.")

        result = await self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("slow", "slow_tool"),)), final
            ),
            (Tool("slow_tool", slow_tool, effect="read_only"),),
            tool_timeout=0.01,
        ).run((Message("user", "Use the slow tool."),))
        self.assertEqual(
            [
                event.data["error_code"]
                for event in result.events
                if event.type == "tool.failed"
            ],
            ["tool_timeout"],
        )
        self.assertNotIn("model.failed", [event.type for event in result.events])

    async def test_timeout_stays_failed_when_async_code_swallows_cancellation(self):
        model_cancelled = asyncio.Event()
        release_model = asyncio.Event()
        model_finished = asyncio.Event()

        class DefiantModel:
            async def complete(self, messages, tools):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    model_cancelled.set()
                    await release_model.wait()
                    model_finished.set()
                    return ModelReply(content="too late")

        with self.assertRaises(ModelTimeoutError):
            await self._harness(DefiantModel(), model_timeout=0.01).run(
                (Message("user", "Respect the deadline."),)
            )
        await asyncio.wait_for(model_cancelled.wait(), 1)
        self.assertFalse(model_finished.is_set())
        release_model.set()
        await asyncio.wait_for(model_finished.wait(), 1)

        tool_cancelled = asyncio.Event()
        release_tool = asyncio.Event()
        tool_finished = asyncio.Event()

        async def defiant_tool():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                tool_cancelled.set()
                await release_tool.wait()
                tool_finished.set()
                return "too late"

        def final(messages):
            self.assertEqual(messages[-1].error_code, "tool_timeout")
            return ModelReply(content="The late tool result was discarded.")

        result = await self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("late", "defiant_tool"),)), final
            ),
            (Tool("defiant_tool", defiant_tool, effect="read_only"),),
            tool_timeout=0.01,
        ).run((Message("user", "Respect the tool deadline."),))
        self.assertEqual(result.final_message.content, "The late tool result was discarded.")
        await asyncio.wait_for(tool_cancelled.wait(), 1)
        self.assertFalse(tool_finished.is_set())
        release_tool.set()
        await asyncio.wait_for(tool_finished.wait(), 1)

    async def test_event_data_is_deeply_stable_and_json_like(self):
        source = {"nested": {"items": [1, 2]}}
        event = Event("test", "run", 0, source)
        source["nested"]["items"].append(3)

        self.assertEqual(event.data["nested"]["items"], (1, 2))
        with self.assertRaises(TypeError):
            Event("test", "run", 0, {"unsafe": object()})

    async def test_caller_cancellation_propagates_while_sync_handler_may_continue(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        events = []

        def blocking_tool():
            started.set()
            release.wait(1)
            finished.set()

        harness = self._harness(
            ScriptedModel(ModelReply(tool_calls=(ToolCall("block", "blocking"),))),
            (Tool("blocking", blocking_tool, effect="read_only"),),
            event_sink=events.append,
        )
        task = asyncio.create_task(harness.run((Message("user", "Block."),)))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(events[-1].type, "run.cancelled")
            self.assertNotIn("run.failed", [event.type for event in events])
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_max_steps_raises_explicit_exception(self):
        class RepeatingModel:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                return ModelReply(
                    tool_calls=(ToolCall(f"c{self.calls}", "echo", {"value": 1}),)
                )

        model = RepeatingModel()
        events = []
        with self.assertRaises(MaxStepsExceeded):
            await self._harness(
                model,
                (Tool("echo", lambda value: value, effect="read_only"),),
                max_steps=2,
                event_sink=events.append,
            ).run((Message("user", "Never finish."),))

        self.assertEqual(model.calls, 2)
        self.assertEqual(events[-1].type, "run.failed")
        self.assertEqual(events[-1].data["error_code"], "max_steps_exceeded")

    async def test_deterministic_incident_triage(self):
        actions = []

        def read_incident(incident_id):
            return {"id": incident_id, "service": "checkout", "symptom": "latency"}

        def read_metric(service):
            return {"service": service, "p95_ms": 2400}

        def restart_service(service):
            actions.append(("restart", service))
            return {"action": "restart", "service": service, "status": "queued"}

        class IncidentModel:
            async def complete(self, messages, tools):
                results = [message for message in messages if message.role == "tool"]
                if not results:
                    return ModelReply(
                        tool_calls=(
                            ToolCall("incident", "read_incident", {"incident_id": "INC-7"}),
                        )
                    )
                if len(results) == 1:
                    incident = json.loads(results[-1].content)
                    return ModelReply(
                        tool_calls=(
                            ToolCall("metric", "read_metric", {"service": incident["service"]}),
                        )
                    )
                if len(results) == 2:
                    metric = json.loads(results[-1].content)
                    self.metric = metric
                    return ModelReply(
                        tool_calls=(
                            ToolCall("restart", "restart_service", {"service": metric["service"]}),
                        )
                    )
                action = json.loads(results[-1].content)
                return ModelReply(
                    content=(
                        f"INC-7: checkout p95 is {self.metric['p95_ms']} ms; "
                        f"restart {action['status']}."
                    )
                )

        harness = self._harness(
            IncidentModel(),
            (
                Tool("read_incident", read_incident, effect="read_only"),
                Tool("read_metric", read_metric, effect="read_only"),
                Tool("restart_service", restart_service, tool_revision="1"),
            ),
        )
        with self.assertRaises(RunPaused) as paused:
            await harness.run((Message("user", "Triage INC-7."),), run_id="incident-run")
        harness.resolve_approval(
            "incident-run", paused.exception.request.fingerprint, True
        )
        result = await harness.resume("incident-run")

        self.assertEqual(actions, [("restart", "checkout")])
        self.assertEqual(
            result.final_message.content,
            "INC-7: checkout p95 is 2400 ms; restart queued.",
        )
        self.assertEqual(
            [event.tool_name for event in result.events if event.type == "tool.requested"],
            ["read_incident", "read_metric", "restart_service"],
        )
        self.assertEqual(result.events[-1].type, "run.completed")
