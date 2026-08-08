from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sasori_workbench_browser_acceptance",
    ROOT / "tests" / "workbench_browser_acceptance.py",
)
browser_acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = browser_acceptance
SPEC.loader.exec_module(browser_acceptance)


class BrowserProcessTests(unittest.TestCase):
    def test_timeout_retries_once_with_a_fresh_profile(self):
        timeout = subprocess.TimeoutExpired(
            ["browser"], browser_acceptance.BROWSER_TIMEOUT_SECONDS
        )
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=0, stdout="ok", stderr=""
        )
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            side_effect=(timeout, completed),
        ) as run:
            result = browser_acceptance.run_browser_process(Path("browser"), 18080)

        self.assertIs(result, completed)
        self.assertEqual(run.call_count, 2)
        commands = [call.args[0] for call in run.call_args_list]
        profiles = [
            next(value for value in command if value.startswith("--user-data-dir="))
            for command in commands
        ]
        self.assertNotEqual(profiles[0], profiles[1])
        self.assertTrue(
            all(not Path(value.removeprefix("--user-data-dir=")).exists() for value in profiles)
        )

    def test_repeated_timeouts_fail_after_the_bounded_attempts(self):
        timeout = subprocess.TimeoutExpired(
            ["browser"], browser_acceptance.BROWSER_TIMEOUT_SECONDS
        )
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            side_effect=(timeout, timeout),
        ) as run, self.assertRaisesRegex(
            RuntimeError,
            "timed out after 2 attempts of 35 seconds",
        ) as raised:
            browser_acceptance.run_browser_process(Path("browser"), 18080)

        self.assertEqual(run.call_count, browser_acceptance.BROWSER_ATTEMPTS)
        self.assertIsInstance(raised.exception.__cause__, subprocess.TimeoutExpired)

    def test_nonzero_browser_exit_is_not_retried(self):
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=17, stdout="", stderr="failed"
        )
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = browser_acceptance.run_browser_process(Path("browser"), 18080)

        self.assertIs(result, completed)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
