import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    ApprovalConflict,
    ApprovalMismatch,
    ConcurrentRunError,
    DuplicateToolCallError,
    Harness,
    InjectedFault,
    Message,
    ModelReply,
    RunCancelled,
    RunPaused,
    SchemaVersionError,
    SQLiteStore,
    Tool,
    ToolCall,
)


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


class CrashOnce:
    def __init__(self, point):
        self.point = point
        self.fired = False

    def __call__(self, point):
        if point == self.point and not self.fired:
            self.fired = True
            raise InjectedFault(point)


class TrustTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "trust.sqlite3"

    def _harness(self, *args, **kwargs):
        return self.enterContext(Harness(*args, **kwargs))

    def _store(self):
        return self.enterContext(SQLiteStore(self.db_path))

    async def test_provider_state_is_opaque_durable_and_not_public_event_data(self):
        provider_state = '{"provider":"openai.responses","version":1,"output":[]}'
        store = self._store()
        result = await self._harness(
            ScriptedModel(ModelReply(content="done", provider_state=provider_state)),
            store=store,
        ).run((Message("user", "finish"),), run_id="provider-state")

        self.assertEqual(result.final_message.provider_state, provider_state)
        store.close()
        reopened = self._store()
        state = reopened.load("provider-state")
        self.assertEqual(state.history[-1].provider_state, provider_state)
        self.assertEqual(state.final_message.provider_state, provider_state)
        self.assertNotIn(provider_state, json.dumps([
            {"type": event.type, "data": dict(event.data)}
            for event in reopened.events("provider-state")
        ]))
        with self.assertRaises(TypeError):
            ModelReply(provider_state={})
        reopened.close()

    async def test_non_read_only_tools_require_explicit_revision(self):
        with self.assertRaises(ValueError):
            Tool("write", lambda: None)
        Tool("read", lambda: None, effect="read_only")

    async def test_approval_is_fingerprint_bound_and_decision_is_cas(self):
        effects = []

        def write(value, nested):
            effects.append(value)
            return "written"

        def final(messages):
            self.assertEqual(messages[-1].content, "written")
            return ModelReply(content="done")

        store = self._store()
        harness = self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write-1",
                            "write",
                            {"value": 7, "nested": {"items": [1, 2]}},
                        ),
                    )
                ),
                final,
            ),
            (Tool("write", write, tool_revision="1"),),
            store=store,
        )
        with self.assertRaises(RunPaused) as paused:
            await harness.run((Message("user", "write"),), run_id="approval")
        request = paused.exception.request
        self.assertEqual(request.arguments["value"], 7)
        self.assertEqual(request.tool_revision, "1")
        self.assertEqual(request.arguments["nested"]["items"], (1, 2))
        with self.assertRaises(TypeError):
            request.arguments["nested"]["changed"] = True
        with self.assertRaises(ApprovalMismatch):
            harness.resolve_approval("approval", "0" * 64, True)

        harness.resolve_approval("approval", request.fingerprint, True)
        harness.resolve_approval("approval", request.fingerprint, True)
        with self.assertRaises(ApprovalConflict):
            harness.resolve_approval("approval", request.fingerprint, False)
        result = await harness.resume("approval")

        self.assertEqual(effects, [7])
        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(
            [event.type for event in result.events if event.type.startswith("approval.")],
            ["approval.requested", "approval.resolved"],
        )
        store.close()

    async def test_denied_tool_result_is_model_visible_and_never_dispatched(self):
        effects = []

        def forbidden():
            effects.append("called")

        def final(messages):
            self.assertEqual(messages[-1].error_code, "approval_denied")
            return ModelReply(content="denied safely")

        harness = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("danger", "forbidden"),)), final
            ),
            (Tool("forbidden", forbidden, tool_revision="1"),),
        )
        with self.assertRaises(RunPaused) as paused:
            await harness.run((Message("user", "do not"),), run_id="denied")
        harness.resolve_approval(
            "denied", paused.exception.request.fingerprint, False
        )
        result = await harness.resume("denied")

        self.assertEqual(effects, [])
        self.assertEqual(result.final_message.content, "denied safely")
        self.assertNotIn(
            "tool.started", [event.type for event in result.events]
        )

    async def test_committed_accepted_reply_resumes_without_recalling_model_step(self):
        first_model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("read", "read", {"value": 3}),))
        )
        store = self._store()
        with self.assertRaises(InjectedFault):
            await self._harness(
                first_model,
                (Tool("read", lambda value: value, effect="read_only"),),
                store=store,
                fault_injector=CrashOnce("after_model_reply_commit"),
            ).run((Message("user", "read"),), run_id="accepted")
        self.assertEqual(first_model.calls, 1)
        store.close()

        def final(messages):
            self.assertEqual(messages[-1].content, "3")
            return ModelReply(content="recovered")

        recovery_model = ScriptedModel(final)
        reopened = self._store()
        result = await self._harness(
            recovery_model,
            (Tool("read", lambda value: value, effect="read_only"),),
            store=reopened,
        ).resume("accepted")

        self.assertEqual(recovery_model.calls, 1)
        self.assertEqual(result.final_message.content, "recovered")
        reopened.close()

    async def test_committed_tool_result_is_reused_without_redispatch(self):
        effects = []

        def read():
            effects.append("read")
            return "value"

        store = self._store()
        with self.assertRaises(InjectedFault):
            await self._harness(
                ScriptedModel(ModelReply(tool_calls=(ToolCall("read", "read"),))),
                (Tool("read", read, effect="read_only"),),
                store=store,
                fault_injector=CrashOnce("after_tool_result_commit"),
            ).run((Message("user", "read"),), run_id="result")
        self.assertEqual(effects, ["read"])
        store.close()

        reopened = self._store()
        result = await self._harness(
            ScriptedModel(ModelReply(content="final")),
            (Tool("read", read, effect="read_only"),),
            store=reopened,
        ).resume("result")
        self.assertEqual(effects, ["read"])
        self.assertEqual(result.final_message.content, "final")
        reopened.close()

    async def test_plain_side_effect_becomes_manual_after_ambiguous_crash(self):
        effects = []

        def send():
            effects.append("sent")
            return "sent"

        store = self._store()
        harness = self._harness(
            ScriptedModel(ModelReply(tool_calls=(ToolCall("send", "send"),))),
            (Tool("send", send, tool_revision="1"),),
            store=store,
            fault_injector=CrashOnce("after_tool_return"),
        )
        with self.assertRaises(RunPaused) as approval:
            await harness.run((Message("user", "send"),), run_id="unknown")
        harness.resolve_approval(
            "unknown", approval.exception.request.fingerprint, True
        )
        with self.assertRaises(InjectedFault):
            await harness.resume("unknown")
        self.assertEqual(effects, ["sent"])
        store.close()

        reopened = self._store()
        recovery = self._harness(
            ScriptedModel(ModelReply(content="manually recovered")),
            (Tool("send", send, tool_revision="1"),),
            store=reopened,
        )
        with self.assertRaises(RunPaused) as paused:
            await recovery.resume("unknown")
        self.assertEqual(paused.exception.reason, "effect_unknown")
        self.assertEqual(effects, ["sent"])
        self.assertNotIn(
            "run.completed",
            [event.type for event in reopened.events("unknown")],
        )
        recovery.resolve_effect(
            "unknown",
            approval.exception.request.fingerprint,
            "record_result",
            reason="operator confirmed the send receipt",
            result="sent",
        )
        result = await recovery.resume("unknown")
        self.assertEqual(result.final_message.content, "manually recovered")
        self.assertEqual(effects, ["sent"])
        reopened.close()

    async def test_manual_fail_and_retry_are_explicit_and_audited(self):
        failed_effects = []

        def final_after_failure(messages):
            self.assertEqual(messages[-1].error_code, "manual_recovery_failed")
            return ModelReply(content="failure recorded")

        failed = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("fail-call", "write"),)),
                final_after_failure,
            ),
            (
                Tool(
                    "write",
                    lambda: failed_effects.append("write"),
                    tool_revision="1",
                ),
            ),
            fault_injector=CrashOnce("after_tool_dispatch"),
        )
        with self.assertRaises(RunPaused) as approval:
            await failed.run((Message("user", "write"),), run_id="manual-fail")
        fingerprint = approval.exception.request.fingerprint
        failed.resolve_approval("manual-fail", fingerprint, True)
        with self.assertRaises(InjectedFault):
            await failed.resume("manual-fail")
        with self.assertRaises(RunPaused):
            await failed.resume("manual-fail")
        failed.resolve_effect(
            "manual-fail",
            fingerprint,
            "fail",
            reason="operator verified the write was not accepted",
        )
        failed_result = await failed.resume("manual-fail")
        self.assertEqual(failed_effects, [])
        self.assertEqual(failed_result.final_message.content, "failure recorded")

        retried_effects = []
        retried = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("retry-call", "write"),)),
                ModelReply(content="retry completed"),
            ),
            (
                Tool(
                    "write",
                    lambda: retried_effects.append("write") or "ok",
                    tool_revision="1",
                ),
            ),
            fault_injector=CrashOnce("after_tool_dispatch"),
        )
        with self.assertRaises(RunPaused) as approval:
            await retried.run((Message("user", "write"),), run_id="manual-retry")
        fingerprint = approval.exception.request.fingerprint
        retried.resolve_approval("manual-retry", fingerprint, True)
        with self.assertRaises(InjectedFault):
            await retried.resume("manual-retry")
        with self.assertRaises(RunPaused):
            await retried.resume("manual-retry")
        retried.resolve_effect(
            "manual-retry",
            fingerprint,
            "retry",
            reason="operator verified the first dispatch never reached the target",
        )
        retry_result = await retried.resume("manual-retry")
        self.assertEqual(retried_effects, ["write"])
        self.assertEqual(retry_result.final_message.content, "retry completed")
        self.assertEqual(
            [
                event.data["action"]
                for event in retry_result.events
                if event.type == "recovery.resolved"
            ],
            ["retry"],
        )

    async def test_model_cannot_override_reserved_idempotency_key(self):
        effects = []

        def send(order_id, *, idempotency_key):
            effects.append(idempotency_key)

        def final(messages):
            self.assertEqual(messages[-1].error_code, "reserved_argument")
            return ModelReply(content="rejected")

        result = await self._harness(
            ScriptedModel(
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "reserved",
                            "send",
                            {"order_id": "O-2", "idempotency_key": "attacker"},
                        ),
                    )
                ),
                final,
            ),
            (
                Tool(
                    "send",
                    send,
                    effect="idempotent",
                    idempotency_key=lambda arguments: f"send:{arguments['order_id']}",
                    tool_revision="1",
                ),
            ),
        ).run((Message("user", "send"),), run_id="reserved")
        self.assertEqual(effects, [])
        self.assertEqual(result.final_message.content, "rejected")

    async def test_side_effect_timeout_and_cancellation_pause_without_model_progress(self):
        async def slow():
            await asyncio.Event().wait()

        timeout_model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("slow", "slow"),)),
            ModelReply(content="must not run"),
        )
        timeout_harness = self._harness(
            timeout_model,
            (Tool("slow", slow, tool_revision="1"),),
            tool_timeout=0.01,
        )
        with self.assertRaises(RunPaused) as approval:
            await timeout_harness.run(
                (Message("user", "slow"),), run_id="side-timeout"
            )
        timeout_harness.resolve_approval(
            "side-timeout", approval.exception.request.fingerprint, True
        )
        with self.assertRaises(RunPaused) as timed_out:
            await timeout_harness.resume("side-timeout")
        self.assertEqual(timed_out.exception.reason, "effect_unknown")
        self.assertEqual(timeout_model.calls, 1)

        started = asyncio.Event()

        async def cancellable():
            started.set()
            await asyncio.Event().wait()

        cancel_model = ScriptedModel(
            ModelReply(tool_calls=(ToolCall("cancel", "cancellable"),)),
            ModelReply(content="must not run"),
        )
        cancel_harness = self._harness(
            cancel_model,
            (Tool("cancellable", cancellable, tool_revision="1"),),
        )
        with self.assertRaises(RunPaused) as approval:
            await cancel_harness.run(
                (Message("user", "cancel"),), run_id="side-cancel"
            )
        cancel_harness.resolve_approval(
            "side-cancel", approval.exception.request.fingerprint, True
        )
        task = asyncio.create_task(cancel_harness.resume("side-cancel"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancel_model.calls, 1)
        self.assertEqual(cancel_harness.store.load("side-cancel").status, "cancelled")
        self.assertEqual(
            cancel_harness.store.events("side-cancel")[-1].type, "run.cancelled"
        )
        with self.assertRaises(RunCancelled):
            await cancel_harness.resume("side-cancel")
        self.assertNotIn(
            "run.completed",
            [event.type for event in cancel_harness.store.events("side-cancel")],
        )

    async def test_idempotent_recovery_reuses_same_key_and_one_external_effect(self):
        attempts = []
        external_effects = set()

        def send(order_id, *, idempotency_key):
            attempts.append(idempotency_key)
            external_effects.add(idempotency_key)
            return "sent"

        key = lambda arguments: f"send:{arguments['order_id']}"
        store = self._store()
        harness = self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("send", "send", {"order_id": "O-1"}),))
            ),
            (
                Tool(
                    "send",
                    send,
                    effect="idempotent",
                    idempotency_key=key,
                    tool_revision="1",
                ),
            ),
            store=store,
            fault_injector=CrashOnce("after_tool_return"),
        )
        with self.assertRaises(RunPaused) as approval:
            await harness.run((Message("user", "send"),), run_id="idempotent")
        harness.resolve_approval(
            "idempotent", approval.exception.request.fingerprint, True
        )
        with self.assertRaises(InjectedFault):
            await harness.resume("idempotent")
        store.close()

        reopened = self._store()
        result = await self._harness(
            ScriptedModel(ModelReply(content="done")),
            (
                Tool(
                    "send",
                    send,
                    effect="idempotent",
                    idempotency_key=key,
                    tool_revision="1",
                ),
            ),
            store=reopened,
        ).resume("idempotent")

        self.assertEqual(attempts, ["send:O-1", "send:O-1"])
        self.assertEqual(external_effects, {"send:O-1"})
        self.assertEqual(result.final_message.content, "done")
        keys = {
            event.data.get("idempotency_key")
            for event in result.events
            if event.type == "tool.started"
        }
        self.assertEqual(keys, {"send:O-1"})
        reopened.close()

    async def test_duplicate_provider_call_id_rejects_whole_reply(self):
        effects = []

        def read(value):
            effects.append(value)

        with self.assertRaises(DuplicateToolCallError):
            await self._harness(
                ScriptedModel(
                    ModelReply(
                        tool_calls=(
                            ToolCall("duplicate", "read", {"value": 1}),
                            ToolCall("duplicate", "read", {"value": 2}),
                        )
                    )
                ),
                (Tool("read", read, effect="read_only"),),
            ).run((Message("user", "duplicate"),), run_id="duplicate")
        self.assertEqual(effects, [])

    async def test_final_commit_resume_returns_same_result_without_model(self):
        store = self._store()
        model = ScriptedModel(ModelReply(content="durable final"))
        with self.assertRaises(InjectedFault):
            await self._harness(
                model,
                store=store,
                fault_injector=CrashOnce("after_final_commit"),
            ).run((Message("user", "finish"),), run_id="final")
        self.assertEqual(model.calls, 1)
        store.close()

        class ForbiddenModel:
            async def complete(self, messages, tools):
                raise AssertionError("completed run must not call the model")

        reopened = self._store()
        result = await self._harness(
            ForbiddenModel(), store=reopened
        ).resume("final")
        self.assertEqual(result.final_message.content, "durable final")
        self.assertEqual(
            [event.type for event in result.events].count("run.completed"), 1
        )
        reopened.close()

    async def test_sink_failure_cannot_change_durable_or_effect_facts(self):
        effects = []

        def sink(event):
            raise RuntimeError("observer offline")

        store = self._store()
        result = await self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("read", "read"),)),
                ModelReply(content="done"),
            ),
            (Tool("read", lambda: effects.append("read") or "ok", effect="read_only"),),
            store=store,
            event_sink=sink,
        ).run((Message("user", "read"),), run_id="sink")

        self.assertEqual(effects, ["read"])
        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(store.load("sink").status, "completed")
        self.assertGreater(store.counts("sink")["checkpoints"], 1)
        store.close()

    async def test_trace_is_append_only_and_future_schema_is_rejected(self):
        store = self._store()
        await self._harness(
            ScriptedModel(ModelReply(content="done")), store=store
        ).run((Message("user", "finish"),), run_id="append-only")
        store.close()

        database = sqlite3.connect(self.db_path)
        with self.assertRaises(sqlite3.DatabaseError):
            database.execute("UPDATE events SET event_json = '{}' ")
        database.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            database.execute("DELETE FROM checkpoints")
        database.rollback()
        database.execute("PRAGMA user_version = 5")
        database.close()

        with self.assertRaises(SchemaVersionError):
            SQLiteStore(self.db_path)

    async def test_file_store_rejects_a_second_writer_connection(self):
        owner = self._store()
        await self._harness(
            ScriptedModel(ModelReply(content="owner")), store=owner
        ).run((Message("user", "own"),), run_id="owner")
        with self.assertRaises(ConcurrentRunError):
            SQLiteStore(self.db_path)
        owner.close()
        reopened = self._store()
        self.assertEqual(reopened.load("owner").status, "completed")
        reopened.close()

    async def test_v1_store_migrates_legacy_calls_and_preserves_trace(self):
        database = sqlite3.connect(self.db_path)
        database.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                generation INTEGER NOT NULL, status TEXT NOT NULL,
                step INTEGER NOT NULL, history_json TEXT NOT NULL,
                accepted_reply_json TEXT, final_message_json TEXT
            );
            CREATE TABLE events (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                event_json TEXT NOT NULL, PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE checkpoints (
                run_id TEXT NOT NULL, generation INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL, PRIMARY KEY (run_id, generation)
            );
            CREATE TABLE tool_calls (
                run_id TEXT NOT NULL, step INTEGER NOT NULL,
                ordinal INTEGER NOT NULL, call_id TEXT,
                fingerprint TEXT NOT NULL UNIQUE, name TEXT,
                arguments_json TEXT NOT NULL, arguments_valid INTEGER NOT NULL,
                complete INTEGER NOT NULL, effect TEXT NOT NULL,
                idempotency_key TEXT, status TEXT NOT NULL, result_json TEXT,
                PRIMARY KEY (run_id, step, ordinal)
            );
            INSERT INTO runs VALUES (
                'legacy', 1, 1, 'processing_reply', 1, '[]', NULL, NULL
            );
            INSERT INTO checkpoints VALUES ('legacy', 1, '{}');
            INSERT INTO tool_calls VALUES (
                'legacy', 1, 0, 'legacy-call', 'legacy-fingerprint', 'write',
                '{}', 1, 1, 'side_effecting', NULL, 'approved', NULL
            );
            PRAGMA user_version = 1;
            """
        )
        database.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (
                "legacy",
                1,
                json.dumps(
                    {
                        "type": "run.started",
                        "run_id": "legacy",
                        "step": 0,
                        "data": {},
                        "version": 1,
                        "tool_name": None,
                        "call_id": None,
                    }
                ),
            ),
        )
        database.commit()
        database.close()

        effects = []
        store = self._store()
        self.assertEqual(store.calls("legacy", 1)[0].tool_revision, "legacy-unversioned")
        self.assertEqual(store.stored_events("legacy")[0].event.type, "run.started")
        with self.assertRaises(RunPaused) as paused:
            await self._harness(
                ScriptedModel(),
                (
                    Tool(
                        "write",
                        lambda: effects.append("write"),
                        tool_revision="2",
                    ),
                ),
                store=store,
            ).resume("legacy")
        self.assertEqual(paused.exception.reason, "tool_contract_changed")
        self.assertEqual(effects, [])
        store.close()
        database = sqlite3.connect(self.db_path)
        self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 4)
        database.close()

    async def test_approved_call_cannot_run_after_tool_revision_changes(self):
        old_effects = []
        store = self._store()
        old = self._harness(
            ScriptedModel(ModelReply(tool_calls=(ToolCall("write", "write"),))),
            (
                Tool(
                    "write",
                    lambda: old_effects.append("old"),
                    tool_revision="revision-1",
                ),
            ),
            store=store,
            fault_injector=CrashOnce("before_tool_dispatch"),
        )
        with self.assertRaises(RunPaused) as approval:
            await old.run((Message("user", "write"),), run_id="revision-change")
        self.assertEqual(approval.exception.request.tool_revision, "revision-1")
        old.resolve_approval(
            "revision-change", approval.exception.request.fingerprint, True
        )
        with self.assertRaises(InjectedFault):
            await old.resume("revision-change")
        self.assertEqual(old_effects, [])
        store.close()

        new_effects = []
        reopened = self._store()
        with self.assertRaises(RunPaused) as paused:
            await self._harness(
                ScriptedModel(),
                (
                    Tool(
                        "write",
                        lambda: new_effects.append("new"),
                        tool_revision="revision-2",
                    ),
                ),
                store=reopened,
            ).resume("revision-change")
        self.assertEqual(paused.exception.reason, "tool_contract_changed")
        self.assertEqual(new_effects, [])
        self.assertEqual(
            [
                event.data.get("error_code")
                for event in reopened.events("revision-change")
                if event.type == "tool.failed"
            ],
            ["tool_contract_changed"],
        )
        reopened.close()

    async def test_completed_sink_cancellation_cannot_rewrite_terminal_state(self):
        def sink(event):
            if event.type == "run.completed":
                raise asyncio.CancelledError

        harness = self._harness(
            ScriptedModel(ModelReply(content="done")), event_sink=sink
        )
        result = await harness.run((Message("user", "finish"),), run_id="sink-cancel")
        self.assertEqual(result.final_message.content, "done")
        self.assertEqual(harness.store.load("sink-cancel").status, "completed")
        self.assertNotIn(
            "run.cancelled",
            [event.type for event in harness.store.events("sink-cancel")],
        )

        faulted = self._harness(
            ScriptedModel(ModelReply(content="committed")),
            fault_injector=lambda point: (
                (_ for _ in ()).throw(asyncio.CancelledError())
                if point == "after_final_commit"
                else None
            ),
        )
        with self.assertRaises(asyncio.CancelledError):
            await faulted.run((Message("user", "finish"),), run_id="terminal-cancel")
        self.assertEqual(faulted.store.load("terminal-cancel").status, "completed")
        self.assertNotIn(
            "run.cancelled",
            [event.type for event in faulted.store.events("terminal-cancel")],
        )

    async def test_cancelled_sync_idempotent_late_effect_is_unknown_and_auditable(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        effects = []

        def send(*, idempotency_key):
            started.set()
            release.wait(1)
            effects.append(idempotency_key)
            finished.set()
            return "sent"

        key = lambda arguments: "stable-send-key"
        harness = self._harness(
            ScriptedModel(ModelReply(tool_calls=(ToolCall("send", "send"),))),
            (
                Tool(
                    "send",
                    send,
                    effect="idempotent",
                    idempotency_key=key,
                    tool_revision="1",
                ),
            ),
        )
        with self.assertRaises(RunPaused) as approval:
            await harness.run((Message("user", "send"),), run_id="late-effect")
        fingerprint = approval.exception.request.fingerprint
        harness.resolve_approval("late-effect", fingerprint, True)
        task = asyncio.create_task(harness.resume("late-effect"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))

        self.assertEqual(effects, ["stable-send-key"])
        self.assertEqual(harness.store.load("late-effect").status, "cancelled")
        self.assertEqual(harness.store.calls("late-effect", 1)[0].status, "effect_unknown")
        cancelled = harness.store.events("late-effect")[-1]
        self.assertEqual(cancelled.type, "run.cancelled")
        self.assertTrue(cancelled.data["effect_unknown"])
        harness.resolve_effect(
            "late-effect",
            fingerprint,
            "record_result",
            reason="operator confirmed the late send receipt",
            result="sent",
        )
        self.assertEqual(harness.store.load("late-effect").status, "cancelled")
        with self.assertRaises(RunCancelled):
            await harness.resume("late-effect")
        self.assertNotIn(
            "run.completed",
            [event.type for event in harness.store.events("late-effect")],
        )

    async def test_durable_event_cursor_recovers_best_effort_sink_gap(self):
        observed = []
        harness = self._harness(
            ScriptedModel(ModelReply(content="done")),
            event_sink=observed.append,
            fault_injector=CrashOnce("after_checkpoint_commit"),
        )
        with self.assertRaises(InjectedFault):
            await harness.run((Message("user", "finish"),), run_id="cursor")
        self.assertEqual(observed, [])

        head = harness.stored_events("cursor")
        self.assertEqual([(item.seq, item.event.type) for item in head], [(1, "run.started")])
        harness.fault_injector = None
        result = await harness.resume("cursor")
        tail = harness.stored_events("cursor", after_seq=head[-1].seq)
        combined = head + tail
        self.assertEqual(
            [item.event.type for item in combined],
            [event.type for event in result.events],
        )
        self.assertEqual(
            len({(item.event.run_id, item.seq) for item in combined}), len(combined)
        )
        self.assertEqual(
            harness.stored_events("cursor", after_seq=combined[-1].seq), ()
        )

    async def test_fault_injector_exposes_all_commit_boundaries(self):
        points = []
        result = await self._harness(
            ScriptedModel(
                ModelReply(tool_calls=(ToolCall("read", "read"),)),
                ModelReply(content="done"),
            ),
            (Tool("read", lambda: "ok", effect="read_only"),),
            fault_injector=points.append,
        ).run((Message("user", "read"),), run_id="points")

        self.assertEqual(result.final_message.content, "done")
        self.assertTrue(
            {
                "before_model_reply_commit",
                "after_model_reply_commit",
                "before_tool_dispatch",
                "after_tool_dispatch",
                "after_tool_return",
                "before_tool_result_commit",
                "after_tool_result_commit",
                "before_checkpoint_commit",
                "after_checkpoint_commit",
                "before_final_commit",
                "after_final_commit",
            }.issubset(points)
        )
