"""Activity projection contract: only explicit transitions may notify."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch, mock_open

from chrome_codex_switcher.activity import ActivityMonitor, _safe_web_url, telegram_send
from chrome_codex_switcher.activity_ui import activity_dashboard_html
from chrome_codex_switcher.broker import EventBroker
from chrome_codex_switcher.daemon import App
from chrome_codex_switcher.store import Store

THREAD = "01a150e4-22a8-7fa0-8b70-8529bc7df1ae"


def event(kind: str, turn: str, timestamp: float) -> str:
    return json.dumps({
        "type": "event_msg",
        "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "payload": {"type": kind, "turn_id": turn}
    }) + "\n"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock_value = time.time()
        self.sent = []
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.db = self.root / "state.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("""CREATE TABLE threads(
                id TEXT, title TEXT, name TEXT, rollout_path TEXT,
                archived INTEGER, recency_at_ms INTEGER, updated_at_ms INTEGER, created_at_ms INTEGER,
                source TEXT, thread_source TEXT, originator TEXT
            )""")
        self.monitor = ActivityMonitor(
            session_db=self.db, sessions_root=self.sessions,
            sender=self.sent.append, clock=lambda: self.clock_value
        )

    def record_thread(self, path: Path, *, source="vscode", originator="Codex Desktop",
                      thread_source="user", title="Sistema di backup"):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, NULL, ?, 0, ?, ?, ?, ?, ?, ?)",
                (THREAD, title, str(path), int(self.clock_value * 1000),
                 int(self.clock_value * 1000), int(self.clock_value * 1000),
                 source, thread_source, originator)
            )

    def test_telegram_uses_existing_personal_notification_transport(self):
        settings = ("TELEGRAM_BOT_TOKEN=unrelated\nTELEGRAM_CHAT_ID=old\n"
                    "TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN=notification-token\n"
                    "TELEGRAM_INSERT_BOT_NOISY_CHAT_ID=123\n")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"result":{"message_id":123}}'
        with patch.dict('os.environ', {'CREDENTIALS_DIRECTORY': '/credentials'}), \
             patch('pathlib.Path.read_text', return_value=settings), \
             patch('chrome_codex_switcher.activity.urlopen', return_value=response) as send:
            telegram_send({'source':'web', 'status':'finished', 'title':'Test',
                           'url':'https://chatgpt.com/c/test'})
        request = send.call_args.args[0]
        self.assertIn('notification-token', request.full_url)
        self.assertEqual('123', json.loads(request.data)['chat_id'])

    def test_delivery_receipt_survives_monitor_restart_without_message_text(self):
        delivery_store = Store(self.root / "notification-receipts.sqlite")
        item = {'source':'desktop', 'status':'finished', 'title':'private title',
                'url':'codex://threads/' + THREAD}
        monitor = ActivityMonitor(sender=self.sent.append,
            delivery_put=lambda receipt: delivery_store.set_meta("delivery",receipt))
        with patch('chrome_codex_switcher.activity.telegram_send', return_value={
            'status':'accepted','message_id':123}):
            monitor._deliver(item)
        reopened_store = Store(delivery_store.path)
        restarted = ActivityMonitor(sender=self.sent.append,
            delivery_get=lambda: reopened_store.get_meta("delivery"))
        receipt = restarted.snapshot()['notification_delivery']
        self.assertEqual(receipt['status'],'accepted')
        self.assertEqual(receipt['message_id'],123)
        self.assertNotIn('private title',json.dumps(receipt))

    def test_missing_credentials_and_timeout_are_not_reported_as_delivery(self):
        from urllib.error import URLError
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(telegram_send({})['status'],'unavailable')
        settings = ('TELEGRAM_INSERT_BOT_NOISY_BOT_TOKEN=test\n'
                    'TELEGRAM_INSERT_BOT_NOISY_CHAT_ID=123\n')
        with patch.dict('os.environ', {'CREDENTIALS_DIRECTORY':'/credentials'}), \
             patch('pathlib.Path.read_text', return_value=settings), \
             patch('chrome_codex_switcher.activity.urlopen', side_effect=URLError('timeout')) as send:
            receipt=telegram_send({'source':'web','status':'finished','title':'Test',
                                   'url':'https://chatgpt.com/c/test'})
        self.assertEqual(receipt['status'],'uncertain')
        self.assertEqual(send.call_count,1)

    def test_web_activity_and_single_completion_notification(self):
        url = "https://chatgpt.com/c/abc?a=2#private"
        base = {"url": url, "title": "Chat test", "tab_id": 22}
        self.monitor.observe_web({**base, "status": "idle"})
        self.assertEqual([], self.sent)
        self.assertEqual(0, self.monitor.snapshot()["active"])
        self.monitor.observe_web({**base, "status": "running"})
        self.assertEqual(1, self.monitor.snapshot()["active"])
        self.monitor.observe_web({**base, "status": "running"})
        self.monitor.observe_web({**base, "status": "idle"})
        self.monitor.observe_web({**base, "status": "idle"})
        self.assertEqual(1, len(self.sent))
        self.assertEqual("https://chatgpt.com/c/abc", self.sent[0]["url"])
        self.assertEqual(1, self.monitor.snapshot()["recent"])

    def test_tab_close_and_navigation_never_fake_completion(self):
        payload = {"tab_id": 8, "title": "Chat", "status": "running"}
        self.monitor.observe_web({**payload, "url": "https://chatgpt.com/c/abc"})
        self.monitor.observe_web({**payload, "url": "https://chatgpt.com/c/def"})
        self.monitor.remove_web_tab({"tab_id": 8})
        self.assertEqual([], self.sent)
        self.assertEqual([], self.monitor.snapshot()["chats"])

    def test_stale_web_tab_never_fires_finished(self):
        self.monitor.observe_web({"tab_id": 8, "title": "Chat", "status": "running",
                                  "url": "https://chatgpt.com/c/abc"})
        self.clock_value += 121
        self.assertEqual(0, self.monitor.snapshot()["active"])
        self.assertEqual([], self.sent)

    def test_reject_unsafe_web_urls(self):
        for url in ("http://chatgpt.com/c/abc", "https://fakechatgpt.com/c/abc",
                    "https://chatgpt.com.evil.test/c/abc", "https://chatgpt.com/settings"):
            self.assertIsNone(_safe_web_url(url))
        with self.assertRaises(ValueError):
            self.monitor.observe_web({"tab_id": 1, "status": "running", "url": "https://evil.test/"})

    def test_desktop_initial_state_new_completion_and_second_turn(self):
        path = self.sessions / (THREAD + ".jsonl")
        path.write_text(event("task_started", "turn-one", self.clock_value - 20))
        self.record_thread(path)
        self.monitor.poll_desktop()
        self.assertEqual(1, self.monitor.snapshot()["active"])
        self.assertEqual([], self.sent)

        with path.open("a") as f:
            f.write(event("task_complete", "turn-one", self.clock_value + 1))
        self.monitor.poll_desktop()
        self.monitor.poll_desktop()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("finished", self.sent[0]["status"])
        self.assertEqual("codex://threads/" + THREAD, self.sent[0]["url"])

        with path.open("a") as f:
            f.write(event("task_started", "turn-two", self.clock_value + 2))
            f.write(event("task_complete", "turn-two", self.clock_value + 3))
        self.monitor.poll_desktop()
        self.assertEqual(1, len(self.sent))  # Same Telegram text, despite a new turn ID.

        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE threads SET title=? WHERE id=?", ("Nuovo compito", THREAD))
        with path.open("a") as f:
            f.write(event("task_started", "turn-three", self.clock_value + 4))
            f.write(event("task_complete", "turn-three", self.clock_value + 5))
        self.monitor.poll_desktop()
        self.assertEqual(2, len(self.sent))  # Changed message is still delivered.

    def test_identical_desktop_notifications_stay_suppressed_after_restart(self):
        path = self.sessions / (THREAD + ".jsonl")
        path.write_text(event("task_started", "one", self.clock_value - 10))
        self.record_thread(path)
        store = Store(self.root / "activity-notifications.sqlite")
        get = lambda: store.get_meta("activity_telegram_desktop_notified")
        put = lambda state: store.set_meta("activity_telegram_desktop_notified", state)
        kwargs = dict(session_db=self.db, sessions_root=self.sessions,
                      sender=self.sent.append, notification_state_get=get,
                      notification_state_put=put)
        monitor = ActivityMonitor(**kwargs, clock=lambda: self.clock_value)
        monitor.poll_desktop()
        with path.open("a") as f:
            f.write(event("task_complete", "one", self.clock_value + 1))
        monitor.poll_desktop()
        self.assertEqual(1, len(self.sent))
        self.assertNotIn("Sistema di backup", json.dumps(get()))  # No title stored.

        restarted = ActivityMonitor(**kwargs, clock=lambda: self.clock_value + 2)
        restarted.poll_desktop()
        with path.open("a") as f:
            f.write(event("task_started", "two", self.clock_value + 3))
            f.write(event("task_complete", "two", self.clock_value + 4))
        restarted.poll_desktop()
        self.assertEqual(1, len(self.sent))

    def test_notification_state_persistence_failure_fails_closed(self):
        path = self.sessions / (THREAD + ".jsonl")
        path.write_text(event("task_started", "one", self.clock_value - 10))
        self.record_thread(path)
        monitor = ActivityMonitor(session_db=self.db, sessions_root=self.sessions,
            sender=self.sent.append, clock=lambda: self.clock_value,
            notification_state_put=Mock(side_effect=sqlite3.OperationalError("locked")))
        monitor.poll_desktop()
        with path.open("a") as f:
            f.write(event("task_complete", "one", self.clock_value + 1))
        monitor.poll_desktop()
        self.assertEqual([], self.sent)

    def test_old_completed_session_never_notifies(self):
        path = self.sessions / "old.jsonl"
        path.write_text(event("task_started", "t1", self.clock_value - 90)
                        + event("task_complete", "t1", self.clock_value - 89))
        self.record_thread(path)
        self.monitor.poll_desktop()
        self.assertEqual(0, len(self.sent))

    def test_incomplete_jsonl_is_retried_without_false_notification(self):
        path = self.sessions / "partial.jsonl"
        path.write_text(event("task_started", "t1", self.clock_value - 5))
        self.record_thread(path)
        self.monitor.poll_desktop()
        complete = event("task_complete", "t1", self.clock_value + 1)
        with path.open("a") as f:
            f.write(complete[:-3])
        self.monitor.poll_desktop()
        self.assertEqual([], self.sent)
        with path.open("a") as f:
            f.write(complete[-3:])
        self.monitor.poll_desktop()
        self.assertEqual(1, len(self.sent))

    def test_existing_partial_jsonl_can_complete_after_first_scan(self):
        path = self.sessions / "partial-first.jsonl"
        first = event("task_started", "t1", self.clock_value + 1)
        path.write_text(first[:-4])
        self.record_thread(path)
        self.monitor.poll_desktop()
        self.assertEqual([], self.sent)
        with path.open("a") as f:
            f.write(first[-4:])
        self.monitor.poll_desktop()
        self.assertEqual(1, self.monitor.snapshot()["active"])

    def test_only_user_visible_desktop_threads_are_selected(self):
        path = self.sessions / "cli.jsonl"
        path.write_text(event("task_started", "t1", self.clock_value - 10))
        self.record_thread(path, source="cli", originator="codex-tui")
        self.monitor.poll_desktop()
        self.assertEqual(0, self.monitor.snapshot()["active"])

    def test_no_untrusted_path(self):
        external = self.root / "elsewhere.jsonl"
        external.write_text(event("task_started", "t1", self.clock_value))
        self.record_thread(external)
        self.monitor.poll_desktop()
        self.assertEqual(0, self.monitor.snapshot()["active"])

    def test_page_and_open_routing(self):
        self.assertIn("/api/activity/open", activity_dashboard_html())
        app = App(Store(self.root / "ccs.sqlite"), EventBroker(), Mock())
        app.open_activity({"url": "codex://threads/" + THREAD})
        app._open_codex.assert_called_once_with("codex://threads/" + THREAD)
        result = app.open_activity({"url": "https://chatgpt.com/c/abc"})
        self.assertEqual("focus_chrome_queued", result["action"])
        with self.assertRaises(ValueError):
            app.open_activity({"url": "codex://threads/injection"})
        with self.assertRaises(ValueError):
            app.open_activity({"url": "file:///etc/passwd"})


if __name__ == "__main__":
    unittest.main()
