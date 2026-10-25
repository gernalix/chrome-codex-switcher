import unittest
from host.chrome_codex_switcher import context_bindings
from host.chrome_codex_switcher.daemon import App, PROMPT_ID_RE


class BindingCapsuleTests(unittest.TestCase):
    def test_app_inherits_store_binding_operations(self):
        for name in ("bind_prompt","prompt_binding","observe_context_prompt_ids",
                     "remove_context_prompt_id","upsert_context","replace_context_url"):
            self.assertIs(getattr(App,name),getattr(context_bindings.ContextBindingsMixin,name))
        self.assertIs(PROMPT_ID_RE,context_bindings.PROMPT_ID_RE)

    def test_prompt_id_validation_is_pure(self):
        self.assertEqual(App._prompt_id(" 123456 "),"123456")
        with self.assertRaisesRegex(ValueError,"invalid_prompt_id"):
            App._prompt_id("1;DROP")


if __name__=="__main__":
    unittest.main()
