from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = ROOT / "extension"
DAEMON_PORT = 43817


class _PageHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>CCS E2E note state</title></head>
<body style="margin:0;min-height:1400px"><main>Chrome Codex Switcher E2E fixture</main></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NoteStatePersistenceE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)
        cls.db_path = cls.tmp_path / "state.sqlite3"

        cls.page_server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        cls.page_port = cls.page_server.server_address[1]
        cls.page_thread = threading.Thread(target=cls.page_server.serve_forever, daemon=True)
        cls.page_thread.start()

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "host")
        env["CCS_DB_PATH"] = str(cls.db_path)
        env["CCS_PORT"] = str(DAEMON_PORT)
        env["CCS_DISABLE_CLIPBOARD_WATCH"] = "1"
        env["XDG_CACHE_HOME"] = str(cls.tmp_path / "cache")
        cls.daemon = subprocess.Popen(
            [sys.executable, "-m", "chrome_codex_switcher.daemon"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls._wait_daemon()

        options = Options()
        chrome_binary = os.environ.get("CHROME_BINARY")
        if chrome_binary:
            options.binary_location = chrome_binary
        extension = str(EXTENSION_DIR.resolve())
        options.add_argument(f"--disable-extensions-except={extension}")
        options.add_argument(f"--load-extension={extension}")
        options.add_argument(f"--user-data-dir={cls.tmp_path / 'chrome-profile'}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")

        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        driver_log = cls.tmp_path / "chromedriver.log"
        service = Service(
            executable_path=driver_path,
            service_args=["--verbose"],
            log_output=str(driver_log),
        ) if driver_path else Service(service_args=["--verbose"], log_output=str(driver_log))
        try:
            cls.driver = webdriver.Chrome(service=service, options=options)
        except Exception:
            if driver_log.exists():
                print(driver_log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        cls.wait = WebDriverWait(cls.driver, 15)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "driver"):
            cls.driver.quit()
        if hasattr(cls, "daemon"):
            cls.daemon.terminate()
            try:
                cls.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.daemon.kill()
        if hasattr(cls, "page_server"):
            cls.page_server.shutdown()
            cls.page_server.server_close()
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    @classmethod
    def _wait_daemon(cls) -> None:
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if cls.daemon.poll() is not None:
                output = cls.daemon.stdout.read() if cls.daemon.stdout else ""
                raise RuntimeError(f"daemon exited early: {output}")
            try:
                with urlopen(f"http://127.0.0.1:{DAEMON_PORT}/api/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception as exc:  # pragma: no cover - diagnostic path
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"daemon did not become ready: {last_error}")

    def _url(self, path: str, fragment: str) -> str:
        return f"http://127.0.0.1:{self.page_port}/{path}#{fragment}"

    def _canonical_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.page_port}/{path}"

    def _wait_extension(self) -> None:
        self.wait.until(
            lambda driver: driver.execute_script(
                """
                const host = document.querySelector("#chrome-codex-switcher-host");
                const root = host?.shadowRoot;
                return !!root?.querySelector(".note") && !!root.querySelector(".status")?.textContent;
                """
            )
        )

    def _post(self, path: str, payload: dict) -> dict:
        request = Request(
            f"http://127.0.0.1:{DAEMON_PORT}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def _context_for_url(self, canonical_url: str) -> dict:
        deadline = time.time() + 8
        last = None
        while time.time() < deadline:
            with sqlite3.connect(self.db_path) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT * FROM contexts WHERE url=? ORDER BY updated_at DESC LIMIT 1",
                    (canonical_url,),
                ).fetchone()
            if row:
                last = dict(row)
                return last
            time.sleep(0.1)
        self.fail(f"context not created for {canonical_url}: {last!r}")

    def _snapshot(self) -> dict:
        return self.driver.execute_script(
            """
            const host = document.querySelector("#chrome-codex-switcher-host");
            const root = host.shadowRoot;
            const box = root.querySelector(".box");
            const note = root.querySelector(".note");
            return {
              note: note.value,
              x: parseInt(host.style.left, 10),
              y: parseInt(host.style.top, 10),
              width: parseInt(host.style.width, 10),
              height: parseInt(host.style.height, 10),
              collapsed: box.classList.contains("collapsed"),
              hidden: host.style.display === "none"
            };
            """
        )

    def _set_full_state(self, note_text: str) -> dict:
        expected = {
            "note": note_text,
            "x": 137,
            "y": 93,
            "width": 421,
            "height": 263,
            "collapsed": True,
            "hidden": True,
        }
        self.driver.execute_script(
            """
            const host = document.querySelector("#chrome-codex-switcher-host");
            const root = host.shadowRoot;
            const note = root.querySelector(".note");
            note.focus();
            note.value = arguments[0];
            note.dispatchEvent(new Event("input", {bubbles: true}));
            host.style.left = arguments[1] + "px";
            host.style.top = arguments[2] + "px";
            host.style.width = arguments[3] + "px";
            host.style.height = arguments[4] + "px";
            """,
            expected["note"],
            expected["x"],
            expected["y"],
            expected["width"],
            expected["height"],
        )
        # Wait for the real debounced note write before moving focus to the UI controls.
        deadline = time.time() + 8
        while time.time() < deadline:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT note FROM contexts WHERE url=? ORDER BY updated_at DESC LIMIT 1",
                    (self.current_canonical_url,),
                ).fetchone()
            if row and row[0] == note_text:
                break
            time.sleep(0.1)
        else:
            self.fail(f"note debounce did not persist {note_text!r}: {row!r}")
        # The ResizeObserver persists geometry through the normal content-script path.
        self.driver.execute_script(
            'document.querySelector("#chrome-codex-switcher-host").shadowRoot.querySelector(".collapse").click()'
        )
        time.sleep(0.35)
        self.driver.execute_script(
            'document.querySelector("#chrome-codex-switcher-host").shadowRoot.querySelector(".close").click()'
        )
        self._wait_db_state(expected)
        return expected

    def _wait_db_state(self, expected: dict) -> None:
        deadline = time.time() + 8
        last = None
        while time.time() < deadline:
            if self.db_path.exists():
                try:
                    with sqlite3.connect(self.db_path) as db:
                        db.row_factory = sqlite3.Row
                        row = db.execute(
                            """
                            SELECT note,geometry_json,collapsed,hidden
                            FROM contexts
                            WHERE url=?
                            ORDER BY updated_at DESC
                            LIMIT 1
                            """,
                            (self.current_canonical_url,),
                        ).fetchone()
                    if row:
                        geometry = json.loads(row["geometry_json"] or "{}")
                        last = {
                            "note": row["note"],
                            "x": geometry.get("x"),
                            "y": geometry.get("y"),
                            "width": geometry.get("width"),
                            "height": geometry.get("height"),
                            "collapsed": bool(row["collapsed"]),
                            "hidden": bool(row["hidden"]),
                        }
                        if last == expected:
                            return
                except sqlite3.Error:
                    pass
            time.sleep(0.1)
        self.fail(f"SQLite state did not converge. expected={expected!r} actual={last!r}")

    def _assert_full_state(self, expected: dict) -> None:
        self._wait_extension()
        actual = None
        deadline = time.time() + 8
        while time.time() < deadline:
            actual = self._snapshot()
            if actual == expected:
                return
            time.sleep(0.1)
        self.assertEqual(expected, actual)

    def test_refresh_restores_text_geometry_and_visibility_state(self) -> None:
        path = "refresh-case"
        self.current_canonical_url = self._canonical_url(path)
        self.driver.get(self._url(path, "before-refresh"))
        self._wait_extension()
        expected = self._set_full_state("persist through refresh")

        self.driver.refresh()

        self._assert_full_state(expected)

    def test_reopened_tab_restores_text_geometry_and_visibility_state(self) -> None:
        path = "reopen-case"
        self.current_canonical_url = self._canonical_url(path)
        self.driver.get(self._url(path, "before-close"))
        self._wait_extension()
        expected = self._set_full_state("persist through reopen")

        original = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        replacement = self.driver.current_window_handle
        self.driver.switch_to.window(original)
        self.driver.close()
        self.driver.switch_to.window(replacement)
        self.driver.get(self._url(path, "after-reopen"))

        self._assert_full_state(expected)

    def test_recovery_reinjects_existing_tab_without_duplicate_or_manual_refresh(self) -> None:
        path = "extension-reload-case"
        self.current_canonical_url = self._canonical_url(path)
        self.driver.get(self._url(path, "before-extension-reload"))
        self._wait_extension()
        old_marker = "stale-instance-marker"
        self.driver.execute_script(
            'document.querySelector("#chrome-codex-switcher-host").dataset.instance = arguments[0]',
            old_marker,
        )

        original = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        self.driver.get("chrome-extension://mfpomnbkkfklealhaacbnmelpgpggglg/sidepanel.html")
        recovered = self.driver.execute_async_script(
            """
            const done = arguments[0];
            (async () => {
              const tabs = await chrome.tabs.query({});
              const tab = tabs.find(item => (item.url || '').includes('/extension-reload-case'));
              if (!tab?.id) throw new Error('fixture_tab_missing');
              await chrome.scripting.executeScript({
                target: {tabId: tab.id},
                func: () => {
                  document.getElementById('chrome-codex-switcher-host')?.remove();
                  window.__chromeCodexSwitcherLoaded = false;
                }
              });
              await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['content.js']});
              done({ok: true});
            })().catch(error => done({ok: false, error: String(error)}));
            """
        )
        self.assertEqual({"ok": True}, recovered)
        self.driver.switch_to.window(original)

        try:
            WebDriverWait(self.driver, 12).until(lambda driver: driver.execute_script(
                """
                const hosts = document.querySelectorAll('#chrome-codex-switcher-host');
                return hosts.length === 1
                  && hosts[0].dataset.instance !== arguments[0]
                  && !!hosts[0].shadowRoot?.querySelector('.note');
                """,
                old_marker,
            ))
        except Exception:
            page_state = self.driver.execute_script(
                """
                const hosts = [...document.querySelectorAll('#chrome-codex-switcher-host')];
                return hosts.map(host => ({instance: host.dataset.instance || '', connected: host.isConnected}));
                """
            )
            with urlopen(f"http://127.0.0.1:{DAEMON_PORT}/api/health", timeout=4) as response:
                health = json.load(response)
            print(
                f"reload diagnostic page={page_state!r} health={health!r}",
                file=sys.stderr,
            )
            raise
        probe = self.driver.execute_script(
            """
            const hosts = document.querySelectorAll('#chrome-codex-switcher-host');
            return {count: hosts.length, note: !!hosts[0]?.shadowRoot?.querySelector('.note')};
            """
        )
        self.assertEqual({"count": 1, "note": True}, probe)

    def test_split_merge_notes_uses_current_value_and_persists_both_surfaces(self) -> None:
        path = "note-mode-case"
        canonical = self._canonical_url(path)
        self.current_canonical_url = canonical
        self.driver.get(self._url(path, "mode"))
        self._wait_extension()
        context = self._context_for_url(canonical)

        self.driver.execute_script(
            """
            const root = document.querySelector('#chrome-codex-switcher-host').shadowRoot;
            const note = root.querySelector('.note');
            note.focus();
            note.value = 'current shared value';
            note.dispatchEvent(new Event('input', {bubbles: true}));
            root.querySelector('.independent').click();
            """
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT note,codex_note,notes_independent FROM contexts WHERE id=?",
                    (context["id"],),
                ).fetchone()
            if row == ("current shared value", "current shared value", 1):
                break
            time.sleep(0.1)
        else:
            self.fail(f"split state did not converge: {row!r}")

        self.driver.execute_script(
            """
            const note = document.querySelector('#chrome-codex-switcher-host').shadowRoot.querySelector('.note');
            note.focus();
            note.value = 'chrome independent';
            note.dispatchEvent(new Event('input', {bubbles: true}));
            """
        )
        self._post("/api/note", {
            "context_id": context["id"],
            "note": "codex independent",
            "surface": "codex",
        })
        time.sleep(0.5)
        with sqlite3.connect(self.db_path) as db:
            split = db.execute(
                "SELECT note,codex_note,notes_independent FROM contexts WHERE id=?",
                (context["id"],),
            ).fetchone()
        self.assertEqual(("chrome independent", "codex independent", 1), split)

        merged = self._post("/api/note-mode", {
            "context_id": context["id"],
            "independent": False,
            "source": "codex",
            "note": "codex independent",
        })["context"]
        self.assertEqual("codex independent", merged["note"])
        self.assertEqual("codex independent", merged["codex_note"])
        self.assertFalse(merged["notes_independent"])

        repeated = self._post("/api/note-mode", {
            "context_id": context["id"],
            "independent": False,
            "source": "codex",
            "note": "codex independent",
        })["context"]
        self.assertEqual(merged["note"], repeated["note"])

    def test_daemon_dashboard_is_not_an_overlay_target(self) -> None:
        self.driver.get(f"http://127.0.0.1:{DAEMON_PORT}/ui/search")
        time.sleep(0.5)
        self.assertIsNone(self.driver.execute_script(
            'return document.querySelector("#chrome-codex-switcher-host")'
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
