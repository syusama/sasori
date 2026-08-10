from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sasori_workbench_browser_journey",
    ROOT / "tests" / "workbench_browser_journey.py",
)
journey = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = journey
SPEC.loader.exec_module(journey)


class RealJourneyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "actions.jsonl"

    def test_exact_incident_and_workflow_actions_are_accepted(self):
        self.path.write_text(
            json.dumps({"summary": journey.EXPECTED_ACTION})
            + "\n"
            + json.dumps({"summary": journey.EXPECTED_WORKFLOW_ACTION})
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            journey.strict_actions(self.path),
            [
                {"summary": journey.EXPECTED_ACTION},
                {"summary": journey.EXPECTED_WORKFLOW_ACTION},
            ],
        )

    def test_missing_duplicate_tampered_and_non_strict_actions_fail_closed(self):
        cases = (
            None,
            "\n",
            json.dumps({"summary": "wrong"}) + "\n",
            json.dumps({"summary": journey.EXPECTED_ACTION})
            + "\n"
            + json.dumps({"summary": journey.EXPECTED_ACTION})
            + "\n",
            '{"summary":"first","summary":"second"}\n'
            + json.dumps({"summary": journey.EXPECTED_WORKFLOW_ACTION})
            + "\n",
        )
        for value in cases:
            with self.subTest(value=value):
                if self.path.exists():
                    self.path.unlink()
                if value is not None:
                    self.path.write_text(value, encoding="utf-8")
                with self.assertRaises(AssertionError):
                    journey.strict_actions(self.path)


if __name__ == "__main__":
    unittest.main()
