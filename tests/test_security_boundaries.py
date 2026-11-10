from email.message import Message
import os
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from chrome_codex_switcher import daemon, http_boundary
from chrome_codex_switcher.broker import EventBroker
from chrome_codex_switcher.store import Store

THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class HttpBoundaryTests(unittest.TestCase):
    def request(self, path, *, post=False, marked=False, origin=None, peer=True, payload=None):
        handler = object.__new__(daemon.Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = f"127.0.0.1:{daemon.PORT}"
        if marked:
            handler.headers[daemon.REQUEST_HEADER] = "1"
        if origin:
            handler.headers["Origin"] = origin
        handler.connection = Mock()
        handler._json = Mock()
        handler._html = Mock()
        handler._read_json = Mock(return_value=payload or {})
        app = Mock()
        with patch.object(daemon, "APP", app), patch.object(daemon, "same_user_tcp_peer", return_value=peer):
            (handler.do_POST if post else handler.do_GET)()
        return handler, app

    def test_private_api_requires_same_user_and_request_marker(self):
        for marked, peer, origin in ((False, True, None), (True, False, None),
                                     (True, True, "https://example.invalid")):
            handler, app = self.request("/api/context?context_id=fixture", marked=marked, peer=peer, origin=origin)
            self.assertEqual(handler._json.call_args.args[0], 403)
            app.store.get_context.assert_not_called()

    def test_public_readiness_omits_private_runtime_fields(self):
        handler, app = self.request("/api/health")
        self.assertEqual(handler._json.call_args.args, (200, {"ok": True}))
        app.health.assert_not_called()

    def test_prompt_get_only_produces_confirmation(self):
        with patch.object(daemon, "run_prompt_fallback_action") as action:
            handler, _ = self.request("/ui/prompt/123456/launch")
        action.assert_not_called()
        self.assertIn('<form method="post"', handler._html.call_args.args[1])

    def test_verification_get_has_no_operational_effect(self):
        with patch("chrome_codex_switcher.verifier.verify_prompt") as verify:
            handler, _ = self.request("/api/verify/prompt/123456", marked=True)
        verify.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0], 405)

    def test_confirmation_is_action_bound_and_single_use(self):
        grants = daemon.PromptConfirmations()
        key = grants.issue("123456", "launch")
        self.assertFalse(grants.consume(key, "123456", "codex"))
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(grants.consume(key, "123456", "launch"))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), [False, True])

    def test_confirmation_requires_same_origin(self):
        with patch.object(daemon, "run_prompt_fallback_action") as action:
            handler, _ = self.request("/ui/prompt/123456/launch", post=True, payload={"grant": "fixture"})
        action.assert_not_called()
        handler._read_json.assert_not_called()
        self.assertEqual(handler._json.call_args.args[0], 403)

    def test_negative_body_length_is_rejected_before_read(self):
        handler = object.__new__(daemon.Handler)
        handler.headers = Message()
        handler.headers["Content-Length"] = "-1"
        handler.rfile = Mock()
        with self.assertRaises(ValueError):
            handler._read_json()
        handler.rfile.read.assert_not_called()


