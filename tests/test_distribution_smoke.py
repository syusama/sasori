from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
with (ROOT / "pyproject.toml").open("rb") as stream:
    PROJECT_VERSION = tomllib.load(stream)["project"]["version"]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installed_smoke = load_script("installed_wheel_smoke")
sdist_smoke = load_script("sdist_consumer_smoke")


class InstalledOriginTests(unittest.TestCase):
    def test_origin_must_resolve_under_the_exact_consumer_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "consumer"
            inside = prefix / "Lib" / "site-packages" / "sasori" / "__init__.py"
            outside = root / "consumer-other" / "sasori" / "__init__.py"
            inside.parent.mkdir(parents=True)
            outside.parent.mkdir(parents=True)
            inside.write_bytes(b"")
            outside.write_bytes(b"")

            self.assertEqual(
                installed_smoke._require_under_prefix(inside, prefix, "package"),
                inside.resolve(),
            )
            with self.assertRaisesRegex(RuntimeError, "outside the consumer environment"):
                installed_smoke._require_under_prefix(outside, prefix, "package")
            with self.assertRaisesRegex(RuntimeError, "missing or unreadable"):
                installed_smoke._require_under_prefix(
                    prefix / "missing.py", prefix, "package"
                )


class SdistConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdist = self.root / f"sasori-{PROJECT_VERSION}.tar.gz"
        self.lock = self.root / "requirements-build.txt"
        self.wheelhouse = self.root / "build-wheelhouse"
        self.wheelhouse.mkdir()
        self.build_wheel = (
            self.wheelhouse / "setuptools-80.9.0-py3-none-any.whl"
        )
        self.build_wheel.write_bytes(b"locked build wheel")
        self.check = self.root / "installed_wheel_smoke.py"
        self.verifier = self.root / "release_verify.py"
        self.source = self.root / "source"
        self.source.mkdir()
        for path in (self.sdist, self.lock, self.check, self.verifier):
            path.write_bytes(b"fixture")

    def test_venv_python_is_platform_exact(self):
        root = Path("environment")
        self.assertEqual(
            sdist_smoke._venv_python(root, "nt"),
            root / "Scripts" / "python.exe",
        )
        self.assertEqual(
            sdist_smoke._venv_python(root, "posix"),
            root / "bin" / "python",
        )

    def test_run_accepts_verified_local_exit_and_rejects_verifier_failure(self):
        with mock.patch.object(
            sdist_smoke.subprocess,
            "run",
            return_value=sdist_smoke.subprocess.CompletedProcess(
                ["verify"], returncode=5
            ),
        ):
            self.assertEqual(
                sdist_smoke._run(
                    ["verify"],
                    cwd=self.root,
                    environment={},
                    accepted=frozenset({0, 5}),
                ),
                5,
            )
        with mock.patch.object(
            sdist_smoke.subprocess,
            "run",
            return_value=sdist_smoke.subprocess.CompletedProcess(
                ["verify"], returncode=4
            ),
        ), self.assertRaises(sdist_smoke.subprocess.CalledProcessError):
            sdist_smoke._run(
                ["verify"],
                cwd=self.root,
                environment={},
                accepted=frozenset({0, 5}),
            )

    def test_build_and_consumer_environments_are_separate_and_locked(self):
        calls = []

        def fake_run(command, *, cwd, environment, accepted=frozenset({0})):
            calls.append((command, cwd, environment, accepted))
            if "wheel" in command and "--wheel-dir" in command:
                wheel_root = Path(command[command.index("--wheel-dir") + 1])
                (wheel_root / f"sasori-{PROJECT_VERSION}-py3-none-any.whl").write_bytes(
                    b"wheel"
                )
            return 5 if "--allow-dirty-local" in command else 0

        with mock.patch.object(sdist_smoke, "_run", side_effect=fake_run):
            evidence = sdist_smoke.run_smoke(
                self.sdist,
                self.lock,
                self.wheelhouse,
                self.check,
                self.verifier,
                self.source,
            )

        self.assertEqual(
            evidence,
            {
                "source_archive": self.sdist.name,
                "build_wheel": self.build_wheel.name,
                "rebuilt_wheel": f"sasori-{PROJECT_VERSION}-py3-none-any.whl",
                "release_verifier_exit": 5,
            },
        )
        self.assertEqual(len(calls), 7)
        commands = [call[0] for call in calls]
        self.assertIn("--isolated", commands[1])
        self.assertIn("--no-cache-dir", commands[1])
        self.assertIn("--no-index", commands[1])
        self.assertIn("--find-links", commands[1])
        self.assertIn(self.wheelhouse.resolve(), commands[1])
        self.assertIn("--require-hashes", commands[1])
        self.assertIn("--only-binary=:all:", commands[1])
        self.assertIn("--isolated", commands[2])
        self.assertIn("--no-cache-dir", commands[2])
        self.assertIn("--no-index", commands[2])
        self.assertIn("--no-build-isolation", commands[2])
        self.assertIn("--no-deps", commands[2])
        self.assertIn("--allow-dirty-local", commands[3])
        self.assertIn("--wheel", commands[3])
        self.assertIn("--sdist", commands[3])
        self.assertEqual(calls[3][3], frozenset({0, 5}))
        self.assertIn("--no-index", commands[5])
        self.assertIn("--isolated", commands[5])
        self.assertIn("--no-cache-dir", commands[5])
        self.assertIn("--no-deps", commands[5])
        self.assertNotEqual(commands[1][0], commands[5][0])
        for _, cwd, environment, _ in calls:
            self.assertNotEqual(cwd.resolve(), ROOT.resolve())
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
            self.assertEqual(environment["PIP_NO_INDEX"], "1")
            for name in (
                "PIP_INDEX_URL",
                "PIP_EXTRA_INDEX_URL",
                "PIP_FIND_LINKS",
                "PIP_TRUSTED_HOST",
            ):
                self.assertNotIn(name, environment)

    def test_rebuild_rejects_multiple_wheels(self):
        def fake_run(command, *, cwd, environment, accepted=frozenset({0})):
            if "wheel" in command and "--wheel-dir" in command:
                wheel_root = Path(command[command.index("--wheel-dir") + 1])
                (wheel_root / "sasori-one.whl").write_bytes(b"one")
                (wheel_root / "sasori-two.whl").write_bytes(b"two")
            return 0

        with mock.patch.object(
            sdist_smoke, "_run", side_effect=fake_run
        ), self.assertRaisesRegex(RuntimeError, "exactly one regular wheel"):
            sdist_smoke.run_smoke(
                self.sdist,
                self.lock,
                self.wheelhouse,
                self.check,
                self.verifier,
                self.source,
            )

    def test_invalid_sdist_name_fails_before_any_command(self):
        invalid = self.root / "other.tar.gz"
        invalid.write_bytes(b"fixture")
        with mock.patch.object(
            sdist_smoke, "_run"
        ) as run, self.assertRaisesRegex(RuntimeError, "filename is invalid"):
            sdist_smoke.run_smoke(
                invalid,
                self.lock,
                self.wheelhouse,
                self.check,
                self.verifier,
                self.source,
            )
        run.assert_not_called()

    def test_invalid_wheelhouse_fails_before_any_command(self):
        invalid_roots = []

        empty = self.root / "empty-wheelhouse"
        empty.mkdir()
        invalid_roots.append(("empty", empty))

        multiple = self.root / "multiple-wheelhouse"
        multiple.mkdir()
        (multiple / "one-py3-none-any.whl").write_bytes(b"one")
        (multiple / "two-py3-none-any.whl").write_bytes(b"two")
        invalid_roots.append(("multiple", multiple))

        non_wheel = self.root / "non-wheelhouse"
        non_wheel.mkdir()
        (non_wheel / "requirements.txt").write_text("unexpected", encoding="utf-8")
        invalid_roots.append(("non-wheel", non_wheel))

        nested = self.root / "nested-wheelhouse"
        nested.mkdir()
        (nested / "nested").mkdir()
        invalid_roots.append(("nested", nested))

        non_portable = self.root / "non-portable-wheelhouse"
        non_portable.mkdir()
        (non_portable / "setuptools-80.9.0-cp312-cp312-win_amd64.whl").write_bytes(
            b"platform wheel"
        )
        invalid_roots.append(("non-portable", non_portable))

        not_directory = self.root / "wheelhouse.txt"
        not_directory.write_text("not a directory", encoding="utf-8")
        invalid_roots.extend(
            (("missing", self.root / "missing-wheelhouse"), ("file", not_directory))
        )

        for label, wheelhouse in invalid_roots:
            with self.subTest(boundary=label), mock.patch.object(
                sdist_smoke, "_run"
            ) as run, self.assertRaisesRegex(RuntimeError, "build wheelhouse"):
                sdist_smoke.run_smoke(
                    self.sdist,
                    self.lock,
                    wheelhouse,
                    self.check,
                    self.verifier,
                    self.source,
                )
            run.assert_not_called()

        original = Path.is_symlink

        def pretend_root_symlink(path):
            return path == self.wheelhouse or original(path)

        with mock.patch.object(
            Path, "is_symlink", pretend_root_symlink
        ), mock.patch.object(sdist_smoke, "_run") as run, self.assertRaisesRegex(
            RuntimeError, "must not be a symlink"
        ):
            sdist_smoke.run_smoke(
                self.sdist,
                self.lock,
                self.wheelhouse,
                self.check,
                self.verifier,
                self.source,
            )
        run.assert_not_called()

        resolved_build_wheel = self.build_wheel.resolve(strict=True)

        def pretend_wheel_symlink(path):
            return path == resolved_build_wheel or original(path)

        with mock.patch.object(
            Path, "is_symlink", pretend_wheel_symlink
        ), mock.patch.object(sdist_smoke, "_run") as run, self.assertRaisesRegex(
            RuntimeError, "build wheel must not be a symlink"
        ):
            sdist_smoke.run_smoke(
                self.sdist,
                self.lock,
                self.wheelhouse,
                self.check,
                self.verifier,
                self.source,
            )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
