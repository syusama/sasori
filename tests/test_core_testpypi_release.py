from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sasori_core_testpypi", ROOT / "scripts" / "core_testpypi_roundtrip.py"
)
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class CoreTestPyPIReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = "0.1.0.dev1"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        (self.artifacts / f"sasori_core-{self.version}-py3-none-any.whl").write_bytes(b"wheel")
        (self.artifacts / f"sasori_core-{self.version}.tar.gz").write_bytes(b"sdist")

    def test_inventory_is_exact_and_versioned(self):
        value = gate._inventory(self.artifacts, self.version)
        self.assertEqual(set(value), gate._expected(self.version))
        (self.artifacts / "unexpected.txt").write_bytes(b"x")
        with self.assertRaisesRegex(gate.GateError, "not exact"):
            gate._inventory(self.artifacts, self.version)

    def test_preflight_requires_version_absence(self):
        args = argparse.Namespace(
            project_root=ROOT / "packages" / "sasori-core",
            artifact_dir=self.artifacts,
            expected_version=self.version,
        )
        with mock.patch.object(gate, "_index", return_value=None):
            value = gate.preflight(args)
        self.assertTrue(value["version_absent"])
        with mock.patch.object(
            gate,
            "_index",
            return_value={"releases": {self.version: []}},
        ), self.assertRaisesRegex(gate.GateError, "already exists"):
            gate.preflight(args)

    def test_finalize_requires_exact_download_bytes_and_smoke_markers(self):
        wheel_dir = self.root / "wheel"
        sdist_dir = self.root / "sdist"
        wheel_dir.mkdir()
        sdist_dir.mkdir()
        (wheel_dir / f"sasori_core-{self.version}-py3-none-any.whl").write_bytes(b"wheel")
        (sdist_dir / f"sasori_core-{self.version}.tar.gz").write_bytes(b"sdist")
        wheel_marker = self.root / "wheel.pass"
        sdist_marker = self.root / "sdist.pass"
        wheel_marker.write_text("PASS\n", encoding="utf-8")
        sdist_marker.write_text("PASS\n", encoding="utf-8")
        args = argparse.Namespace(
            artifact_dir=self.artifacts,
            wheel_dir=wheel_dir,
            sdist_dir=sdist_dir,
            expected_version=self.version,
            wheel_smoke_marker=wheel_marker,
            sdist_smoke_marker=sdist_marker,
        )
        self.assertTrue(gate.finalize(args)["wheel_smoke"])
        (sdist_dir / f"sasori_core-{self.version}.tar.gz").write_bytes(b"tampered")
        with self.assertRaisesRegex(gate.GateError, "do not match"):
            gate.finalize(args)


if __name__ == "__main__":
    unittest.main()
