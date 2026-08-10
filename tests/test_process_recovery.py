import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, RunPaused, SQLiteStore, Tool  # noqa: E402
from sasori_flow import (  # noqa: E402
    InputRef,
    InputSlot,
    ToolStep,
    WorkflowSpec,
    compile_workflow,
    json_sha256,
)


WORKER = textwrap.dedent(
    """
    import asyncio
    import os
    import sys
    from pathlib import Path

    from sasori import Harness, Message, ModelReply, RunPaused, SQLiteStore, Tool, ToolCall

    database, marker, mode = sys.argv[1:]

    class ToolModel:
        async def complete(self, messages, tools):
            return ModelReply(tool_calls=(ToolCall("send-1", "send"),))

    class FinalModel:
        async def complete(self, messages, tools):
            return ModelReply(content="durable final")

    def effect():
        with Path(marker).open("a", encoding="utf-8") as stream:
            stream.write("sent\\n")
            stream.flush()
            os.fsync(stream.fileno())
        return "sent"

    def crash(point):
        if point == mode:
            os._exit(72)

    async def main():
        store = SQLiteStore(database)
        if mode == "after_final_commit":
            await Harness(
                FinalModel(), store=store, fault_injector=crash
            ).run((Message("user", "finish"),), run_id="process-final")
            raise AssertionError("final crash point did not fire")

        harness = Harness(
            ToolModel(),
            (Tool("send", effect, tool_revision="1"),),
            store=store,
            fault_injector=crash,
        )
        try:
            await harness.run((Message("user", "send"),), run_id="process-effect")
        except RunPaused as paused:
            harness.resolve_approval(
                "process-effect", paused.request.fingerprint, True
            )
        await harness.resume("process-effect")
        raise AssertionError("tool crash point did not fire")

    asyncio.run(main())
    """
)


WORKFLOW_WORKER = textwrap.dedent(
    """
    import asyncio
    import os
    import sys
    from pathlib import Path

    from sasori import Harness, ModelReply, RunPaused, SQLiteStore, Tool
    from sasori_flow import InputRef, InputSlot, ToolStep, WorkflowSpec, compile_workflow

    database, attempts_path, effects_path, mode = sys.argv[1:]
    target = {
        "side_dispatch": "after_tool_dispatch",
        "side_return": "after_tool_return",
        "idempotent_return": "after_tool_return",
        "final_commit": "after_final_commit",
    }[mode]

    class UnusedModel:
        async def complete(self, messages, tools):
            raise AssertionError("workflow compiler must replace the base model")

    def append(path, value):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(value + "\\n")
            stream.flush()
            os.fsync(stream.fileno())

    def side(text: str) -> str:
        append(effects_path, "effect:" + text)
        return "done:" + text

    def remember(text: str, *, idempotency_key: str) -> str:
        append(attempts_path, idempotency_key)
        path = Path(effects_path)
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if idempotency_key not in existing:
            append(effects_path, idempotency_key)
        return "remembered:" + text

    def read(text: str) -> str:
        append(attempts_path, "read:" + text)
        return "read:" + text

    def crash(point):
        if point == target:
            os._exit(72)

    async def main():
        if mode.startswith("side_"):
            tool = Tool("side", side, effect="side_effecting", tool_revision="1")
            workflow_id = "process-side"
            run_id = "WorkflowProcessSide"
        elif mode == "idempotent_return":
            tool = Tool(
                "remember",
                remember,
                effect="idempotent",
                idempotency_key=lambda arguments: "business:" + str(arguments["text"]),
                tool_revision="1",
            )
            workflow_id = "process-idempotent"
            run_id = "WorkflowProcessIdempotent"
        else:
            tool = Tool("read", read, effect="read_only")
            workflow_id = "process-final"
            run_id = "WorkflowProcessFinal"
        spec = WorkflowSpec(
            workflow_id,
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "step",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "step",
        )
        store = SQLiteStore(database)
        base = Harness(UnusedModel(), (tool,), store=store, fault_injector=crash)
        workflow = compile_workflow(spec, base)
        if tool.effect == "read_only":
            await workflow.run({"text": "same"}, run_id=run_id)
            raise AssertionError("final crash point did not fire")
        try:
            await workflow.run({"text": "same"}, run_id=run_id)
        except RunPaused as paused:
            workflow.resolve_approval(run_id, paused.request.fingerprint, True)
        await workflow.resume(run_id)
        raise AssertionError("tool crash point did not fire")

    asyncio.run(main())
    """
)


