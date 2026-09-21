from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
CONTENT = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))


class ExtensionLifecycleContractTests(unittest.TestCase):
    def test_reload_can_reinject_all_eligible_open_tabs(self) -> None:
        self.assertIn("scripting", MANIFEST["permissions"])
        self.assertIn("http://*/*", MANIFEST["host_permissions"])
        self.assertIn("https://*/*", MANIFEST["host_permissions"])
        self.assertIn("async function recoverEligibleTabs", BACKGROUND)
        self.assertIn("chrome.scripting.executeScript", BACKGROUND)
        self.assertIn("recoverEligibleTabs();", BACKGROUND)

    def test_reinjection_replaces_stale_overlay_and_invalid_context_self_destructs(self) -> None:
        self.assertIn('document.getElementById("chrome-codex-switcher-host")?.remove()', CONTENT)
        self.assertIn("destroyStaleOverlay", CONTENT)
        self.assertIn("extension_context_invalidated", CONTENT)
        self.assertIn('chrome.runtime.connect({name: "content-lifecycle"})', CONTENT)
        self.assertIn("destroyStaleOverlay(true)", CONTENT)
        self.assertIn('port.name !== "content-lifecycle"', BACKGROUND)

    def test_recovery_is_scoped_to_supported_http_pages(self) -> None:
        self.assertIn("function isSupportedPageUrl", BACKGROUND)
        self.assertIn('url.protocol !== "http:" && url.protocol !== "https:"', BACKGROUND)
        self.assertIn("127.0.0.1:43817", BACKGROUND)


if __name__ == "__main__":
    unittest.main()
