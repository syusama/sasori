import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, RunPaused, SQLiteStore, Tool  # noqa: E402


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
