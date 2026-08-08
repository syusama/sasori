from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from provider_conformance import (  # noqa: E402
    PROVIDER_FACTORIES,
    LoopbackProviderServer,
    ProviderFactory,
    exception_in_chain,
    wait_for_event,
)
from sasori import (  # noqa: E402
    Harness,
    Message,
    ModelCallError,
    ProviderIncompleteError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    Tool,
)


class ProviderConformanceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _event_types(harness: Harness, run_id: str) -> list[str]:
        return [item.event.type for item in harness.stored_events(run_id)]

    @staticmethod
    def _assert_request(
        case: ProviderFactory,
        server: LoopbackProviderServer,
        expected_count: int = 1,
    ) -> None:
        if len(server.requests) != expected_count:
            raise AssertionError(
                f"expected {expected_count} provider request(s), got {len(server.requests)}"
            )
        for request in server.requests:
            if request["path"] != case.request_path:
                raise AssertionError(
                    f"expected {case.request_path}, got {request['path']}"
                )

    @staticmethod
    async def _wait_for_spawned_tasks(
        baseline: set[asyncio.Task], timeout: float = 2.0
    ) -> set[asyncio.Task]:
        current = asyncio.current_task()
        spawned = {
            task
            for task in asyncio.all_tasks()
            if task not in baseline and task is not current
        }
        if not spawned:
            return set()
        _, pending = await asyncio.wait(spawned, timeout=timeout)
        return pending

    async def test_malformed_or_incomplete_tool_output_fails_before_acceptance(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                called = 0

                def guarded(value: str) -> str:
                    nonlocal called
                    called += 1
                    return value

                server = LoopbackProviderServer()
                with server:
                    server.queue_sse(case.malformed_tool_stream())
                    run_id = f"conformance-malformed-{case.name}"
                    with Harness(
                        case.model(server, stream=True),
                        (Tool("guarded", guarded, effect="read_only"),),
                    ) as harness:
                        with self.assertRaises(ModelCallError) as raised:
                            await harness.run(
                                (Message("user", "invoke the guarded tool"),),
                                run_id=run_id,
                            )
                        events = self._event_types(harness, run_id)
                    self.assertIsNotNone(
                        exception_in_chain(raised.exception, ProviderProtocolError)
                    )
                    self.assertEqual(called, 0)
                    self.assertNotIn("model.completed", events)
                    self.assertFalse(any(item.startswith("tool.") for item in events))
                    self.assertEqual(events[-2:], ["model.failed", "run.failed"])
                    self._assert_request(case, server)
                self.assertFalse(server.thread.is_alive())

    async def test_timeout_is_retryable_distinct_and_releases_resources(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                gate = threading.Event()
                server = LoopbackProviderServer()
                with server:
                    body = server.queue_sse(
                        case.interrupted_stream(),
                        body_gate=gate,
                        declared_length=4096,
                    )
                    self.assertLess(len(body), 4096)
                    model = case.model(server, stream=True, timeout=0.5)
                    baseline_tasks = asyncio.all_tasks()
                    task = asyncio.create_task(
                        model.complete((Message("user", "wait"),), ())
                    )
                    try:
                        self.assertTrue(await wait_for_event(server.headers_sent))
                        with self.assertRaises(ProviderTimeoutError) as raised:
                            await task
                    finally:
                        gate.set()
                    error = raised.exception
                    self.assertNotIsInstance(error, asyncio.CancelledError)
                    self.assertEqual(error.provider, case.error_provider)
                    self.assertTrue(error.retryable)
                    self.assertTrue(await wait_for_event(server.response_finished))
                    self.assertTrue(await wait_for_event(server.idle))
                    self.assertEqual(server.active_handlers, 0)
                    self.assertEqual(
                        await self._wait_for_spawned_tasks(baseline_tasks),
                        set(),
                    )
                    self._assert_request(case, server)
                self.assertFalse(server.thread.is_alive())

    async def test_rate_limit_metadata_is_stable_without_hidden_retry(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                server = LoopbackProviderServer()
                with server:
                    server.queue_json(
                        case.rate_limit_body(),
                        status=429,
                        headers={
                            "x-request-id": f"request-{case.name}",
                            "Retry-After": "2.5",
                        },
                    )
                    with self.assertRaises(ProviderRateLimitError) as raised:
                        await case.model(server).complete(
                            (Message("user", "rate limited"),), ()
                        )
                    error = raised.exception
                    self.assertEqual(error.provider, case.error_provider)
                    self.assertEqual(error.status, 429)
                    self.assertEqual(error.status_code, 429)
                    self.assertEqual(error.request_id, f"request-{case.name}")
                    self.assertEqual(error.retry_after, 2.5)
                    self.assertEqual(error.code, case.rate_limit_code)
                    self.assertTrue(error.retryable)
                    self._assert_request(case, server)
                self.assertFalse(server.thread.is_alive())

    async def test_interrupted_sse_is_retryable_and_never_completes(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                called = 0

                def guarded(value: str) -> str:
                    nonlocal called
                    called += 1
                    return value

                server = LoopbackProviderServer()
                with server:
                    tool = Tool("guarded", guarded, effect="read_only")
                    server.queue_sse(case.interrupted_stream())
                    server.queue_sse(case.interrupted_stream())
                    with self.assertRaises(ProviderIncompleteError) as direct:
                        await case.model(server, stream=True).complete(
                            (Message("user", "stream a tool call"),), (tool,)
                        )
                    self.assertEqual(direct.exception.code, "interrupted_stream")
                    self.assertEqual(
                        direct.exception.provider, case.error_provider
                    )
                    self.assertTrue(direct.exception.retryable)
                    run_id = f"conformance-interrupted-{case.name}"
                    with Harness(
                        case.model(server, stream=True),
                        (tool,),
                    ) as harness:
                        with self.assertRaises(ModelCallError) as raised:
                            await harness.run(
                                (Message("user", "stream a tool call"),),
                                run_id=run_id,
                            )
                        events = self._event_types(harness, run_id)
                    incomplete = exception_in_chain(
                        raised.exception, ProviderIncompleteError
                    )
                    self.assertIsNotNone(incomplete)
                    self.assertEqual(incomplete.code, "interrupted_stream")
                    self.assertEqual(incomplete.provider, case.error_provider)
                    self.assertTrue(incomplete.retryable)
                    self.assertEqual(called, 0)
                    self.assertNotIn("model.completed", events)
                    self.assertFalse(any(item.startswith("tool.") for item in events))
                    self.assertEqual(events[-2:], ["model.failed", "run.failed"])
                    self._assert_request(case, server, expected_count=2)
                self.assertFalse(server.thread.is_alive())

    async def test_duplicate_tool_calls_fail_closed_in_the_shared_loop(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                called = 0

                def guarded(value: str) -> str:
                    nonlocal called
                    called += 1
                    return value

                server = LoopbackProviderServer()
                with server:
                    server.queue_sse(case.duplicate_tool_stream())
                    run_id = f"conformance-duplicate-{case.name}"
                    with Harness(
                        case.model(server, stream=True),
                        (Tool("guarded", guarded, effect="read_only"),),
                    ) as harness:
                        with self.assertRaises(ModelCallError) as raised:
                            await harness.run(
                                (Message("user", "invoke once"),), run_id=run_id
                            )
                        events = self._event_types(harness, run_id)
                    protocol = exception_in_chain(
                        raised.exception, ProviderProtocolError
                    )
                    self.assertIsNotNone(protocol)
                    self.assertEqual(protocol.provider, case.error_provider)
                    self.assertEqual(called, 0)
                    self.assertNotIn("model.completed", events)
                    self.assertFalse(any(item.startswith("tool.") for item in events))
                    self.assertEqual(events[-2:], ["model.failed", "run.failed"])
                    self._assert_request(case, server)
                self.assertFalse(server.thread.is_alive())

    async def test_cancellation_propagates_and_commits_a_cancelled_checkpoint(self) -> None:
        for case in PROVIDER_FACTORIES:
            with self.subTest(provider=case.name):
                called = 0

                def guarded(value: str) -> str:
                    nonlocal called
                    called += 1
                    return value

                gate = threading.Event()
                loop = asyncio.get_running_loop()
                loop_errors: list[dict[str, object]] = []
                previous_handler = loop.get_exception_handler()
                server = LoopbackProviderServer()
                with server:
                    server.queue_sse(
                        case.interrupted_stream(),
                        body_gate=gate,
                        declared_length=4096,
                    )
                    run_id = f"conformance-cancel-{case.name}"
                    with Harness(
                        case.model(server, stream=True, timeout=3.0),
                        (Tool("guarded", guarded, effect="read_only"),),
                    ) as harness:
                        baseline_tasks = asyncio.all_tasks()
                        loop.set_exception_handler(
                            lambda current_loop, context: loop_errors.append(context)
                        )
                        try:
                            task = asyncio.create_task(
                                harness.run(
                                    (Message("user", "cancel this run"),),
                                    run_id=run_id,
                                )
                            )
                            self.assertTrue(await wait_for_event(server.headers_sent))
                            cancellation_marker = f"cancel-{case.name}"
                            task.cancel(cancellation_marker)
                            with self.assertRaises(asyncio.CancelledError) as raised:
                                await task
                            self.assertEqual(
                                raised.exception.args, (cancellation_marker,)
                            )
                            events = self._event_types(harness, run_id)
                            checkpoint = harness.store.load(run_id)
                        finally:
                            gate.set()
                            try:
                                response_finished = await wait_for_event(
                                    server.response_finished
                                )
                                server_idle = await wait_for_event(server.idle)
                            finally:
                                loop.set_exception_handler(previous_handler)
                    self.assertTrue(response_finished)
                    self.assertTrue(server_idle)
                    self.assertEqual(
                        events,
                        ["run.started", "model.started", "run.cancelled"],
                    )
                    self.assertEqual(checkpoint.status, "cancelled")
                    self.assertEqual(called, 0)
                    self.assertEqual(server.active_handlers, 0)
                    self.assertEqual(loop_errors, [])
                    self.assertEqual(
                        await self._wait_for_spawned_tasks(baseline_tasks),
                        set(),
                    )
                    self._assert_request(case, server)
                self.assertFalse(server.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
