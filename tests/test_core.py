import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from chrome_codex_switcher.broker import EventBroker
from chrome_codex_switcher.daemon import App
from chrome_codex_switcher.store import Store
from chrome_codex_switcher.util import canonical_url, parse_codex_link


class UtilTests(unittest.TestCase):
    def test_parse_codex_link(self):
        self.assertEqual(parse_codex_link("x codex://threads/abc-123 y"), ("abc-123", "codex://threads/abc-123"))
        self.assertIsNone(parse_codex_link("https://example.com"))

    def test_canonical_url_drops_fragment_only(self):
        self.assertEqual(canonical_url("https://x.test/a?q=1#frag"), "https://x.test/a?q=1")


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        self.broker = EventBroker()
        self.open_codex = Mock()
        self.app = App(self.store, self.broker, self.open_codex)
        self.context = {"context_id": "ctx-1", "url": "https://chatgpt.com/c/abc", "title": "Chat A"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pair_then_switch_both_directions(self):
        armed = self.app.arm_link(self.context)
        self.assertTrue(armed["ok"])
        linked = self.app.handle_clipboard("codex://threads/thread-1")
        self.assertEqual(linked["action"], "linked")

        result = self.app.switch_from_chrome(self.context)
        self.assertTrue(result["ok"])
        self.open_codex.assert_called_once_with("codex://threads/thread-1")

        self.app._last_clipboard_at = 0
        result = self.app.handle_clipboard("codex://threads/thread-1")
        self.assertEqual(result["action"], "focus_chrome")
        _seq, events = self.broker.wait_after(0, 0)
        focus = [event for event in events if event["type"] == "focus_chrome"][-1]
        self.assertEqual(focus["payload"]["context_id"], "ctx-1")

    def test_duplicate_clipboard_delivery_is_ignored(self):
        self.app.arm_link(self.context)
        first = self.app.handle_clipboard("codex://threads/thread-1")
        second = self.app.handle_clipboard("codex://threads/thread-1")
        self.assertEqual(first["action"], "linked")
        self.assertTrue(second["duplicate"])
        _seq, events = self.broker.wait_after(0, 0)
        self.assertFalse(any(event["type"] == "focus_chrome" for event in events))

    def test_one_to_one_relink(self):
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/one")
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/two")
        self.assertIsNone(self.store.twin_by_thread("one"))
        self.assertEqual(self.store.twin_by_thread("two")["context_id"], "ctx-1")

    def test_note_is_persistent(self):
        self.app.upsert_context(self.context)
        result = self.app.set_note({"context_id": "ctx-1", "note": "Fix PersonalHub"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.get_context("ctx-1")["note"], "Fix PersonalHub")


if __name__ == "__main__":
    unittest.main()