class ForbiddenModel:
    async def complete(self, messages, tools):
        raise AssertionError("recovery must not call the model")


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    def tearDown(self):
        self.temp.cleanup()

    def run_worker(self, database, marker, point):
        completed = subprocess.run(
            [sys.executable, "-c", WORKER, str(database), str(marker), point],
            cwd=Path(__file__).parents[1],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            72,
            completed.stdout + completed.stderr,
        )

    def run_workflow_worker(self, database, attempts, effects, mode):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                WORKFLOW_WORKER,
                str(database),
                str(attempts),
                str(effects),
                mode,
            ],
            cwd=Path(__file__).parents[1],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            72,
            completed.stdout + completed.stderr,
        )

    def workflow(self, store, tool, workflow_id):
        spec = WorkflowSpec(
            workflow_id,
            "1",
            (InputSlot("text", "string"),),
            (
                ToolStep.from_tool(
                    "step",
                    tool,
                    {"text": InputRef("text")},
                    result_type="string",
                ),
            ),
            "step",
        )
        return spec, compile_workflow(
            spec,
            Harness(ForbiddenModel(), (tool,), store=store),
        )

    def test_real_process_exit_never_replays_ambiguous_side_effect(self):
        for point, expected_effects in (
            ("after_tool_dispatch", []),
            ("after_tool_return", ["sent"]),
        ):
            with self.subTest(point=point):
                database = self.root / f"{point}.sqlite3"
                marker = self.root / f"{point}.txt"
                self.run_worker(database, marker, point)
                effects = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
                self.assertEqual(effects, expected_effects)

                called = []
                store = SQLiteStore(database)
                try:
                    with self.assertRaises(RunPaused) as paused:
                        asyncio.run(
                            Harness(
                                ForbiddenModel(),
                                (
                                    Tool(
                                        "send",
                                        lambda: called.append("duplicate"),
                                        tool_revision="1",
                                    ),
                                ),
                                store=store,
                            ).resume("process-effect")
                        )
                    self.assertEqual(paused.exception.reason, "effect_unknown")
                    self.assertEqual(called, [])
                    self.assertNotIn(
                        "run.completed",
                        [event.type for event in store.events("process-effect")],
                    )
                finally:
                    store.close()

    def test_real_process_exit_after_final_commit_returns_stored_final(self):
        database = self.root / "final.sqlite3"
        marker = self.root / "unused.txt"
        self.run_worker(database, marker, "after_final_commit")

        store = SQLiteStore(database)
        try:
            result = asyncio.run(
                Harness(ForbiddenModel(), store=store).resume("process-final")
            )
            self.assertEqual(result.final_message.content, "durable final")
            self.assertEqual(
                [event.type for event in result.events].count("run.completed"), 1
            )
        finally:
            store.close()
    def test_workflow_process_exit_never_replays_ambiguous_side_effect(self):
        for mode, expected_effects in (
            ("side_dispatch", []),
            ("side_return", ["effect:same"]),
        ):
            with self.subTest(mode=mode):
                database = self.root / f"workflow-{mode}.sqlite3"
                attempts = self.root / f"workflow-{mode}-attempts.txt"
                effects = self.root / f"workflow-{mode}-effects.txt"
                self.run_workflow_worker(database, attempts, effects, mode)
                lines = (
                    effects.read_text(encoding="utf-8").splitlines()
                    if effects.exists()
                    else []
                )
                self.assertEqual(lines, expected_effects)

                called: list[str] = []

                def side(text: str) -> str:
                    called.append(text)
                    return "duplicate:" + text

                tool = Tool(
                    "side",
                    side,
                    effect="side_effecting",
                    tool_revision="1",
                )
                store = SQLiteStore(database)
                try:
                    spec, workflow = self.workflow(store, tool, "process-side")
                    with self.assertRaises(RunPaused) as paused:
                        asyncio.run(workflow.resume("WorkflowProcessSide"))
                    self.assertEqual(paused.exception.reason, "effect_unknown")
                    self.assertEqual(called, [])
                    self.assertEqual(
                        [
                            event.type
                            for event in store.events("WorkflowProcessSide")
                        ].count("tool.completed"),
                        0,
                    )
                    if mode == "side_return":
                        call = store.calls("WorkflowProcessSide", 1)[0]
                        value = "done:same"
                        workflow.resolve_effect(
                            "WorkflowProcessSide",
                            call.fingerprint,
                            "record_result",
                            reason="operator verified the external effect",
                            result={
                                "version": 1,
                                "definition_sha256": spec.digest,
                                "step_id": "step",
                                "value": value,
                                "value_sha256": json_sha256(value),
                            },
                        )
                        result = asyncio.run(workflow.resume("WorkflowProcessSide"))
                        self.assertEqual(
                            json.loads(result.final_message.content)["output"]["value"],
                            value,
                        )
                        again = asyncio.run(workflow.resume("WorkflowProcessSide"))
                        self.assertEqual(again.final_message, result.final_message)
                        self.assertEqual(
                            effects.read_text(encoding="utf-8").splitlines(),
                            ["effect:same"],
                        )
                finally:
                    store.close()

    def test_workflow_process_exit_retries_idempotent_with_same_business_key(self):
        database = self.root / "workflow-idempotent.sqlite3"
        attempts = self.root / "workflow-idempotent-attempts.txt"
        effects = self.root / "workflow-idempotent-effects.txt"
        self.run_workflow_worker(
            database, attempts, effects, "idempotent_return"
        )

        def remember(text: str, *, idempotency_key: str) -> str:
            with attempts.open("a", encoding="utf-8") as stream:
                stream.write(idempotency_key + "\n")
            existing = (
                effects.read_text(encoding="utf-8").splitlines()
                if effects.exists()
                else []
            )
            if idempotency_key not in existing:
                with effects.open("a", encoding="utf-8") as stream:
                    stream.write(idempotency_key + "\n")
            return "remembered:" + text

        tool = Tool(
            "remember",
            remember,
            effect="idempotent",
            idempotency_key=lambda arguments: "business:" + str(arguments["text"]),
            tool_revision="1",
        )
        store = SQLiteStore(database)
        try:
            _, workflow = self.workflow(store, tool, "process-idempotent")
            result = asyncio.run(workflow.resume("WorkflowProcessIdempotent"))
            self.assertEqual(
                json.loads(result.final_message.content)["output"]["value"],
                "remembered:same",
            )
            self.assertEqual(
                attempts.read_text(encoding="utf-8").splitlines(),
                ["business:same", "business:same"],
            )
            self.assertEqual(
                effects.read_text(encoding="utf-8").splitlines(),
                ["business:same"],
            )
            events = store.events("WorkflowProcessIdempotent")
            again = asyncio.run(workflow.resume("WorkflowProcessIdempotent"))
            self.assertEqual(again.final_message, result.final_message)
            self.assertEqual(store.events("WorkflowProcessIdempotent"), events)
        finally:
            store.close()
    def test_workflow_process_exit_after_final_commit_returns_typed_final(self):
        database = self.root / "workflow-final.sqlite3"
        attempts = self.root / "workflow-final-attempts.txt"
        effects = self.root / "workflow-final-effects.txt"
        self.run_workflow_worker(database, attempts, effects, "final_commit")

        def read(text: str) -> str:
            raise AssertionError("completed recovery must not replay the tool")

        tool = Tool("read", read, effect="read_only")
        store = SQLiteStore(database)
        try:
            spec, workflow = self.workflow(store, tool, "process-final")

            async def forbidden_complete(messages, tools):
                raise AssertionError("completed recovery must not replay the model")

            workflow.model.complete = forbidden_complete
            events = store.events("WorkflowProcessFinal")
            result = asyncio.run(workflow.resume("WorkflowProcessFinal"))
            final = json.loads(result.final_message.content)
            self.assertEqual(final["definition_sha256"], spec.digest)
            self.assertEqual(final["output"]["value"], "read:same")
            self.assertEqual(
                attempts.read_text(encoding="utf-8").splitlines(),
                ["read:same"],
            )
            self.assertEqual(store.events("WorkflowProcessFinal"), events)
            again = asyncio.run(workflow.resume("WorkflowProcessFinal"))
            self.assertEqual(again.final_message, result.final_message)
            self.assertEqual(store.events("WorkflowProcessFinal"), events)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
