from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sasori_core import (  # noqa: E402
    Harness,
    Message,
    ModelCallError,
    ModelReply,
    ModelStreamEvent,
    ModelStreamProtocolError,
    ModelTimeoutError,
    MAX_TOOL_PROGRESS_EVENT_BYTES,
    MAX_TOOL_PROGRESS_EVENTS,
    RunBusy,
    Tool,
    ToolCall,
    ToolExecutionContext,
    ToolProgressEvent,
)
from sasori_core.testing import (  # noqa: E402
    ScriptedModel,
    ScriptedStreamingModel,
)


def _stream(reply: ModelReply, *deltas: ModelStreamEvent):
    return (
        ModelStreamEvent("start"),
        *deltas,
        ModelStreamEvent("done", reply=reply),
    )


class CoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_tool_progress_is_ordered_immutable_and_transient(self):
        observed: list[ToolProgressEvent] = []
        mutation_errors: list[type[BaseException]] = []

        def observe(event: ToolProgressEvent) -> None:
            try:
                if event.sequence == 1:
                    event.data["nested"]["value"] = 99
                else:
                    event.data["items"].__setitem__(0, 99)
            except (AttributeError, TypeError) as error:
                mutation_errors.append(type(error))
            observed.append(event)
            if event.sequence == 1:
                raise asyncio.CancelledError()

        async def work(
            value: int, *, tool_context: ToolExecutionContext
        ) -> dict[str, int]:
            self.assertTrue(
                tool_context.report_progress(
                    {"phase": "prepare", "nested": {"value": value}}
                )
            )
            self.assertTrue(
                tool_context.report_progress({"phase": "finish", "items": [value]})
            )
            return {"value": value}

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("progress-1", "work", {"value": 7}),)),
            ModelReply(content="done"),
        )
        harness = Harness(
            model,
            (Tool("work", work, effect="read_only"),),
            tool_progress_sink=observe,
        )
        self.addCleanup(harness.close)

        result = await harness.run(
            (Message("user", "work"),), run_id="async-tool-progress"
        )

        self.assertEqual(result.final_message.content, "done")
        self.assertEqual([event.sequence for event in observed], [1, 2])
        self.assertEqual(
            [event.data["phase"] for event in observed], ["prepare", "finish"]
        )
        self.assertEqual(len(mutation_errors), 2)
        self.assertTrue(
            all(
                event.run_id == "async-tool-progress"
                and event.step == 1
                and event.ordinal == 0
                and event.call_id == "progress-1"
                and event.tool_name == "work"
                for event in observed
            )
        )
        self.assertNotIn(
            "tool.progress", [event.type for event in result.events]
        )
        self.assertFalse(
            any(
                message.role == "tool" and "prepare" in message.content
                for message in result.messages
            )
        )
        model.assert_consumed()

    async def test_sync_tool_progress_is_fenced_after_return(self):
        observed: list[int] = []
        worker_finished = asyncio.Event()
        late_results: list[bool] = []
        loop = asyncio.get_running_loop()

        def work(*, tool_context: ToolExecutionContext) -> str:
            self.assertTrue(tool_context.report_progress({"value": 1}))

            def late() -> None:
                import time

                time.sleep(0.05)
                late_results.append(tool_context.report_progress({"value": 2}))
                loop.call_soon_threadsafe(worker_finished.set)

            import threading

            threading.Thread(target=late, daemon=True).start()
            return "ok"

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("sync-progress", "work", {}),)),
            ModelReply(content="done"),
        )
        harness = Harness(
            model,
            (Tool("work", work, effect="read_only"),),
            tool_progress_sink=lambda event: observed.append(event.data["value"]),
        )
        self.addCleanup(harness.close)

        result = await harness.run(
            (Message("user", "work"),), run_id="sync-progress-fence"
        )
        await asyncio.wait_for(worker_finished.wait(), 1)
        await asyncio.sleep(0)

        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(observed, [1])
        self.assertEqual(late_results, [False])
        self.assertTrue(harness.is_idle)

    async def test_tool_progress_is_bounded_and_invalid_updates_do_not_fail_tool(self):
        observed: list[ToolProgressEvent] = []
        accepted: list[bool] = []

        async def work(*, tool_context: ToolExecutionContext) -> str:
            accepted.extend(
                (
                    tool_context.report_progress([]),  # type: ignore[arg-type]
                    tool_context.report_progress({"bad": float("nan")}),
                    tool_context.report_progress(
                        {"oversized": "x" * MAX_TOOL_PROGRESS_EVENT_BYTES}
                    ),
                )
            )
            for index in range(MAX_TOOL_PROGRESS_EVENTS + 2):
                accepted.append(tool_context.report_progress({"index": index}))
            return "bounded"

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("bounded-progress", "work", {}),)),
            ModelReply(content="done"),
        )
        harness = Harness(
            model,
            (Tool("work", work, effect="read_only"),),
            tool_progress_sink=observed.append,
        )
        self.addCleanup(harness.close)
        result = await harness.run(
            (Message("user", "work"),), run_id="bounded-tool-progress"
        )

        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(accepted[:3], [False, False, False])
        self.assertEqual(accepted[3:].count(True), MAX_TOOL_PROGRESS_EVENTS)
        self.assertEqual(accepted[-2:], [False, False])
        self.assertEqual(len(observed), MAX_TOOL_PROGRESS_EVENTS)
        self.assertEqual(observed[-1].sequence, MAX_TOOL_PROGRESS_EVENTS)

        cumulative_observed: list[ToolProgressEvent] = []
        cumulative_results: list[bool] = []

        async def cumulative(*, tool_context: ToolExecutionContext) -> str:
            for index in range(20):
                cumulative_results.append(
                    tool_context.report_progress(
                        {"index": index, "chunk": "x" * 60_000}
                    )
                )
            return "bounded"

        cumulative_model = ScriptedModel(
            ModelReply(
                tool_calls=(ToolCall("cumulative-progress", "cumulative", {}),)
            ),
            ModelReply(content="done"),
        )
        cumulative_harness = Harness(
            cumulative_model,
            (Tool("cumulative", cumulative, effect="read_only"),),
            tool_progress_sink=cumulative_observed.append,
        )
        self.addCleanup(cumulative_harness.close)
        cumulative_result = await cumulative_harness.run(
            (Message("user", "work"),), run_id="cumulative-tool-progress"
        )
        self.assertEqual(cumulative_result.final_message.content, "done")
        self.assertGreater(cumulative_results.count(True), 0)
        self.assertLess(cumulative_results.count(True), len(cumulative_results))
        self.assertEqual(
            cumulative_results,
            [True] * len(cumulative_observed)
            + [False] * (20 - len(cumulative_observed)),
        )

    async def test_tool_timeout_fences_late_sync_progress(self):
        observed: list[str] = []
        late_results: list[bool] = []
        worker_finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow(*, tool_context: ToolExecutionContext) -> str:
            import time

            self.assertTrue(tool_context.report_progress({"phase": "started"}))
            time.sleep(0.08)
            late_results.append(
                tool_context.report_progress({"phase": "after-timeout"})
            )
            loop.call_soon_threadsafe(worker_finished.set)
            return "too late"

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("timeout-progress", "slow", {}),)),
            ModelReply(content="recovered"),
        )
        harness = Harness(
            model,
            (Tool("slow", slow, effect="read_only"),),
            tool_timeout=0.02,
            tool_progress_sink=lambda event: observed.append(event.data["phase"]),
        )
        self.addCleanup(harness.close)
        result = await harness.run(
            (Message("user", "slow"),), run_id="timeout-progress-fence"
        )
        await asyncio.wait_for(worker_finished.wait(), 1)
        await asyncio.sleep(0)

        self.assertEqual(result.final_message.content, "recovered")
        self.assertEqual(observed, ["started"])
        self.assertEqual(late_results, [False])
        timeout_result = next(
            message for message in result.messages if message.error_code
        )
        self.assertEqual(timeout_result.error_code, "tool_timeout")

    async def test_tool_exception_fences_late_sync_progress(self):
        observed: list[str] = []
        late_results: list[bool] = []
        worker_finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fail(*, tool_context: ToolExecutionContext) -> str:
            self.assertTrue(tool_context.report_progress({"phase": "started"}))

            def late() -> None:
                import time

                time.sleep(0.05)
                late_results.append(
                    tool_context.report_progress({"phase": "after-exception"})
                )
                loop.call_soon_threadsafe(worker_finished.set)

            import threading

            threading.Thread(target=late, daemon=True).start()
            raise RuntimeError("boom")

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("exception-progress", "fail", {}),)),
            ModelReply(content="recovered"),
        )
        harness = Harness(
            model,
            (Tool("fail", fail, effect="read_only"),),
            tool_progress_sink=lambda event: observed.append(event.data["phase"]),
        )
        self.addCleanup(harness.close)
        result = await harness.run(
            (Message("user", "fail"),), run_id="exception-progress-fence"
        )
        await asyncio.wait_for(worker_finished.wait(), 1)
        await asyncio.sleep(0)

        self.assertEqual(result.final_message.content, "recovered")
        self.assertEqual(observed, ["started"])
        self.assertEqual(late_results, [False])
        exception_result = next(
            message for message in result.messages if message.error_code
        )
        self.assertEqual(exception_result.error_code, "tool_exception")

    async def test_tool_cancellation_fences_late_sync_progress(self):
        observed: list[str] = []
        late_results: list[bool] = []
        tool_started = asyncio.Event()
        worker_finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow(*, tool_context: ToolExecutionContext) -> str:
            import time

            self.assertTrue(tool_context.report_progress({"phase": "started"}))
            loop.call_soon_threadsafe(tool_started.set)
            time.sleep(0.05)
            late_results.append(
                tool_context.report_progress({"phase": "after-cancel"})
            )
            loop.call_soon_threadsafe(worker_finished.set)
            return "too late"

        model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("cancel-progress", "slow", {}),))
        )
        harness = Harness(
            model,
            (Tool("slow", slow, effect="read_only"),),
            tool_progress_sink=lambda event: observed.append(event.data["phase"]),
        )
        self.addCleanup(harness.close)
        drive = asyncio.create_task(
            harness.run(
                (Message("user", "slow"),), run_id="cancel-progress-fence"
            )
        )
        await asyncio.wait_for(tool_started.wait(), 1)
        await asyncio.sleep(0)
        drive.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await drive
        await asyncio.wait_for(worker_finished.wait(), 1)
        await asyncio.sleep(0)

        self.assertEqual(observed, ["started"])
        self.assertEqual(late_results, [False])
        self.assertEqual(
            harness.store.load("cancel-progress-fence").status, "cancelled"
        )
        self.assertTrue(harness.is_idle)

    async def test_tool_progress_context_is_reserved_and_keyword_only(self):
        effects: list[str] = []

        def work(*, tool_context: ToolExecutionContext) -> str:
            effects.append("executed")
            return "unsafe"

        harness = Harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "reserved-progress",
                            "work",
                            {"tool_context": "attacker"},
                        ),
                    )
                ),
                ModelReply(content="refused"),
            ),
            (Tool("work", work, effect="read_only"),),
        )
        self.addCleanup(harness.close)
        result = await harness.run(
            (Message("user", "work"),), run_id="reserved-progress-context"
        )
        self.assertEqual(result.final_message.content, "refused")
        self.assertEqual(effects, [])
        error = next(message for message in result.messages if message.error_code)
        self.assertEqual(error.error_code, "reserved_argument")

        with self.assertRaisesRegex(ValueError, "tool_context is reserved"):
            Harness(
                ScriptedModel(ModelReply(content="unused")),
                (
                    Tool(
                        "invalid",
                        lambda tool_context: None,
                        effect="read_only",
                    ),
                ),
            )

    async def test_stream_and_complete_models_share_the_committed_semantics(self):
        observed: list[ModelStreamEvent] = []
        stream = ScriptedStreamingModel(
            _stream(
                ModelReply(content="eternal"),
                ModelStreamEvent("thinking_delta", delta="plan"),
                ModelStreamEvent("text_delta", delta="eternal"),
            )
        )
        streamed = Harness(stream, model_stream_sink=observed.append)
        self.addCleanup(streamed.close)
        complete = Harness(ScriptedModel(ModelReply(content="eternal")))
        self.addCleanup(complete.close)

        streamed_result = await streamed.run(
            (Message("user", "art"),), run_id="streamed"
        )
        complete_result = await complete.run(
            (Message("user", "art"),), run_id="complete"
        )

        self.assertEqual(streamed_result.final_message, complete_result.final_message)
        self.assertEqual(
            [event.type for event in streamed_result.events],
            [event.type for event in complete_result.events],
        )
        self.assertEqual(
            [event.type for event in observed],
            ["start", "thinking_delta", "text_delta", "done"],
        )
        stream.assert_consumed()

    async def test_complete_stream_can_drive_the_real_tool_loop(self):
        calls: list[int] = []

        def double(value: int) -> int:
            calls.append(value)
            return value * 2

        model = ScriptedStreamingModel(
            _stream(
                ModelReply(
                    tool_calls=(ToolCall("stream-call", "double", {"value": 4}),)
                ),
                ModelStreamEvent("tool_call_delta", delta='{"value":'),
            ),
            _stream(ModelReply(content="8"), ModelStreamEvent("text_delta", delta="8")),
        )
        harness = Harness(
            model,
            (Tool("double", double, effect="read_only"),),
        )
        self.addCleanup(harness.close)

        result = await harness.run((Message("user", "double"),), run_id="stream-tool")

        self.assertEqual(calls, [4])
        self.assertEqual(result.final_message.content, "8")
        model.assert_consumed()

    async def test_stream_done_observer_cannot_mutate_nested_tool_arguments(self):
        handler_seen: list[tuple[int, list[int], list[int]]] = []
        mutation_errors: list[type[BaseException]] = []
        observed_tool_terminals = 0

        def inspect(payload, items, rows):
            handler_seen.append(
                (
                    payload["amount"],
                    list(items),
                    [row["value"] for row in rows],
                )
            )
            return "safe"

        def observe(event: ModelStreamEvent) -> None:
            nonlocal observed_tool_terminals
            if event.type != "done" or not event.reply.tool_calls:
                return
            observed_tool_terminals += 1
            arguments = event.reply.tool_calls[0].arguments
            for mutate in (
                lambda: arguments["payload"].__setitem__("amount", 99),
                lambda: arguments["items"].__setitem__(0, 99),
                lambda: arguments["rows"][0].__setitem__("value", 99),
            ):
                try:
                    mutate()
                except (AttributeError, TypeError) as error:
                    mutation_errors.append(type(error))
            raise RuntimeError("observer offline after attempted mutation")

        model = ScriptedStreamingModel(
            _stream(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "inspect-1",
                            "inspect",
                            {
                                "payload": {"amount": 1},
                                "items": [2, 3],
                                "rows": [{"value": 4}],
                            },
                        ),
                    )
                )
            ),
            _stream(ModelReply(content="finished")),
        )
        harness = Harness(
            model,
            (Tool("inspect", inspect, effect="read_only"),),
            model_stream_sink=observe,
        )
        self.addCleanup(harness.close)

        result = await harness.run(
            (Message("user", "inspect"),), run_id="stream-observer-snapshot"
        )

        self.assertEqual(result.final_message.content, "finished")
        self.assertEqual(handler_seen, [(1, [2, 3], [4])])
        self.assertEqual(len(mutation_errors), 3)
        self.assertEqual(observed_tool_terminals, 1)
        stored = harness.store.calls("stream-observer-snapshot", 1)[0]
        self.assertEqual(dict(stored.arguments["payload"]), {"amount": 1})
        self.assertEqual(list(stored.arguments["items"]), [2, 3])
        self.assertEqual(dict(stored.arguments["rows"][0]), {"value": 4})
        assistant = next(
            message
            for message in result.messages
            if message.role == "assistant" and message.tool_calls
        )
        self.assertEqual(
            dict(assistant.tool_calls[0].arguments["payload"]), {"amount": 1}
        )
        model.assert_consumed()

    async def test_stream_terminal_snapshot_survives_generator_mutation_after_done(self):
        source = {
            "payload": {"amount": 1},
            "items": [2, 3],
            "rows": [{"value": 4}],
        }
        handler_seen: list[tuple[int, list[int], list[int]]] = []

        class MutatingTerminalModel:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools):
                raise AssertionError("runtime must prefer complete_stream")

            async def complete_stream(self, messages, tools):
                self.calls += 1
                yield ModelStreamEvent("start")
                if self.calls == 1:
                    yield ModelStreamEvent(
                        "done",
                        reply=ModelReply(
                            tool_calls=(ToolCall("inspect-1", "inspect", source),)
                        ),
                    )
                    source["payload"]["amount"] = 99
                    source["items"][0] = 99
                    source["rows"][0]["value"] = 99
                else:
                    yield ModelStreamEvent("done", reply=ModelReply(content="finished"))

        def inspect(payload, items, rows):
            handler_seen.append(
                (
                    payload["amount"],
                    list(items),
                    [row["value"] for row in rows],
                )
            )
            return "safe"

        model = MutatingTerminalModel()
        harness = Harness(
            model,
            (Tool("inspect", inspect, effect="read_only"),),
        )
        self.addCleanup(harness.close)

        result = await harness.run(
            (Message("user", "inspect"),), run_id="stream-generator-snapshot"
        )

        self.assertEqual(result.final_message.content, "finished")
        self.assertEqual(model.calls, 2)
        self.assertEqual(handler_seen, [(1, [2, 3], [4])])
        stored = harness.store.calls("stream-generator-snapshot", 1)[0]
        self.assertEqual(dict(stored.arguments["payload"]), {"amount": 1})
        self.assertEqual(list(stored.arguments["items"]), [2, 3])
        self.assertEqual(dict(stored.arguments["rows"][0]), {"value": 4})

    async def test_stream_protocol_failures_are_terminal_and_never_execute_deltas(self):
        terminal = ModelStreamEvent("done", reply=ModelReply(content="done"))
        cases = {
            "empty": (),
            "before-start": (terminal,),
            "duplicate-start": (
                ModelStreamEvent("start"),
                ModelStreamEvent("start"),
            ),
            "missing-terminal": (
                ModelStreamEvent("start"),
                ModelStreamEvent("tool_call_delta", delta='{"danger":true'),
            ),
            "after-terminal": (
                ModelStreamEvent("start"),
                terminal,
                ModelStreamEvent("text_delta", delta="late"),
            ),
            "duplicate-terminal": (
                ModelStreamEvent("start"),
                terminal,
                terminal,
            ),
            "wrong-value": (ModelStreamEvent("start"), object()),
            "too-many-events": (
                ModelStreamEvent("start"),
                *(ModelStreamEvent("text_delta", delta="x"),) * 4096,
            ),
            "too-many-bytes": (
                ModelStreamEvent("start"),
                ModelStreamEvent("text_delta", delta="x" * (4 * 1024 * 1024 + 1)),
            ),
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                effects: list[str] = []
                observed: list[str] = []
                harness = Harness(
                    ScriptedStreamingModel(events),
                    (
                        Tool(
                            "danger",
                            lambda: effects.append("executed"),
                            effect="read_only",
                        ),
                    ),
                    model_stream_sink=lambda event: observed.append(event.type),
                )
                self.addCleanup(harness.close)
                with self.assertRaises(ModelStreamProtocolError):
                    await harness.run((Message("user", name),), run_id=f"bad-{name}")
                self.assertEqual(effects, [])
                self.assertEqual(harness.store.load(f"bad-{name}").status, "failed")
                if name == "after-terminal":
                    self.assertEqual(observed, ["start", "done"])

    async def test_stream_error_abort_timeout_and_observer_isolation(self):
        failed = Harness(
            ScriptedStreamingModel(
                (
                    ModelStreamEvent("start"),
                    ModelStreamEvent(
                        "error", error_code="rate_limit", message="try later"
                    ),
                )
            )
        )
        self.addCleanup(failed.close)
        with self.assertRaises(ModelCallError):
            await failed.run((Message("user", "fail"),), run_id="stream-error")
        self.assertEqual(failed.store.load("stream-error").status, "failed")

        aborted = Harness(
            ScriptedStreamingModel(
                (ModelStreamEvent("start"), ModelStreamEvent("aborted"))
            )
        )
        self.addCleanup(aborted.close)
        with self.assertRaises(asyncio.CancelledError):
            await aborted.run((Message("user", "abort"),), run_id="stream-aborted")
        self.assertEqual(aborted.store.load("stream-aborted").status, "cancelled")
        self.assertTrue(aborted.is_idle)

        class HangingStream:
            async def complete(self, messages, tools):
                raise AssertionError

            async def complete_stream(self, messages, tools):
                yield ModelStreamEvent("start")
                await asyncio.Event().wait()

        timed = Harness(HangingStream(), model_timeout=0.02)
        self.addCleanup(timed.close)
        with self.assertRaises(ModelTimeoutError):
            await timed.run((Message("user", "wait"),), run_id="stream-timeout")
        self.assertEqual(timed.store.load("stream-timeout").status, "failed")

        observed = Harness(
            ScriptedStreamingModel(_stream(ModelReply(content="safe"))),
            model_stream_sink=lambda event: (_ for _ in ()).throw(
                RuntimeError("observer offline")
            ),
        )
        self.addCleanup(observed.close)
        result = await observed.run(
            (Message("user", "observe"),), run_id="stream-observer"
        )
        self.assertEqual(result.final_message.content, "safe")

    async def test_stream_rejects_every_event_after_error_or_abort_terminal(self):
        terminal_cases = {
            "error-late-done": (
                ModelStreamEvent(
                    "error", error_code="rate_limit", message="try later"
                ),
                ModelStreamEvent("done", reply=ModelReply(content="late")),
            ),
            "abort-late-delta": (
                ModelStreamEvent("aborted"),
                ModelStreamEvent("text_delta", delta="late"),
            ),
            "double-error": (
                ModelStreamEvent("error", error_code="first", message="one"),
                ModelStreamEvent("error", error_code="second", message="two"),
            ),
            "double-abort": (
                ModelStreamEvent("aborted"),
                ModelStreamEvent("aborted"),
            ),
        }
        for name, suffix in terminal_cases.items():
            with self.subTest(name=name):
                harness = Harness(
                    ScriptedStreamingModel((ModelStreamEvent("start"), *suffix))
                )
                self.addCleanup(harness.close)
                with self.assertRaises(ModelStreamProtocolError):
                    await harness.run(
                        (Message("user", name),), run_id=f"terminal-{name}"
                    )
                self.assertEqual(
                    harness.store.load(f"terminal-{name}").status, "failed"
                )

    async def test_invalid_complete_and_tool_name_never_dispatch(self):
        for index, invalid in enumerate((1, "true", object())):
            with self.subTest(complete=type(invalid).__name__):
                effects: list[str] = []
                harness = Harness(
                    ScriptedModel(
                        ModelReply(
                            tool_calls=(
                                ToolCall(
                                    "call-1",
                                    "danger",
                                    {},
                                    complete=invalid,  # type: ignore[arg-type]
                                ),
                            )
                        ),
                        ModelReply(content="refused"),
                    ),
                    (
                        Tool(
                            "danger",
                            lambda: effects.append("executed"),
                            effect="read_only",
                        ),
                    ),
                )
                self.addCleanup(harness.close)
                result = await harness.run(
                    (Message("user", "do not execute"),),
                    run_id=f"invalid-complete-{index}",
                )
                self.assertEqual(result.final_message.content, "refused")
                self.assertEqual(effects, [])

        with self.assertRaises(ValueError):
            Harness(
                ScriptedModel(ModelReply(content="unused")),
                (Tool(7, lambda: None, effect="read_only"),),  # type: ignore[arg-type]
            )

        effects = []
        harness = Harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall("call-1", 7, {}),  # type: ignore[arg-type]
                    )
                ),
                ModelReply(content="refused"),
            ),
            (
                Tool(
                    "danger",
                    lambda: effects.append("executed"),
                    effect="read_only",
                ),
            ),
        )
        self.addCleanup(harness.close)
        await harness.run(
            (Message("user", "invalid tool name"),), run_id="invalid-tool-name"
        )
        self.assertEqual(effects, [])

    async def test_invalid_unicode_and_reply_text_fail_closed(self):
        effects: list[str] = []
        malformed_arguments = Harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "call-1",
                            "danger",
                            {"value": "\ud800"},
                        ),
                    )
                ),
                ModelReply(content="refused"),
            ),
            (
                Tool(
                    "danger",
                    lambda value: effects.append(value),
                    effect="read_only",
                ),
            ),
        )
        self.addCleanup(malformed_arguments.close)
        result = await malformed_arguments.run(
            (Message("user", "surrogate"),), run_id="surrogate-arguments"
        )
        self.assertEqual(result.final_message.content, "refused")
        self.assertEqual(effects, [])
        self.assertFalse(
            malformed_arguments.store.calls("surrogate-arguments", 1)[0].arguments_valid
        )

        malformed_name = Harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(ToolCall("call-1", "\ud800", {}),)
                ),
                ModelReply(content="refused"),
            ),
            (
                Tool(
                    "danger",
                    lambda: effects.append("executed"),
                    effect="read_only",
                ),
            ),
        )
        self.addCleanup(malformed_name.close)
        await malformed_name.run(
            (Message("user", "surrogate name"),), run_id="surrogate-name"
        )
        self.assertEqual(effects, [])

        with self.assertRaises(ValueError):
            Harness(
                ScriptedModel(ModelReply(content="unused")),
                (Tool("\ud800", lambda: None, effect="read_only"),),
            )

        invalid_replies = (
            ModelReply(content=7),  # type: ignore[arg-type]
            ModelReply(content="\ud800"),
            ModelReply(content="safe", provider_state="\ud800"),
        )
        for index, reply in enumerate(invalid_replies):
            with self.subTest(reply=index):
                harness = Harness(ScriptedModel(reply))
                self.addCleanup(harness.close)
                run_id = f"invalid-reply-text-{index}"
                with self.assertRaises(ModelCallError):
                    await harness.run((Message("user", "reply"),), run_id=run_id)
                self.assertEqual(harness.store.load(run_id).status, "failed")

    async def test_non_json_argument_shapes_and_runtime_identifiers_fail_closed(self):
        effects: list[object] = []
        deeply_nested: dict[str, object] = {}
        cursor = deeply_nested
        for _ in range(2000):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child

        model = ScriptedModel(
            ModelReply(
                tool_calls=(
                    ToolCall("deep", "danger", deeply_nested),
                    ToolCall("key", "danger", {1: "coerced"}),
                )
            ),
            ModelReply(content="refused"),
        )
        harness = Harness(
            model,
            (
                Tool(
                    "danger",
                    lambda **arguments: effects.append(arguments),
                    effect="read_only",
                ),
            ),
        )
        self.addCleanup(harness.close)
        result = await harness.run(
            (Message("user", "malformed arguments"),),
            run_id="malformed-json-shapes",
        )
        self.assertEqual(result.final_message.content, "refused")
        self.assertEqual(effects, [])
        self.assertEqual(
            [call.arguments_valid for call in harness.store.calls("malformed-json-shapes", 1)],
            [False, False],
        )

        with self.assertRaises(ValueError):
            Harness(
                ScriptedModel(ModelReply(content="unused")),
                (
                    Tool(
                        "danger",
                        lambda: None,
                        effect="side_effecting",
                        tool_revision="\ud800",
                    ),
                ),
            )

        idempotent_effects: list[str] = []

        def invalid_key(arguments):
            return "\ud800"

        invalid_key_harness = Harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("charge", "charge", {}),)),
                ModelReply(content="refused"),
            ),
            (
                Tool(
                    "charge",
                    lambda *, idempotency_key: idempotent_effects.append(
                        idempotency_key
                    ),
                    effect="idempotent",
                    idempotency_key=invalid_key,
                    tool_revision="charge-v1",
                ),
            ),
        )
        self.addCleanup(invalid_key_harness.close)
        invalid_key_result = await invalid_key_harness.run(
            (Message("user", "invalid idempotency key"),),
            run_id="invalid-idempotency-key-text",
        )
        self.assertEqual(invalid_key_result.final_message.content, "refused")
        self.assertEqual(idempotent_effects, [])

    async def test_same_run_admission_and_wait_for_idle_settle_on_cancellation(self):
        started = asyncio.Event()

        class BlockingModel:
            async def complete(self, messages, tools):
                started.set()
                await asyncio.Event().wait()

        harness = Harness(BlockingModel())
        self.addCleanup(harness.close)
        drive = asyncio.create_task(
            harness.run((Message("user", "hold"),), run_id="busy")
        )
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(harness.is_idle)

        with self.assertRaises(RunBusy):
            await harness.resume("busy")

        idle = asyncio.create_task(harness.wait_for_idle())
        await asyncio.sleep(0)
        self.assertFalse(idle.done())
        drive.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await drive
        await asyncio.wait_for(idle, 1)
        self.assertTrue(harness.is_idle)
        self.assertEqual(harness.store.load("busy").status, "cancelled")

    async def test_invalid_runtime_identity_fails_before_drive_or_store_mutation(self):
        class CountingModel:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                return ModelReply(content="ok")

        model = CountingModel()
        harness = Harness(model)
        self.addCleanup(harness.close)
        invalid_run_ids = ("", "-bad", "bad/id", "空", "a" * 65, 7)
        for index, run_id in enumerate(invalid_run_ids):
            with self.subTest(kind="run_id", value=run_id):
                with self.assertRaisesRegex(ValueError, "^run_id must match"):
                    await harness.run(
                        (Message("user", f"bad run {index}"),),
                        run_id=run_id,  # type: ignore[arg-type]
                    )

        invalid_app_ids = ("", "Bad App", "UPPER", "-bad", "空", "a" * 65, 7)
        for index, app_id in enumerate(invalid_app_ids):
            with self.subTest(kind="app_id", value=app_id):
                with self.assertRaisesRegex(ValueError, "^app_id must match"):
                    await harness.run(
                        (Message("user", f"bad app {index}"),),
                        run_id=f"invalid-app-{index}",
                        app_id=app_id,  # type: ignore[arg-type]
                    )

        self.assertEqual(model.calls, 0)
        self.assertEqual(harness.store.list_runs(limit=100), ())
        self.assertTrue(harness.is_idle)

        explicit = await harness.run(
            (Message("user", "valid"),),
            run_id="Run_1.ok",
            app_id="app.one",
        )
        automatic = await harness.run((Message("user", "automatic"),))
        self.assertEqual(model.calls, 2)
        self.assertEqual(explicit.run_id, "Run_1.ok")
        self.assertRegex(automatic.run_id, r"^[a-f0-9]{32}$")
        self.assertEqual(harness.store.load("Run_1.ok").app_id, "app.one")
        self.assertTrue(harness.is_idle)

    def test_stream_event_shapes_are_strict(self):
        invalid = (
            lambda: ModelStreamEvent("start", delta="x"),
            lambda: ModelStreamEvent("text_delta"),
            lambda: ModelStreamEvent("done"),
            lambda: ModelStreamEvent("error", error_code="", message="x"),
            lambda: ModelStreamEvent("aborted", reply=ModelReply(content="x")),
            lambda: ModelStreamEvent("text_delta", delta=7),  # type: ignore[arg-type]
        )
        for make in invalid:
            with self.assertRaises(ValueError):
                make()


if __name__ == "__main__":
    unittest.main()
