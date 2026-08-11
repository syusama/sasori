from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import sasori  # noqa: E402
import sasori_core  # noqa: E402
from sasori import Harness as BundleHarness  # noqa: E402
from sasori_core import Harness as CoreHarness  # noqa: E402


class CoreBoundaryTests(unittest.TestCase):
    def test_bundle_harness_is_a_thin_subclass_of_the_only_core_runtime(self):
        self.assertIs(BundleHarness.__mro__[1], CoreHarness)
        self.assertIs(sasori.Message, sasori_core.Message)
        self.assertIs(sasori.Event, sasori_core.Event)
        self.assertIs(sasori.Tool, sasori_core.Tool)
        self.assertFalse(hasattr(sasori_core, "PluginRegistration"))
        self.assertFalse(hasattr(sasori_core, "WorkerSpec"))
        self.assertTrue(hasattr(sasori, "PluginRegistration"))
        self.assertTrue(hasattr(sasori, "WorkerSpec"))

    def test_core_contract_module_excludes_plugin_and_multi_agent_types(self):
        from sasori_core import contracts

        self.assertNotIn("PluginRegistration", contracts.__all__)
        self.assertNotIn("WorkerSpec", contracts.__all__)
        self.assertFalse(hasattr(contracts, "PluginRegistration"))
        self.assertFalse(hasattr(contracts, "WorkerSpec"))

    def test_core_source_imports_only_standard_library_and_itself(self):
        package = ROOT / "packages" / "sasori-core" / "src" / "sasori_core"
        forbidden = []
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name == "sasori" or name.startswith("sasori_"):
                        forbidden.append(f"{path.name}:{node.lineno}:{name}")
                    if name in {"sqlite3", "urllib", "http", "socket"}:
                        forbidden.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual(forbidden, [])

    def test_fresh_core_import_does_not_load_bundle_or_adapter_modules(self):
        script = r'''
import json
import sys
import sasori_core
forbidden = [
    name for name in sys.modules
    if name == "sasori"
    or name.startswith("sasori.")
    or name.startswith("sasori_web")
    or name.startswith("sasori_flow")
    or name == "sqlite3"
    or name.startswith("urllib.request")
]
print(json.dumps({"forbidden": forbidden, "store": type(sasori_core.EphemeralRunStore()).__name__}))
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
        self.assertEqual(value["forbidden"], [])
        self.assertEqual(value["store"], "EphemeralRunStore")

    def test_workflow_projection_validator_is_owned_by_flow(self):
        bundle_path = SRC / "sasori" / "projection.py"
        flow_path = SRC / "sasori_flow" / "projection.py"
        bundle_tree = ast.parse(
            bundle_path.read_text(encoding="utf-8"), filename=str(bundle_path)
        )
        flow_tree = ast.parse(
            flow_path.read_text(encoding="utf-8"), filename=str(flow_path)
        )
        bundle_functions = {
            node.name for node in ast.walk(bundle_tree) if isinstance(node, ast.FunctionDef)
        }
        flow_functions = {
            node.name for node in ast.walk(flow_tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("_validate_workflow_projection", bundle_functions)
        self.assertNotIn("_validate_workflow_semantics", bundle_functions)
        self.assertIn("_validate_workflow_projection", flow_functions)
        self.assertIn("_validate_workflow_semantics", flow_functions)
        self.assertIn("validate_workflow_projection_extension", flow_functions)

    def test_harness_uses_the_one_exported_low_level_loop(self):
        runtime_path = (
            ROOT
            / "packages"
            / "sasori-core"
            / "src"
            / "sasori_core"
            / "runtime.py"
        )
        tree = ast.parse(
            runtime_path.read_text(encoding="utf-8"), filename=str(runtime_path)
        )
        harness = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Harness"
        )
        methods = {
            node.name for node in harness.body if isinstance(node, ast.AsyncFunctionDef)
        }
        self.assertNotIn("_drive", methods)
        self.assertIn("run_agent_loop", sasori_core.__all__)
        self.assertTrue(callable(sasori_core.run_agent_loop))


if __name__ == "__main__":
    unittest.main()
