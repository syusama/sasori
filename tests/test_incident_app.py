import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Message, RunPaused, SQLiteStore  # noqa: E402
from sasori_apps.incident import create_harness  # noqa: E402


class IncidentAppTests(unittest.TestCase):
    def test_real_approval_flow_writes_one_auditable_action(self):
        with tempfile.TemporaryDirectory() as directory:
            action_log = Path(directory) / "actions.jsonl"
            store = SQLiteStore(Path(directory) / "runs.sqlite3")
            with patch.dict(os.environ, {"SASORI_ACTION_LOG": str(action_log)}):
                harness = create_harness(store)
                with self.assertRaises(RunPaused) as paused:
                    asyncio.run(
                        harness.run(
                            (Message("user", "checkout latency is high"),),
                            run_id="incident-app",
                        )
                    )
                self.assertFalse(action_log.exists())
                harness.resolve_approval(
                    "incident-app", paused.exception.request.fingerprint, True
                )
                result = asyncio.run(harness.resume("incident-app"))

            self.assertIn("Incident action recorded", result.final_message.content)
            actions = [json.loads(line) for line in action_log.read_text("utf-8").splitlines()]
            self.assertEqual(len(actions), 1)
            self.assertIn("checkout latency is high", actions[0]["summary"])
            store.close()


if __name__ == "__main__":
    unittest.main()
