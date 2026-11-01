import unittest

from host.chrome_codex_switcher.daemon import App, CLIPBOARD_DUPLICATE_WINDOW
from host.chrome_codex_switcher.clipboard_binding import ClipboardBindingMixin
import host.chrome_codex_switcher.clipboard_binding as capsule


class ClipboardBoundaryTests(unittest.TestCase):
    def test_legacy_public_api_composes_clipboard_policy(self):
        self.assertTrue(issubclass(App,ClipboardBindingMixin))
        self.assertIs(App.handle_clipboard,ClipboardBindingMixin.handle_clipboard)
        self.assertIs(CLIPBOARD_DUPLICATE_WINDOW,capsule.CLIPBOARD_DUPLICATE_WINDOW)

    def test_non_link_clipboard_ignored_without_store(self):
        app=App.__new__(App)
        self.assertEqual(app.handle_clipboard('ordinary text'),{'ok':True,'ignored':True})


if __name__=='__main__':
    unittest.main()
