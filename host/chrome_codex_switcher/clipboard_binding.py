"""Clipboard Codex-link association, deduplication and focus event policy.

The enclosing App provides store/broker and lifecycle primitives.  No UI,
background poller or HTTP server is created by importing this capsule.
"""
from __future__ import annotations
import threading
import time
from typing import Any
from .util import cache_dir, now, parse_codex_link, write_json_atomic


CLIPBOARD_DUPLICATE_WINDOW = 1.0


class ClipboardBindingMixin:
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
