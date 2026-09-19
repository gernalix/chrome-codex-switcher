import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from chrome_codex_switcher.verifier import (
    discover_codex_session,
    verify_binding,
    verify_note,
    verify_prompt,
    verify_workflowy,
)


class VerifierTests(unittest.TestCase):
    def test_discover_codex_session_requires_unique_exact_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2026" / "09" / "19"
            path.mkdir(parents=True)
            one = path / "rollout-2026-09-19T22-00-00-01a0bb57-51d2-7eb3-a467-58f7bb6cfba0.jsonl"
            one.write_text('{"message":"PROMPT_ID=123456"}\n', encoding="utf-8")
            result = discover_codex_session("123456", session_root=root)
            self.assertEqual(
                "01a0bb57-51d2-7eb3-a467-58f7bb6cfba0",
                result["session_id"],
            )
            two = path / "rollout-2026-09-19T22-01-00-01a0bb58-51d2-7eb3-a467-58f7bb6cfba1.jsonl"
            two.write_text('{"message":"PROMPT_ID=123456"}\n', encoding="utf-8")
            self.assertIsNone(discover_codex_session("123456", session_root=root))

    def test_granular_binding_note_and_workflowy_checks(self):
        now = time.time()
        binding = {
            "prompt_id": "123456",
            "context_id": "ctx-1",
            "codex_thread": "thread-1",
            "codex_deep_link": "codex://threads/thread-1",
            "url": "https://chatgpt.com/c/abc",
            "title": "Chat",
        }
        context = {
            "id": "ctx-1",
            "url": binding["url"],
            "title": "Chat",
            "note": "hello",
            "codex_note": "hello",
            "notes_independent": False,
            "twin": {
                "codex_thread": "thread-1",
                "codex_deep_link": "codex://threads/thread-1",
            },
        }

        def request(path, payload=None, timeout=4.0):
            del timeout
            if path == "/api/prompt?prompt_id=123456":
                return {"ok": True, "binding": dict(binding)}
            if path == "/api/context?context_id=ctx-1":
                return {"ok": True, "context": dict(context)}
            if path == "/api/prompt/control":
                return {"ok": True, "request_id": "req-note"}
            if path == "/api/control/ack?request_id=req-note":
                return {
                    "ok": True,
                    "ack": {
                        "ok": True,
                        "context_id": "ctx-1",
                        "rendered": {
                            "ok": True,
                            "context_id": "ctx-1",
                            "note": "hello",
                        },
                    },
                }
            if path == "/api/gnome-runtime":
                return {
                    "ok": True,
                    "gnome_runtime": {
                        "seen_at": now,
                        "visible": False,
                        "focused_codex": False,
                    },
                }
            if path == "/api/prompt/text?prompt_id=123456":
                return {"ok": True, "prompt_text": "PROMPT_ID=123456"}
            if path == "/api/health":
                return {
                    "ok": True,
                    "extension_runtime": {"version": "0.4.0", "seen_at": now},
                }
            self.fail(f"unexpected request: {path} {payload}")

        self.assertEqual("PASS", verify_binding("123456", request=request)["result"])
        self.assertEqual("PASS", verify_note("123456", request=request)["result"])
        self.assertEqual("PASS", verify_workflowy("123456", request=request)["result"])

    def test_verify_prompt_read_only_path(self):
        now = time.time()
        binding = {
            "prompt_id": "123456",
            "context_id": "ctx-1",
            "codex_thread": "thread-1",
            "codex_deep_link": "codex://threads/thread-1",
            "url": "https://chatgpt.com/c/abc",
            "title": "Chat",
        }
        ack = {
            "ok": True,
            "context_id": "ctx-1",
            "rendered": {
                "ok": True,
                "context_id": "ctx-1",
                "note": "hello",
            },
        }

        def request(path, payload=None, timeout=4.0):
            del timeout
            if path == "/api/health":
                return {
                    "ok": True,
                    "extension_runtime": {"version": "0.4.0", "seen_at": now},
                    "codex_a11y_watch": True,
                    "codex_a11y_error": None,
                }
            if path == "/api/prompt?prompt_id=123456":
                return {"ok": True, "binding": dict(binding)}
            if path == "/api/context?context_id=ctx-1":
                return {
                    "ok": True,
                    "context": {
                        "id": "ctx-1",
                        "url": binding["url"],
                        "title": "Chat",
                        "note": "hello",
                        "codex_note": "hello",
                        "notes_independent": False,
                        "twin": {
                            "codex_thread": "thread-1",
                            "codex_deep_link": "codex://threads/thread-1",
                        },
                    },
                }
            if path == "/api/prompt/control":
                self.assertEqual("probe", payload["action"])
                return {"ok": True, "request_id": "req-1"}
            if path == "/api/control/ack?request_id=req-1":
                return {"ok": True, "ack": dict(ack)}
            self.fail(f"unexpected request: {path} {payload}")

        result = verify_prompt("123456", request=request, full=False)
        self.assertEqual("PASS", result["result"])
        self.assertTrue(result["gates"]["binding"])
        self.assertTrue(result["gates"]["twin"])
        self.assertTrue(result["gates"]["chrome_probe"])


if __name__ == "__main__":
    unittest.main()
