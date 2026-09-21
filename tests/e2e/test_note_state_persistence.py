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
from urllib.request import urlopen

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
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")

        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        service = Service(executable_path=driver_path) if driver_path else Service()
        cls.driver = webdriver.Chrome(service=service, options=options)
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
                return !!host?.shadowRoot?.querySelector(".note");
                """
            )
        )

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
        # The ResizeObserver persists geometry through the normal content-script path.
        time.sleep(0.35)
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
        actual = self._snapshot()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
