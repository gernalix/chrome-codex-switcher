from __future__ import annotations

import json
import os
import re
import time
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CODEX_LINK_RE = re.compile(r"codex://threads/([^\s/?#]+)(?:[^\s]*)?", re.IGNORECASE)


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "chrome-codex-switcher"


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "chrome-codex-switcher"


def db_path() -> Path:
    return Path(os.environ.get("CCS_DB_PATH", state_dir() / "state.sqlite3"))


def overlay_path() -> Path:
    return Path(os.environ.get("CCS_OVERLAY_PATH", cache_dir() / "overlay.json"))


def parse_codex_link(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    match = CODEX_LINK_RE.search(text.strip())
    if not match:
        return None
    thread = match.group(1)
    return thread, f"codex://threads/{thread}"


def canonical_url(url: str) -> str:
    """Drop only fragments; query strings remain meaningful."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            return url
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    except ValueError:
        return url


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def now() -> float:
    return time.time()
