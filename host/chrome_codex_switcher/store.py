from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contexts (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    geometry_json TEXT NOT NULL DEFAULT '{}',
    hidden INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contexts_url ON contexts(url);

CREATE TABLE IF NOT EXISTS twins (
    codex_thread TEXT PRIMARY KEY,
    codex_deep_link TEXT NOT NULL UNIQUE,
    context_id TEXT NOT NULL UNIQUE REFERENCES contexts(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_bindings (
    prompt_id TEXT PRIMARY KEY,
    context_id TEXT UNIQUE REFERENCES contexts(id) ON DELETE SET NULL,
    codex_thread TEXT UNIQUE,
    codex_deep_link TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _context_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        try:
            result["geometry"] = json.loads(result.pop("geometry_json") or "{}")
        except json.JSONDecodeError:
            result["geometry"] = {}
            result.pop("geometry_json", None)
        result["hidden"] = bool(result["hidden"])
        result["collapsed"] = bool(result["collapsed"])
        return result

    def upsert_context(self, context_id: str, url: str, title: str = "") -> dict[str, Any]:
        ts = now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO contexts(id,url,title,created_at,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE contexts.title END,
                    updated_at=excluded.updated_at
                """,
                (context_id, url, title, ts, ts),
            )
        return self.get_context(context_id) or {}

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contexts WHERE id=?", (context_id,)).fetchone()
            context = self._context_row(row)
            if context:
                twin = db.execute("SELECT codex_thread,codex_deep_link FROM twins WHERE context_id=?", (context_id,)).fetchone()
                context["twin"] = dict(twin) if twin else None
            return context

    def set_note(self, context_id: str, note: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("UPDATE contexts SET note=?, updated_at=? WHERE id=?", (note, now(), context_id))
        return self.get_context(context_id)

    def set_ui(self, context_id: str, *, geometry: dict | None = None, hidden: bool | None = None, collapsed: bool | None = None) -> dict[str, Any] | None:
        pieces: list[str] = []
        values: list[Any] = []
        if geometry is not None:
            pieces.append("geometry_json=?")
            values.append(json.dumps(geometry, separators=(",", ":")))
        if hidden is not None:
            pieces.append("hidden=?")
            values.append(int(hidden))
        if collapsed is not None:
            pieces.append("collapsed=?")
            values.append(int(collapsed))
        if not pieces:
            return self.get_context(context_id)
        pieces.append("updated_at=?")
        values.append(now())
        values.append(context_id)
        with self._connect() as db:
            db.execute(f"UPDATE contexts SET {', '.join(pieces)} WHERE id=?", values)
        return self.get_context(context_id)

    def link(self, context_id: str, thread: str, deep_link: str) -> dict[str, Any]:
        ts = now()
        prompt_id: str | None = None
        with self._connect() as db:
            # Maintain one-to-one semantics in both directions.
            db.execute("DELETE FROM twins WHERE context_id=? OR codex_thread=?", (context_id, thread))
            db.execute(
                "INSERT INTO twins(codex_thread,codex_deep_link,context_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (thread, deep_link, context_id, ts, ts),
            )
            binding = db.execute(
                "SELECT prompt_id FROM prompt_bindings WHERE context_id=?",
                (context_id,),
            ).fetchone()
            prompt_id = str(binding["prompt_id"]) if binding else None
        if prompt_id:
            self.bind_prompt(
                prompt_id,
                context_id=context_id,
                codex_thread=thread,
                codex_deep_link=deep_link,
            )
        return self.get_context(context_id) or {}

    def unlink_context(self, context_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM twins WHERE context_id=?", (context_id,))

    def twin_by_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT t.codex_thread,t.codex_deep_link,t.context_id,c.url,c.title,c.note
                FROM twins t JOIN contexts c ON c.id=t.context_id
                WHERE t.context_id=?
                """,
                (context_id,),
            ).fetchone()
            return dict(row) if row else None

    def twin_by_thread(self, thread: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT t.codex_thread,t.codex_deep_link,t.context_id,c.url,c.title,c.note
                FROM twins t JOIN contexts c ON c.id=t.context_id
                WHERE t.codex_thread=?
                """,
                (thread,),
            ).fetchone()
            return dict(row) if row else None

    def list_contexts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.*,t.codex_thread,t.codex_deep_link
                FROM contexts c LEFT JOIN twins t ON t.context_id=c.id
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._context_row(row)
            if item is None:
                continue
            if item.get("codex_thread"):
                item["twin"] = {
                    "codex_thread": item.pop("codex_thread"),
                    "codex_deep_link": item.pop("codex_deep_link"),
                }
            else:
                item.pop("codex_thread", None)
                item.pop("codex_deep_link", None)
                item["twin"] = None
            result.append(item)
        return result


    def bind_prompt(
        self,
        prompt_id: str,
        *,
        context_id: str | None = None,
        codex_thread: str | None = None,
        codex_deep_link: str | None = None,
    ) -> dict[str, Any]:
        ts = now()
        with self._connect() as db:
            current = db.execute(
                "SELECT * FROM prompt_bindings WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
            created = float(current["created_at"]) if current else ts
            existing_context = str(current["context_id"]) if current and current["context_id"] else None
            existing_thread = str(current["codex_thread"]) if current and current["codex_thread"] else None
            existing_link = str(current["codex_deep_link"]) if current and current["codex_deep_link"] else None
            if context_id is not None:
                db.execute(
                    "DELETE FROM prompt_bindings WHERE prompt_id<>? AND context_id=?",
                    (prompt_id, context_id),
                )
            if codex_thread is not None:
                db.execute(
                    "DELETE FROM prompt_bindings WHERE prompt_id<>? AND codex_thread=?",
                    (prompt_id, codex_thread),
                )
            db.execute(
                """INSERT INTO prompt_bindings(
                     prompt_id,context_id,codex_thread,codex_deep_link,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(prompt_id) DO UPDATE SET
                     context_id=excluded.context_id,
                     codex_thread=excluded.codex_thread,
                     codex_deep_link=excluded.codex_deep_link,
                     updated_at=excluded.updated_at""",
                (
                    prompt_id,
                    context_id if context_id is not None else existing_context,
                    codex_thread if codex_thread is not None else existing_thread,
                    codex_deep_link if codex_deep_link is not None else existing_link,
                    created,
                    ts,
                ),
            )
        return self.prompt_binding(prompt_id) or {"prompt_id": prompt_id}

    def prompt_binding(self, prompt_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT p.prompt_id,p.context_id,p.codex_thread,p.codex_deep_link,
                          p.created_at,p.updated_at,c.url,c.title,c.note
                   FROM prompt_bindings p
                   LEFT JOIN contexts c ON c.id=p.context_id
                   WHERE p.prompt_id=?""",
                (prompt_id,),
            ).fetchone()
            return dict(row) if row else None

    def prompt_by_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM prompt_bindings WHERE context_id=?",
                (context_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        raw = json.dumps(value, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def delete_meta(self, key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM meta WHERE key=?", (key,))
