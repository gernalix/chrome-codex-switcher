from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .broker import EventBroker
from .store import Store
from .util import canonical_url, db_path, now, overlay_path, parse_codex_link, write_json_atomic

HOST = os.environ.get("CCS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCS_PORT", "43817"))
EXTENSION_ID = "mfpomnbkkfklealhaacbnmelpgpggglg"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
PENDING_TTL = 120.0
CLIPBOARD_DUPLICATE_WINDOW = 1.0


class App:
    def __init__(self, store: Store | None = None, broker: EventBroker | None = None, open_codex=None):
        self.store = store or Store(db_path())
        self.broker = broker or EventBroker()
        self._open_codex = open_codex or self._default_open_codex
        self._clipboard_process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_clipboard_thread: str | None = None
        self._last_clipboard_at = 0.0
        self.settings = self.store.get_meta("settings", {"auto_switch_on_codex_copy": True})
        if "auto_switch_on_codex_copy" not in self.settings:
            self.settings["auto_switch_on_codex_copy"] = True

    @staticmethod
    def _default_open_codex(deep_link: str) -> None:
        subprocess.Popen(
            ["gio", "open", deep_link],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def start_clipboard_watch(self) -> None:
        if os.environ.get("CCS_DISABLE_CLIPBOARD_WATCH") == "1":
            return
        if not shutil.which("wl-paste"):
            print("chrome-codex-switcher: wl-paste not found; clipboard auto-switch disabled", file=sys.stderr)
            return
        cmd = ["wl-paste", "--type", "text", "--watch", sys.executable, "-m", "chrome_codex_switcher.clip_event"]
        try:
            self._clipboard_process = subprocess.Popen(cmd, start_new_session=True)
        except OSError as exc:
            print(f"chrome-codex-switcher: clipboard watcher failed: {exc}", file=sys.stderr)

    def stop_clipboard_watch(self) -> None:
        proc = self._clipboard_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": "0.1.0",
            "host": HOST,
            "port": PORT,
            "clipboard_watch": bool(self._clipboard_process and self._clipboard_process.poll() is None),
            "auto_switch_on_codex_copy": bool(self.settings.get("auto_switch_on_codex_copy", True)),
        }

    def upsert_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload["context_id"])
        url = canonical_url(str(payload.get("url", "")))
        title = str(payload.get("title", ""))
        return self.store.upsert_context(context_id, url, title)

    def arm_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.upsert_context(payload)
        pending = {
            "context_id": context["id"],
            "url": context["url"],
            "title": context.get("title", ""),
            "armed_at": now(),
            "expires_at": now() + PENDING_TTL,
        }
        self.store.set_meta("pending_link", pending)
        self.broker.emit("link_armed", pending)
        return {"ok": True, "pending": pending}

    def switch_from_chrome(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.upsert_context(payload)
        twin = self.store.twin_by_context(context["id"])
        if not twin:
            return {"ok": False, "error": "not_linked"}
        self.store.set_meta("active_codex_thread", twin["codex_thread"])
        self._write_overlay_for_thread(twin["codex_thread"])
        self._open_codex(twin["codex_deep_link"])
        return {"ok": True, "twin": twin}

    def handle_clipboard(self, text: str) -> dict[str, Any]:
        parsed = parse_codex_link(text)
        if not parsed:
            return {"ok": True, "ignored": True}
        thread, deep_link = parsed
        seen_at = now()
        with self._lock:
            if (
                self._last_clipboard_thread == thread
                and seen_at - self._last_clipboard_at < CLIPBOARD_DUPLICATE_WINDOW
            ):
                return {"ok": True, "ignored": True, "duplicate": True, "thread": thread}
            self._last_clipboard_thread = thread
            self._last_clipboard_at = seen_at
        self.store.set_meta("active_codex_thread", thread)

        pending = self.store.get_meta("pending_link")
        if pending:
            if float(pending.get("expires_at", 0)) >= now():
                context_id = str(pending["context_id"])
                context = self.store.get_context(context_id)
                if context:
                    linked = self.store.link(context_id, thread, deep_link)
                    self.store.delete_meta("pending_link")
                    self._write_overlay_for_thread(thread)
                    self.broker.emit(
                        "linked",
                        {
                            "context_id": context_id,
                            "codex_thread": thread,
                            "codex_deep_link": deep_link,
                        },
                    )
                    return {"ok": True, "action": "linked", "context": linked}
            else:
                self.store.delete_meta("pending_link")
                self.broker.emit("link_expired", {"context_id": pending.get("context_id")})

        twin = self.store.twin_by_thread(thread)
        self._write_overlay_for_thread(thread)
        if twin and bool(self.settings.get("auto_switch_on_codex_copy", True)):
            payload = {
                "context_id": twin["context_id"],
                "url": twin["url"],
                "title": twin.get("title", ""),
                "codex_thread": thread,
            }
            self.broker.emit("focus_chrome", payload)
            return {"ok": True, "action": "focus_chrome", "target": payload}
        return {"ok": True, "action": "active_thread_updated", "linked": bool(twin)}

    def set_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.store.set_note(str(payload["context_id"]), str(payload.get("note", "")))
        if not context:
            return {"ok": False, "error": "context_not_found"}
        twin = context.get("twin")
        if twin and self.store.get_meta("active_codex_thread") == twin.get("codex_thread"):
            self._write_overlay_for_thread(twin["codex_thread"])
        return {"ok": True, "context": context}

    def set_ui(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self.store.set_ui(
            str(payload["context_id"]),
            geometry=payload.get("geometry"),
            hidden=payload.get("hidden") if "hidden" in payload else None,
            collapsed=payload.get("collapsed") if "collapsed" in payload else None,
        )
        return {"ok": bool(context), "context": context}

    def unlink(self, context_id: str) -> dict[str, Any]:
        self.store.unlink_context(context_id)
        self.broker.emit("unlinked", {"context_id": context_id})
        return {"ok": True}

    def set_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"auto_switch_on_codex_copy"}
        for key in allowed:
            if key in payload:
                self.settings[key] = bool(payload[key])
        self.store.set_meta("settings", self.settings)
        return {"ok": True, "settings": self.settings}

    def _write_overlay_for_thread(self, thread: str) -> None:
        twin = self.store.twin_by_thread(thread)
        if twin:
            state = {
                "visible": True,
                "codex_thread": thread,
                "context_id": twin["context_id"],
                "title": twin.get("title", ""),
                "note": twin.get("note", ""),
                "url": twin.get("url", ""),
                "updated_at": now(),
            }
        else:
            state = {
                "visible": False,
                "codex_thread": thread,
                "context_id": None,
                "title": "",
                "note": "",
                "url": "",
                "updated_at": now(),
            }
        write_json_atomic(overlay_path(), state)


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ChromeCodexSwitcher/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("CCS_HTTP_LOG") == "1":
            super().log_message(fmt, *args)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        # No Origin = trusted local helper/CLI. Browser callers must be our extension.
        return origin is None or origin == EXTENSION_ORIGIN

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin == EXTENSION_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("payload_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("object_required")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_forbidden"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        assert APP is not None
        if not self._origin_allowed():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_forbidden"})
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._json(HTTPStatus.OK, APP.health())
            elif parsed.path == "/api/context":
                context_id = query.get("context_id", [""])[0]
                context = APP.store.get_context(context_id)
                self._json(HTTPStatus.OK, {"ok": bool(context), "context": context})
            elif parsed.path == "/api/list":
                self._json(HTTPStatus.OK, {"ok": True, "contexts": APP.store.list_contexts()})
            elif parsed.path == "/api/events":
                after = int(query.get("after", ["0"])[0])
                timeout = min(28.0, max(0.0, float(query.get("timeout", ["25"])[0])))
                seq, events = APP.broker.wait_after(after, timeout)
                self._json(HTTPStatus.OK, {"ok": True, "seq": seq, "events": events})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except (ValueError, KeyError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        assert APP is not None
        if not self._origin_allowed():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_forbidden"})
            return
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/context":
                self._json(HTTPStatus.OK, {"ok": True, "context": APP.upsert_context(payload)})
            elif parsed.path == "/api/note":
                self._json(HTTPStatus.OK, APP.set_note(payload))
            elif parsed.path == "/api/ui":
                self._json(HTTPStatus.OK, APP.set_ui(payload))
            elif parsed.path == "/api/arm-link":
                self._json(HTTPStatus.OK, APP.arm_link(payload))
            elif parsed.path == "/api/switch-from-chrome":
                self._json(HTTPStatus.OK, APP.switch_from_chrome(payload))
            elif parsed.path == "/api/clipboard":
                text = payload.get("text")
                if text is None:
                    text = parse_qs(parsed.query).get("text", [""])[0]
                self._json(HTTPStatus.OK, APP.handle_clipboard(str(text)))
            elif parsed.path == "/api/unlink":
                self._json(HTTPStatus.OK, APP.unlink(str(payload["context_id"])))
            elif parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, APP.set_settings(payload))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:  # keep daemon alive; surface concise failure to caller
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"internal:{type(exc).__name__}"})


def serve() -> None:
    global APP
    APP = App()
    APP.start_clipboard_watch()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    def stop(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        APP.stop_clipboard_watch()
        server.server_close()


if __name__ == "__main__":
    serve()
