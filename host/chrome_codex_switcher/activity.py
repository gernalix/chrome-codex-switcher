"""Observed ChatGPT Web / Codex Desktop activity for the existing CCS bridge.

This is a read-only projection of browser signals and Codex JSONL events, not
a task registry or an inference of success/failure from a final assistant text.
"""
from __future__ import annotations

import json
import os
from contextlib import closing
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
TERMINAL = {"task_complete": "finished", "turn_aborted": "interrupted", "task_failed": "interrupted"}
WEB_TTL = 120
RECENT_TTL = 30 * 60
DESKTOP_IDLE_LIMIT = 60 * 60
MAX_THREADS = 150
MAX_TAIL = 64 * 1024 * 1024
MAX_DELTA = 16 * 1024 * 1024


def _ts(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _lifecycle(raw: bytes) -> tuple[str, str, float] | None:
    if b'"event_msg"' not in raw or not any(marker.encode() in raw for marker in ("task_started", *TERMINAL)):
        return None
    try:
        record = json.loads(raw)
        event = record.get("payload") or {}
        kind = event.get("type") if record.get("type") == "event_msg" else None
        if kind not in {"task_started", *TERMINAL}:
            return None
        turn_id = str(event.get("turn_id") or event.get("root_turn_id") or "")
        return (kind, turn_id, _ts(record.get("timestamp")))
    except (ValueError, TypeError, AttributeError):
        return None


def _tail_event(path: Path, size: int) -> tuple[str, str, float] | None:
    """Find the last authoritative turn boundary without scanning entire rollouts."""
    with path.open("rb") as stream:
        end = size
        start = max(0, end - MAX_TAIL)
        remainder = b""
        while end > start:
            begin = max(start, end - 256 * 1024)
            stream.seek(begin)
            chunk = stream.read(end - begin) + remainder
            segments = chunk.split(b"\n")
            remainder = segments.pop(0)
            for raw in reversed(segments):
                result = _lifecycle(raw)
                if result:
                    return result
            end = begin
        return _lifecycle(remainder)


def _last_complete_offset(path: Path, size: int) -> int:
    """Leave an unfinished final JSONL record unread at first observation."""
    with path.open("rb") as stream:
        start = max(0, size - 4 * 1024 * 1024)
        stream.seek(start)
        data = stream.read(size - start)
    marker = data.rfind(b"\n")
    return start + marker + 1 if marker >= 0 else 0


def _events_since(path: Path, offset: int, size: int) -> tuple[list[tuple[str, str, float]], int]:
    """Only advance past whole JSONL lines; retry any incomplete suffix."""
    if size - offset > MAX_DELTA:
        last = _tail_event(path, size)
        return ([last] if last else []), size
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(size - offset)
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return [], offset
    complete = data[:boundary].split(b"\n")
    return [event for line in complete if (event := _lifecycle(line))], offset + boundary + 1


def _safe_web_url(raw: object) -> str | None:
    try:
        u = urlsplit(str(raw))
        if u.scheme != "https" or u.hostname != "chatgpt.com" or u.port not in (None, 443):
            return None
        if not (u.path.startswith("/c/") or (u.path.startswith("/g/") and "/c/" in u.path)):
            return None
        return urlunsplit(("https", "chatgpt.com", u.path, "", ""))
    except ValueError:
        return None


def _display_title(title: object, fallback: str) -> str:
    cleaned = " ".join(str(title or "").split())
    return cleaned[:140] or fallback


def telegram_send(item: dict) -> None:
    """Use the existing user's Telegram credential only through systemd LoadCredential."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return
    path = Path(directory) / "telegram.env"
    try:
        settings = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                settings[key.strip()] = value.strip().strip("'\"")
        prefix = "TELEGRAM_INSERT_BOT_NOISY"
        token, chat_id = settings[prefix + "_BOT_TOKEN"], settings[prefix + "_CHAT_ID"]
        if not token or not chat_id:
            raise ValueError("missing_credentials")
        label = "Web" if item["source"] == "web" else "Desktop"
        state = "terminata" if item["status"] == "finished" else "interrotta"
        message = f"ChatGPT {label}: {item['title']} — {state}\n{item['url']}"
        body = json.dumps({"chat_id": chat_id, "text": message, "disable_web_page_preview": True}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=12) as response:
            if json.load(response).get("ok") is not True:
                raise ValueError("telegram_rejected")
    except (OSError, ValueError, KeyError, TypeError):
        # Never log request URLs, tokens, chat IDs or response bodies.
        print("CCS activity: Telegram delivery unavailable", file=sys.stderr)


class ActivityMonitor:
    def __init__(
        self,
        *,
        session_db: Path | None = None,
        sessions_root: Path | None = None,
        sender: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.session_db = session_db or Path.home() / ".codex/state_5.sqlite"
        self.sessions_root = (sessions_root or Path.home() / ".codex/sessions").resolve()
        self.clock = clock
        self.started_at = clock()
        self.sender = sender or (lambda item: threading.Thread(target=telegram_send, args=(item,), daemon=True).start())
        self._web: dict[int, dict] = {}
        self._desktop: dict[str, dict] = {}
        self._cursors: dict[str, int] = {}
        self._notified: set[str] = set()
        self._last_web_notification: dict[str, float] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._watch, daemon=True, name="ccs-activity")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_desktop()
            except (OSError, sqlite3.Error, ValueError):
                # A temporarily locked/unavailable Codex DB must not stop the bridge.
                pass
            self._stop.wait(3)

    def observe_web(self, payload: dict) -> dict:
        url = _safe_web_url(payload.get("url"))
        try:
            tab_id = int(payload.get("tab_id"))
        except (TypeError, ValueError):
            tab_id = -1
        status = payload.get("status")
        if not url or tab_id < 0 or status not in {"running", "idle"}:
            raise ValueError("invalid_web_activity")
        now = self.clock()
        to_send = None
        with self._lock:
            old = self._web.get(tab_id)
            if old and old["url"] != url:
                old = None  # navigating to another chat is not completion
            name = _display_title(payload.get("title"), "Chat Web")
            if status == "running":
                self._web[tab_id] = dict(source="web", key=f"web:{tab_id}", url=url, title=name,
                                          status="running", observed_at=now, updated_at=now)
            elif old and old["status"] == "running":
                current = dict(source="web", key=f"web:{tab_id}", url=url, title=name,
                               status="finished", observed_at=now, updated_at=now)
                self._web[tab_id] = current
                if now - self._last_web_notification.get(url, 0) > 15:
                    self._last_web_notification[url] = now
                    to_send = current.copy()
            elif old:
                old.update(title=name, observed_at=now)
            else:
                # Initial idle never counts as a completed answer.
                self._web[tab_id] = dict(source="web", key=f"web:{tab_id}", url=url, title=name,
                                          status="idle", observed_at=now, updated_at=now)
        if to_send:
            self.sender(to_send)
        return {"ok": True}

    def remove_web_tab(self, payload: dict) -> dict:
        try:
            tab_id = int(payload.get("tab_id"))
        except (TypeError, ValueError):
            raise ValueError("invalid_tab_id") from None
        with self._lock:
            self._web.pop(tab_id, None)  # closing is never a confirmed completion
        return {"ok": True}

    def _recent_threads(self) -> list[tuple]:
        uri = self.session_db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as conn:
            return conn.execute(
                "SELECT id,COALESCE(NULLIF(name,''),NULLIF(title,''),''),rollout_path "
                "FROM threads WHERE COALESCE(archived,0)=0 "
                "AND source='vscode' AND thread_source='user' "
                "AND (originator='Codex Desktop' OR originator IS NULL) "
                "ORDER BY COALESCE(recency_at_ms,updated_at_ms,created_at_ms,0) DESC LIMIT ?",
                (MAX_THREADS,),
            ).fetchall()

    def _desktop_update(self, tid: str, title: str, event: tuple[str, str, float]) -> dict | None:
        kind, turn_id, timestamp = event
        now = self.clock()
        new_status = "running" if kind == "task_started" else TERMINAL[kind]
        old = self._desktop.get(tid)
        url = "codex://threads/" + tid
        row = dict(source="desktop", key="desktop:" + tid, url=url, title=title,
                   status=new_status, updated_at=timestamp or now, observed_at=now)
        self._desktop[tid] = row
        fingerprint = tid + ":" + (turn_id or str(timestamp)) + ":" + kind
        if new_status in {"finished", "interrupted"} and fingerprint not in self._notified:
            self._notified.add(fingerprint)
            # An old completion encountered at startup must never fire a notification.
            if timestamp > self.started_at:
                return row.copy()
        return None

    def poll_desktop(self) -> None:
        selected = []
        for tid, title, path_raw in self._recent_threads():
            if not isinstance(tid, str) or not UUID_RE.fullmatch(tid) or not path_raw:
                continue
            path = Path(path_raw).resolve()
            if not path.is_relative_to(self.sessions_root):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            selected.append((tid, _display_title(title, "Codex " + tid[:8]), path, stat.st_size))
        pending = []
        with self._lock:
            for tid, title, path, size in selected:
                key = str(path)
                old_size = self._cursors.get(key)
                try:
                    if old_size is None or size < old_size:
                        event = _tail_event(path, size)
                        events = [event] if event else []
                        next_offset = _last_complete_offset(path, size)
                    elif size > old_size:
                        events, next_offset = _events_since(path, old_size, size)
                    else:
                        events = []
                        next_offset = size
                except (OSError, ValueError):
                    continue
                # Only consume completed JSONL records; preserve incomplete suffix.
                self._cursors[key] = next_offset
                for event in events:
                    notification = self._desktop_update(tid, title, event)
                    if notification:
                        pending.append(notification)
                if tid in self._desktop:
                    self._desktop[tid]["title"] = title
            # Bounded caches, not a second persistent task lifecycle source.
            if len(self._notified) > 1000:
                self._notified = set(list(self._notified)[-500:])
            if len(self._cursors) > MAX_THREADS * 3:
                valid = {str(path) for _, _, path, _ in selected}
                self._cursors = {p: n for p, n in self._cursors.items() if p in valid}
        for row in pending:
            self.sender(row)

    def snapshot(self) -> dict:
        now = self.clock()
        rows = []
        with self._lock:
            for row in self._web.values():
                age = now - row["observed_at"]
                if age > WEB_TTL:
                    continue  # stale tab is not evidence of a running/completed answer
                if row["status"] == "running" or (row["status"] == "finished" and age < RECENT_TTL):
                    rows.append(row.copy())
            for row in self._desktop.values():
                age = now - row["updated_at"]
                if row["status"] == "running" and age < DESKTOP_IDLE_LIMIT:
                    rows.append(row.copy())
                elif row["status"] in {"finished", "interrupted"} and age < RECENT_TTL:
                    rows.append(row.copy())
        rows.sort(key=lambda r: (r["status"] != "running", -r["updated_at"]))
        return {"ok": True, "active": sum(r["status"] == "running" for r in rows),
                "recent": sum(r["status"] != "running" for r in rows), "chats": rows}
