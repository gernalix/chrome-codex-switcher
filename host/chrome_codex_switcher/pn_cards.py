"""Read-only Pn presentation; local annotations live in the existing CCS meta store."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

PN_START = re.compile(r'^(P[1-9][0-9]*)(?=$|[\s:;,|/\-–—])')
STATES = ('sconosciuto', 'non avviato', 'in corso', 'BLOCKED', 'FAIL', 'PASS', 'annullato')
FIELDS = ('title', 'what', 'why', 'status', 'blocker', 'next_step')
STALE_SECONDS = 24 * 3600


def classify(note: str) -> tuple[str, str] | None:
    first = str(note or '').split('\n', 1)[0].rstrip('\r')
    match = PN_START.match(first)
    if not match:
        return None
    return match[1], first[match.end():].strip(' \t:;,|/-–—')


def sync_note(note: str) -> str:
    """UI-only preview. Never mutate the original note or the sync contract."""
    match = classify(note)
    if not match:
        return note
    first, *tail = note.split('\n', 1)
    return '\n'.join([match[0], *([match[1]] if match[1] else []), *tail])


class PnCards:
    def __init__(self, store, *, root: Path | None = None):
        self.store = store
        self.root = root or Path(os.environ.get('CCS_MEGAVAULT_ROOT', '/home/daniele/MegaVault'))
        self._lock = threading.RLock()
        self._checked = {}
        self._results = {}

    def _canonical(self, pid):
        key = 'pn-card-cache:' + pid
        cached = self.store.get_meta(key)
        stamp = time.time()
        # Requests use the existing refresh cycle, without another poller.
        if stamp - self._checked.get(pid, 0) < 30:
            return self._results.get(pid)
        self._checked[pid] = stamp
        db = self.root / 'megavault.sqlite'
        try:
            with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=0.3)) as conn:
                row = conn.execute('SELECT updated_at FROM prompt_codex WHERE id=?', (int(pid[1:]),)).fetchone()
                if not row:
                    self._results[pid] = None
                    return None  # Do not reuse an entry that vanished from the authority.
                project = None
                if cached and cached['record']['updated_at'] == row[0]:
                    record = cached['record']
                else:
                    proc = subprocess.run(
                        ['python3', str(self.root / 'megavault.py'), 'prompt', 'show', pid],
                        capture_output=True, text=True, timeout=4, check=True,
                    )
                    record = json.loads(proc.stdout)
                    if record['prompt_id'] != pid or not record['verification']['VALID']:
                        self._results[pid] = None
                        return None
                    if hashlib.sha256(record['prompt_body'].encode()).hexdigest() != record['prompt_sha256']:
                        self._results[pid] = None
                        return None
                project_row = conn.execute('SELECT name FROM projects WHERE project_id=?', (record['project_id'],)).fetchone()
                project = project_row[0] if project_row else None
            # Cache only the verified projection, never the private complete prompt body.
            safe = {k: record.get(k) for k in ('prompt_id', 'title', 'updated_at', 'project_id', 'status', 'chat_ids', 'chatgpt_source_url')}
            result = {'record': safe, 'project': project, 'verified_at': stamp, 'offline': False}
            self.store.set_meta(key, result)
            self._results[pid] = result
            return result
        except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError, KeyError):
            result = {**cached, 'offline': True} if cached else None
            self._results[pid] = result
            return result

    def enrich(self, context):
        if not context:
            return context
        context = dict(context)
        match = classify(context.get('note', ''))
        context['pn_card'] = None
        if not match:
            return context
        pid, note_title = match
        with self._lock:
            cached = self._canonical(pid)
        record = cached['record'] if cached else {}
        manual = self.store.get_meta('pn-card-manual:' + pid, {})
        status = manual.get('status') or 'sconosciuto'
        timestamp = manual.get('updated_at') if manual.get('status') else None
        stale = bool(timestamp and time.time() - timestamp > STALE_SECONDS)
        links = []
        if record.get('chatgpt_source_url'):
            links.append({'label': 'ChatGPT Web', 'url': record['chatgpt_source_url']})
        for cid in record.get('chat_ids') or []:
            links.append({'label': 'Codex ' + cid[:8], 'url': 'codex://threads/' + cid})
        context['pn_card'] = {
            'id': pid,
            'title': manual.get('title') or record.get('title') or note_title or 'non disponibile',
            'what': manual.get('what') or 'non disponibile',
            'why': manual.get('why') or 'non disponibile',
            'status': status,
            'status_source': 'Manuale locale (utente)' if timestamp else 'Nessun feed Codex verificato',
            'status_at': timestamp,
            'status_stale': stale,
            'updated_at': max(float(context.get('updated_at') or 0), float(manual.get('updated_at') or 0)),
            'registry_updated_at': record.get('updated_at'),
            'registry_status': record.get('status') or 'non disponibile',
            'project': cached.get('project') or 'non disponibile' if cached else 'non disponibile',
            'project_id': record.get('project_id'),
            'verified_at': cached.get('verified_at') if cached else None,
            'metadata_stale': bool(cached and (cached.get('offline') or time.time() - cached['verified_at'] > STALE_SECONDS)),
            'metadata_source': 'MegaVault verificato' if cached else 'MegaVault non disponibile / Pn non verificato',
            'links': links,
            'blocker': manual.get('blocker') or 'non disponibile',
            'next_step': manual.get('next_step') or 'non disponibile',
            'manual': manual,
            'sync_ready': context.get('note', '').split('\n', 1)[0].rstrip('\r') == pid,
            'sync_note': sync_note(context.get('note', '')),
        }
        return context

    def update_manual(self, pid, values):
        if not re.fullmatch(r'P[1-9][0-9]*', str(pid)):
            raise ValueError('invalid_pn')
        if values.get('status', '') not in ('', *STATES):
            raise ValueError('invalid_codex_status')
        if any(not isinstance(values.get(k, ''), str) or len(values.get(k, '')) > 2000 for k in FIELDS):
            raise ValueError('invalid_manual_annotation')
        manual = {k: values.get(k, '').strip() for k in FIELDS}
        manual.update(updated_at=time.time(), source='utente locale')
        self.store.set_meta('pn-card-manual:' + pid, manual)
        return {'ok': True, 'manual': manual}
