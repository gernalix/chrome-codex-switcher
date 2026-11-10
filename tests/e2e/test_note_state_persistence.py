from __future__ import annotations

import json
import os
import sqlite3
import shutil
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
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchShadowRootException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = ROOT / "extension"
DAEMON_PORT = int(os.environ.get("CCS_E2E_PORT", "43817"))


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
        cls.daemon_env = env
        cls.daemon = cls._start_daemon()
        cls._wait_daemon()

        options = Options()
        options.add_experimental_option("enableExtensionTargets", True)
        if os.environ.get("CCS_E2E_HEADLESS") == "1":
            options.add_argument("--headless=new")
        chrome_binary = os.environ.get("CHROME_BINARY")
        if chrome_binary:
            options.binary_location = chrome_binary
        extension_dir = EXTENSION_DIR
        if DAEMON_PORT != 43817:
            # Isolate local acceptance from the always-on user's bridge.
            extension_dir = cls.tmp_path / "extension"
            shutil.copytree(EXTENSION_DIR, extension_dir)
            for path in extension_dir.iterdir():
                if path.suffix in {".js", ".json"}:
                    path.write_text(path.read_text().replace(":43817", f":{DAEMON_PORT}"))
        extension = str(extension_dir.resolve())
        options.add_argument(f"--disable-extensions-except={extension}")
        options.add_argument(f"--load-extension={extension}")
        options.add_argument(f"--user-data-dir={cls.tmp_path / 'chrome-profile'}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        # Xvfb alone is insufficient on a Wayland desktop: Chrome can otherwise
        # connect to the inherited native compositor and steal the user's focus.
        options.add_argument("--ozone-platform=x11")
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
    def _start_daemon(cls) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "chrome_codex_switcher.daemon"],
            cwd=ROOT,
            env=cls.daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @classmethod
    def _stop_daemon(cls) -> None:
        cls.daemon.terminate()
        try:
            cls.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.daemon.kill()
            cls.daemon.wait()
        if cls.daemon.stdout:
            cls.daemon.stdout.close()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "driver"):
            cls.driver.quit()
        if hasattr(cls, "daemon"):
            cls._stop_daemon()
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

    def _overlay_element(self, selector: str):
        host = self.driver.find_element("id", "chrome-codex-switcher-host")
        return host.shadow_root.find_element("css selector", selector)

    def _overlay_script(self, script: str, *args):
        # WebDriver can inspect closed roots without changing the page or extension.
        return self.driver.execute_script(
            f"const overlay = arguments[{len(args)}].getRootNode();\n" + script,
            *args, self._overlay_element(".note"),
        )

    def _open_note(self):
        original = self.driver.current_window_handle
        handles = set(self.driver.window_handles)
        self.wait.until(lambda _: self._overlay_element(".note").is_enabled())
        ActionChains(self.driver).click(self._overlay_element(".note")).perform()
        def opened(driver):
            message = self._overlay_element(".flash").get_attribute("textContent")
            self.assertFalse(message.startswith("Impossibile aprire"), message)
            return next(iter(set(driver.window_handles) - handles), None)

        editor = self.wait.until(opened)
        self.driver.switch_to.window(editor)
        self.assertTrue(self.driver.current_url.startswith("chrome-extension://"))
        note = self.wait.until(lambda driver: driver.find_element("id", "note"))
        self.wait.until(lambda _: note.is_enabled())
        return original, note

    def _set_note(self, text: str) -> None:
        original, note = self._open_note()
        note.clear()
        note.send_keys(text)
        self.driver.find_element("css selector", "form button").click()
        self.wait.until(lambda driver: driver.find_element("css selector", "[role=status]").text == "Salvata")
        self.driver.close()
        self.driver.switch_to.window(original)

    def _wait_extension(self, *, hydrated: bool = True) -> None:
        WebDriverWait(self.driver, 15, ignored_exceptions=(NoSuchShadowRootException,)).until(
            lambda _: self._overlay_element(".note")
        )
        self.assertIsNone(self.driver.execute_script(
            'return document.getElementById("chrome-codex-switcher-host").shadowRoot'
        ))
        if hydrated:
            self.wait.until(lambda _: self._overlay_element(".note").is_enabled())
            self._context_for_url(self.driver.current_url.split("#", 1)[0])

    def _api(self, path: str, payload: dict | None = None) -> dict:
        headers = {"X-CCS-Request": "1"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"http://127.0.0.1:{DAEMON_PORT}{path}", data=data, headers=headers)
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
        return self._overlay_script(
            """
            const host = overlay.host;
            const root = overlay;
            const box = root.querySelector(".box");
            return {
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
            "collapsed": False,
            "hidden": True,
        }
        self._set_note(note_text)
        self._overlay_script(
            """
            const host = overlay.host;
            const box = overlay.querySelector(".box");
            host.style.left = arguments[0] + "px";
            host.style.top = arguments[1] + "px";
            // The browser's resize control changes the box. Observe the same
            // element and wait for its persisted geometry before hiding it.
            box.style.width = arguments[2] + "px";
            box.style.height = arguments[3] + "px";
            """,
            expected["x"], expected["y"], expected["width"], expected["height"],
        )
        self._wait_db_state({**expected, "hidden": False})
        self._overlay_element(".close").click()
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
            actual = {**self._snapshot(), "note": self._context_for_url(self.current_canonical_url)["note"]}
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
                  && hosts[0].isConnected;
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
            health = self._api("/api/health")
            print(
                f"reload diagnostic page={page_state!r} health={health!r}",
                file=sys.stderr,
            )
            raise
        probe = self._overlay_script(
            """
            const hosts = document.querySelectorAll('#chrome-codex-switcher-host');
            return {count: hosts.length, note: !!overlay.querySelector('.note')};
            """
        )
        self.assertEqual({"count": 1, "note": True}, probe)

    def test_delayed_daemon_start_rehydrates_open_tab_without_page_refresh(self) -> None:
        path = "delayed-start-case"
        canonical = self._canonical_url(path)
        self.driver.get(self._url(path, "seed"))
        self._wait_extension()
        context = self._context_for_url(canonical)
        self._api("/api/note", {"context_id": context["id"], "note": "delayed startup note", "surface": "chrome"})
        self._stop_daemon()

        self.driver.refresh()
        self._wait_extension(hydrated=False)
        self.assertEqual("BUTTON", self._overlay_element(".note").tag_name.upper())

        type(self).daemon = type(self)._start_daemon()
        type(self)._wait_daemon()
        self.wait.until(lambda _: self._overlay_script("return !!overlay.host.dataset.overlayUi"))
        original, note = self._open_note()
        self.assertEqual("delayed startup note", note.get_attribute("value"))
        self.driver.close()
        self.driver.switch_to.window(original)

    def test_daemon_restart_reconnects_without_overwriting_focused_edit(self) -> None:
        path = "daemon-restart-case"
        canonical = self._canonical_url(path)
        self.driver.get(self._url(path, "before-restart"))
        self._wait_extension()
        context = self._context_for_url(canonical)
        original, note = self._open_note()
        note.send_keys("local focused edit")
        self._stop_daemon()
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE contexts SET note=?, codex_note=? WHERE id=?", ("server after restart", "server after restart", context["id"]))
        type(self).daemon = type(self)._start_daemon()
        type(self)._wait_daemon()

        self.assertEqual("local focused edit", note.get_attribute("value"))
        self.driver.find_element("css selector", "form button").click()
        self.wait.until(lambda driver: driver.find_element("css selector", "[role=status]").text == "Salvata")
        self.assertEqual("local focused edit", self._context_for_url(canonical)["note"])
        self.driver.close()
        self.driver.switch_to.window(original)

    def test_split_merge_notes_uses_current_value_and_persists_both_surfaces(self) -> None:
        path = "note-mode-case"
        canonical = self._canonical_url(path)
        self.current_canonical_url = canonical
        self.driver.get(self._url(path, "mode"))
        self._wait_extension()
        context = self._context_for_url(canonical)

        self._set_note("current shared value")
        # Separate mode remains an API/side-panel setting; the overlay no longer
        # contains a checkbox that could merge notes accidentally.
        self._api("/api/note-mode", {
            "context_id": context["id"], "independent": True,
            "source": "chrome", "note": "current shared value",
        })
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

        self._set_note("chrome independent")
        self._api("/api/note", {
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

        merged = self._api("/api/note-mode", {
            "context_id": context["id"],
            "independent": False,
            "source": "codex",
            "note": "codex independent",
        })["context"]
        self.assertEqual("codex independent", merged["note"])
        self.assertEqual("codex independent", merged["codex_note"])
        self.assertFalse(merged["notes_independent"])

        repeated = self._api("/api/note-mode", {
            "context_id": context["id"],
            "independent": False,
            "source": "codex",
            "note": "codex independent",
        })["context"]
        self.assertEqual(merged["note"], repeated["note"])

    def test_overlay_minimal_layout_and_pairing_controls(self) -> None:
        self.driver.get(self._url("minimal-fixture", "layout"))
        self._wait_extension()
        self.driver.execute_script("document.getElementById('chrome-codex-switcher-host').remove()")
        for name in ("tests/e2e/overlay_fixture.js", "extension/content.js"):
            self.driver.execute_script((ROOT / name).read_text())
        def layout():
            return self._overlay_script("""
                const host=overlay.host,r=overlay;
                const h=r.querySelector('.head'),n=r.querySelector('.note');
                const buttons=[...h.querySelectorAll('button')].filter(b=>!b.hidden);
                return {controls:buttons.map(b=>b.textContent),
                  allInHeader:buttons.every(b=>b.parentElement===h),
                  accessible:buttons.every(b=>b.title&&b.getAttribute('aria-label')&&b.tabIndex===0&&b.offsetWidth>=28&&b.offsetHeight>=28),
                  forbidden:!!r.querySelector('.pn-info,.title,.status,.hint,.grip,.collapse,.independent,.actions,a,details'),
                  extraText:[...r.querySelector('.box').childNodes].filter(n=>n.nodeType===3).some(n=>n.textContent.trim()),
                  headerText:h.innerText.trim().replace(/\\s+/g,' '),
                  emptySpace:buttons[0].getBoundingClientRect().left-h.getBoundingClientRect().left,
                  placeholder:n.getAttribute('placeholder'),note:n.textContent,visible:host.style.display!=='none',
                  bottom:host.getBoundingClientRect().bottom,viewport:innerHeight,
                  editorVisible:n.getBoundingClientRect().height>=58};
            """)
        self.wait.until(lambda _: layout()["note"] == "Apri nota privata")
        state=layout()
        self.assertEqual(["❓","❌"], state["controls"])
        for key in ("allInHeader","accessible","editorVisible"):
            self.assertTrue(state[key], state)
        self.assertFalse(state["forbidden"])
        self.assertFalse(state["extraText"])
        self.assertIsNone(state["placeholder"])
        self.assertGreater(state["emptySpace"], 100)
        self.assertEqual("❓ ❌",state["headerText"])
        # Keyboard activation uses the same pairing action.
        self._overlay_script("overlay.querySelector('.link').focus()")
        ActionChains(self.driver).send_keys(Keys.ENTER).perform()
        self.assertTrue(self.driver.execute_script("return fixtureMessages.some(m=>m.type==='context:arm')"))
        self.driver.execute_script("""
            fixtureContext.twin={codex_thread:'fixture-thread'};
            fixtureContext.collapsed=true;
            fixtureContext.pn_card.what='Descrizione non visibile. '.repeat(300);
            fixtureRefresh();
        """)
        self.wait.until(lambda _: layout()["controls"] == ["🔗","🗑","❌"])
        self.assertTrue(layout()["editorVisible"])
        self.assertEqual("🔗 🗑 ❌",layout()["headerText"])
        self.assertFalse(layout()["forbidden"])
        self._overlay_element(".switch").click()
        self.assertTrue(self.driver.execute_script("return fixtureMessages.some(m=>m.type==='context:switch')"))
        self.driver.execute_script("fixtureFailUnlink=true")
        self._overlay_element(".unlink").click()
        self.assertEqual(["🔗","🗑","❌"], layout()["controls"])
        self.driver.execute_script("fixtureFailUnlink=false")
        self._overlay_element(".unlink").click()
        self.wait.until(lambda _: layout()["controls"] == ["❓","❌"])
        self.assertEqual("Apri nota privata", layout()["note"])
        self.assertEqual(["Codex text",True],self.driver.execute_script("return [fixtureContext.codex_note,fixtureContext.notes_independent]"))
        self.assertTrue(self.driver.execute_script("return fixtureMessages.some(m=>m.type==='context:read')"))
        # The blank part of the header still handles real pointer dragging.
        head=self._overlay_element(".head")
        before=self.driver.execute_script("return document.getElementById('chrome-codex-switcher-host').offsetLeft")
        ActionChains(self.driver).move_to_element_with_offset(head,-head.size['width']/2+10,0).click_and_hold().move_by_offset(35,20).release().perform()
        self.wait.until(lambda _: self.driver.execute_script("return document.getElementById('chrome-codex-switcher-host').offsetLeft") > before)
        self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",{"width":320,"height":220,"deviceScaleFactor":1,"mobile":False})
        self.wait.until(lambda _: layout()["bottom"]<=layout()["viewport"])
        self.assertTrue(layout()["editorVisible"])
        self.driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride",{})
        self._overlay_element(".note").click()
        self.assertTrue(self.driver.execute_script(
            "return fixtureMessages.some(m=>m.type==='context:open-note'&&m.contextId==='overlay-fixture')"
        ))

    def test_automation_pause_preserves_normal_overlay_input_and_saved_state(self) -> None:
        self.driver.get(self._url("automation-pause", "overlay"))
        self._wait_extension()
        self.driver.execute_script("document.getElementById('chrome-codex-switcher-host').remove()")
        for name in ("tests/e2e/overlay_fixture.js", "extension/content.js"):
            self.driver.execute_script((ROOT / name).read_text())
        self.wait.until(lambda _: self._overlay_element(".note").is_displayed())
        before=self._snapshot()
        self._overlay_script("""
            const r=overlay.querySelector('.note').getBoundingClientRect();
            const b=document.createElement('button'); b.id='covered-page-button';
            b.textContent='Page clicks: 0';
            b.style.cssText=`position:fixed;left:${r.x+10}px;top:${r.y+10}px;width:140px;height:28px`;
            b.onclick=()=>b.textContent='Page clicks: '+(Number(b.textContent.split(': ')[1])+1);
            document.body.append(b);
        """)
        button=self.driver.find_element('id', 'covered-page-button')
        with self.assertRaises(ElementClickInterceptedException):
            button.click()

        def shortcut():
            ActionChains(self.driver).key_down(Keys.ALT).key_down(Keys.SHIFT).send_keys('h').key_up(Keys.SHIFT).key_up(Keys.ALT).perform()

        shortcut()
        self.assertEqual('hidden', self.driver.execute_script("return getComputedStyle(document.getElementById('chrome-codex-switcher-host')).visibility"))
        button.click()  # Real hit-tested input, without force or synthetic click.
        self.assertEqual('Page clicks: 1', button.text)
        self.assertEqual(before, self._snapshot())
        self.assertFalse(self.driver.execute_script('return fixtureContext.hidden || false'))
        # Background refresh must not reintroduce the blocker during automation.
        self.driver.execute_script('fixtureRefresh()')
        button.click()
        self.assertEqual('Page clicks: 2', button.text)
        shortcut()
        self.assertEqual('visible', self.driver.execute_script("return getComputedStyle(document.getElementById('chrome-codex-switcher-host')).visibility"))
        self.assertEqual(before, self._snapshot())
        close=self._overlay_element(".close")
        close.click()
        self.assertTrue(self._snapshot()['hidden'])
        # A pre-existing saved hidden preference must survive pause/resume too.
        shortcut()
        shortcut()
        self.assertTrue(self._snapshot()['hidden'])

    def test_legacy_collapsed_preference_does_not_hide_the_note(self) -> None:
        self.driver.get(self._url("legacy-collapse", "seed"))
        self._wait_extension()
        context=self._context_for_url(self._canonical_url("legacy-collapse"))
        self._api("/api/ui",{"context_id":context["id"],"collapsed":True})
        self.driver.refresh()
        self._wait_extension()
        state=self._overlay_script("""
            const r=overlay;
            return {height:r.querySelector('.note').getBoundingClientRect().height,button:!!r.querySelector('.collapse')};
        """)
        self.assertGreaterEqual(state['height'],58)
        self.assertFalse(state['button'])
        self.assertTrue(self._context_for_url(self._canonical_url("legacy-collapse"))['collapsed'])

    def test_overlay_unlink_preserves_actual_separate_notes(self) -> None:
        self.driver.get(self._url("unlink-real", "pair"))
        self._wait_extension()
        context = self._context_for_url(self._canonical_url("unlink-real"))
        self._overlay_element(".link").click()
        # Wait for the click to arm pairing before delivering the clipboard link.
        self.wait.until(lambda _: "conferma" in self._overlay_element(".flash").text)
        thread = "11111111-2222-4333-8444-555555555555"
        self._api("/api/clipboard", {"text": f"codex://threads/{thread}"})
        proposal = self._api("/api/link/proposal")["proposal"]
        self.assertEqual(thread, proposal["thread"])
        self.assertFalse(self._overlay_element(".unlink").is_displayed())
        original = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        self.driver.get("chrome-extension://mfpomnbkkfklealhaacbnmelpgpggglg/sidepanel.html")
        self.wait.until(lambda driver: driver.find_element("css selector", "header section button")).click()
        self.wait.until(lambda _: self._api("/api/link/proposal")["proposal"] is None)
        self.driver.close()
        self.driver.switch_to.window(original)
        self.wait.until(lambda _: self._overlay_element(".unlink").is_displayed())
        self._api("/api/note-mode", {"context_id":context["id"],"independent":True,"source":"chrome","note":"Chrome survives"})
        self._api("/api/note", {"context_id":context["id"],"surface":"codex","note":"Codex survives"})
        self.driver.refresh()
        self._wait_extension()
        self._overlay_element(".unlink").click()
        self.wait.until(lambda _: self._overlay_element(".link").is_displayed())
        after = self._context_for_url(self._canonical_url("unlink-real"))
        self.assertEqual(("Chrome survives","Codex survives",1),(after["note"],after["codex_note"],after["notes_independent"]))
        self.driver.refresh()
        self._wait_extension()
        self.assertFalse(self._overlay_element(".unlink").is_displayed())

    def test_daemon_dashboard_is_not_an_overlay_target(self) -> None:
        self.driver.get(f"http://127.0.0.1:{DAEMON_PORT}/ui/search")
        time.sleep(0.5)
        self.assertIsNone(self.driver.execute_script(
            'return document.querySelector("#chrome-codex-switcher-host")'
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
