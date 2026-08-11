from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
WEB = ROOT / "src" / "sasori_web"


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.elements.append((tag, dict(attrs)))


class WorkbenchShellContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.css = (WEB / "app.0.2.0.css").read_text(encoding="utf-8")
        script = (WEB / "app.0.2.0.js").read_text(encoding="utf-8")
        cls.shell_script = script.split("Red Sand Atelier shell.", 1)[1]
        cls.document = _Document()
        cls.document.feed(cls.html)

    def test_navigation_and_real_workbench_surfaces_are_wired_once(self):
        ids = [attrs["id"] for _, attrs in self.document.elements if "id" in attrs]
        self.assertEqual(len(ids), len(set(ids)), "Workbench IDs must be unique")
        for required in (
            "workbench-main",
            "task-form",
            "workflow-studio",
            "surface-panel",
            "artifacts-panel",
            "timeline-panel",
            "operator-action",
        ):
            self.assertIn(required, ids)

        destinations = [
            attrs["data-workbench-destination"]
            for _, attrs in self.document.elements
            if "data-workbench-destination" in attrs
        ]
        self.assertEqual(
            destinations,
            ["command", "workflows", "capabilities", "artifacts", "trace"],
        )
        self.assertIn("setInspectorTab(tab)", self.shell_script)
        self.assertIn('$("#task-input")', self.shell_script)
        self.assertIn("renderSurfaceWithoutCapabilityCenter(app)", self.shell_script)

    def test_separators_publish_the_complete_keyboard_contract(self):
        elements = {
            attrs.get("id"): attrs
            for _, attrs in self.document.elements
            if attrs.get("id") in {"left-separator", "right-separator"}
        }
        self.assertEqual(set(elements), {"left-separator", "right-separator"})
        for side, minimum, maximum, current in (
            ("left", "220", "380", "286"),
            ("right", "300", "520", "370"),
        ):
            attrs = elements[f"{side}-separator"]
            self.assertEqual(attrs.get("role"), "separator")
            self.assertEqual(attrs.get("aria-orientation"), "vertical")
            self.assertEqual(attrs.get("aria-valuemin"), minimum)
            self.assertEqual(attrs.get("aria-valuemax"), maximum)
            self.assertEqual(attrs.get("aria-valuenow"), current)
            self.assertEqual(attrs.get("tabindex"), "0")

        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(f'event.key === "{key}"', self.shell_script)
        self.assertIn('addEventListener("pointerdown"', self.shell_script)
        self.assertIn('addEventListener("pointermove"', self.shell_script)
        self.assertIn('setAttribute("aria-valuenow"', self.shell_script)

    def test_capability_center_is_evidence_bounded(self):
        filters = [
            attrs["data-capability-filter"]
            for _, attrs in self.document.elements
            if "data-capability-filter" in attrs
        ]
        self.assertEqual(
            filters,
            ["all", "skills", "tools", "mcp", "providers", "plugins"],
        )
        self.assertIn('plugin.capability_kind === "mcp_transport"', self.shell_script)
        self.assertNotIn("/mcp/i", self.shell_script)
        self.assertIn("没有投影独立 MCP transport", self.shell_script)
        self.assertIn("不会把普通插件冒充为 MCP", self.shell_script)
        self.assertNotIn("fetch(", self.shell_script)
        self.assertNotIn("api(", self.shell_script)
        self.assertNotIn("innerHTML", self.shell_script)
        self.assertNotIn("localStorage", self.shell_script)

    def test_responsive_and_reduced_motion_boundaries_are_explicit(self):
        for contract in (
            "--left-panel-width",
            "--right-panel-width",
            ".panel-separator",
            "touch-action: none",
            "@media (max-width: 940px)",
            "@media (max-width: 380px)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            self.assertIn(contract, self.css)
        self.assertIn('data-mobile-view="workers"', self.html)
        self.assertIn('data-mobile-view="stage" class="active" aria-pressed="true"', self.html)
        self.assertIn('data-mobile-view="inspector"', self.html)


if __name__ == "__main__":
    unittest.main()
