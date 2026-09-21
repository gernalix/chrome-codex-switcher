from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "contrib" / "gnome-extension@gernalix.github.com" / "extension.js").read_text(
    encoding="utf-8"
)
STYLE = (ROOT / "contrib" / "gnome-extension@gernalix.github.com" / "stylesheet.css").read_text(
    encoding="utf-8"
)


class GnomeOverlayContractTests(unittest.TestCase):
    def test_app_detection_never_uses_window_title(self) -> None:
        match = re.search(
            r"function isChatGptDesktopWindow\(win\) \{(?P<body>.*?)\n\}",
            SOURCE,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertNotIn("get_title", body)
        self.assertIn("get_wm_class", body)
        self.assertIn("get_gtk_application_id", body)
        self.assertIn("Shell.WindowTracker", body)

    def test_overlay_hides_outside_chatgpt_desktop(self) -> None:
        self.assertIn("if (!isChatGptDesktopWindow(win)) {", SOURCE)
        self.assertIn("this._box.hide();", SOURCE)
        self.assertNotIn("function isCodexWindow", SOURCE)

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
