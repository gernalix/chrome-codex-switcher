"""Stage exact clipboard pairing proposals for explicit local confirmation."""
from __future__ import annotations

import re
import secrets
from typing import Any

from .util import now


CLIPBOARD_DUPLICATE_WINDOW = 1.0
THREAD_LINK = re.compile(
    r"codex://threads/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.IGNORECASE
)


class ClipboardBindingMixin:
    def handle_clipboard(self, text: str, codex_title: str | None = None) -> dict[str, Any]:
        # Clipboard content is a candidate identifier, never an active-thread observation.
        match = THREAD_LINK.fullmatch(text.strip())
        if not match:
            return {"ok": True, "ignored": True}
        thread = match.group(1).lower()
        pending = self.store.pairing_intent()
        twin = None if pending else self.store.twin_by_thread(thread)
        if pending is None and (twin is None or not self.settings.get("auto_switch_on_codex_copy", True)):
            return {"ok": True, "ignored": True}
        intent = pending[1] if pending else {}
        prompt_id = intent.get("prompt_id")
        context_id = intent.get("context_id") or (twin or {}).get("context_id")
        if prompt_id and not context_id:
            context_id = (self.store.prompt_binding(str(prompt_id)) or {}).get("context_id")
        context = self.store.get_context(context_id) if context_id else None
        url = intent.get("url") or (context or {}).get("url")
        proposal = {
            "id": secrets.token_urlsafe(24),
            "thread": thread,
            "prompt_id": prompt_id,
            "context_id": context_id,
            "url": url,
            "title": (context or {}).get("title"),
            "action": "pair" if pending else "focus",
            "expires_at": min(float(intent.get("expires_at", now() + 120)), now() + 120),
        }
        with self._lock:
            old = getattr(self, "_clipboard_proposal", None)
            if (old and old[0]["thread"] == thread and old[1] == pending
                    and old[0]["url"] == url and old[0]["expires_at"] > now()):
                return {"ok": True, "action": "confirmation_required", "proposal": old[0]}
            self._clipboard_proposal = proposal, pending
        self.broker.emit("clipboard_confirmation_required", {"action": proposal["action"]})
        return {"ok": True, "action": "confirmation_required", "proposal": proposal}

    def clipboard_proposal(self) -> dict[str, Any] | None:
        with self._lock:
            candidate = getattr(self, "_clipboard_proposal", None)
            return candidate[0].copy() if candidate and candidate[0]["expires_at"] > now() else None

    def confirm_clipboard_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            candidate = getattr(self, "_clipboard_proposal", None)
            if not candidate or candidate[0]["id"] != proposal_id or candidate[0]["expires_at"] <= now():
                return {"ok": False, "error": "clipboard_confirmation_expired"}
            self._clipboard_proposal = None
        proposal, pending = candidate
        thread = proposal["thread"]
        context_id = proposal["context_id"]
        prompt_id = proposal["prompt_id"]
        if pending is None:
            twin = self.store.twin_by_thread(thread)
            if not twin or twin["context_id"] != context_id or twin["url"] != proposal["url"]:
                return {"ok": False, "error": "clipboard_target_changed"}
            payload = {"context_id": context_id, "url": twin["url"], "issued_at": now()}
            self.broker.emit("focus_chrome", payload)
            return {"ok": True, "action": "focus_chrome"}
        deep_link = f"codex://threads/{thread}"
        claim = (*pending, proposal["url"])
        if prompt_id:
            if context_id:
                binding = self.store.link_prompt(str(prompt_id), context_id, thread, deep_link, pending=claim)
            else:
                binding = self.store.bind_prompt(str(prompt_id), codex_thread=thread, codex_deep_link=deep_link, pending=claim)
            self.broker.emit("prompt_linked", binding)
        elif context_id:
            self.store.link(context_id, thread, deep_link, pending=claim)
        else:
            return {"ok": False, "error": "clipboard_target_missing"}
        if context_id:
            self.broker.emit("linked", {"context_id": context_id, "codex_thread": thread, "codex_deep_link": deep_link})
        return {"ok": True, "action": "linked"}
