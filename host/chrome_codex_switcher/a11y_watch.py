from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

_APP_RE = re.compile(r"\b(chatgpt|codex)\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
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


class CodexA11yWatch:
    """Best-effort active Codex/ChatGPT conversation detector via AT-SPI.

    The watcher never guesses a thread id. It only reports a stable selected
    conversation title; the daemon resolves that title against titles learned
    while an exact codex:// deep link was observed.
    """

    def __init__(
        self,
        callback: Callable[[bool, str | None], None],
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
        pending: tuple[bool, str | None] | None = None
        samples = 0
        emitted: tuple[bool, str | None] | None = None
        while not self._stop.wait(self._interval):
            if self._refresh.is_set():
                self._refresh.clear()
                emitted = None
            try:
                state = self._probe()
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}:{exc}"
                state = (False, None)

            if state == pending:
                samples += 1
            else:
                pending = state
                samples = 1

            if samples < self._stable_samples or state == emitted:
                continue

            emitted = state
            self.current_title = state[1] if state[0] else None
            try:
                self._callback(*state)
            except Exception:
                # Never let an overlay/update failure kill the watcher.
                pass

        self.active = False

    def _probe(self) -> tuple[bool, str | None]:
        Atspi = self._atspi
        if Atspi is None:
            return False, None

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

            focused, title = self._probe_app(app)
            if focused:
                return True, title

        return False, None

    def _probe_app(self, app: Any) -> tuple[bool, str | None]:
        Atspi = self._atspi
        assert Atspi is not None

        stack: list[tuple[Any, int]] = [(app, 0)]
        visited = 0
        focused = False
        candidates: list[tuple[float, str]] = []

        while stack and visited < self._max_nodes:
            node, depth = stack.pop()
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

            if name and (node_selected or current):
                lowered = name.casefold()
                if (
                    lowered not in _IGNORED_NAMES
                    and 3 <= len(name) <= 180
                    and not name.lower().startswith(("gpt-", "alt+", "ctrl+"))
                ):
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
                    candidates.append((score, name))

            if depth >= self._max_depth:
                continue
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
                    stack.append((child, depth + 1))

        if not focused:
            return False, None
        if not candidates:
            return True, None

        candidates.sort(key=lambda item: item[0], reverse=True)
        top_score = candidates[0][0]
        top_names: list[str] = []
        for score, name in candidates:
            if top_score - score > 0.75:
                break
            if name.casefold() not in {item.casefold() for item in top_names}:
                top_names.append(name)

        # Ambiguous accessibility state is treated as unknown, never guessed.
        return True, top_names[0] if len(top_names) == 1 else None
