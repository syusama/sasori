from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sasori import SQLiteStore  # noqa: E402
from sasori_core import (  # noqa: E402
    ApprovalConflict,
    CallRecord,
    ConcurrentRunError,
    EphemeralRunStore,
    Event,
    Harness,
    InjectedFault,
    Message,
    ModelReply,
    ModelStreamEvent,
    RunPaused,
    StoreError,
    Tool,
    ToolCall,
    ToolExecutionContext,
    run_projection,
)
from sasori_core.store import DuplicateCallIdError  # noqa: E402
from sasori_core.testing import ScriptedModel, ScriptedStreamingModel  # noqa: E402


class CoreStoreConformanceTests(unittest.IsolatedAsyncioTestCase):
    def _stores(self):
        return (EphemeralRunStore(), SQLiteStore())

    @staticmethod
    def _raise_once_at(point: str):
        fired = False

        def inject(current: str) -> None:
            nonlocal fired
            if current == point and not fired:
                fired = True
                raise InjectedFault(point)

        return inject

    async def test_same_runtime_and_semantic_trace_for_ephemeral_and_sqlite(self):
        traces = []
        for store in self._stores():
            self.addCleanup(store.close)
            model = ScriptedModel(
                ModelReply(
                    tool_calls=(ToolCall("call-1", "double", {"value": 4}),)
                ),
                ModelReply(content="8"),
            )
            harness = Harness(
                model,
                (Tool("double", lambda value: value * 2, effect="read_only"),),
                store=store,
            )
            result = await harness.run((Message("user", "double four"),), run_id="same")
            model.assert_consumed()
            traces.append(
                [
                    (
                        event.type,
                        event.version,
                        event.step,
                        event.tool_name,
                        event.call_id,
                        dict(event.data),
                    )
                    for event in result.events
                ]
            )
            projection = run_projection(store, "same")
            self.assertEqual(projection["state"], "completed")
            self.assertEqual(projection["final_message"], {"role": "assistant", "content": "8"})
        self.assertEqual(traces[0], traces[1])

    async def test_stream_observer_isolation_matches_ephemeral_and_sqlite(self):
        outcomes = []
        for store in self._stores():
            self.addCleanup(store.close)
            handler_seen: list[tuple[int, list[int]]] = []
            mutation_failures: list[type[BaseException]] = []

            def observe(event):
                if event.type != "done" or not event.reply.tool_calls:
                    return
                arguments = event.reply.tool_calls[0].arguments
                try:
                    arguments["payload"].__setitem__("amount", 99)
                except (AttributeError, TypeError) as error:
                    mutation_failures.append(type(error))
                try:
                    arguments["items"].__setitem__(0, 99)
                except (AttributeError, TypeError) as error:
                    mutation_failures.append(type(error))
                raise RuntimeError("observer offline after attempted mutation")

            def inspect(payload, items):
                handler_seen.append((payload["amount"], list(items)))
                return "safe"

            model = ScriptedStreamingModel(
                (
                    ModelStreamEvent("start"),
                    ModelStreamEvent(
                        "done",
                        reply=ModelReply(
                            tool_calls=(
                                ToolCall(
                                    "inspect-1",
                                    "inspect",
                                    {"payload": {"amount": 1}, "items": [2, 3]},
                                ),
                            )
                        ),
                    ),
                ),
                (
                    ModelStreamEvent("start"),
                    ModelStreamEvent("done", reply=ModelReply(content="finished")),
                ),
            )
            harness = Harness(
                model,
                (Tool("inspect", inspect, effect="read_only"),),
                store=store,
                model_stream_sink=observe,
            )
            result = await harness.run(
                (Message("user", "inspect"),), run_id="stream-observer-store"
            )
            call = store.calls("stream-observer-store", 1)[0]
            trace = tuple(
                (
                    item.event.type,
                    item.event.version,
                    item.event.step,
                    item.event.tool_name,
                    item.event.call_id,
                    dict(item.event.data),
                )
                for item in store.stored_events("stream-observer-store")
            )
            outcomes.append(
                (
                    handler_seen,
                    mutation_failures,
                    call.fingerprint,
                    dict(call.arguments["payload"]),
                    list(call.arguments["items"]),
                    trace,
                    result.final_message.content,
                )
            )
            model.assert_consumed()

        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], [(1, [2, 3])])
        self.assertEqual(len(outcomes[0][1]), 2)
        self.assertEqual(outcomes[0][3], {"amount": 1})
        self.assertEqual(outcomes[0][4], [2, 3])
        self.assertEqual(outcomes[0][6], "finished")

    async def test_tool_progress_is_transient_across_store_adapters(self):
        outcomes = []
        for index, store in enumerate(self._stores()):
            self.addCleanup(store.close)
            observed = []

            async def inspect(
                value: int, *, tool_context: ToolExecutionContext
            ) -> int:
                self.assertTrue(tool_context.report_progress({"phase": "one"}))
                self.assertTrue(tool_context.report_progress({"phase": "two"}))
                return value * 2

            model = ScriptedModel(
                ModelReply(
                    tool_calls=(ToolCall("progress-1", "inspect", {"value": 4}),)
                ),
                ModelReply(content="8"),
            )
            harness = Harness(
                model,
                (Tool("inspect", inspect, effect="read_only"),),
                store=store,
                tool_progress_sink=observed.append,
            )
            run_id = f"progress-store-{index}"
            result = await harness.run(
                (Message("user", "inspect"),), run_id=run_id
            )
            outcomes.append(
                (
                    [
                        (event.sequence, dict(event.data))
                        for event in observed
                    ],
                    [item.event.type for item in store.stored_events(run_id)],
                    result.final_message.content,
                )
            )
            self.assertFalse(
                any(
                    item.event.type == "tool.progress"
                    for item in store.stored_events(run_id)
                )
            )
            model.assert_consumed()

        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(
            outcomes[0][0],
            [(1, {"phase": "one"}), (2, {"phase": "two"})],
        )

    async def test_approval_and_resume_match_across_store_adapters(self):
        projections = []
        for index, store in enumerate(self._stores()):
            self.addCleanup(store.close)
            model = ScriptedModel(
                ModelReply(tool_calls=(ToolCall("mutate-1", "record", {"value": 7}),)),
                ModelReply(content="recorded"),
            )
            harness = Harness(
                model,
                (
                    Tool(
                        "record",
                        lambda value: value,
                        effect="side_effecting",
                        tool_revision="record-v1",
                    ),
                ),
                store=store,
            )
            run_id = f"approval-{index}"
            with self.assertRaises(RunPaused) as paused:
                await harness.run((Message("user", "record seven"),), run_id=run_id)
            request = paused.exception.request
            self.assertIsNotNone(request)
            harness.resolve_approval(run_id, request.fingerprint, True)
            resolved_events = store.stored_events(run_id)
            harness.resolve_approval(run_id, request.fingerprint, True)
            self.assertEqual(store.stored_events(run_id), resolved_events)
            with self.assertRaises(ApprovalConflict):
                harness.resolve_approval(run_id, request.fingerprint, False)
            self.assertEqual(store.stored_events(run_id), resolved_events)
            result = await harness.resume(run_id)
            self.assertEqual(result.final_message.content, "recorded")
            projection = run_projection(store, run_id)
            projection["run_id"] = "approval"
            projections.append(projection)
        self.assertEqual(projections[0], projections[1])

    async def test_core_default_is_non_durable_ephemeral_state(self):
        model = ScriptedModel(ModelReply(content="small core"))
        harness = Harness(model)
        self.addCleanup(harness.close)
        self.assertIsInstance(harness.store, EphemeralRunStore)
        result = await harness.run((Message("user", "hello"),), run_id="ephemeral")
        self.assertEqual(result.final_message.content, "small core")

    async def test_revision_call_identity_and_event_cursor_match_across_adapters(self):
        for index, store in enumerate(self._stores()):
            with self.subTest(adapter=type(store).__name__):
                self.addCleanup(store.close)
                run_id = f"store-contract-{index}"
                started = Event("run.started", run_id, 0, {"message_count": 1})
                initial = store.start(run_id, (Message("user", "hello"),), started)
                checkpoint = Event("run.checkpointed", run_id, 0)
                current = store.transition(initial, initial, (checkpoint,))

                with self.assertRaises(ConcurrentRunError):
                    store.transition(
                        initial,
                        replace(initial, status="failed"),
                        (Event("run.failed", run_id, 0),),
                    )
                self.assertEqual(store.load(run_id), current)
                self.assertEqual(
                    [(item.seq, item.event.type) for item in store.stored_events(run_id)],
                    [(1, "run.started"), (2, "run.checkpointed")],
                )
                self.assertEqual(
                    [(item.seq, item.event.type) for item in store.stored_events(run_id, 1)],
                    [(2, "run.checkpointed")],
                )
                self.assertEqual(store.stored_events(run_id, 2), ())
                with self.assertRaises(ValueError):
                    store.stored_events(run_id, -1)

                first_call = CallRecord(
                    run_id,
                    1,
                    0,
                    "provider-call",
                    "fingerprint-1",
                    "inspect",
                    {},
                    True,
                    True,
                    "read_only",
                    None,
                    "read-only-unversioned",
                )
                reply = ModelReply(
                    tool_calls=(ToolCall("provider-call", "inspect", {}),)
                )
                accepted = store.accept_reply(
                    current,
                    replace(current, status="processing_reply", step=1),
                    reply,
                    (first_call,),
                    (Event("model.completed", run_id, 1),),
                )
                duplicate = replace(
                    first_call,
                    step=2,
                    fingerprint="fingerprint-2",
                )
                before_events = store.stored_events(run_id)
                with self.assertRaises(DuplicateCallIdError):
                    store.accept_reply(
                        accepted,
                        replace(accepted, status="processing_reply", step=2),
                        reply,
                        (duplicate,),
                        (Event("model.completed", run_id, 2),),
                    )
                self.assertEqual(store.load(run_id), accepted)
                self.assertEqual(store.stored_events(run_id), before_events)
                self.assertEqual(store.calls(run_id, 2), ())

    async def test_crash_boundary_recovery_matches_across_adapters(self):
        for index, store in enumerate(self._stores()):
            with self.subTest(adapter=type(store).__name__, effect="idempotent"):
                self.addCleanup(store.close)
                invocations: list[tuple[int, str]] = []

                def write(value: int, *, idempotency_key: str) -> int:
                    invocations.append((value, idempotency_key))
                    return value

                harness = Harness(
                    ScriptedModel(
                        ModelReply(
                            tool_calls=(ToolCall("write-1", "write", {"value": 7}),)
                        ),
                        ModelReply(content="written"),
                    ),
                    (
                        Tool(
                            "write",
                            write,
                            effect="idempotent",
                            idempotency_key=lambda arguments: f"write:{arguments['value']}",
                            tool_revision="write-v1",
                        ),
                    ),
                    store=store,
                    fault_injector=self._raise_once_at("after_tool_dispatch"),
                )
                run_id = f"idempotent-recovery-{index}"
                with self.assertRaises(RunPaused) as paused:
                    await harness.run((Message("user", "write"),), run_id=run_id)
                request = paused.exception.request
                self.assertIsNotNone(request)
                harness.resolve_approval(run_id, request.fingerprint, True)
                with self.assertRaises(InjectedFault):
                    await harness.resume(run_id)
                self.assertEqual(invocations, [])
                result = await harness.resume(run_id)
                self.assertEqual(result.final_message.content, "written")
                self.assertEqual(invocations, [(7, "write:7")])
                self.assertEqual(store.calls(run_id, 1)[0].status, "result")

        for index, store in enumerate(self._stores()):
            with self.subTest(adapter=type(store).__name__, effect="side_effecting"):
                self.addCleanup(store.close)
                invocations: list[int] = []
                harness = Harness(
                    ScriptedModel(
                        ModelReply(
                            tool_calls=(ToolCall("effect-1", "effect", {"value": 9}),)
                        )
                    ),
                    (
                        Tool(
                            "effect",
                            lambda value: invocations.append(value),
                            effect="side_effecting",
                            tool_revision="effect-v1",
                        ),
                    ),
                    store=store,
                    fault_injector=self._raise_once_at("after_tool_dispatch"),
                )
                run_id = f"effect-recovery-{index}"
                with self.assertRaises(RunPaused) as paused:
                    await harness.run((Message("user", "effect"),), run_id=run_id)
                request = paused.exception.request
                self.assertIsNotNone(request)
                harness.resolve_approval(run_id, request.fingerprint, True)
                with self.assertRaises(InjectedFault):
                    await harness.resume(run_id)
                with self.assertRaises(RunPaused) as unknown:
                    await harness.resume(run_id)
                self.assertEqual(unknown.exception.reason, "effect_unknown")
                self.assertEqual(invocations, [])
                self.assertEqual(store.load(run_id).status, "effect_unknown")
                self.assertEqual(store.calls(run_id, 1)[0].status, "effect_unknown")

    async def test_cancellation_and_close_ownership_match_across_adapters(self):
        for index, store in enumerate(self._stores()):
            with self.subTest(adapter=type(store).__name__):
                started = asyncio.Event()

                async def mutate(value: int) -> int:
                    started.set()
                    await asyncio.Event().wait()
                    return value

                harness = Harness(
                    ScriptedModel(
                        ModelReply(
                            tool_calls=(ToolCall("cancel-1", "mutate", {"value": 1}),)
                        )
                    ),
                    (
                        Tool(
                            "mutate",
                            mutate,
                            effect="side_effecting",
                            tool_revision="mutate-v1",
                        ),
                    ),
                    store=store,
                )
                run_id = f"cancel-store-{index}"
                with self.assertRaises(RunPaused) as paused:
                    await harness.run((Message("user", "mutate"),), run_id=run_id)
                request = paused.exception.request
                self.assertIsNotNone(request)
                harness.resolve_approval(run_id, request.fingerprint, True)
                drive = asyncio.create_task(harness.resume(run_id))
                await asyncio.wait_for(started.wait(), 1)
                drive.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await drive
                self.assertEqual(store.load(run_id).status, "cancelled")
                call = store.calls(run_id, 1)[0]
                self.assertEqual(call.status, "effect_unknown")
                cancelled = store.stored_events(run_id)[-1].event
                self.assertEqual(cancelled.type, "run.cancelled")
                self.assertEqual(
                    dict(cancelled.data),
                    {
                        "effect_unknown": True,
                        "previous_status": "effect_unknown",
                        "resumable": False,
                    },
                )
                harness.resolve_effect(
                    run_id,
                    call.fingerprint,
                    "record_result",
                    reason="operator reconciled the external system",
                    result={"accepted": True},
                )
                self.assertEqual(store.load(run_id).status, "cancelled")
                self.assertEqual(store.calls(run_id, 1)[0].status, "result")

                harness.close()
                self.assertFalse(store.closed)
                store.close()
                self.assertTrue(store.closed)
                store.close()
                with self.assertRaisesRegex(StoreError, "^store is closed$"):
                    store.load(run_id)

    async def test_nested_tool_arguments_are_detached_across_timeout_and_retry(self):
        for index, store in enumerate(self._stores()):
            with self.subTest(adapter=type(store).__name__):
                self.addCleanup(store.close)
                handler_amounts: list[int] = []
                key_amounts: list[int] = []

                def idempotency_key(arguments):
                    key_amounts.append(arguments["payload"]["amount"])
                    arguments["payload"]["amount"] = 700
                    return "order-1"

                async def charge(payload, *, idempotency_key):
                    handler_amounts.append(payload["amount"])
                    payload["amount"] = 999
                    if len(handler_amounts) == 1:
                        await asyncio.Event().wait()
                    return {"amount": handler_amounts[-1]}

                run_id = f"nested-argument-retry-{index}"
                harness = Harness(
                    ScriptedModel(
                        ModelReply(
                            tool_calls=(
                                ToolCall(
                                    "charge-1",
                                    "charge",
                                    {"payload": {"amount": 1}},
                                ),
                            )
                        ),
                        ModelReply(content="charged"),
                    ),
                    (
                        Tool(
                            "charge",
                            charge,
                            effect="idempotent",
                            idempotency_key=idempotency_key,
                            tool_revision="charge-v1",
                        ),
                    ),
                    store=store,
                    tool_timeout=0.02,
                )
                with self.assertRaises(RunPaused) as approval:
                    await harness.run(
                        (Message("user", "charge"),), run_id=run_id
                    )
                request = approval.exception.request
                self.assertIsNotNone(request)
                self.assertEqual(
                    dict(request.arguments["payload"]), {"amount": 1}
                )
                harness.resolve_approval(
                    run_id, request.fingerprint, True
                )
                with self.assertRaises(RunPaused) as retry:
                    await harness.resume(run_id)
                self.assertEqual(retry.exception.reason, "paused_recovery")
                stored = store.calls(run_id, 1)[0]
                self.assertEqual(
                    dict(stored.arguments["payload"]), {"amount": 1}
                )

                result = await harness.resume(run_id)
                self.assertEqual(result.final_message.content, "charged")
                self.assertEqual(handler_amounts, [1, 1])
                self.assertEqual(key_amounts, [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
