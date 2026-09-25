from __future__ import annotations

import html as html_lib
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
from . import __version__
from .store import Store
from .util import cache_dir, canonical_url, db_path, now, overlay_path, parse_codex_link, write_json_atomic
from .verifier import discover_codex_session
from .xfixes_watch import XFixesWatch

HOST = os.environ.get("CCS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCS_PORT", "43817"))
EXTENSION_ID = "mfpomnbkkfklealhaacbnmelpgpggglg"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
LOCAL_ORIGINS = {
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
}
PENDING_TTL = 120.0
CLIPBOARD_DUPLICATE_WINDOW = 1.0
PROMPT_ID_RE = re.compile(r"\d{6}\Z")
PROMPT_PENDING_TTL = 10 * 60.0
PROMPT_CODEX_DISCOVERY_AGE = 30 * 24 * 60 * 60.0


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

    def _record_codex_resolution(
        self,
        method: str,
        *,
        thread: str | None = None,
        title: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.store.set_meta(
            "active_codex_resolution",
            {
                "method": method,
                "thread": thread,
                "title": title,
                "reason": reason,
                "updated_at": now(),
            },
        )

    def handle_codex_ui_state(
        self,
        focused: bool,
        title: str | None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        clean = self._clean_codex_title(title)
        exact_thread = str(thread_id or "").strip() or None

        if not focused:
            self._active_codex_title = None
            self.store.delete_meta("active_codex_thread")
            self._record_codex_resolution("unknown", reason="codex_unfocused")
            self._write_overlay_hidden("codex_unfocused")
            return {"ok": True, "action": "codex_unfocused"}

        # Exact identity from the selected/current AT-SPI node is authoritative.
        # This path works even when two chats have the same visible title.
        if exact_thread:
            self._active_codex_title = clean
            if clean:
                self.store.remember_codex_title(exact_thread, clean)
            self._expected_codex_thread = None
            self._expected_previous_title = None
            self._expected_until = 0.0
            self.store.set_meta("active_codex_thread", exact_thread)
            self._record_codex_resolution(
                "a11y_thread", thread=exact_thread, title=clean
            )
            self._write_overlay_for_thread(exact_thread, codex_title=clean)
            return {
                "ok": True,
                "action": "active_thread_resolved",
                "thread": exact_thread,
                "title": clean,
                "resolution": "a11y_thread",
                "linked": bool(self.store.twin_by_thread(exact_thread)),
            }

        if not clean:
            self._active_codex_title = None
            self.store.delete_meta("active_codex_thread")
            self._record_codex_resolution("unknown", reason="active_thread_unknown")
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
            self._record_codex_resolution(
                "title_fallback", thread=thread, title=clean
            )
            self._write_overlay_for_thread(thread, codex_title=clean)
            return {
                "ok": True,
                "action": "active_thread_resolved",
                "thread": thread,
                "title": clean,
                "resolution": "title_fallback",
            }

        reason = "active_thread_unmapped" if previous != clean else "active_thread_unresolved"
        self.store.delete_meta("active_codex_thread")
        self._record_codex_resolution("unknown", title=clean, reason=reason)
        self._write_overlay_hidden(reason, codex_title=clean)
        return {"ok": True, "action": "overlay_hidden", "reason": reason, "title": clean}

    def invalidate_codex_overlay(self, reason: str = "possible_thread_change") -> dict[str, Any]:
        self._active_codex_title = None
        # Clearing this cache is essential: note edits use it to decide whether
        # to refresh the desktop overlay. Leaving the old thread here could
        # resurrect the stale overlay while the new chat is still resolving.
        self.store.delete_meta("active_codex_thread")
        self._record_codex_resolution("unknown", reason=reason)
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
                    "source_version",
                    "focused_codex",
                    "focused_app_id",
                    "focused_wm_class",
                    "visible",
                    "context_id",
                    "codex_thread",
                    "notes_independent",
                )
            }
        return {
            "ok": True,
            "version": __version__,
            "host": HOST,
            "port": PORT,
            "clipboard_watch": bool(self._clipboard_process and self._clipboard_process.poll() is None),
            "xfixes_watch": bool(self._xfixes_watch and self._xfixes_watch.active),
            "auto_switch_on_codex_copy": bool(self.settings.get("auto_switch_on_codex_copy", True)),
            "extension_runtime": self.store.get_meta("extension_runtime"),
            "gnome_runtime": gnome_summary,
            "codex_a11y_watch": bool(self._a11y_watch and self._a11y_watch.active),
            "codex_a11y_error": self._a11y_watch.error if self._a11y_watch else None,
            "active_codex_thread": self.store.get_meta("active_codex_thread"),
            "active_codex_title": self._active_codex_title,
            "active_codex_resolution": self.store.get_meta("active_codex_resolution"),
        }

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def gnome_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = {
            "seen_at": now(),
            "source_version": str(payload.get("source_version") or ""),
            "focused_codex": self._truthy(payload.get("focused_codex")),
            "focused_app_id": str(payload.get("focused_app_id") or ""),
            "focused_wm_class": str(payload.get("focused_wm_class") or ""),
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
        if action not in {"probe", "focus", "ensure", "workflowy"}:
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
        result = {"ok": True, "prompt_id": prompt_id, "prompt_text": text}
        for key in (
            "source", "project_id", "project_name", "repo", "chat_guidance",
            "prompt_type", "model", "reasoning",
        ):
            if key in payload:
                result[key] = payload.get(key)
        return result

    def prompt_binding(self, prompt_id: str) -> dict[str, Any]:
        prompt_id = self._prompt_id(prompt_id)
        binding = self.store.prompt_binding(prompt_id)
        return {"ok": bool(binding), "binding": binding}

    def observe_context_prompt_ids(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        raw_ids = payload.get("prompt_ids", [])
        if not context_id:
            raise ValueError("context_id_missing")
        if not isinstance(raw_ids, list):
            raise ValueError("prompt_ids_list_required")
        prompt_ids = [self._prompt_id(value) for value in raw_ids]
        items = self.store.observe_context_prompt_ids(context_id, prompt_ids)
        return {"ok": True, "context_id": context_id, "prompt_ids": [item["prompt_id"] for item in items], "items": items}

    def add_context_prompt_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        if not context_id:
            raise ValueError("context_id_missing")
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        items = self.store.add_context_prompt_id(context_id, prompt_id)
        return {"ok": True, "context_id": context_id, "prompt_ids": [item["prompt_id"] for item in items], "items": items}

    def remove_context_prompt_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        if not context_id:
            raise ValueError("context_id_missing")
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        items = self.store.remove_context_prompt_id(context_id, prompt_id)
        return {"ok": True, "context_id": context_id, "prompt_ids": [item["prompt_id"] for item in items], "items": items}

    def bind_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        context_id = str(payload.get("context_id") or "").strip() or None
        current = self.store.prompt_binding(prompt_id)
        if context_id:
            owner = self.store.prompt_by_context(context_id)
            if owner and str(owner.get("prompt_id") or "") != prompt_id:
                raise ValueError("prompt_context_conflict")
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
        self.store.delete_meta("pending_prompt_codex")
        self.store.set_meta("pending_prompt", pending)
        self.broker.emit("prompt_armed", pending)
        return {"ok": True, "pending": pending}

    def _codex_thread_conflict(self, prompt_id: str, thread: str) -> bool:
        return any(
            str(item.get("prompt_id") or "") != prompt_id
            and str(item.get("codex_thread") or "") == thread
            for item in self.store.list_prompt_bindings()
        )

    def recover_prompt_codex(
        self,
        payload: dict[str, Any],
        *,
        session_root: Path | None = None,
    ) -> dict[str, Any]:
        """Attach the Codex side later, preferring exact PROMPT_ID session discovery."""
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        force = self._truthy(payload.get("force"))
        current = self.store.prompt_binding(prompt_id)
        if (
            current
            and current.get("codex_thread")
            and current.get("codex_deep_link")
            and not force
        ):
            return {"ok": True, "stage": "complete", "source": "existing", "binding": current}

        discovered = discover_codex_session(
            prompt_id,
            session_root=session_root,
            max_age_seconds=PROMPT_CODEX_DISCOVERY_AGE,
        )
        if discovered:
            thread = str(discovered["session_id"])
            deep_link = str(discovered["deep_link"])
            if self._codex_thread_conflict(prompt_id, thread):
                return {"ok": False, "error": "prompt_codex_conflict"}
            context_id = str((current or {}).get("context_id") or "").strip() or None
            if context_id:
                binding = self.store.link_prompt(prompt_id, context_id, thread, deep_link)
                stage = "complete"
            else:
                binding = self.store.bind_prompt(
                    prompt_id,
                    codex_thread=thread,
                    codex_deep_link=deep_link,
                )
                stage = "chrome"
            self.store.delete_meta("pending_prompt")
            self.store.delete_meta("pending_prompt_codex")
            self.broker.emit("prompt_linked", binding)
            return {
                "ok": True,
                "stage": stage,
                "source": "native_session",
                "binding": binding,
            }

        pending = {
            "prompt_id": prompt_id,
            "armed_at": now(),
            "expires_at": now() + PROMPT_PENDING_TTL,
            "force": force,
        }
        self.store.delete_meta("pending_prompt")
        self.store.set_meta("pending_prompt_codex", pending)
        self.broker.emit("prompt_codex_armed", pending)
        return {
            "ok": True,
            "stage": "codex",
            "source": "clipboard",
            "pending": pending,
            "binding": current,
        }

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

    def launch_prompt_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Launch a roadmap prompt directly in Codex Desktop, never through Chrome."""
        prompt_id = self._prompt_id(payload.get("prompt_id"))
        binding = self.store.prompt_binding(prompt_id)

        if binding and binding.get("codex_deep_link") and binding.get("codex_thread"):
            result = self.open_prompt_codex({"prompt_id": prompt_id})
            return {**result, "mode": "existing"}

        spec = self.prompt_text(prompt_id)
        if not spec.get("ok"):
            return {**spec, "ok": False, "error": spec.get("error") or "prompt_spec_unavailable"}

        requested_at = now()
        launch = {
            "prompt_id": prompt_id,
            "chat_title": prompt_id,
            "prompt_text": str(spec.get("prompt_text") or ""),
            "project_id": spec.get("project_id"),
            "project_name": spec.get("project_name"),
            "repo": spec.get("repo"),
            "model": spec.get("model"),
            "reasoning": spec.get("reasoning"),
            "requested_at": requested_at,
            "expires_at": requested_at + PROMPT_PENDING_TTL,
        }

        # A launch no longer creates or requires a ChatGPT Chrome context.
        # Keep an explicit pending Codex association so the first observed
        # codex:// deep link can bind the new native thread to this PROMPT_ID.
        self.store.delete_meta("pending_prompt")
        self.store.set_meta(
            "pending_prompt_codex",
            {
                "prompt_id": prompt_id,
                "armed_at": requested_at,
                "expires_at": requested_at + PROMPT_PENDING_TTL,
                "force": False,
            },
        )
        self.store.set_meta("pending_desktop_launch", launch)
        self.broker.emit("desktop_launch_requested", launch)

        deep_link = "codex://threads/new"
        self._open_codex(deep_link)
        return {
            "ok": True,
            "mode": "new",
            "prompt_id": prompt_id,
            "deep_link": deep_link,
            "launch": {key: value for key, value in launch.items() if key != "prompt_text"},
        }

    def upsert_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload["context_id"])
        url = canonical_url(str(payload.get("url", "")))
        title = str(payload.get("title", ""))
        return self.store.upsert_context(context_id, url, title)

    def replace_context_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        old_url = canonical_url(str(payload.get("old_url") or ""))
        new_url = canonical_url(str(payload.get("new_url") or ""))
        context = self.store.replace_context_url(context_id, old_url, new_url)
        event = {
            "context_id": context["id"],
            "old_url": old_url,
            "new_url": context["url"],
        }
        self.broker.emit("context_url_replaced", event)
        return {"ok": True, "context": context, "replacement": event}

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
        self._record_codex_resolution("clipboard", thread=thread, title=ui_title)

        paired_prompt = False
        pending_prompt = self.store.get_meta("pending_prompt")
        if pending_prompt:
            if float(pending_prompt.get("expires_at", 0)) >= now():
                prompt_id = self._prompt_id(pending_prompt.get("prompt_id"))
                context_id = str(pending_prompt.get("context_id") or "").strip() or None
                if context_id:
                    binding = self.store.link_prompt(prompt_id, context_id, thread, deep_link)
                    self.store.delete_meta("pending_prompt")
                    self.store.delete_meta("pending_prompt_codex")
                    self.broker.emit("prompt_linked", binding)
                    self.broker.emit("linked", {"context_id": context_id, "codex_thread": thread, "codex_deep_link": deep_link})
                    paired_prompt = True
                else:
                    self.store.delete_meta("pending_prompt")
            else:
                self.store.delete_meta("pending_prompt")

        if not paired_prompt:
            pending_codex = self.store.get_meta("pending_prompt_codex")
            if pending_codex:
                if float(pending_codex.get("expires_at", 0)) >= now():
                    prompt_id = self._prompt_id(pending_codex.get("prompt_id"))
                    current = self.store.prompt_binding(prompt_id)
                    if self._codex_thread_conflict(prompt_id, thread):
                        self.store.delete_meta("pending_prompt_codex")
                        return {"ok": False, "error": "prompt_codex_conflict"}
                    context_id = str((current or {}).get("context_id") or "").strip() or None
                    if context_id:
                        binding = self.store.link_prompt(prompt_id, context_id, thread, deep_link)
                    else:
                        binding = self.store.bind_prompt(
                            prompt_id,
                            codex_thread=thread,
                            codex_deep_link=deep_link,
                        )
                    self.store.delete_meta("pending_prompt_codex")
                    self.broker.emit("prompt_linked", binding)
                    paired_prompt = True
                else:
                    self.store.delete_meta("pending_prompt_codex")

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

    def focus_dashboard_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        context = self.store.get_context(context_id)
        if not context:
            return {"ok": False, "error": "context_not_found"}
        target = {
            "context_id": context["id"],
            "url": context["url"],
            "title": context.get("title", ""),
        }
        self.broker.emit("focus_chrome", target)
        return {"ok": True, "action": "focus_chrome", "target": target}

    def open_dashboard_codex(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload.get("context_id") or "").strip()
        context = self.store.get_context(context_id)
        if not context:
            return {"ok": False, "error": "context_not_found"}
        return self.switch_from_chrome({
            "context_id": context["id"],
            "url": context["url"],
            "title": context.get("title", ""),
        })

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



def search_dashboard_html() -> str:
    return r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Context Search</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:Canvas;color:CanvasText}
header{position:sticky;top:0;background:Canvas;padding:18px 18px 12px;border-bottom:1px solid #7775;z-index:2}h1{font-size:1.1rem;margin:0 0 10px}
#q{width:100%;font:inherit;padding:11px 13px;border:1px solid #7778;border-radius:10px;background:Canvas;color:CanvasText}#status{font-size:.82rem;opacity:.65;margin-top:7px;min-height:1.1em}main{padding:10px}
.card{border:1px solid #7775;border-radius:12px;padding:12px;margin:8px 0;cursor:pointer}.card.selected{outline:2px solid Highlight;outline-offset:1px}
.note{font-size:1.03rem;font-weight:650;line-height:1.5;color:CanvasText;background:#7772;border:1px solid #7775;border-radius:10px;padding:12px 13px;margin:0 0 10px;white-space:pre-wrap;overflow-wrap:anywhere}
.title{font-size:.9rem;font-weight:650;opacity:.72;overflow-wrap:anywhere}.prompt-ids{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:8px}
.prompt-chip{display:inline-flex;align-items:center;gap:4px;font:600 .78rem/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;border:1px solid #7776;background:#7772;border-radius:999px;padding:4px 7px}.prompt-chip.bound{font-weight:750}
.prompt-remove,.prompt-add{padding:1px 5px;min-width:0;border-radius:999px}.prompt-remove{border:0;background:transparent}.meta{font-size:.76rem;opacity:.58;margin-top:8px;overflow-wrap:anywhere}.buttons{display:flex;gap:7px;margin-top:10px}
button{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid #7777;background:ButtonFace;color:ButtonText;cursor:pointer}.empty{padding:32px 12px;text-align:center;opacity:.65}
</style></head><body><header><h1>Context Search</h1><input id="q" type="search" autocomplete="off" placeholder="Cerca note, PROMPT_ID, titoli Chrome/Codex…"><div id="status">Caricamento…</div></header>
<main id="list" role="listbox" aria-label="Chrome and Codex contexts"></main><script>
const q=document.querySelector('#q'),list=document.querySelector('#list'),statusEl=document.querySelector('#status');let contexts=[],visible=[],selected=0;
const searchable=item=>[item.prompt_id,...(item.prompt_ids||[]),item.title,item.twin?.codex_title,item.note,item.codex_note].filter(Boolean).join(' ').toLowerCase();
const noteText=item=>[...new Set([item.note,item.codex_note].filter(Boolean))].join('\n\n');
function select(index){if(!visible.length){selected=0;return}selected=(index+visible.length)%visible.length;[...list.querySelectorAll('.card')].forEach((el,i)=>{const on=i===selected;el.classList.toggle('selected',on);el.setAttribute('aria-selected',on?'true':'false');if(on)el.scrollIntoView({block:'nearest'})})}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await r.json();if(!data.ok)throw new Error(data.error||'azione fallita');return data}
async function action(item,target){if(!item)return;const path=target==='codex'?'/api/dashboard/open-codex':'/api/dashboard/focus-chrome';statusEl.textContent=target==='codex'?'Apro Codex…':'Passo alla scheda Chrome…';try{await post(path,{context_id:item.id});statusEl.textContent=target==='codex'?'Codex aperto':'Comando inviato a Chrome';window.close();}catch(e){statusEl.textContent='Errore: '+e.message}}
async function editPrompt(item,mode,promptId=null){let value=promptId;if(mode==='add'){value=(window.prompt('PROMPT_ID da associare (6 cifre)')||'').trim();if(!value)return;if(!/^\d{6}$/.test(value)){statusEl.textContent='PROMPT_ID non valido: servono esattamente 6 cifre';return}}try{const endpoint=mode==='add'?'/api/context/prompts/add':'/api/context/prompts/remove';await post(endpoint,{context_id:item.id,prompt_id:value});statusEl.textContent=mode==='add'?'PROMPT_ID aggiunto':'PROMPT_ID rimosso';await load()}catch(e){statusEl.textContent='Errore: '+e.message}}
function promptRow(item){const row=document.createElement('div');row.className='prompt-ids';const canonical=String(item.prompt_id||'');if(canonical){const chip=document.createElement('span');chip.className='prompt-chip bound';chip.title='Binding canonico';chip.textContent=canonical;row.append(chip)}for(const entry of item.prompt_id_index||[]){const id=String(entry.prompt_id||'');if(!id||id===canonical)continue;const chip=document.createElement('span');chip.className='prompt-chip';chip.title=entry.manual_added?(entry.auto_detected?'Rilevato e aggiunto manualmente':'Aggiunto manualmente'):'Rilevato nella pagina';const label=document.createElement('span');label.textContent=id;const remove=document.createElement('button');remove.className='prompt-remove';remove.textContent='×';remove.title='Rimuovi associazione';remove.onclick=e=>{e.stopPropagation();editPrompt(item,'remove',id)};chip.append(label,remove);row.append(chip)}const add=document.createElement('button');add.className='prompt-add';add.textContent='+ ID';add.title='Aggiungi PROMPT_ID';add.onclick=e=>{e.stopPropagation();editPrompt(item,'add')};row.append(add);return row}
function render(){const needle=q.value.trim().toLowerCase();visible=contexts.filter(item=>!needle||searchable(item).includes(needle));list.textContent='';if(!visible.length){selected=0;const e=document.createElement('div');e.className='empty';e.textContent='Nessun risultato';list.append(e);return}selected=Math.min(selected,visible.length-1);visible.forEach((item,index)=>{const card=document.createElement('section');card.className='card'+(index===selected?' selected':'');card.role='option';card.setAttribute('aria-selected',index===selected?'true':'false');card.onclick=()=>action(item,'chrome');card.onmouseenter=()=>select(index);const nv=noteText(item);if(nv){const note=document.createElement('div');note.className='note';note.textContent=nv;card.append(note)}const title=document.createElement('div');title.className='title';title.textContent=item.title||item.url||'Contesto Chrome senza titolo';card.append(title,promptRow(item));const meta=document.createElement('div');meta.className='meta';const bits=[];if(item.twin?.codex_title)bits.push('Codex: '+item.twin.codex_title);else if(item.twin?.codex_thread)bits.push('Codex thread: '+item.twin.codex_thread);else bits.push('Nessun Codex twin');meta.textContent=bits.join(' · ');card.append(meta);const buttons=document.createElement('div');buttons.className='buttons';const chromeBtn=document.createElement('button');chromeBtn.textContent='Chrome';chromeBtn.onclick=e=>{e.stopPropagation();action(item,'chrome')};buttons.append(chromeBtn);if(item.twin){const codexBtn=document.createElement('button');codexBtn.textContent='Codex';codexBtn.onclick=e=>{e.stopPropagation();action(item,'codex')};buttons.append(codexBtn)}card.append(buttons);list.append(card)})}
async function load(){try{const r=await fetch('/api/list',{cache:'no-store'}),data=await r.json();contexts=data.contexts||[];statusEl.textContent=contexts.length+' contesti';render()}catch(e){statusEl.textContent='Daemon non raggiungibile: '+e.message}}
q.addEventListener('input',()=>{selected=0;render()});q.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault();select(selected+1)}else if(e.key==='ArrowUp'){e.preventDefault();select(selected-1)}else if(e.key==='Enter'){e.preventDefault();action(visible[selected]||visible[0],e.shiftKey?'codex':'chrome')}else if(e.key==='Escape'&&q.value){e.preventDefault();q.value='';selected=0;render()}});
q.focus();load();setInterval(load,5000);</script></body></html>"""

PROMPT_UI_ACTIONS = {
    "copy": "Copia prompt",
    "launch": "Avvia",
    "bind": "Completa collegamenti",
    "bind-chrome": "Associa Chrome",
    "bind-codex": "Associa Codex",
    "chrome": "Apri Chrome",
    "codex": "Apri Codex",
    "verify": "Verifica",
}


def prompt_action_fallback_html(
    prompt_id: str,
    action: str,
    result: dict[str, Any],
) -> str:
    if not PROMPT_ID_RE.fullmatch(prompt_id) or action not in PROMPT_UI_ACTIONS:
        raise ValueError("invalid_prompt_action")
    label = PROMPT_UI_ACTIONS[action]
    rendered = html_lib.escape(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    )
    hint = (
        "Questa pagina compare solo quando il click WorkFlowy non è stato "
        "intercettato dall'estensione. Il runtime locale ha quindi eseguito "
        "il fallback sicuro disponibile per questa azione."
    )
    return (
        "<!doctype html><html lang=\"it\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>CCS · {html_lib.escape(label)} · {prompt_id}</title>"
        "<style>:root{color-scheme:light dark;font-family:system-ui,sans-serif}"
        "body{max-width:760px;margin:8vh auto;padding:0 22px;line-height:1.45}"
        ".card{border:1px solid #7775;border-radius:14px;padding:22px}"
        "h1{font-size:1.35rem;margin:0 0 8px}"
        ".muted{opacity:.72}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "background:#7772;border-radius:10px;padding:12px}</style></head><body>"
        "<div class=\"card\">"
        f"<h1>{html_lib.escape(label)} · prompt {prompt_id}</h1>"
        f"<p class=\"muted\">{hint}</p>"
        f"<pre>{rendered}</pre>"
        "<p><a href=\"https://workflowy.com/\">Torna a WorkFlowy</a></p>"
        "</div></body></html>"
    )


def run_prompt_fallback_action(prompt_id: str, action: str) -> dict[str, Any]:
    assert APP is not None
    if action == "copy":
        result = APP.prompt_text(prompt_id)
        if result.get("ok"):
            result = {
                **result,
                "fallback": "prompt_text_shown_below",
                "note": "Il browser non ha intercettato Copia; il testo resta disponibile in questa pagina.",
            }
        return result
    if action == "verify":
        from .cli import request
        from .verifier import verify_prompt
        return verify_prompt(prompt_id, request=request, full=True, scope="prompt")
    if action == "codex":
        return APP.open_prompt_codex({"prompt_id": prompt_id})
    if action == "chrome":
        return APP.request_chrome_control(
            {"prompt_id": prompt_id, "action": "focus"}
        )
    if action == "bind-codex":
        return APP.recover_prompt_codex(
            {"prompt_id": prompt_id, "force": True}
        )
    if action == "launch":
        return APP.launch_prompt_codex({"prompt_id": prompt_id})
    return {
        "ok": False,
        "error": "workflowy_extension_interception_required",
        "prompt_id": prompt_id,
        "action": action,
        "note": (
            "Questa associazione richiede il contesto della tab ChatGPT. "
            "Ricarica WorkFlowy e riprova con l'estensione attiva."
        ),
    }


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ChromeCodexSwitcher/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("CCS_HTTP_LOG") == "1":
            super().log_message(fmt, *args)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        # No Origin = trusted local helper/CLI. Browser callers may be the
        # extension or a same-origin fallback page served by this daemon.
        return origin is None or origin == EXTENSION_ORIGIN or origin in LOCAL_ORIGINS

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

    def _html(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            ui_match = re.fullmatch(
                r"/ui/prompt/(\d{6})/"
                r"(copy|launch|bind|bind-chrome|bind-codex|chrome|codex|verify)",
                parsed.path,
            )
            if parsed.path == "/ui/search":
                self._html(HTTPStatus.OK, search_dashboard_html())
            elif ui_match:
                prompt_id, action = ui_match.groups()
                result = run_prompt_fallback_action(prompt_id, action)
                self._html(
                    HTTPStatus.OK,
                    prompt_action_fallback_html(prompt_id, action, result),
                )
            elif parsed.path == "/api/health":
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
            elif re.fullmatch(r"/api/verify/prompt/\d{6}", parsed.path):
                from .verifier import verify_prompt
                from .cli import request
                prompt_id = parsed.path.rsplit("/", 1)[-1]
                scope = query.get("scope", ["prompt"])[0]
                self._json(HTTPStatus.OK, verify_prompt(prompt_id, request=request, full=scope == "prompt", scope=scope))
            elif parsed.path == "/api/prompts":
                self._json(HTTPStatus.OK, {"ok": True, "bindings": APP.store.list_prompt_bindings()})
            elif parsed.path == "/api/gnome-runtime":
                state = APP.store.get_meta("gnome_runtime")
                self._json(HTTPStatus.OK, {"ok": isinstance(state, dict), "gnome_runtime": state if isinstance(state, dict) else None})
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
            elif parsed.path == "/api/context/replace-url":
                self._json(HTTPStatus.OK, APP.replace_context_url(payload))
            elif parsed.path == "/api/note":
                self._json(HTTPStatus.OK, APP.set_note(payload))
            elif parsed.path == "/api/note-mode":
                self._json(HTTPStatus.OK, APP.set_note_mode(payload))
            elif parsed.path == "/api/ui":
                self._json(HTTPStatus.OK, APP.set_ui(payload))
            elif parsed.path == "/api/context/prompts/observe":
                self._json(HTTPStatus.OK, APP.observe_context_prompt_ids(payload))
            elif parsed.path == "/api/context/prompts/add":
                self._json(HTTPStatus.OK, APP.add_context_prompt_id(payload))
            elif parsed.path == "/api/context/prompts/remove":
                self._json(HTTPStatus.OK, APP.remove_context_prompt_id(payload))
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
            elif parsed.path == "/api/dashboard/focus-chrome":
                self._json(HTTPStatus.OK, APP.focus_dashboard_context(payload))
            elif parsed.path == "/api/dashboard/open-codex":
                self._json(HTTPStatus.OK, APP.open_dashboard_codex(payload))
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
            elif parsed.path == "/api/prompt/recover-codex":
                self._json(HTTPStatus.OK, APP.recover_prompt_codex(payload))
            elif parsed.path == "/api/prompt/open-codex":
                self._json(HTTPStatus.OK, APP.open_prompt_codex(payload))
            elif parsed.path == "/api/prompt/launch-codex":
                self._json(HTTPStatus.OK, APP.launch_prompt_codex(payload))
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
