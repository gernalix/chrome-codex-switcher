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
    codex_note TEXT NOT NULL DEFAULT '',
    notes_independent INTEGER NOT NULL DEFAULT 0,
    geometry_json TEXT NOT NULL DEFAULT '{}',
    hidden INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contexts_url ON contexts(url);

CREATE TABLE IF NOT EXISTS context_url_supersessions (
    old_url TEXT PRIMARY KEY,
    new_url TEXT NOT NULL,
    context_id TEXT NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_url_supersessions_new_url
ON context_url_supersessions(new_url);

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

CREATE TABLE IF NOT EXISTS context_prompt_ids (
    context_id TEXT NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL,
    auto_detected INTEGER NOT NULL DEFAULT 0,
    manual_added INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(context_id, prompt_id),
    CHECK(length(prompt_id)=6 AND prompt_id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]')
);

CREATE INDEX IF NOT EXISTS idx_context_prompt_ids_prompt ON context_prompt_ids(prompt_id);

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
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(contexts)").fetchall()}
            if "codex_note" not in columns:
                db.execute("ALTER TABLE contexts ADD COLUMN codex_note TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE contexts SET codex_note=note")
            if "notes_independent" not in columns:
                db.execute("ALTER TABLE contexts ADD COLUMN notes_independent INTEGER NOT NULL DEFAULT 0")

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
        result["notes_independent"] = bool(result.get("notes_independent", 0))
        return result

    @staticmethod
    def _resolve_context_url_db(db: sqlite3.Connection, url: str) -> str:
        current = str(url or "")
        seen: set[str] = set()
        for _ in range(16):
            if not current or current in seen:
                break
            seen.add(current)
            row = db.execute(
                "SELECT new_url FROM context_url_supersessions WHERE old_url=?",
                (current,),
            ).fetchone()
            if row is None:
                break
            current = str(row["new_url"] or current)
        return current

    def resolve_context_url(self, url: str) -> str:
        with self._connect() as db:
            return self._resolve_context_url_db(db, url)

    def upsert_context(self, context_id: str, url: str, title: str = "") -> dict[str, Any]:
        ts = now()
        effective_id = context_id
        with self._connect() as db:
            url = self._resolve_context_url_db(db, url)
            current = db.execute(
                "SELECT id FROM contexts WHERE id=?",
                (context_id,),
            ).fetchone()
            if current is None and url:
                existing = db.execute(
                    """
                    SELECT c.id
                    FROM contexts c
                    LEFT JOIN prompt_bindings p ON p.context_id=c.id
                    LEFT JOIN twins t ON t.context_id=c.id
                    WHERE c.url=?
                    ORDER BY
                        CASE WHEN p.prompt_id IS NOT NULL THEN 1 ELSE 0 END DESC,
                        CASE WHEN t.codex_thread IS NOT NULL THEN 1 ELSE 0 END DESC,
                        CASE WHEN c.note <> '' OR c.codex_note <> ''
                                  OR c.geometry_json <> '{}'
                                  OR c.hidden <> 0 OR c.collapsed <> 0
                             THEN 1 ELSE 0 END DESC,
                        c.updated_at DESC
                    LIMIT 1
                    """,
                    (url,),
                ).fetchone()
                if existing is not None:
                    effective_id = str(existing["id"])

            db.execute(
                """
                INSERT INTO contexts(id,url,title,created_at,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE contexts.title END,
                    updated_at=excluded.updated_at
                """,
                (effective_id, url, title, ts, ts),
            )
        return self.get_context(effective_id) or {}

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM contexts WHERE id=?", (context_id,)).fetchone()
            context = self._context_row(row)
            if context:
                twin = db.execute("SELECT codex_thread,codex_deep_link FROM twins WHERE context_id=?", (context_id,)).fetchone()
                context["twin"] = dict(twin) if twin else None
            return context

    def set_note(self, context_id: str, note: str, *, surface: str = "chrome") -> dict[str, Any] | None:
        context = self.get_context(context_id)
        if not context:
            return None
        surface = "codex" if surface == "codex" else "chrome"
        with self._connect() as db:
            if context.get("notes_independent"):
                column = "codex_note" if surface == "codex" else "note"
                db.execute(
                    f"UPDATE contexts SET {column}=?, updated_at=? WHERE id=?",
                    (note, now(), context_id),
                )
            else:
                db.execute(
                    "UPDATE contexts SET note=?, codex_note=?, updated_at=? WHERE id=?",
                    (note, note, now(), context_id),
                )
        return self.get_context(context_id)

    def set_note_mode(
        self,
        context_id: str,
        independent: bool,
        *,
        source: str = "chrome",
        current_note: str | None = None,
    ) -> dict[str, Any] | None:
        context = self.get_context(context_id)
        if not context:
            return None
        source = "codex" if source == "codex" else "chrome"
        was_independent = bool(context.get("notes_independent"))
        with self._connect() as db:
            if independent:
                if not was_independent:
                    shared = (
                        str(current_note)
                        if current_note is not None
                        else str(context.get("note") or "")
                    )
                    db.execute(
                        "UPDATE contexts SET note=?, codex_note=?, notes_independent=1, updated_at=? WHERE id=?",
                        (shared, shared, now(), context_id),
                    )
                else:
                    db.execute(
                        "UPDATE contexts SET notes_independent=1, updated_at=? WHERE id=?",
                        (now(), context_id),
                    )
            else:
                if current_note is not None:
                    shared = str(current_note)
                elif source == "codex":
                    shared = str(context.get("codex_note") or "")
                else:
                    shared = str(context.get("note") or "")
                db.execute(
                    "UPDATE contexts SET note=?, codex_note=?, notes_independent=0, updated_at=? WHERE id=?",
                    (shared, shared, now(), context_id),
                )
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

    def link_prompt(self, prompt_id: str, context_id: str, thread: str, deep_link: str) -> dict[str, Any]:
        ts = now()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM contexts WHERE id=?", (context_id,)).fetchone():
                raise ValueError("prompt_context_missing")
            current = db.execute("SELECT context_id FROM prompt_bindings WHERE prompt_id=?", (prompt_id,)).fetchone()
            if current and current["context_id"] not in (None, context_id):
                raise ValueError("prompt_context_mismatch")
            conflict = db.execute(
                "SELECT 1 FROM prompt_bindings WHERE prompt_id<>? AND (context_id=? OR codex_thread=?)",
                (prompt_id, context_id, thread),
            ).fetchone()
            if conflict:
                raise ValueError("prompt_pairing_conflict")
            db.execute("DELETE FROM twins WHERE context_id=? OR codex_thread=?", (context_id, thread))
            db.execute(
                "INSERT INTO twins(codex_thread,codex_deep_link,context_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (thread, deep_link, context_id, ts, ts),
            )
            db.execute(
                """INSERT INTO prompt_bindings(prompt_id,context_id,codex_thread,codex_deep_link,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(prompt_id) DO UPDATE SET
                   context_id=excluded.context_id,codex_thread=excluded.codex_thread,
                   codex_deep_link=excluded.codex_deep_link,updated_at=excluded.updated_at""",
                (prompt_id, context_id, thread, deep_link, ts, ts),
            )
        return self.prompt_binding(prompt_id) or {}

    def unlink_context(self, context_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM twins WHERE context_id=?", (context_id,))

    def twin_by_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT t.codex_thread,t.codex_deep_link,t.context_id,c.url,c.title,c.note,c.codex_note,c.notes_independent
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
                SELECT t.codex_thread,t.codex_deep_link,t.context_id,c.url,c.title,c.note,c.codex_note,c.notes_independent
                FROM twins t JOIN contexts c ON c.id=t.context_id
                WHERE t.codex_thread=?
                """,
                (thread,),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _normalize_prompt_id(value: Any) -> str:
        prompt_id = str(value or "").strip()
        if len(prompt_id) != 6 or not prompt_id.isdigit():
            raise ValueError("invalid_prompt_id")
        return prompt_id

    @staticmethod
    def _prompt_index_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["auto_detected"] = bool(item.get("auto_detected"))
        item["manual_added"] = bool(item.get("manual_added"))
        item["excluded"] = bool(item.get("excluded"))
        return item

    def context_prompt_index(
        self,
        context_id: str,
        *,
        include_excluded: bool = False,
    ) -> list[dict[str, Any]]:
        clause = "" if include_excluded else (
            " AND (manual_added=1 OR (auto_detected=1 AND excluded=0))"
        )
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT context_id,prompt_id,auto_detected,manual_added,excluded,
                           first_seen_at,updated_at
                    FROM context_prompt_ids
                    WHERE context_id=?{clause}
                    ORDER BY prompt_id""",
                (context_id,),
            ).fetchall()
        return [self._prompt_index_row(row) for row in rows]

    def observe_context_prompt_ids(
        self,
        context_id: str,
        prompt_ids: list[str],
    ) -> list[dict[str, Any]]:
        clean = sorted({self._normalize_prompt_id(value) for value in prompt_ids})
        ts = now()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM contexts WHERE id=?", (context_id,)).fetchone():
                raise ValueError("context_not_found")
            for prompt_id in clean:
                db.execute(
                    """INSERT INTO context_prompt_ids(
                         context_id,prompt_id,auto_detected,manual_added,excluded,
                         first_seen_at,updated_at
                       ) VALUES(?,?,1,0,0,?,?)
                       ON CONFLICT(context_id,prompt_id) DO UPDATE SET
                         auto_detected=1,
                         updated_at=excluded.updated_at""",
                    (context_id, prompt_id, ts, ts),
                )
        return self.context_prompt_index(context_id)

    def add_context_prompt_id(
        self,
        context_id: str,
        prompt_id: str,
    ) -> list[dict[str, Any]]:
        prompt_id = self._normalize_prompt_id(prompt_id)
        ts = now()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM contexts WHERE id=?", (context_id,)).fetchone():
                raise ValueError("context_not_found")
            db.execute(
                """INSERT INTO context_prompt_ids(
                     context_id,prompt_id,auto_detected,manual_added,excluded,
                     first_seen_at,updated_at
                   ) VALUES(?,?,0,1,0,?,?)
                   ON CONFLICT(context_id,prompt_id) DO UPDATE SET
                     manual_added=1,
                     excluded=0,
                     updated_at=excluded.updated_at""",
                (context_id, prompt_id, ts, ts),
            )
        return self.context_prompt_index(context_id)

    def remove_context_prompt_id(
        self,
        context_id: str,
        prompt_id: str,
    ) -> list[dict[str, Any]]:
        prompt_id = self._normalize_prompt_id(prompt_id)
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM contexts WHERE id=?", (context_id,)).fetchone():
                raise ValueError("context_not_found")
            row = db.execute(
                """SELECT auto_detected FROM context_prompt_ids
                   WHERE context_id=? AND prompt_id=?""",
                (context_id, prompt_id),
            ).fetchone()
            if row is None:
                return self.context_prompt_index(context_id)
            if bool(row["auto_detected"]):
                db.execute(
                    """UPDATE context_prompt_ids
                       SET manual_added=0, excluded=1, updated_at=?
                       WHERE context_id=? AND prompt_id=?""",
                    (now(), context_id, prompt_id),
                )
            else:
                db.execute(
                    "DELETE FROM context_prompt_ids WHERE context_id=? AND prompt_id=?",
                    (context_id, prompt_id),
                )
        return self.context_prompt_index(context_id)

    def list_contexts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT c.*,
                       COALESCE(t.codex_thread,p.codex_thread) AS codex_thread,
                       COALESCE(t.codex_deep_link,p.codex_deep_link) AS codex_deep_link,
                       p.prompt_id
                FROM contexts c
                LEFT JOIN twins t ON t.context_id=c.id
                LEFT JOIN prompt_bindings p ON p.context_id=c.id
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
            prompt_rows = db.execute(
                """SELECT context_id,prompt_id,auto_detected,manual_added,excluded,
                          first_seen_at,updated_at
                   FROM context_prompt_ids
                   WHERE manual_added=1 OR (auto_detected=1 AND excluded=0)
                   ORDER BY prompt_id"""
            ).fetchall()

        prompt_index: dict[str, list[dict[str, Any]]] = {}
        for row in prompt_rows:
            association = self._prompt_index_row(row)
            prompt_index.setdefault(str(association["context_id"]), []).append(association)

        codex_titles = self.get_meta("codex_ui_titles", {})
        if not isinstance(codex_titles, dict):
            codex_titles = {}
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._context_row(row)
            if item is None:
                continue
            if item.get("codex_thread"):
                thread = str(item.pop("codex_thread"))
                item["twin"] = {
                    "codex_thread": thread,
                    "codex_deep_link": item.pop("codex_deep_link"),
                    "codex_title": str(codex_titles.get(thread) or ""),
                }
            else:
                item.pop("codex_thread", None)
                item.pop("codex_deep_link", None)
                item["twin"] = None
            associations = prompt_index.get(str(item["id"]), [])
            item["prompt_id_index"] = associations
            item["prompt_ids"] = [entry["prompt_id"] for entry in associations]
            result.append(item)
        return result


    @staticmethod
    def _normalize_codex_title(title: str) -> str:
        return " ".join(str(title or "").split()).casefold()

    def remember_codex_title(self, thread: str, title: str) -> None:
        clean = " ".join(str(title or "").split()).strip()
        if not thread or not clean:
            return
        mapping = self.get_meta("codex_ui_titles", {})
        if not isinstance(mapping, dict):
            mapping = {}
        mapping[str(thread)] = clean
        self.set_meta("codex_ui_titles", mapping)

    def codex_title_for_thread(self, thread: str) -> str | None:
        mapping = self.get_meta("codex_ui_titles", {})
        if not isinstance(mapping, dict):
            return None
        title = mapping.get(str(thread))
        return str(title) if title else None

    def thread_by_codex_title(self, title: str) -> str | None:
        wanted = self._normalize_codex_title(title)
        if not wanted:
            return None
        mapping = self.get_meta("codex_ui_titles", {})
        if not isinstance(mapping, dict):
            return None
        matches = [
            str(thread)
            for thread, saved_title in mapping.items()
            if self._normalize_codex_title(str(saved_title or "")) == wanted
        ]
        # Duplicate chat titles are inherently ambiguous: never guess.
        return matches[0] if len(matches) == 1 else None


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
                          p.created_at,p.updated_at,c.url,c.title,c.note,c.codex_note,c.notes_independent
                   FROM prompt_bindings p
                   LEFT JOIN contexts c ON c.id=p.context_id
                   WHERE p.prompt_id=?""",
                (prompt_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_prompt_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.prompt_id,p.context_id,p.codex_thread,p.codex_deep_link,
                          p.created_at,p.updated_at,c.url,c.title,c.note,c.codex_note,c.notes_independent
                   FROM prompt_bindings p
                   LEFT JOIN contexts c ON c.id=p.context_id
                   ORDER BY p.prompt_id"""
            ).fetchall()
            return [dict(row) for row in rows]

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
