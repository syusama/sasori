from __future__ import annotations

import asyncio
import json
import unittest

from sasori import Harness, Message, ModelReply, Tool, ToolCall
from sasori.provider_anthropic import _wire_history as anthropic_wire_history
from sasori.provider_openai import _input_items as openai_input_items
from sasori_context import (
    BoundedContextModel,
    ContextBudget,
    ContextBudgetExceeded,
    ContextProjector,
    ContextStructureError,
    ProtectedContextMessage,
    default_message_units,
)


class CaptureModel:
    def __init__(self) -> None:
        self.messages: tuple[Message, ...] = ()

    async def complete(self, messages, tools):
        self.messages = messages
        return ModelReply(content="ok")


def units(messages: tuple[Message, ...]) -> int:
    return sum(default_message_units(message) for message in messages)


class ContextBudgetTests(unittest.TestCase):
    def test_protected_data_prelude_survives_long_history_and_is_budgeted(self) -> None:
        system = Message("system", "policy")
        protected = ProtectedContextMessage(
            "assistant", '{"matches":[{"content":"alpha durable fact"}]}'
        )
        old = (
            Message("user", "old user " * 400),
            Message("assistant", "old answer " * 400),
        )
        current = Message("user", "alpha")
        projection = ContextProjector(
            ContextBudget(2600, reserve_units=0, hot_turns=1)
        ).project((system, protected, *old, current))

        self.assertIn(protected, projection.messages)
        self.assertIn(current, projection.messages)
        self.assertNotIn(old[0], projection.messages)
        self.assertLessEqual(projection.projected_units, 2600)
        with self.assertRaises(ContextBudgetExceeded):
            ContextProjector(ContextBudget(300)).project(
                (
                    system,
                    ProtectedContextMessage("assistant", "x" * 1000),
                    current,
                )
            )

    def test_protected_data_prelude_is_strictly_positioned_and_ordinary(self) -> None:
        invalid = (
            (
                Message("system", "policy"),
                Message("user", "hello"),
                ProtectedContextMessage("assistant", "late"),
            ),
            (ProtectedContextMessage("user", "wrong role"), Message("user", "hello")),
            (
                ProtectedContextMessage(
                    "assistant", "call", tool_calls=(ToolCall("x", "lookup", {}),)
                ),
                Message("user", "hello"),
            ),
            (
                ProtectedContextMessage(
                    "assistant", "result", tool_call_id="x", tool_name="lookup"
                ),
                Message("user", "hello"),
            ),
            (
                ProtectedContextMessage(
                    "assistant", "private", provider_state="opaque"
                ),
                Message("user", "hello"),
            ),
        )
        projector = ContextProjector(ContextBudget(100_000))
        for messages in invalid:
            with self.subTest(messages=messages), self.assertRaises(
                ContextStructureError
            ):
                projector.project(messages)

    def test_ordinary_assistant_message_does_not_gain_budget_protection(self) -> None:
        ordinary = Message("assistant", "ordinary oldest data " * 300)
        messages = (
            Message("system", "policy"),
            ordinary,
            Message("user", "old " * 300),
            Message("assistant", "old answer " * 300),
            Message("user", "latest"),
        )
        projection = ContextProjector(ContextBudget(2200)).project(messages)
        self.assertNotIn(ordinary, projection.messages)

    def test_under_budget_preserves_exact_message_objects(self) -> None:
        messages = (Message("system", "policy"), Message("user", "hello"))
        projection = ContextProjector(ContextBudget(units(messages))).project(messages)

        self.assertEqual(projection.messages, messages)
        self.assertFalse(projection.compacted)
        self.assertEqual(projection.original_units, projection.projected_units)

    def test_projection_drops_old_turns_and_preserves_tool_group(self) -> None:
        old = (
            Message("user", "old " * 100),
            Message(
                "assistant",
                tool_calls=(ToolCall("call-1", "lookup", {"q": "old"}),),
            ),
            Message("tool", "result " * 80, tool_call_id="call-1", tool_name="lookup"),
            Message("assistant", "old answer " * 50),
        )
        hot = (Message("user", "current"), Message("assistant", "current answer"))
        system = (Message("system", "policy"),)
        required = system + (
            Message(
                "system",
                "[sasori-context/v1 compacted history] "
                "messages=4; tool_calls=1; roles=assistant:2,tool:1,user:1; "
                "sha256=" + "0" * 64 + ". Content was removed by deterministic "
                "budget projection; do not infer omitted facts.",
            ),
        ) + hot
        budget = units(required) + 50

        projection = ContextProjector(ContextBudget(budget)).project(system + old + hot)

        self.assertTrue(projection.compacted)
        self.assertEqual(projection.removed_messages, 4)
        self.assertEqual(projection.messages[0], system[0])
        self.assertEqual(projection.messages[-2:], hot)
        self.assertRegex(projection.messages[1].content, r"sha256=[0-9a-f]{64}")
        self.assertNotIn("old answer", " ".join(item.content for item in projection.messages))
        self.assertLessEqual(projection.projected_units, budget)

    def test_never_splits_parallel_tool_calls_from_results(self) -> None:
        messages = (
            Message("user", "old"),
            Message(
                "assistant",
                tool_calls=(
                    ToolCall("one", "lookup", {}),
                    ToolCall("two", "lookup", {}),
                ),
            ),
            Message("tool", "one", tool_call_id="one", tool_name="lookup"),
            Message("tool", "two", tool_call_id="two", tool_name="lookup"),
            Message("user", "latest"),
            Message("assistant", "answer"),
        )
        tail = messages[-2:]
        marker_allowance = default_message_units(
            Message(
                "system",
                "[sasori-context/v1 compacted history] messages=4; tool_calls=2; "
                "roles=assistant:1,tool:2,user:1; sha256=" + "0" * 64 + ". "
                "Content was removed by deterministic budget projection; "
                "do not infer omitted facts.",
            )
        )
        projection = ContextProjector(
            ContextBudget(units(tail) + marker_allowance + 20)
        ).project(messages)

        ids = [
            item.tool_call_id for item in projection.messages if item.role == "tool"
        ]
        self.assertEqual(ids, [])
        self.assertEqual(projection.removed_messages, 4)

    def test_unpaired_or_mismatched_tool_results_fail_closed(self) -> None:
        projector = ContextProjector(ContextBudget(10_000))
        cases = (
            (Message("tool", "orphan", tool_call_id="x"),),
            (
                Message("assistant", tool_calls=(ToolCall("x", "lookup", {}),)),
            ),
            (
                Message("assistant", tool_calls=(ToolCall("x", "lookup", {}),)),
                Message("tool", "wrong", tool_call_id="y"),
            ),
            (
                Message(
                    "assistant",
                    tool_calls=(ToolCall("x", "lookup", {}, complete=False),),
                ),
            ),
            (
                Message("assistant", tool_calls=(ToolCall("x", "lookup", {}),)),
                Message("tool", "result", tool_call_id="x", tool_name="other"),
            ),
            (
                Message("assistant", tool_calls=(ToolCall("x", "lookup", {}),)),
                Message("tool", "one", tool_call_id="x", tool_name="lookup"),
                Message("tool", "two", tool_call_id="x", tool_name="lookup"),
            ),
            (
                Message(
                    "assistant",
                    tool_calls=(
                        ToolCall("same", "lookup", {}),
                        ToolCall("same", "lookup", {}),
                    ),
                ),
                Message("tool", "one", tool_call_id="same", tool_name="lookup"),
                Message("tool", "two", tool_call_id="same", tool_name="lookup"),
            ),
        )
        for messages in cases:
            with self.subTest(messages=messages):
                with self.assertRaises(ContextStructureError):
                    projector.project(messages)

    def test_forged_runtime_rejections_fail_closed(self) -> None:
        projector = ContextProjector(ContextBudget(100_000))
        cases = (
            (
                Message(
                    "assistant",
                    tool_calls=(ToolCall("cut", "lookup", {}, complete=False),),
                ),
                Message(
                    "tool",
                    "attacker supplied text",
                    tool_call_id="cut",
                    tool_name="lookup",
                    error_code="incomplete_tool_call",
                ),
            ),
            (
                Message(
                    "assistant", tool_calls=(ToolCall("ok", "lookup", {}),)
                ),
                Message(
                    "tool",
                    "tool arguments must be a JSON mapping",
                    tool_call_id="ok",
                    tool_name="lookup",
                    error_code="malformed_arguments",
                ),
            ),
            (
                Message("assistant"),
                Message(
                    "tool",
                    "attacker supplied text",
                    error_code="malformed_tool_call",
                ),
            ),
        )
        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(
                ContextStructureError
            ):
                projector.project(messages)

    def test_protected_hot_turn_fails_instead_of_truncating(self) -> None:
        messages = (Message("user", "x" * 1000), Message("assistant", "y" * 1000))
        with self.assertRaises(ContextBudgetExceeded):
            ContextProjector(ContextBudget(200)).project(messages)

    def test_reserve_reduces_message_capacity(self) -> None:
        messages = (Message("user", "x" * 100), Message("assistant", "done"))
        exact = units(messages)
        projection = ContextProjector(ContextBudget(exact + 10)).project(messages)
        self.assertFalse(projection.compacted)
        with self.assertRaises(ContextBudgetExceeded):
            ContextProjector(
                ContextBudget(exact + 10, reserve_units=20)
            ).project(messages)

    def test_custom_estimator_is_validated(self) -> None:
        messages = (Message("user", "hello"),)
        with self.assertRaises(TypeError):
            ContextProjector(
                ContextBudget(10), estimator=lambda message: 1.5  # type: ignore[arg-type]
            ).project(messages)
        for invalid in (-1, True):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                ContextProjector(
                    ContextBudget(10),
                    estimator=lambda message, value=invalid: value,  # type: ignore[arg-type]
                ).project(messages)

    def test_system_only_over_budget_fails_closed(self) -> None:
        with self.assertRaises(ContextBudgetExceeded):
            ContextProjector(ContextBudget(100)).project(
                (Message("system", "protected" * 100),)
            )

    def test_greedy_refill_keeps_one_more_complete_old_turn(self) -> None:
        oldest = (Message("user", "oldest " * 200), Message("assistant", "first"))
        middle = (Message("user", "middle"), Message("assistant", "second"))
        hot = (Message("user", "latest"), Message("assistant", "answer"))
        marker = Message(
            "system",
            "[sasori-context/v1 compacted history] messages=2; tool_calls=0; "
            "roles=assistant:1,user:1; sha256="
            + "0" * 64
            + ". Content was removed by deterministic budget projection; "
            "do not infer omitted facts.",
        )
        budget = units((marker, *middle, *hot))

        projection = ContextProjector(ContextBudget(budget)).project(
            (*oldest, *middle, *hot)
        )

        self.assertEqual(projection.removed_messages, 2)
        self.assertEqual(projection.messages[-4:], (*middle, *hot))

    def test_model_adapter_forwards_only_projected_history(self) -> None:
        capture = CaptureModel()
        messages = (
            Message("user", "old " * 200),
            Message("assistant", "old answer " * 100),
            Message("user", "latest"),
            Message("assistant", "latest answer"),
        )
        tail_units = units(messages[-2:])
        projector = ContextProjector(ContextBudget(tail_units + 500))
        adapter = BoundedContextModel(capture, projector)

        result = asyncio.run(adapter.complete(messages, ()))

        self.assertEqual(result.content, "ok")
        self.assertEqual(capture.messages[-2:], messages[-2:])
        self.assertEqual(capture.messages[0].role, "system")
        self.assertNotEqual(capture.messages, messages)

    def test_public_marker_excludes_private_provider_state(self) -> None:
        def project(private_state: str):
            messages = (
                Message("user", "old " * 100),
                Message(
                    "assistant",
                    "old answer " * 80,
                    provider_state=private_state,
                ),
                Message("user", "latest"),
                Message("assistant", "answer"),
            )
            marker = Message(
                "system",
                "[sasori-context/v1 compacted history] messages=2; "
                "tool_calls=0; roles=assistant:1,user:1; sha256="
                + "0" * 64
                + ". Content was removed by deterministic budget projection; "
                "do not infer omitted facts.",
            )
            budget = units((marker, *messages[-2:])) + 20
            return ContextProjector(ContextBudget(budget)).project(messages)

        first = project('{"provider":"one","secret":"PRIVATE-A"}')
        second = project('{"provider":"two","secret":"PRIVATE-B"}')

        self.assertNotEqual(first.removed_sha256, second.removed_sha256)
        self.assertEqual(first.messages[0].content, second.messages[0].content)
        self.assertNotIn("PRIVATE", first.messages[0].content)

    def test_shared_projector_is_deterministic_under_concurrency(self) -> None:
        messages = (
            Message("user", "old " * 100),
            Message("assistant", "old answer " * 80),
            Message("user", "latest"),
            Message("assistant", "answer"),
        )
        projector = ContextProjector(ContextBudget(units(messages[-2:]) + 500))

        async def project_many():
            return await asyncio.gather(
                *(asyncio.to_thread(projector.project, messages) for _ in range(16))
            )

        projections = asyncio.run(project_many())
        self.assertTrue(all(item == projections[0] for item in projections))


class ContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_harness_can_recover_from_each_rejected_call_shape(self) -> None:
        cases = (
            (
                ("incomplete_tool_call",),
                ModelReply(
                    tool_calls=(
                        ToolCall("cut", "guarded", {"value": 1}, complete=False),
                    )
                ),
                0,
            ),
            (
                ("malformed_tool_call",),
                ModelReply(tool_calls=(ToolCall("", "guarded", {"value": 1}),)),
                0,
            ),
            (
                ("malformed_tool_call",),
                ModelReply(tool_calls=(object(),)),  # type: ignore[arg-type]
                0,
            ),
            (
                ("malformed_arguments",),
                ModelReply(
                    tool_calls=(ToolCall("bad-args", "guarded", ["not-a-map"]),)
                ),
                0,
            ),
            (
                ("malformed_tool_call", "malformed_tool_call"),
                ModelReply(
                    tool_calls=(
                        object(),  # type: ignore[arg-type]
                        ToolCall("", "guarded", {"value": 1}),
                    )
                ),
                0,
            ),
            (
                ("malformed_tool_call", "malformed_tool_call"),
                ModelReply(
                    tool_calls=(
                        ToolCall("", "guarded", {"value": 1}),
                        object(),  # type: ignore[arg-type]
                    )
                ),
                0,
            ),
            (
                ("malformed_tool_call", "malformed_tool_call"),
                ModelReply(tool_calls=(object(), object())),  # type: ignore[arg-type]
                0,
            ),
            (
                ("malformed_tool_call", "malformed_tool_call"),
                ModelReply(
                    tool_calls=(
                        ToolCall("", "guarded", {"value": 1}),
                        ToolCall("", "guarded", {"value": 2}),
                    )
                ),
                0,
            ),
            (
                ("malformed_tool_call",),
                ModelReply(
                    tool_calls=(
                        object(),  # type: ignore[arg-type]
                        ToolCall("valid", "guarded", {"value": 2}),
                    )
                ),
                1,
            ),
            (
                ("malformed_tool_call",),
                ModelReply(
                    tool_calls=(
                        ToolCall("valid", "guarded", {"value": 2}),
                        object(),  # type: ignore[arg-type]
                    )
                ),
                1,
            ),
            (
                ("incomplete_tool_call",),
                ModelReply(
                    tool_calls=(
                        ToolCall("cut", "guarded", {"value": 1}, complete=False),
                        ToolCall("valid", "guarded", {"value": 2}),
                    )
                ),
                1,
            ),
            (
                ("incomplete_tool_call",),
                ModelReply(
                    tool_calls=(
                        ToolCall("valid", "guarded", {"value": 2}),
                        ToolCall("cut", "guarded", {"value": 1}, complete=False),
                    )
                ),
                1,
            ),
        )
        for error_codes, unsafe_reply, expected_tool_calls in cases:
            with self.subTest(error_codes=error_codes, reply=unsafe_reply):
                tool_calls = 0

                def guarded(value):
                    nonlocal tool_calls
                    tool_calls += 1
                    return value

                class RecoveringModel:
                    def __init__(self) -> None:
                        self.calls = 0

                    async def complete(self, messages, tools):
                        self.calls += 1
                        if self.calls == 1:
                            return unsafe_reply
                        visible = "\n".join(message.content for message in messages)
                        self.assertion(error_codes, visible)
                        return ModelReply(content="recovered")

                    @staticmethod
                    def assertion(expected, visible):
                        for code in set(expected):
                            marker = f'"error_code":"{code}"'
                            if visible.count(marker) != expected.count(code):
                                raise AssertionError(
                                    "second model turn received the wrong count "
                                    f"for {code}"
                                )

                inner = RecoveringModel()
                adapter = BoundedContextModel(
                    inner, ContextProjector(ContextBudget(100_000))
                )
                with Harness(
                    adapter, (Tool("guarded", guarded, effect="read_only"),)
                ) as harness:
                    result = await harness.run((Message("user", "unsafe"),))

                self.assertEqual(result.final_message.content, "recovered")
                self.assertEqual(inner.calls, 2)
                self.assertEqual(tool_calls, expected_tool_calls)
                self.assertEqual(
                    [event.type for event in result.events].count("tool.started"),
                    expected_tool_calls,
                )

    async def test_rejected_projection_is_safe_for_both_provider_wires(self) -> None:
        private_state = '{"provider":"foreign","secret":"DO-NOT-FORWARD"}'
        history = (
            Message("user", "unsafe"),
            Message(
                "assistant",
                tool_calls=(ToolCall("cut", "guarded", {}, complete=False),),
                provider_state=private_state,
            ),
            Message(
                "tool",
                "incomplete tool call was refused",
                tool_call_id="cut",
                tool_name="guarded",
                error_code="incomplete_tool_call",
            ),
        )
        projection = ContextProjector(ContextBudget(100_000)).project(history)
        rendered = json.dumps(
            [message.content for message in projection.messages], ensure_ascii=False
        )

        self.assertIn("incomplete_tool_call", rendered)
        self.assertNotIn("DO-NOT-FORWARD", rendered)
        self.assertTrue(openai_input_items(projection.messages))
        system, anthropic_messages = anthropic_wire_history(projection.messages)
        self.assertIsNone(system)
        self.assertTrue(anthropic_messages)

    async def test_complete_provider_tool_groups_survive_and_wire(self) -> None:
        openai_state = json.dumps(
            {
                "provider": "openai.responses",
                "version": 1,
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
            },
            separators=(",", ":"),
        )
        anthropic_state = json.dumps(
            {
                "provider": "anthropic.messages",
                "version": 1,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "lookup",
                        "input": {},
                    }
                ],
            },
            separators=(",", ":"),
        )
        openai_history = (
            Message("user", "lookup"),
            Message(
                "assistant",
                tool_calls=(ToolCall("call-1", "lookup", {}),),
                provider_state=openai_state,
            ),
            Message("tool", "ok", tool_call_id="call-1", tool_name="lookup"),
        )
        anthropic_history = (
            Message("user", "lookup"),
            Message(
                "assistant",
                tool_calls=(ToolCall("toolu-1", "lookup", {}),),
                provider_state=anthropic_state,
            ),
            Message("tool", "ok", tool_call_id="toolu-1", tool_name="lookup"),
        )
        projector = ContextProjector(ContextBudget(100_000))

        openai_projection = projector.project(openai_history)
        anthropic_projection = projector.project(anthropic_history)

        self.assertEqual(openai_projection.messages, openai_history)
        self.assertEqual(anthropic_projection.messages, anthropic_history)
        self.assertTrue(openai_input_items(openai_projection.messages))
        self.assertTrue(anthropic_wire_history(anthropic_projection.messages)[1])

        marker_allowance = default_message_units(
            Message(
                "system",
                "[sasori-context/v1 compacted history] messages=3; tool_calls=1; "
                "roles=assistant:1,tool:1,user:1; sha256="
                + "0" * 64
                + ". Content was removed by deterministic budget projection; "
                "do not infer omitted facts.",
            )
        )
        tail = (Message("user", "latest"), Message("assistant", "answer"))
        compacting = ContextProjector(
            ContextBudget(units(tail) + marker_allowance + 20)
        )
        compacted_openai = compacting.project((*openai_history, *tail))
        compacted_anthropic = compacting.project((*anthropic_history, *tail))

        self.assertEqual(compacted_openai.removed_messages, 3)
        self.assertEqual(compacted_anthropic.removed_messages, 3)
        self.assertTrue(openai_input_items(compacted_openai.messages))
        self.assertTrue(anthropic_wire_history(compacted_anthropic.messages)[1])
        self.assertTrue(anthropic_wire_history(compacted_openai.messages)[1])
        self.assertTrue(openai_input_items(compacted_anthropic.messages))

    async def test_adapter_propagates_cancellation(self) -> None:
        started = asyncio.Event()

        class WaitingModel:
            async def complete(self, messages, tools):
                started.set()
                await asyncio.Future()

        adapter = BoundedContextModel(
            WaitingModel(), ContextProjector(ContextBudget(10_000))
        )
        task = asyncio.create_task(adapter.complete((Message("user", "wait"),), ()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("context-cancel")
        with self.assertRaises(asyncio.CancelledError) as raised:
            await task
        self.assertEqual(raised.exception.args, ("context-cancel",))


if __name__ == "__main__":
    unittest.main()
