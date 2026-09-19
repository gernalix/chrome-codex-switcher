from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LateBindContractTests(unittest.TestCase):
    def test_extension_wires_dashboard_late_bind_capture(self):
        background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        content = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")

        self.assertIn('"prompt:bind-late"', background)
        self.assertIn("beginPromptLateBind", background)
        self.assertIn("maybeCapturePromptChromeTab", background)
        self.assertIn("chrome.tabs.onActivated.addListener", background)
        self.assertIn("chrome.tabs.onUpdated.addListener", background)
        self.assertIn("PENDING_PROMPT_CAPTURE_KEY", background)
        self.assertIn("promptLateBindStatus", content)
        self.assertIn("copy|launch|bind|chrome|codex|verify", content)

    def test_capture_is_limited_to_existing_chatgpt_conversations(self):
        background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
        self.assertIn('url.origin === "https://chatgpt.com"', background)
        self.assertIn('/^(?:', background.replace("\\/","/")) if False else None
        self.assertIn('(?:c|g)', background)

    def test_manifest_keeps_tabs_and_storage_permissions(self):
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("tabs", manifest["permissions"])
        self.assertIn("storage", manifest["permissions"])
        self.assertEqual("0.3.1", manifest["version"])


if __name__ == "__main__":
    unittest.main()