class PeerOwnershipTests(unittest.TestCase):
    def test_kernel_accepts_an_isolated_same_uid_connection(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with socket.create_connection(listener.getsockname()) as client:
                connection, _ = listener.accept()
                with connection:
                    self.assertTrue(http_boundary.same_user_tcp_peer(connection))

    def test_response_must_match_kernel_and_exact_tuple(self):
        identity = http_boundary.socket_tuple(("127.0.0.1", 41000), ("127.0.0.1", 43817))
        body = (bytes((socket.AF_INET, 1, 0, 0)) + identity
                + http_boundary.IDENTITY_TRAILER.pack(0, 1, 2)
                + http_boundary.STATS.pack(0, 0, 0, os.getuid(), 123))
        packet = http_boundary.HEADER.pack(16 + len(body), 20, 0, 1, 0) + body
        self.assertEqual(http_boundary.response_uid(packet, (0, 0), identity), os.getuid())
        self.assertIsNone(http_boundary.response_uid(packet, (7, 0), identity))
        self.assertIsNone(http_boundary.response_uid(packet, (0, 0), b"x" * len(identity)))
        self.assertIsNone(http_boundary.response_uid(packet[:20], (0, 0), identity))


class PairingIntegrityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")
        self.app = daemon.App(self.store, EventBroker(), Mock())
        self.context = {"context_id": "fixture", "url": "https://chatgpt.com/c/fixture", "title": "Fixture"}

    def test_clipboard_only_proposes_and_confirmation_pairs_once(self):
        self.app.arm_link(self.context)
        proposed = self.app.handle_clipboard("codex://threads/" + THREAD)
        self.assertEqual(proposed["action"], "confirmation_required")
        self.assertIsNone(self.store.twin_by_context("fixture"))
        self.assertIsNone(self.store.get_meta("active_codex_thread"))
        proposal_id = proposed["proposal"]["id"]
        self.assertTrue(self.app.confirm_clipboard_proposal(proposal_id)["ok"])
        self.assertEqual(self.store.twin_by_context("fixture")["codex_thread"], THREAD)
        self.assertFalse(self.app.confirm_clipboard_proposal(proposal_id)["ok"])

    def test_changed_intent_and_invalid_identifiers_cannot_pair(self):
        self.app.arm_link(self.context)
        self.assertTrue(self.app.handle_clipboard("codex://threads/not-a-uuid")["ignored"])
        proposed = self.app.handle_clipboard("codex://threads/" + THREAD)
        self.store.set_meta("pending_link", {"context_id": "another", "expires_at": 9999999999})
        with self.assertRaisesRegex(ValueError, "pairing_intent_changed"):
            self.app.confirm_clipboard_proposal(proposed["proposal"]["id"])
        self.assertIsNone(self.store.twin_by_context("fixture"))

    def test_new_arm_replaces_older_prompt_intent(self):
        self.store.set_meta("pending_prompt_codex", {"prompt_id": "123456", "expires_at": 9999999999})
        old = self.app.handle_clipboard("codex://threads/" + THREAD)["proposal"]
        self.app.arm_link(self.context)
        self.assertIsNone(self.store.get_meta("pending_prompt_codex"))
        with self.assertRaisesRegex(ValueError, "pairing_intent_changed"):
            self.app.confirm_clipboard_proposal(old["id"])
        proposal = self.app.handle_clipboard("codex://threads/" + OTHER)["proposal"]
        self.assertEqual(proposal["context_id"], "fixture")
        self.assertTrue(self.app.confirm_clipboard_proposal(proposal["id"])["ok"])
        self.assertIsNone(self.store.prompt_binding("123456"))

    def test_proposal_cannot_pair_after_reviewed_context_navigates(self):
        self.app.arm_link(self.context)
        proposal = self.app.handle_clipboard("codex://threads/" + THREAD)["proposal"]
        self.assertEqual(proposal["url"], self.context["url"])
        self.store.upsert_context("fixture", "https://chatgpt.com/c/changed")
        with self.assertRaisesRegex(ValueError, "clipboard_target_changed"):
            self.app.confirm_clipboard_proposal(proposal["id"])
        self.assertIsNone(self.store.twin_by_context("fixture"))

    def test_codex_only_proposal_cannot_adopt_a_later_chrome_binding(self):
        self.store.set_meta("pending_prompt_codex", {"prompt_id": "123456", "expires_at": 9999999999})
        proposal = self.app.handle_clipboard("codex://threads/" + THREAD)["proposal"]
        self.assertIsNone(proposal["context_id"])
        self.app.upsert_context(self.context)
        self.store.bind_prompt("123456", context_id="fixture")
        with self.assertRaisesRegex(ValueError, "clipboard_target_changed"):
            self.app.confirm_clipboard_proposal(proposal["id"])
        self.assertIsNone(self.store.prompt_binding("123456")["codex_thread"])

    def test_foreign_ownership_conflict_preserves_both_contexts(self):
        self.store.upsert_context("first", "https://example.invalid/first")
        self.store.upsert_context("second", "https://example.invalid/second")
        self.store.link("first", THREAD, "codex://threads/" + THREAD)
        before = self.store.get_context("first")
        with self.assertRaisesRegex(ValueError, "already_linked"):
            self.store.link("second", THREAD, "codex://threads/" + THREAD)
        self.assertEqual(before, self.store.get_context("first"))
        self.assertIsNone(self.store.twin_by_context("second"))

    def test_failed_canonical_update_rolls_back_twin_and_pending_claim(self):
        self.store.upsert_context("fixture", "https://example.invalid/")
        self.store.bind_prompt("123456", context_id="fixture")
        self.store.link("fixture", THREAD, "codex://threads/" + THREAD)
        before = self.store.get_context("fixture")
        intent = {"context_id": "fixture", "expires_at": 9999999999}
        self.store.set_meta("pending_link", intent)
        with self.store._connect() as db:
            db.execute("CREATE TRIGGER reject_binding BEFORE UPDATE ON prompt_bindings BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.link("fixture", OTHER, "codex://threads/" + OTHER,
                            pending=("pending_link", intent, "https://example.invalid/"))
        self.assertEqual(before, self.store.get_context("fixture"))
        self.assertEqual(intent, self.store.get_meta("pending_link"))

    def test_database_is_private_under_permissive_umask(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
