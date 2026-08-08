import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, Message, ModelReply, RunPaused, Tool, ToolCall  # noqa: E402
from sasori.projection import event_projection, run_projection, validate_run_id  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
