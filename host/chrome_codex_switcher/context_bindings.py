"""Context and prompt binding lifecycle independent of clipboard/browser watches.

Consumers supply store, broker and presentation enrichment via App composition;
this module performs no network calls or background scheduling.
"""
from __future__ import annotations

import re
from typing import Any
from .util import canonical_url

PROMPT_ID_RE = re.compile(r"\d{6}\Z")


class ContextBindingsMixin:
    @staticmethod
    def _prompt_id(value: Any) -> str:
        prompt_id = str(value or "").strip()
        if not PROMPT_ID_RE.fullmatch(prompt_id):
            raise ValueError("invalid_prompt_id")
        return prompt_id

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

    def upsert_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        context_id = str(payload["context_id"])
        url = canonical_url(str(payload.get("url", "")))
        title = str(payload.get("title", ""))
        return self.pn_cards.enrich(self.store.upsert_context(context_id, url, title))

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
