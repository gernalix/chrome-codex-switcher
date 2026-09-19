from __future__ import annotations

import json
import os
import shutil
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from .a11y_watch import CodexA11yWatch
from .broker import EventBroker
from .store import Store
from .util import cache_dir, canonical_url, db_path, now, overlay_path, parse_codex_link, write_json_atomic
from .xfixes_watch import XFixesWatch

HOST = os.environ.get("CCS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCS_PORT", "43817"))
EXTENSION_ID = "mfpomnbkkfklealhaacbnmelpgpggglg"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
PENDING_TTL = 120.0
CLIPBOARD_DUPLICATE_WINDOW = 1.0
PROMPT_ID_RE = re.compile(r"\d{6}\Z")
PROMPT_PENDING_TTL = 10 * 60.0


class App:
    def __init__(self, store: Store | None = None, broker: EventBroker | None = None, open_codex=None):
        self.store = store or Store(db_path())
        self.broker = broker or EventBroker()
        self._open_codex = open_codex or self._default_open_codex
        self._clipboard_process: subprocess.Popen | None = None
        self._xfixes_watch: XFixesWatch | None = None
        self._a11y_watch: CodexA11yWatch | None = None
        self._lock = threading.Lock()
        self._last_clipboard_thread: str | None = None
        self._last_clipboard_at = 0.0
        self._active_codex_title: str | None = None
        self._expected_codex_thread: str | None = None
        self._expected_previous_title: str | None = None
        self._expected_until = 0.0
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
        if shutil.which("wl-paste"):
            watch = XFixesWatch(self._read_xfixes_clipboard)
            if watch.start():
                self._xfixes_watch = watch
                return
        if not shutil.which("wl-paste"):
            print("chrome-codex-switcher: wl-paste not found; clipboard auto-switch disabled", file=sys.stderr)
            return
        cmd = ["wl-paste", "--type", "text", "--watch", sys.executable, "-m", "chrome_codex_switcher.clip_event"]
        try:
            self._clipboard_process = subprocess.Popen(cmd, start_new_session=True)
        except OSError as exc:
            print(f"chrome-codex-switcher: clipboard watcher failed: {exc}", file=sys.stderr)

    def _read_xfixes_clipboard(self) -> None:
        # XWayland can announce the new owner before its Wayland text offer is ready.
        for delay in (0.15, 0.25, 0.35):
            time.sleep(delay)
            try:
                proc = subprocess.run(
                    ["wl-paste", "--type", "text", "--no-newline"],
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode or len(proc.stdout) > 4096:
                continue
            text = proc.stdout.decode("utf-8", errors="replace").strip()
            if text.lower().startswith("codex://threads/") and "\n" not in text:
                self.handle_clipboard(text)
                return

    def stop_clipboard_watch(self) -> None:
        proc = self._clipboard_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def start_active_thread_watch(self) -> None:
        if self._a11y_watch and self._a11y_watch.active:
            return
        watcher = CodexA11yWatch(self.handle_codex_ui_state)
        watcher.start()
        self._a11y_watch = watcher

    def stop_active_thread_watch(self) -> None:
        if self._a11y_watch:
            self._a11y_watch.stop()

    @staticmethod
    def _clean_codex_title(value: Any) -> str | None:
        title = " ".join(str(value or "").split()).strip()
        return title or None

    def handle_codex_ui_state(self, focused: bool, title: str | None) -> dict[str, Any]:
        if not focused:
            return {"ok": True, "action": "codex_unfocused"}

        clean = self._clean_codex_title(title)
        if not clean:
            self._active_codex_title = None
            self._write_overlay_hidden("active_thread_unknown")
            return {"ok": True, "action": "overlay_hidden", "reason": "active_thread_unknown"}

        previous = self._active_codex_title
        self._active_codex_title = clean
        thread = self.store.thread_by_codex_title(clean)

        expected = self._expected_codex_thread
        if not thread and expected and now() <= self._expected_until:
            before = self._expected_previous_title
            if before is None or clean.casefold() != before.casefold():
                self.store.remember_codex_title(expected, clean)
                thread = expected

        if thread:
            self._expected_codex_thread = None
            self._expected_previous_title = None
            self._expected_until = 0.0
            self.store.set_meta("active_codex_thread", thread)
            self._write_overlay_for_thread(thread, codex_title=clean)
            return {"ok": True, "action": "active_thread_resolved", "thread": thread, "title": clean}

        reason = "active_thread_unmapped" if previous != clean else "active_thread_unresolved"
        self._write_overlay_hidden(reason, codex_title=clean)
        return {"ok": True, "action": "overlay_hidden", "reason": reason, "title": clean}

    def invalidate_codex_overlay(self, reason: str = "possible_thread_change") -> dict[str, Any]:
        self._active_codex_title = None
        self._write_overlay_hidden(reason)
        if self._a11y_watch:
            self._a11y_watch.refresh()
        return {"ok": True, "action": "overlay_hidden", "reason": reason}

    def health(self) -> dict[str, Any]:
        gnome = self.store.get_meta("gnome_runtime")
        if not isinstance(gnome, dict):
            gnome = None
        gnome_summary = None
        if gnome:
            gnome_summary = {
                key: gnome.get(key)
                for key in (
                    "seen_at",
                    "focused_codex",
                    "visible",
                    "context_id",
                    "codex_thread",
                    "notes_independent",
                )
            }
        return {
            "ok": True,
            "version": "0.3.0",
            "host": HOST,
            "port": PORT,
            "clipboard_watch": bool(self._clipboard_process and self._clipboard_process.poll() is None),
            "xfixes_watch": bool(self._xfixes_watch and self._xfixes_watch.active),
            "auto_switch_on_codex_copy": bool(self.settings.get("auto_switch_on_codex_copy", True)),
            "extension_runtime": self.store.get_meta("extension_runtime"),
            "gnome_runtime": gnome_summary,
            "codex_a11y_watch": bool(self._a11y_watch and self._a11y_watch.active),
            "codex_a11y_error": self._a11y_watch.error if self._a11y_watch else None,
            "active_codex_title": self._active_codex_title,
        }

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def gnome_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = {
            "seen_at": now(),
            "focused_codex": self._truthy(payload.get("focused_codex")),
            "visible": self._truthy(payload.get("visible")),
            "context_id": str(payload.get("context_id") or "") or None,
            "codex_thread": str(payload.get("codex_thread") or "") or None,
            "note": str(payload.get("note") or ""),
            "notes_independent": self._truthy(payload.get("notes_independent")),
        }
        self.store.set_meta("gnome_runtime", state)
        return {"ok": True, "gnome_runtime": state}

    def control_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("control_request_id_missing")
        ack = dict(payload)
        ack["request_id"] = request_id
        ack["seen_at"] = now()
        self.store.set_meta(f"control_ack:{request_id}", ack)
        return {"ok": True, "ack": ack}

    def request_chrome_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        action = str(payload.get("action") or "probe").strip().lower()
        if action not in {"probe", "focus", "ensure"}:
            raise ValueError("invalid_control_action")
        binding = self.store.prompt_binding(prompt_id)
        if action != "ensure" and (not binding or not binding.get("context_id")):
            return {"ok": False, "error": "chrome_context_missing"}
        request_id = uuid.uuid4().hex
        event_payload = {
            "request_id": request_id,
            "action": action,
            "prompt_id": prompt_id,
            "context_id": binding.get("context_id") if binding else None,
            "url": binding.get("url") if binding else None,
            "title": binding.get("title") if binding else None,
        }
        self.broker.emit("control_chrome", event_payload)
        return {"ok": True, "request_id": request_id, "request": event_payload}

    def verify_prompt_snapshot(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = self._prompt_id(prompt_id)
        binding = self.store.prompt_binding(prompt_id)
        if not binding:
            return {
                "ok": False,
                "result": "BLOCKED",
                "prompt_id": prompt_id,
                "error": "binding_missing",
                "checks": {},
            }

        context_id = str(binding.get("context_id") or "")
        thread = str(binding.get("codex_thread") or "")
        deep_link = str(binding.get("codex_deep_link") or "")
        context = self.store.get_context(context_id) if context_id else None
        twin = context.get("twin") if isinstance(context, dict) else None
        twin_consistent = bool(
            isinstance(twin, dict)
            and twin.get("codex_thread") == thread
            and twin.get("codex_deep_link") == deep_link
        )
        note_consistent = bool(
            isinstance(context, dict)
            and (
                bool(context.get("notes_independent"))
                or str(context.get("note") or "") == str(context.get("codex_note") or "")
            )
        )

        extension = self.store.get_meta("extension_runtime")
        extension_fresh = bool(
            isinstance(extension, dict)
            and extension.get("version")
            and now() - float(extension.get("seen_at") or 0) <= 90
        )
        gnome = self.store.get_meta("gnome_runtime")
        gnome_fresh = bool(
            isinstance(gnome, dict)
            and now() - float(gnome.get("seen_at") or 0) <= 10
        )

        overlay: dict[str, Any] = {}
        try:
            if overlay_path().is_file():
                value = json.loads(overlay_path().read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    overlay = value
        except (OSError, json.JSONDecodeError):
            overlay = {}

        overlay_consistent = True
        if overlay.get("visible") and overlay.get("codex_thread") == thread:
            overlay_consistent = overlay.get("context_id") == context_id
        if isinstance(gnome, dict) and gnome.get("visible") and gnome.get("codex_thread") == thread:
            overlay_consistent = bool(
                overlay_consistent
                and gnome.get("context_id") == context_id
                and overlay.get("context_id") == context_id
            )

        checks = {
            "context_id": bool(context_id),
            "codex_thread": bool(thread),
            "deep_link": bool(deep_link),
            "twin_consistent": twin_consistent,
            "note_consistent": note_consistent,
            "extension_heartbeat": extension_fresh,
            "gnome_heartbeat": gnome_fresh,
            "a11y": bool(self._a11y_watch and self._a11y_watch.active and not self._a11y_watch.error),
            "overlay_consistent": overlay_consistent,
        }
        passed = all(checks.values())
        return {
            "ok": passed,
            "result": "PASS" if passed else "BLOCKED",
            "prompt_id": prompt_id,
            "checks": checks,
            "binding": binding,
            "overlay": overlay,
            "error": None if passed else "runtime_snapshot_incomplete",
        }

    @staticmethod
    def _prompt_id(value: Any) -> str:
        prompt_id = str(value or "").strip()
        if not PROMPT_ID_RE.fullmatch(prompt_id):
            raise ValueError("invalid_prompt_id")
        return prompt_id

    def prompt_text(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = self._prompt_id(prompt_id)
        try:
            with urlopen(
                f"http://127.0.0.1:8765/roadmap/prompt/{prompt_id}",
                timeout=1.5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "roadmap_prompt_unavailable"}
        text = str(payload.get("prompt_text") or "")
        if not text:
            return {"ok": False, "error": "prompt_text_missing"}
        return {"ok": True, "prompt_id": prompt_id, "prompt_text": text}

    def prompt_binding(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = self._prompt_id(prompt_id)
        binding = self.store.prompt_binding(prompt_id)
        return {"ok": bool(binding), "binding": binding}

    def bind_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        context_id = str(payload.get("context_id") or "").strip() or None
        current = self.store.prompt_binding(prompt_id)
        if context_id:
            url = canonical_url(str(payload.get("url") or ""))
            title = str(payload.get("title") or "")
            if url:
                self.store.upsert_context(context_id, url, title)
            if current and current.get("codex_thread") and current.get("codex_deep_link"):
                binding = self.store.link_prompt(
                    prompt_id,
                    context_id,
                    str(current["codex_thread"]),
                    str(current["codex_deep_link"]),
                )
            else:
                binding = self.store.bind_prompt(prompt_id, context_id=context_id)
        else:
            binding = self.store.bind_prompt(prompt_id, context_id=context_id)
        self.broker.emit("prompt_bound", {"prompt_id": prompt_id, "context_id": context_id})
        return {"ok": True, "binding": binding}

    def arm_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        context_id = str(payload.get("context_id") or "").strip() or None
        if context_id:
            self.bind_prompt(payload)
        else:
            existing = self.store.prompt_binding(prompt_id)
            context_id = (
                str(existing.get("context_id"))
                if existing and existing.get("context_id")
                else None
            )
        if not context_id or not self.store.get_context(context_id):
            raise ValueError("prompt_context_missing")
        pending = {
            "prompt_id": prompt_id,
            "context_id": context_id,
            "armed_at": now(),
            "expires_at": now() + PROMPT_PENDING_TTL,
        }
        self.store.set_meta("pending_prompt", pending)
        self.broker.emit("prompt_armed", pending)
        return {"ok": True, "pending": pending}

    def open_prompt_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        binding = self.store.prompt_binding(prompt_id)
        if not binding or not binding.get("codex_deep_link") or not binding.get("codex_thread"):
            return {"ok": False, "error": "codex_not_linked"}
        thread = str(binding["codex_thread"])
        self._expected_codex_thread = thread
        self._expected_previous_title = self._active_codex_title
        self._expected_until = now() + 8.0
        self.store.set_meta("active_codex_thread", thread)
        self._write_overlay_for_thread(thread)
        self._open_codex(str(binding["codex_deep_link"]))
        return {"ok": True, "binding": binding}

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
        self._expected_codex_thread = str(twin["codex_thread"])
        self._expected_previous_title = self._active_codex_title
        self._expected_until = now() + 8.0
        self.store.set_meta("active_codex_thread", twin["codex_thread"])
        self._write_overlay_for_thread(twin["codex_thread"])
        self._open_codex(twin["codex_deep_link"])
        return {"ok": True, "twin": twin}

    def handle_clipboard(self, text: str, codex_title: str | None = None) -> dict[str, Any]:
        parsed = parse_codex_link(text)
        if not parsed:
            return {"ok": True, "ignored": True}
        thread, deep_link = parsed
        ui_title = self._clean_codex_title(codex_title) or self._active_codex_title
        if ui_title:
            self.store.remember_codex_title(thread, ui_title)
            self._active_codex_title = ui_title
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

        paired_prompt = False
        pending_prompt = self.store.get_meta("pending_prompt")
        if pending_prompt:
            if float(pending_prompt.get("expires_at", 0)) >= now():
                prompt_id = self._prompt_id(pending_prompt.get("prompt_id"))
                context_id = str(pending_prompt.get("context_id") or "").strip() or None
                if context_id:
                    binding = self.store.link_prompt(prompt_id, context_id, thread, deep_link)
                    self.store.delete_meta("pending_prompt")
                    self.broker.emit("prompt_linked", binding)
                    self.broker.emit("linked", {"context_id": context_id, "codex_thread": thread, "codex_deep_link": deep_link})
                    paired_prompt = True
                else:
                    self.store.delete_meta("pending_prompt")
            else:
                self.store.delete_meta("pending_prompt")

        pending = None if paired_prompt else self.store.get_meta("pending_link")
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
        self._write_overlay_for_thread(thread, codex_title=ui_title)
        if twin and bool(self.settings.get("auto_switch_on_codex_copy", True)):
            payload = {
                "context_id": twin["context_id"],
                "url": twin["url"],
                "title": twin.get("title", ""),
                "codex_thread": thread,
            }
            self.broker.emit("focus_chrome", payload)
            def request_focus() -> None:
                write_json_atomic(
                    cache_dir() / "focus_request.json",
                    {"id": str(time.time_ns()), "title": payload["title"], "issued_at": now()},
                )

            request_focus()
            if self._xfixes_watch or self._clipboard_process:
                retry = threading.Timer(1.5, request_focus)
                retry.daemon = True
                retry.start()
            return {"ok": True, "action": "focus_chrome", "target": payload}
        return {"ok": True, "action": "active_thread_updated", "linked": bool(twin)}

    def set_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload["context_id"])
        surface = "codex" if str(payload.get("surface") or "") == "codex" else "chrome"
        context = self.store.set_note(
            context_id,
            str(payload.get("note", "")),
            surface=surface,
        )
        if not context:
            return {"ok": False, "error": "context_not_found"}
        twin = context.get("twin")
        if twin and self.store.get_meta("active_codex_thread") == twin.get("codex_thread"):
            self._write_overlay_for_thread(twin["codex_thread"], codex_title=self._active_codex_title)
        self.broker.emit("note_changed", {"context_id": context_id, "surface": surface})
        return {"ok": True, "context": context}

    def set_note_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload["context_id"])
        source = "codex" if str(payload.get("source") or "") == "codex" else "chrome"
        raw_independent = payload.get("independent")
        independent = (
            raw_independent
            if isinstance(raw_independent, bool)
            else str(raw_independent or "").strip().lower() in {"1", "true", "yes", "on"}
        )
        context = self.store.set_note_mode(
            context_id,
            independent,
            source=source,
            current_note=str(payload["note"]) if "note" in payload else None,
        )
        if not context:
            return {"ok": False, "error": "context_not_found"}
        twin = context.get("twin")
        if twin and self.store.get_meta("active_codex_thread") == twin.get("codex_thread"):
            self._write_overlay_for_thread(twin["codex_thread"], codex_title=self._active_codex_title)
        self.broker.emit(
            "note_mode_changed",
            {"context_id": context_id, "independent": bool(context.get("notes_independent"))},
        )
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

    def extension_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = {
            "version": str(payload.get("version") or ""),
            "seen_at": now(),
        }
        self.store.set_meta("extension_runtime", state)
        return {"ok": True, "extension_runtime": state}

    def set_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"auto_switch_on_codex_copy"}
        for key in allowed:
            if key in payload:
                self.settings[key] = bool(payload[key])
        self.store.set_meta("settings", self.settings)
        return {"ok": True, "settings": self.settings}

    def _write_overlay_hidden(self, reason: str, codex_title: str | None = None) -> None:
        write_json_atomic(
            overlay_path(),
            {
                "visible": False,
                "codex_thread": None,
                "context_id": None,
                "title": "",
                "note": "",
                "chrome_note": "",
                "codex_note": "",
                "notes_independent": False,
                "url": "",
                "codex_title": codex_title or "",
                "reason": reason,
                "updated_at": now(),
            },
        )

    def _write_overlay_for_thread(self, thread: str, codex_title: str | None = None) -> None:
        twin = self.store.twin_by_thread(thread)
        if twin:
            state = {
                "visible": True,
                "codex_thread": thread,
                "context_id": twin["context_id"],
                "title": twin.get("title", ""),
                "note": (
                    twin.get("codex_note", "")
                    if bool(twin.get("notes_independent"))
                    else twin.get("note", "")
                ),
                "chrome_note": twin.get("note", ""),
                "codex_note": twin.get("codex_note", ""),
                "notes_independent": bool(twin.get("notes_independent")),
                "url": twin.get("url", ""),
                "codex_title": codex_title or self.store.codex_title_for_thread(thread) or "",
                "reason": "",
                "updated_at": now(),
            }
        else:
            state = {
                "visible": False,
                "codex_thread": thread,
                "context_id": None,
                "title": "",
                "note": "",
                "chrome_note": "",
                "codex_note": "",
                "notes_independent": False,
                "url": "",
                "codex_title": codex_title or self.store.codex_title_for_thread(thread) or "",
                "reason": "thread_not_linked",
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
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/x-www-form-urlencoded":
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return {key: values[-1] if values else "" for key, values in parsed.items()}
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
            elif parsed.path == "/api/prompt":
                prompt_id = query.get("prompt_id", [""])[0]
                self._json(HTTPStatus.OK, APP.prompt_binding(prompt_id))
            elif parsed.path == "/api/prompt/text":
                prompt_id = query.get("prompt_id", [""])[0]
                self._json(HTTPStatus.OK, APP.prompt_text(prompt_id))
            elif parsed.path == "/api/prompts":
                self._json(HTTPStatus.OK, {"ok": True, "bindings": APP.store.list_prompt_bindings()})
            elif parsed.path == "/api/gnome-runtime":
                state = APP.store.get_meta("gnome_runtime")
                self._json(HTTPStatus.OK, {"ok": isinstance(state, dict), "gnome_runtime": state if isinstance(state, dict) else None})
            elif re.fullmatch(r"/api/verify/prompt/\\d{6}", parsed.path):
                prompt_id = parsed.path.rsplit("/", 1)[-1]
                self._json(HTTPStatus.OK, APP.verify_prompt_snapshot(prompt_id))
            elif parsed.path == "/api/control/ack":
                request_id = query.get("request_id", [""])[0]
                ack = APP.store.get_meta(f"control_ack:{request_id}") if request_id else None
                self._json(HTTPStatus.OK, {"ok": isinstance(ack, dict), "ack": ack if isinstance(ack, dict) else None})
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
            elif parsed.path == "/api/note-mode":
                self._json(HTTPStatus.OK, APP.set_note_mode(payload))
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
                codex_title = payload.get("codex_title")
                self._json(HTTPStatus.OK, APP.handle_clipboard(str(text), codex_title=codex_title))
            elif parsed.path == "/api/codex-activity":
                kind = str(payload.get("kind") or parse_qs(parsed.query).get("kind", ["possible_thread_change"])[0])
                self._json(HTTPStatus.OK, APP.invalidate_codex_overlay(kind))
            elif parsed.path == "/api/unlink":
                self._json(HTTPStatus.OK, APP.unlink(str(payload["context_id"])))
            elif parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, APP.set_settings(payload))
            elif parsed.path == "/api/extension-heartbeat":
                self._json(HTTPStatus.OK, APP.extension_heartbeat(payload))
            elif parsed.path == "/api/gnome-heartbeat":
                self._json(HTTPStatus.OK, APP.gnome_heartbeat(payload))
            elif parsed.path == "/api/control-ack":
                self._json(HTTPStatus.OK, APP.control_ack(payload))
            elif parsed.path == "/api/prompt/control":
                self._json(HTTPStatus.OK, APP.request_chrome_control(payload))
            elif parsed.path == "/api/prompt/bind":
                self._json(HTTPStatus.OK, APP.bind_prompt(payload))
            elif parsed.path == "/api/prompt/arm":
                self._json(HTTPStatus.OK, APP.arm_prompt(payload))
            elif parsed.path == "/api/prompt/open-codex":
                self._json(HTTPStatus.OK, APP.open_prompt_codex(payload))
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
    APP.start_active_thread_watch()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    def stop(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        APP.stop_active_thread_watch()
        APP.stop_clipboard_watch()
        server.server_close()


if __name__ == "__main__":
    serve()
