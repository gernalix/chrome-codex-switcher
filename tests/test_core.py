import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from chrome_codex_switcher.broker import EventBroker
from chrome_codex_switcher.daemon import App, prompt_action_fallback_html, search_dashboard_html
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
        self._old_overlay_path = os.environ.get("CCS_OVERLAY_PATH")
        self.overlay_path = Path(self.tmp.name) / "overlay.json"
        os.environ["CCS_OVERLAY_PATH"] = str(self.overlay_path)

    def tearDown(self):
        if self._old_overlay_path is None:
            os.environ.pop("CCS_OVERLAY_PATH", None)
        else:
            os.environ["CCS_OVERLAY_PATH"] = self._old_overlay_path
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

    def test_prompt_binding_is_explicit_and_survives_codex_pairing(self):
        self.app.upsert_context(self.context)
        bound = self.app.bind_prompt({**self.context, "prompt_id": "514458"})
        self.assertTrue(bound["ok"])
        self.assertEqual("ctx-1", self.store.prompt_binding("514458")["context_id"])

        armed = self.app.arm_prompt({**self.context, "prompt_id": "514458"})
        self.assertTrue(armed["ok"])
        linked = self.app.handle_clipboard("codex://threads/thread-prompt")
        self.assertIn(linked["action"], {"linked", "active_thread_updated", "focus_chrome"})
        binding = self.store.prompt_binding("514458")
        self.assertEqual("thread-prompt", binding["codex_thread"])
        self.assertEqual("codex://threads/thread-prompt", binding["codex_deep_link"])
        self.assertEqual("ctx-1", self.store.twin_by_thread("thread-prompt")["context_id"])

    def test_dashboard_contexts_include_prompt_id_and_codex_title(self):
        self.app.upsert_context(self.context)
        self.store.link_prompt(
            "514458",
            "ctx-1",
            "thread-dashboard",
            "codex://threads/thread-dashboard",
        )
        self.store.remember_codex_title("thread-dashboard", "Fix dashboard UI")
        self.store.set_note("ctx-1", "custom searchable note")
        self.store.observe_context_prompt_ids("ctx-1", ["614458", "714458"])

        item = next(row for row in self.store.list_contexts() if row["id"] == "ctx-1")
        self.assertEqual("514458", item["prompt_id"])
        self.assertEqual(["614458", "714458"], item["prompt_ids"])
        self.assertEqual("Fix dashboard UI", item["twin"]["codex_title"])
        self.assertEqual("custom searchable note", item["note"])


    def test_context_prompt_index_supports_many_to_many_and_manual_suppression(self):
        self.app.upsert_context(self.context)
        self.app.upsert_context({"context_id": "ctx-2", "url": "https://example.com/other", "title": "Other"})
        first = self.store.observe_context_prompt_ids("ctx-1", ["614458", "714458", "614458"])
        second = self.store.observe_context_prompt_ids("ctx-2", ["614458"])
        self.assertEqual(["614458", "714458"], [item["prompt_id"] for item in first])
        self.assertEqual(["614458"], [item["prompt_id"] for item in second])
        self.store.remove_context_prompt_id("ctx-1", "614458")
        self.assertEqual(["714458"], [item["prompt_id"] for item in self.store.context_prompt_index("ctx-1")])
        removed = next(item for item in self.store.context_prompt_index("ctx-1", include_excluded=True) if item["prompt_id"] == "614458")
        self.assertTrue(removed["auto_detected"])
        self.assertTrue(removed["excluded"])
        self.store.observe_context_prompt_ids("ctx-1", ["614458"])
        self.assertEqual(["714458"], [item["prompt_id"] for item in self.store.context_prompt_index("ctx-1")])
        self.store.add_context_prompt_id("ctx-1", "614458")
        restored = self.store.context_prompt_index("ctx-1")
        self.assertEqual(["614458", "714458"], [item["prompt_id"] for item in restored])
        self.assertTrue(next(item for item in restored if item["prompt_id"] == "614458")["manual_added"])
        self.store.add_context_prompt_id("ctx-1", "814458")
        self.store.remove_context_prompt_id("ctx-1", "814458")
        self.assertNotIn("814458", [item["prompt_id"] for item in self.store.context_prompt_index("ctx-1", include_excluded=True)])

    def test_dashboard_focus_emits_exact_chrome_target(self):
        self.app.upsert_context(self.context)
        result = self.app.focus_dashboard_context({"context_id": "ctx-1"})
        self.assertTrue(result["ok"])
        _seq, events = self.broker.wait_after(0, 0)
        event = [item for item in events if item["type"] == "focus_chrome"][-1]
        self.assertEqual("ctx-1", event["payload"]["context_id"])
        self.assertEqual(self.context["url"], event["payload"]["url"])

    def test_search_dashboard_covers_requested_fields(self):
        page = search_dashboard_html()
        for token in ("item.prompt_id", "item.prompt_ids", "item.title", "item.twin?.codex_title", "item.note", "item.codex_note"):
            self.assertIn(token, page)
        self.assertIn("ArrowDown", page)
        self.assertIn("shiftKey", page)
        self.assertIn("window.close();", page)
        self.assertIn("/api/context/prompts/add", page)
        self.assertIn("/api/context/prompts/remove", page)
        self.assertIn("prompt-chip", page)

    def test_prompt_arm_requires_real_context(self):
        with self.assertRaisesRegex(ValueError, "prompt_context_missing"):
            self.app.arm_prompt({"prompt_id": "514458"})
        self.assertIsNone(self.store.get_meta("pending_prompt"))

    def test_prompt_pairing_is_atomic_and_context_bound(self):
        self.app.upsert_context(self.context)
        self.app.arm_prompt({**self.context, "prompt_id": "514458"})
        self.app.handle_clipboard("codex://threads/thread-prompt")
        binding = self.store.prompt_binding("514458")
        self.assertEqual(binding["context_id"], self.store.twin_by_thread("thread-prompt")["context_id"])
        self.assertEqual(binding["codex_deep_link"], "codex://threads/thread-prompt")
        self.assertIsNone(self.store.get_meta("pending_prompt"))
        self.app.upsert_context({"context_id": "other-context", "url": "https://example.com/"})
        with self.assertRaisesRegex(ValueError, "prompt_context_mismatch"):
            self.store.link_prompt("514458", "other-context", "other-thread", "codex://threads/other-thread")
        self.assertEqual(self.store.prompt_binding("514458"), binding)

    def test_late_codex_discovery_can_bind_before_chrome(self):
        session_root = Path(self.tmp.name) / "sessions"
        session_root.mkdir()
        session_id = "01a0bb57-51d2-7eb3-a467-58f7bb6cfba0"
        (session_root / f"rollout-{session_id}.jsonl").write_text(
            '{"message":"PROMPT_ID=514458"}\n',
            encoding="utf-8",
        )

        recovered = self.app.recover_prompt_codex(
            {"prompt_id": "514458"},
            session_root=session_root,
        )

        self.assertTrue(recovered["ok"])
        self.assertEqual("chrome", recovered["stage"])
        self.assertEqual("native_session", recovered["source"])
        partial = self.store.prompt_binding("514458")
        self.assertIsNone(partial["context_id"])
        self.assertEqual(session_id, partial["codex_thread"])

        self.app.upsert_context(self.context)
        completed = self.app.bind_prompt({**self.context, "prompt_id": "514458"})
        self.assertEqual("ctx-1", completed["binding"]["context_id"])
        self.assertEqual(session_id, self.store.twin_by_context("ctx-1")["codex_thread"])

    def test_late_codex_falls_back_to_next_explicit_deep_link(self):
        session_root = Path(self.tmp.name) / "empty-sessions"
        session_root.mkdir()

        armed = self.app.recover_prompt_codex(
            {"prompt_id": "514458"},
            session_root=session_root,
        )
        self.assertTrue(armed["ok"])
        self.assertEqual("codex", armed["stage"])
        self.assertEqual("clipboard", armed["source"])

        linked = self.app.handle_clipboard("codex://threads/manual-thread")
        self.assertTrue(linked["ok"])
        binding = self.store.prompt_binding("514458")
        self.assertIsNone(binding["context_id"])
        self.assertEqual("manual-thread", binding["codex_thread"])

    def test_prompt_context_cannot_be_stolen_by_another_prompt(self):
        self.app.upsert_context(self.context)
        self.app.bind_prompt({**self.context, "prompt_id": "514458"})
        with self.assertRaisesRegex(ValueError, "prompt_context_conflict"):
            self.app.bind_prompt({**self.context, "prompt_id": "614458"})

    def test_open_prompt_codex_uses_bound_deep_link(self):
        self.app.upsert_context(self.context)
        self.store.bind_prompt(
            "514458",
            context_id="ctx-1",
            codex_thread="thread-prompt",
            codex_deep_link="codex://threads/thread-prompt",
        )
        result = self.app.open_prompt_codex({"prompt_id": "514458"})
        self.assertTrue(result["ok"])
        self.open_codex.assert_called_once_with("codex://threads/thread-prompt")

    def test_launch_prompt_codex_opens_new_thread_without_chrome_context(self):
        self.app.prompt_text = Mock(return_value={
            "ok": True,
            "prompt_id": "514458",
            "prompt_text": "PROMPT_ID=514458\nDo the task.",
            "project_id": "23",
            "project_name": "chrome-codex-switcher",
            "repo": "gernalix/chrome-codex-switcher",
            "model": "gpt-5.6-terra",
            "reasoning": "medium",
        })

        result = self.app.launch_prompt_codex({"prompt_id": "514458"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "new")
        self.assertNotIn("context_id", result)
        self.assertEqual(result["launch"]["chat_title"], "514458")
        self.assertEqual(result["launch"]["project_id"], "23")
        self.assertEqual(result["launch"]["model"], "gpt-5.6-terra")
        self.assertEqual(result["launch"]["reasoning"], "medium")
        pending = self.store.get_meta("pending_desktop_launch")
        self.assertEqual(pending["prompt_id"], "514458")
        self.assertEqual(pending["prompt_text"], "PROMPT_ID=514458\nDo the task.")
        self.assertEqual(self.store.get_meta("pending_prompt_codex")["prompt_id"], "514458")
        self.open_codex.assert_called_once_with("codex://threads/new")

    def test_launch_prompt_codex_reuses_existing_thread(self):
        self.app.upsert_context(self.context)
        self.store.bind_prompt(
            "514458",
            context_id="ctx-1",
            codex_thread="thread-prompt",
            codex_deep_link="codex://threads/thread-prompt",
        )

        result = self.app.launch_prompt_codex({"prompt_id": "514458"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "existing")
        self.open_codex.assert_called_once_with("codex://threads/thread-prompt")

    def test_launch_prompt_codex_refuses_missing_prompt_spec(self):
        self.app.prompt_text = Mock(return_value={
            "ok": False,
            "error": "roadmap_prompt_unavailable",
        })

        result = self.app.launch_prompt_codex({"prompt_id": "514458"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "roadmap_prompt_unavailable")
        self.open_codex.assert_not_called()

    def test_invalid_prompt_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.app.bind_prompt({**self.context, "prompt_id": "abc"})

    def test_note_is_persistent(self):
        self.app.upsert_context(self.context)
        result = self.app.set_note({"context_id": "ctx-1", "note": "Fix PersonalHub"})
        self.assertTrue(result["ok"])
        context = self.store.get_context("ctx-1")
        self.assertEqual(context["note"], "Fix PersonalHub")
        self.assertEqual(context["codex_note"], "Fix PersonalHub")
        self.assertFalse(context["notes_independent"])

    def test_note_survives_reopened_tab_by_canonical_url(self):
        self.app.upsert_context({
            "context_id": "ctx-original",
            "url": "https://chatgpt.com/c/note-test#first",
            "title": "Original tab",
        })
        self.app.set_note({
            "context_id": "ctx-original",
            "note": "persist after reopen",
            "surface": "chrome",
        })
        self.app.set_ui({
            "context_id": "ctx-original",
            "geometry": {"x": 144, "y": 88, "width": 420, "height": 260},
            "collapsed": True,
        })

        reopened = self.app.upsert_context({
            "context_id": "ctx-new-tab",
            "url": "https://chatgpt.com/c/note-test#second",
            "title": "Reopened tab",
        })

        self.assertEqual("ctx-original", reopened["id"])
        self.assertEqual("persist after reopen", reopened["note"])
        self.assertEqual("persist after reopen", reopened["codex_note"])
        self.assertEqual(
            {"x": 144, "y": 88, "width": 420, "height": 260},
            reopened["geometry"],
        )
        self.assertTrue(reopened["collapsed"])
        self.assertIsNone(self.store.get_context("ctx-new-tab"))

    def test_note_survives_store_reopen(self):
        path = self.store.path
        self.app.upsert_context({"context_id": "ctx-reopen", "url": "https://chatgpt.com/c/reopen"})
        self.app.set_note({"context_id": "ctx-reopen", "note": "durable note"})

        reopened = Store(path)

        context = reopened.get_context("ctx-reopen")
        self.assertEqual("durable note", context["note"])
        self.assertEqual("durable note", context["codex_note"])

    def test_three_distinct_reopened_contexts_keep_their_notes(self):
        rows = [
            ("ctx-a", "https://chatgpt.com/c/three-a", "note A"),
            ("ctx-b", "https://chatgpt.com/c/three-b", "note B"),
            ("ctx-c", "https://chatgpt.com/c/three-c", "note C"),
        ]
        for context_id, url, note in rows:
            self.app.upsert_context({"context_id": context_id, "url": url})
            self.app.set_note({"context_id": context_id, "note": note})

        reopened = Store(self.store.path)
        for index, (_context_id, url, note) in enumerate(rows):
            restored = reopened.upsert_context(f"new-tab-{index}", url)
            self.assertEqual(note, restored["note"])
            self.assertNotEqual(f"new-tab-{index}", restored["id"])

    def test_url_recovery_prefers_saved_geometry_over_newer_empty_legacy_context(self):
        url = "https://chatgpt.com/c/legacy-note"
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO contexts(
                       id,url,title,note,codex_note,notes_independent,geometry_json,
                       hidden,collapsed,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "ctx-with-layout", url, "Saved layout", "", "", 0,
                    '{"x":70,"y":90,"width":360,"height":240}',
                    0, 0, 1.0, 1.0,
                ),
            )
            db.execute(
                """INSERT INTO contexts(
                       id,url,title,note,codex_note,notes_independent,geometry_json,
                       hidden,collapsed,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "ctx-empty-newer", url, "Empty duplicate", "", "", 0,
                    "{}", 0, 0, 2.0, 2.0,
                ),
            )

        reopened = self.app.upsert_context({
            "context_id": "ctx-reopened",
            "url": url,
            "title": "Reopened",
        })

        self.assertEqual("ctx-with-layout", reopened["id"])
        self.assertEqual(
            {"x": 70, "y": 90, "width": 360, "height": 240},
            reopened["geometry"],
        )

    def test_notes_can_split_and_merge_from_either_surface(self):
        self.app.upsert_context(self.context)
        self.app.set_note({"context_id": "ctx-1", "note": "shared", "surface": "chrome"})
        enabled = self.app.set_note_mode({
            "context_id": "ctx-1",
            "independent": True,
            "source": "chrome",
            "note": "shared",
        })
        self.assertTrue(enabled["context"]["notes_independent"])

        self.app.set_note({"context_id": "ctx-1", "note": "codex only", "surface": "codex"})
        self.app.set_note({"context_id": "ctx-1", "note": "chrome only", "surface": "chrome"})
        context = self.store.get_context("ctx-1")
        self.assertEqual(context["note"], "chrome only")
        self.assertEqual(context["codex_note"], "codex only")

        merged = self.app.set_note_mode({
            "context_id": "ctx-1",
            "independent": False,
            "source": "codex",
            "note": "codex only",
        })
        self.assertFalse(merged["context"]["notes_independent"])
        self.assertEqual(merged["context"]["note"], "codex only")
        self.assertEqual(merged["context"]["codex_note"], "codex only")

    def test_enabling_split_uses_current_ui_value_before_debounce_flush(self):
        self.app.upsert_context(self.context)
        self.app.set_note({"context_id": "ctx-1", "note": "persisted old", "surface": "chrome"})

        enabled = self.app.set_note_mode({
            "context_id": "ctx-1",
            "independent": True,
            "source": "chrome",
            "note": "current unsaved",
        })

        self.assertTrue(enabled["context"]["notes_independent"])
        self.assertEqual(enabled["context"]["note"], "current unsaved")
        self.assertEqual(enabled["context"]["codex_note"], "current unsaved")

    def test_two_by_two_pairing_and_ui_state_remain_isolated(self):
        other = {"context_id": "ctx-2", "url": "https://chatgpt.com/c/def", "title": "Chat B"}
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/thread-1", codex_title="Same visible title")
        self.app._last_clipboard_at = 0
        self.app.arm_link(other)
        self.app.handle_clipboard("codex://threads/thread-2", codex_title="Same visible title")

        self.app.set_ui({
            "context_id": "ctx-1",
            "geometry": {"x": 10, "y": 20, "width": 310, "height": 210},
            "hidden": True,
            "collapsed": False,
        })
        self.app.set_ui({
            "context_id": "ctx-2",
            "geometry": {"x": 70, "y": 80, "width": 390, "height": 260},
            "hidden": False,
            "collapsed": True,
        })

        self.assertEqual(self.store.twin_by_context("ctx-1")["codex_thread"], "thread-1")
        self.assertEqual(self.store.twin_by_context("ctx-2")["codex_thread"], "thread-2")
        self.assertIsNone(self.store.thread_by_codex_title("Same visible title"))
        first = self.store.get_context("ctx-1")
        second = self.store.get_context("ctx-2")
        self.assertNotEqual(first["geometry"], second["geometry"])
        self.assertTrue(first["hidden"])
        self.assertTrue(second["collapsed"])

    def test_accessible_title_resolves_thread_and_hides_stale_overlay(self):
        self.app.arm_link(self.context)
        unknown = self.app.handle_codex_ui_state(True, "Indaga logout Fedora e profilo")
        self.assertEqual(unknown["action"], "overlay_hidden")
        state = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertFalse(state["visible"])

        linked = self.app.handle_clipboard(
            "codex://threads/thread-1",
            codex_title="Indaga logout Fedora e profilo",
        )
        self.assertEqual(linked["action"], "linked")
        self.assertEqual(
            self.store.thread_by_codex_title("Indaga logout Fedora e profilo"),
            "thread-1",
        )

        resolved = self.app.handle_codex_ui_state(True, "Indaga logout Fedora e profilo")
        self.assertEqual(resolved["action"], "active_thread_resolved")
        state = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertTrue(state["visible"])
        self.assertEqual(state["codex_thread"], "thread-1")

        stale = self.app.handle_codex_ui_state(True, "Another Codex chat")
        self.assertEqual(stale["action"], "overlay_hidden")
        state = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertFalse(state["visible"])
        self.assertEqual(state["reason"], "active_thread_unmapped")

    def test_exact_a11y_thread_wins_when_visible_titles_are_ambiguous(self):
        other = {"context_id": "ctx-2", "url": "https://chatgpt.com/c/def", "title": "Chat B"}
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/thread-1", codex_title="Same visible title")
        self.app._last_clipboard_at = 0
        self.app.arm_link(other)
        self.app.handle_clipboard("codex://threads/thread-2", codex_title="Same visible title")

        self.assertIsNone(self.store.thread_by_codex_title("Same visible title"))
        resolved = self.app.handle_codex_ui_state(
            True, "Same visible title", "thread-2"
        )

        self.assertEqual("active_thread_resolved", resolved["action"])
        self.assertEqual("a11y_thread", resolved["resolution"])
        self.assertEqual("thread-2", resolved["thread"])
        state = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertTrue(state["visible"])
        self.assertEqual("thread-2", state["codex_thread"])
        self.assertEqual("ctx-2", state["context_id"])

    def test_thread_change_invalidation_clears_cache_and_cannot_resurrect_old_overlay(self):
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/thread-1", codex_title="Chat A")
        self.assertEqual("thread-1", self.store.get_meta("active_codex_thread"))

        self.app.invalidate_codex_overlay("sidebar_pointer")
        self.assertIsNone(self.store.get_meta("active_codex_thread"))
        hidden = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertFalse(hidden["visible"])

        self.app.set_note({"context_id": "ctx-1", "note": "edited while resolving"})
        still_hidden = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertFalse(still_hidden["visible"])
        self.assertEqual("sidebar_pointer", still_hidden["reason"])

    def test_unfocused_codex_clears_active_thread_and_hides_overlay(self):
        self.app.arm_link(self.context)
        self.app.handle_clipboard("codex://threads/thread-1", codex_title="Chat A")

        result = self.app.handle_codex_ui_state(False, None)

        self.assertEqual("codex_unfocused", result["action"])
        self.assertIsNone(self.store.get_meta("active_codex_thread"))
        state = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        self.assertFalse(state["visible"])
        self.assertEqual("codex_unfocused", state["reason"])

    def test_bind_prompt_repairs_existing_thread_only_binding(self):
        self.store.bind_prompt(
            "514458",
            codex_thread="thread-prompt",
            codex_deep_link="codex://threads/thread-prompt",
        )
        result = self.app.bind_prompt({**self.context, "prompt_id": "514458"})
        self.assertTrue(result["ok"])
        binding = self.store.prompt_binding("514458")
        self.assertEqual("ctx-1", binding["context_id"])
        twin = self.store.twin_by_thread("thread-prompt")
        self.assertEqual("ctx-1", twin["context_id"])

    def test_control_plane_request_and_ack(self):
        self.app.upsert_context(self.context)
        self.store.bind_prompt("514458", context_id="ctx-1")
        started = self.app.request_chrome_control({"prompt_id": "514458", "action": "probe"})
        self.assertTrue(started["ok"])
        _seq, events = self.broker.wait_after(0, 0)
        control = [event for event in events if event["type"] == "control_chrome"][-1]
        self.assertEqual("514458", control["payload"]["prompt_id"])
        request_id = started["request_id"]
        acked = self.app.control_ack({
            "request_id": request_id,
            "ok": True,
            "surface": "chrome",
            "context_id": "ctx-1",
        })
        self.assertTrue(acked["ok"])
        self.assertTrue(self.store.get_meta(f"control_ack:{request_id}")["ok"])

    def test_gnome_heartbeat_is_normalized(self):
        result = self.app.gnome_heartbeat({
            "source_version": "8",
            "focused_codex": "1",
            "focused_app_id": "chatgpt.desktop",
            "focused_wm_class": "chatgpt",
            "visible": "true",
            "context_id": "ctx-1",
            "codex_thread": "thread-1",
            "note": "hello",
            "notes_independent": "0",
        })
        state = result["gnome_runtime"]
        self.assertTrue(state["focused_codex"])
        self.assertEqual("8", state["source_version"])
        self.assertEqual("chatgpt.desktop", state["focused_app_id"])
        self.assertTrue(state["visible"])
        self.assertFalse(state["notes_independent"])
        self.assertEqual("hello", state["note"])

    def test_open_prompt_codex_sets_expected_thread_for_a11y(self):
        self.app.upsert_context(self.context)
        self.store.link_prompt(
            "514458",
            "ctx-1",
            "thread-prompt",
            "codex://threads/thread-prompt",
        )
        result = self.app.open_prompt_codex({"prompt_id": "514458"})
        self.assertTrue(result["ok"])
        self.assertEqual("thread-prompt", self.app._expected_codex_thread)
        self.open_codex.assert_called_once_with("codex://threads/thread-prompt")

    def test_store_migrates_existing_note_column_without_data_loss(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript(
                """
                CREATE TABLE contexts (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    geometry_json TEXT NOT NULL DEFAULT '{}',
                    hidden INTEGER NOT NULL DEFAULT 0,
                    collapsed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            db.execute(
                "INSERT INTO contexts(id,url,title,note,created_at,updated_at) VALUES(?,?,?,?,1,1)",
                ("legacy", "https://example.test/", "Legacy", "keep me"),
            )
        migrated = Store(legacy_path)
        context = migrated.get_context("legacy")
        self.assertEqual(context["note"], "keep me")
        self.assertEqual(context["codex_note"], "keep me")
        self.assertFalse(context["notes_independent"])


class PromptFallbackPageTests(unittest.TestCase):
    def test_fallback_page_is_human_readable_and_escapes_result(self):
        body = prompt_action_fallback_html(
            "514458",
            "copy",
            {"ok": False, "error": "<bad>"},
        )
        self.assertIn("Copia prompt · prompt 514458", body)
        self.assertIn("fallback sicuro", body)
        self.assertIn("&lt;bad&gt;", body)
        self.assertNotIn('{"ok":false', body)

    def test_fallback_page_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "invalid_prompt_action"):
            prompt_action_fallback_html("514458", "unknown", {"ok": False})


if __name__ == "__main__":
    unittest.main()
