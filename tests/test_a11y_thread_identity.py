from __future__ import annotations

import unittest

from chrome_codex_switcher.a11y_watch import _thread_from_attributes


class CodexA11yThreadIdentityTests(unittest.TestCase):
    def test_extracts_exact_codex_deep_link(self) -> None:
        self.assertEqual(
            "01abc-def",
            _thread_from_attributes(["href:codex://threads/01abc-def?source=desktop"]),
        )

    def test_extracts_exact_thread_route(self) -> None:
        self.assertEqual(
            "thread-42",
            _thread_from_attributes(["href=https://chatgpt.com/threads/thread-42"]),
        )

    def test_extracts_explicit_data_thread_id(self) -> None:
        self.assertEqual(
            "thread_42",
            _thread_from_attributes(["data-thread-id:thread_42"]),
        )

    def test_does_not_guess_from_unrelated_uuid(self) -> None:
        self.assertIsNone(
            _thread_from_attributes(["id:01abc-def", "name:unrelated selected row"])
        )

    def test_conflicting_explicit_threads_fail_closed(self) -> None:
        self.assertIsNone(
            _thread_from_attributes(
                ["href:codex://threads/thread-a", "data-thread-id:thread-b"]
            )
        )


if __name__ == "__main__":
    unittest.main()
