from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

_APP_RE = re.compile(r"\b(chatgpt|codex)\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_THREAD_ROUTE_RE = re.compile(
    r"(?:codex://threads/|/threads/)([A-Za-z0-9][A-Za-z0-9._:-]{1,255})(?=$|[/?#\\\"'\\s])",
    re.IGNORECASE,
)
_THREAD_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,255}$")
_THREAD_ATTRIBUTE_KEYS = {"threadid", "codexthread", "codexthreadid"}
_IGNORED_NAMES = {
    "chatgpt",
    "codex",
    "new chat",
    "explore",
    "projects",
    "pinned",
    "getting started",
    "voice",
    "share",
    "search",
}


def _clean_name(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _attribute_strings(node: Any) -> list[str]:
    try:
        raw = node.get_attributes()
    except Exception:
        return []
    if isinstance(raw, dict):
        return [f"{key}:{value}" for key, value in raw.items()]
    try:
        return [str(item) for item in raw]
    except Exception:
        return []


def _looks_current(attributes: list[str]) -> bool:
    for raw in attributes:
        value = raw.lower().replace("=", ":")
        if (
            ("current:" in value or "aria-current:" in value or "selected:" in value)
            and not value.endswith(":false")
            and not value.endswith(":0")
        ):
            return True
    return False


def _thread_from_attributes(attributes: list[str]) -> str | None:
    """Return one exact Codex thread id exposed by the active accessibility node.

    Only explicit thread routes/attributes are accepted. Arbitrary UUID-looking
    text is intentionally ignored so an unrelated id can never select an overlay.
    Multiple different ids are treated as ambiguous and therefore unresolved.
    """
    candidates: set[str] = set()
    for raw in attributes:
        text = str(raw or "").strip()
        if not text:
            continue
        for match in _THREAD_ROUTE_RE.finditer(text):
            candidates.add(match.group(1))

        normalized = text.replace("=", ":")
        key, separator, value = normalized.partition(":")
        if not separator:
            continue
        key_normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        for prefix in ("data", "aria"):
            if key_normalized.startswith(prefix):
                key_normalized = key_normalized[len(prefix):]
        candidate = value.strip().strip("\"'")
        if (
            key_normalized in _THREAD_ATTRIBUTE_KEYS
            and _THREAD_VALUE_RE.fullmatch(candidate)
        ):
            candidates.add(candidate)

    return next(iter(candidates)) if len(candidates) == 1 else None


class CodexA11yWatch:
    """Fail-closed active Codex/ChatGPT conversation detector via AT-SPI.

    Prefer an exact thread id exposed by the selected/current accessibility node.
    A stable visible title is retained only as a fallback for builds that do not
    expose thread identity directly.
    """

    def __init__(
        self,
        callback: Callable[[bool, str | None, str | None], None],
        *,
        interval: float = 0.7,
        stable_samples: int = 2,
        max_nodes: int = 1200,
        max_depth: int = 12,
    ):
        self._callback = callback
        self._interval = interval
        self._stable_samples = max(1, stable_samples)
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._thread: threading.Thread | None = None
        self._atspi: Any = None
        self.active = False
        self.error: str | None = None
        self.current_thread: str | None = None
        self.current_title: str | None = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            result = Atspi.init()
            if result not in (0, None):
                raise RuntimeError(f"atspi_init_{result}")
            self._atspi = Atspi
        except Exception as exc:
            self.error = f"{type(exc).__name__}:{exc}"
            self.active = False
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="chrome-codex-switcher-a11y",
            daemon=True,
        )
        self._thread.start()
        self.active = True
        self.error = None
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None
        self.active = False

    def refresh(self) -> None:
        self._refresh.set()

    def _run(self) -> None:
        pending: tuple[bool, str | None, str | None] | None = None
        samples = 0
        emitted: tuple[bool, str | None, str | None] | None = None
        while not self._stop.wait(self._interval):
            if self._refresh.is_set():
                self._refresh.clear()
                emitted = None
            try:
                state = self._probe()
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}:{exc}"
                state = (False, None, None)

            if state == pending:
                samples += 1
            else:
                pending = state
                samples = 1

            if samples < self._stable_samples or state == emitted:
                continue

            emitted = state
            self.current_thread = state[1] if state[0] else None
            self.current_title = state[2] if state[0] else None
            try:
                self._callback(*state)
            except Exception:
                # Never let an overlay/update failure kill the watcher.
                pass

        self.active = False

    def _probe(self) -> tuple[bool, str | None, str | None]:
        Atspi = self._atspi
        if Atspi is None:
            return False, None, None

        desktop = Atspi.get_desktop(0)
        app_count = min(int(desktop.get_child_count()), 256)
        for index in range(app_count):
            try:
                app = desktop.get_child_at_index(index)
                app_name = _clean_name(app.get_name())
            except Exception:
                continue
            if not _APP_RE.search(app_name):
                continue

            focused, thread_id, title = self._probe_app(app)
            if focused:
                return True, thread_id, title

        return False, None, None

    def _probe_app(self, app: Any) -> tuple[bool, str | None, str | None]:
        Atspi = self._atspi
        assert Atspi is not None

        # active_descendants is non-zero only for a very small subtree below a
        # selected/current node. This lets us see an href on the child anchor of
        # a selected sidebar row without scanning unrelated conversation links.
        stack: list[tuple[Any, int, int]] = [(app, 0, 0)]
        visited = 0
        focused = False
        title_candidates: list[tuple[float, str]] = []
        thread_candidates: list[tuple[float, str, str | None]] = []

        while stack and visited < self._max_nodes:
            node, depth, active_descendants = stack.pop()
            visited += 1

            try:
                state = node.get_state_set()
                node_focused = bool(state.contains(Atspi.StateType.FOCUSED))
                node_selected = bool(state.contains(Atspi.StateType.SELECTED))
            except Exception:
                node_focused = False
                node_selected = False

            focused = focused or node_focused

            name = ""
            role = ""
            try:
                name = _clean_name(node.get_name())
            except Exception:
                pass
            try:
                role = _clean_name(node.get_role_name()).lower()
            except Exception:
                pass

            attrs = _attribute_strings(node)
            current = _looks_current(attrs)
            active_scope = node_selected or current or active_descendants > 0

            score = 0.0
            if node_selected:
                score += 6.0
            if current:
                score += 7.0
            if any(token in role for token in ("link", "list item", "tree item", "page tab", "row")):
                score += 4.0
            elif "button" in role:
                score += 1.5
            score += min(depth, 12) * 0.08

            if active_scope:
                thread_id = _thread_from_attributes(attrs)
                if thread_id:
                    usable_name = name if name and name.casefold() not in _IGNORED_NAMES else None
                    # An exact id outranks title-only evidence while preserving
                    # selected/current scoring among multiple accessible nodes.
                    thread_candidates.append((score + 10.0, thread_id, usable_name))

            if name and (node_selected or current):
                lowered = name.casefold()
                if (
                    lowered not in _IGNORED_NAMES
                    and 3 <= len(name) <= 180
                    and not name.lower().startswith(("gpt-", "alt+", "ctrl+"))
                ):
                    title_candidates.append((score, name))

            if depth >= self._max_depth:
                continue
            next_active_descendants = (
                2 if (node_selected or current) else max(active_descendants - 1, 0)
            )
            try:
                child_count = min(int(node.get_child_count()), 256)
            except Exception:
                child_count = 0
            for child_index in range(child_count - 1, -1, -1):
                try:
                    child = node.get_child_at_index(child_index)
                except Exception:
                    continue
                if child is not None:
                    stack.append((child, depth + 1, next_active_descendants))

        if not focused:
            return False, None, None

        if thread_candidates:
            thread_candidates.sort(key=lambda item: item[0], reverse=True)
            top_score = thread_candidates[0][0]
            top = [item for item in thread_candidates if top_score - item[0] <= 0.75]
            thread_ids: list[str] = []
            for _score, thread_id, _name in top:
                if thread_id not in thread_ids:
                    thread_ids.append(thread_id)
            # Explicit but contradictory IDs are never resolved by falling back
            # to a potentially duplicate human title.
            if len(thread_ids) != 1:
                return True, None, None
            thread_id = thread_ids[0]
            title = next(
                (name for _score, candidate, name in top if candidate == thread_id and name),
                None,
            )
            return True, thread_id, title

        if not title_candidates:
            return True, None, None

        title_candidates.sort(key=lambda item: item[0], reverse=True)
        top_score = title_candidates[0][0]
        top_names: list[str] = []
        for score, name in title_candidates:
            if top_score - score > 0.75:
                break
            if name.casefold() not in {item.casefold() for item in top_names}:
                top_names.append(name)

        # Ambiguous title-only accessibility state is treated as unknown.
        return True, None, top_names[0] if len(top_names) == 1 else None
