from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GNOME_DIR = ROOT / "contrib" / "gnome-extension@gernalix.github.com"
LOADER = (GNOME_DIR / "extension.js").read_text(
    encoding="utf-8"
)
SOURCE = (GNOME_DIR / "runtime.js").read_text(
    encoding="utf-8"
)
STYLE = (ROOT / "contrib" / "gnome-extension@gernalix.github.com" / "stylesheet.css").read_text(
    encoding="utf-8"
)


class GnomeOverlayContractTests(unittest.TestCase):
    def test_loader_reads_runtime_on_each_enable_to_avoid_module_cache(self) -> None:
        self.assertIn("loadRuntime(this.path)", LOADER)
        self.assertIn("runtime.js", LOADER)
        self.assertIn("new Function", LOADER)

    def test_app_detection_never_uses_window_title(self) -> None:
        match = re.search(
            r"function isChatGptDesktopWindow\(win\) \{(?P<body>.*?)\n\}",
            SOURCE,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("get_title", body)
        self.assertIn("get_wm_class", SOURCE)
        self.assertIn("get_gtk_application_id", SOURCE)
        self.assertIn("Shell.WindowTracker", SOURCE)
        self.assertNotIn("get_name", SOURCE)
        self.assertIn("CODEX_DESKTOP_APP_IDS", SOURCE)
        self.assertIn("CODEX_DESKTOP_WM_CLASSES", SOURCE)
        self.assertNotIn(".test(identity)", body)

    def test_overlay_hides_outside_chatgpt_desktop(self) -> None:
        self.assertIn("if (!isChatGptDesktopWindow(win)) {", SOURCE)
        self.assertIn("this._box.hide();", SOURCE)
        self.assertNotIn("function isCodexWindow", SOURCE)

    def test_runtime_heartbeat_reports_source_version_and_focused_identity(self) -> None:
        self.assertIn("source_version", SOURCE)
        self.assertIn("focused_app_id", SOURCE)
        self.assertIn("focused_wm_class", SOURCE)

    def test_overlay_has_move_resize_collapse_and_close_controls(self) -> None:
        for needle in (
            "_beginOverlayInteraction('move'",
            "_beginOverlayInteraction('resize'",
            "Clutter.EventType.MOTION",
            "_toggleCollapsed()",
            "_dismissOverlay()",
            "_placeOverlay(win)",
            "context-twin-control",
            "context-twin-resize",
        ):
            self.assertIn(needle, SOURCE + STYLE)


if __name__ == "__main__":
    unittest.main()
