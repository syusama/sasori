from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
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
    def test_hosted_browser_cell_runs_race_and_real_lifecycle_gates(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        test_job = workflow.split("\n  test:\n", 1)[1].split("\n  container:\n", 1)[0]
        condition = "if: ${{ matrix.os == 'ubuntu-24.04' && matrix.python-version == '3.12' }}"
        race = "run: python tests/workbench_browser_acceptance.py --require-browser"
        lifecycle = "run: python tests/workbench_browser_journey.py --require-browser"

        self.assertEqual(test_job.count(race), 1)
        self.assertEqual(test_job.count(lifecycle), 1)
        self.assertEqual(test_job.count(condition), 2)
        self.assertLess(test_job.index(race), test_job.index(lifecycle))

    def test_windows_install_directory_version_avoids_a_second_browser_process(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory) / "Application"
            application.mkdir()
            binary = application / "chrome.exe"
            binary.write_bytes(b"launcher")
            for version in (
                "99.0.9999.999",
                "151.0.7922.9",
                "151.0.7922.108",
                "151.0.7922",
                "not-a-version",
            ):
                (application / version).mkdir()

            with mock.patch.object(browser_acceptance.subprocess, "run") as run:
                version = browser_acceptance.browser_version(binary)

        self.assertEqual(version, "chrome.exe 151.0.7922.108")
        run.assert_not_called()

    def test_version_command_is_used_without_a_windows_install_directory(self):
        completed = subprocess.CompletedProcess(
            ["browser", "--version"],
            returncode=0,
            stdout=b"Chromium 151.0.8123.45\n",
            stderr=b"",
        )
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            return_value=completed,
        ) as run:
            version = browser_acceptance.browser_version(Path("browser"))

        self.assertEqual(version, "browser 151.0.8123.45")
        run.assert_called_once_with(
            ["browser", "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_unreadable_install_directory_falls_back_to_the_version_command(self):
        completed = subprocess.CompletedProcess(
            ["browser", "--version"],
            returncode=0,
            stdout=b"Chromium 151.0.8123.46\n",
            stderr=b"",
        )
        with mock.patch.object(
            Path,
            "iterdir",
            side_effect=PermissionError("browser directory is unreadable"),
        ), mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            return_value=completed,
        ) as run:
            version = browser_acceptance.browser_version(Path("browser"))

        self.assertEqual(version, "browser 151.0.8123.46")
        run.assert_called_once()

    def test_version_command_timeout_is_explicit(self):
        timeout = subprocess.TimeoutExpired(["browser", "--version"], 10)
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            side_effect=timeout,
        ), self.assertRaises(subprocess.TimeoutExpired):
            browser_acceptance.browser_version(Path("browser"))

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
        self.assertTrue(
            all("--virtual-time-budget=10000" in command for command in commands)
        )
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

    def test_stateful_journey_can_disable_timeout_replay(self):
        timeout = subprocess.TimeoutExpired(["browser"], 70)
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            side_effect=timeout,
        ) as run, self.assertRaisesRegex(
            RuntimeError,
            "timed out after 1 attempts of 70 seconds",
        ):
            browser_acceptance.run_browser_process(
                Path("browser"),
                18180,
                attempts=1,
                timeout_seconds=70,
            )

        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 70)

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

    def test_custom_browser_profile_is_passed_to_chromium(self):
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=0, stdout="ok", stderr=""
        )
        with mock.patch.object(
            browser_acceptance.subprocess,
            "run",
            return_value=completed,
        ) as run:
            result = browser_acceptance.run_browser_process(
                Path("browser"),
                18080,
                window_size=(390, 844),
                extra_arguments=("--force-prefers-reduced-motion",),
                browser_path="/#profile=narrow-reduced",
            )

        self.assertIs(result, completed)
        command = run.call_args.args[0]
        self.assertIn("--window-size=390,844", command)
        self.assertIn("--force-prefers-reduced-motion", command)
        self.assertEqual(command[-1], "http://127.0.0.1:18080/#profile=narrow-reduced")

    @staticmethod
    def _png(width=1600, height=1000):
        header = (
            browser_acceptance.PNG_SIGNATURE
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
        )
        return header + b"x" * browser_acceptance.MIN_SCREENSHOT_BYTES

    def test_screenshot_must_be_created_by_the_current_attempt(self):
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=0, stdout="ok", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "workbench.png"
            screenshot.write_bytes(self._png())
            with mock.patch.object(
                browser_acceptance.subprocess,
                "run",
                return_value=completed,
            ), self.assertRaisesRegex(
                AssertionError, "did not produce the requested screenshot"
            ):
                browser_acceptance.run_browser_process(
                    Path("browser"), 18080, screenshot=screenshot
                )
            self.assertFalse(screenshot.exists())

    def test_retry_deletes_partial_screenshot_before_accepting_evidence(self):
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=0, stdout="ok", stderr=""
        )
        timeout = subprocess.TimeoutExpired(
            ["browser"], browser_acceptance.BROWSER_TIMEOUT_SECONDS
        )
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "workbench.png"
            attempts = 0

            def run(command, **options):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    screenshot.write_bytes(b"partial")
                    raise timeout
                return completed

            with mock.patch.object(
                browser_acceptance.subprocess, "run", side_effect=run
            ), self.assertRaisesRegex(
                AssertionError, "did not produce the requested screenshot"
            ):
                browser_acceptance.run_browser_process(
                    Path("browser"), 18080, screenshot=screenshot
                )
            self.assertFalse(screenshot.exists())

    def test_screenshot_png_signature_and_dimensions_are_verified(self):
        completed = subprocess.CompletedProcess(
            ["browser"], returncode=0, stdout="ok", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "workbench.png"

            def write_valid(command, **options):
                screenshot.write_bytes(self._png())
                return completed

            with mock.patch.object(
                browser_acceptance.subprocess, "run", side_effect=write_valid
            ):
                result = browser_acceptance.run_browser_process(
                    Path("browser"), 18080, screenshot=screenshot
                )
            self.assertIs(result, completed)

            screenshot.write_bytes(self._png(width=800))
            with self.assertRaisesRegex(AssertionError, "dimensions are 800x1000"):
                browser_acceptance.validate_screenshot(screenshot)


if __name__ == "__main__":
    unittest.main()
