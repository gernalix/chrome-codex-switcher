import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class InstallContractTests(unittest.TestCase):
    def test_install_restarts_and_health_checks_deployed_daemon(self):
        script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(
            encoding="utf-8"
        )

        restart = "systemctl --user restart chrome-codex-switcher.service"
        health = "http://127.0.0.1:43817/api/health"

        self.assertIn(restart, script)
        self.assertIn(health, script)
        self.assertLess(script.index('cp -a "$ROOT/host"'), script.index(restart))

    def test_install_configures_native_gnome_shortcut_and_uninstall_removes_it(self):
        root = Path(__file__).resolve().parents[1]
        install = (root / "install.sh").read_text(encoding="utf-8")
        uninstall = (root / "uninstall.sh").read_text(encoding="utf-8")
        shortcut = (root / "contrib" / "configure-global-search-shortcut.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('configure-global-search-shortcut.sh', install)
        self.assertIn('configure-global-search-shortcut.sh" uninstall', uninstall)
        self.assertIn('org.gnome.settings-daemon.plugins.media-keys', shortcut)
        self.assertIn("'<Alt><Shift>s'", shortcut)
        self.assertIn('google-chrome-stable', shortcut)
        self.assertIn('http://127.0.0.1:43817/ui/search', shortcut)
        self.assertNotIn('flatpak', shortcut.lower())

    def test_gnome_update_migrates_to_cache_safe_companion(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / "contrib" / "install-gnome-overlay.sh").read_text(encoding="utf-8")
        metadata = (root / "contrib" / "gnome-extension@gernalix.github.com" / "metadata.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('UUID="chrome-codex-switcher-v2@gernalix.github.com"', installer)
        self.assertIn('LEGACY_UUID="chrome-codex-switcher@gernalix.github.com"', installer)
        self.assertIn('gnome-extensions disable "$LEGACY_UUID"', installer)
        self.assertIn('gnome-extensions enable "$LEGACY_UUID"', installer)
        self.assertLess(
            installer.index('grep -Fxq "$UUID"'),
            installer.index('gnome-extensions disable "$LEGACY_UUID"'),
        )
        self.assertIn('"uuid": "chrome-codex-switcher-v2@gernalix.github.com"', metadata)

    def test_workflowy_launch_is_codex_desktop_first(self):
        root = Path(__file__).resolve().parents[1]
        background = (root / "extension" / "background.js").read_text(encoding="utf-8")
        content = (root / "extension" / "content.js").read_text(encoding="utf-8")

        start = background.index("async function launchPrompt")
        end = background.index("\nasync function findContextTab", start)
        launch = background[start:end]
        self.assertIn("/api/prompt/launch-codex", launch)
        self.assertNotIn("focusPrompt(", launch)
        self.assertNotIn("chrome.tabs.create", launch)

        action = 'else if (action === "launch")'
        start = content.index(action)
        end = content.index('else if (action === "bind"', start)
        launch_click = content[start:end]
        self.assertIn('type: "prompt:launch"', launch_click)
        self.assertNotIn("copyPrompt(", launch_click)

    def test_uninstall_removes_only_fixture_program_files_and_keeps_state(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            prefix = home / ".local" / "share" / "chrome-codex-switcher"
            fixture_bin = Path(tmp) / "bin"
            (prefix / "contrib").mkdir(parents=True)
            (home / ".config" / "systemd" / "user").mkdir(parents=True)
            (home / ".local" / "bin").mkdir(parents=True)
            state = home / ".local" / "state" / "chrome-codex-switcher"
            state.mkdir(parents=True)
            sentinel = state / "state.sqlite3"
            sentinel.write_text("fixture-state", encoding="utf-8")

            shutil.copy2(
                root / "contrib" / "configure-global-search-shortcut.sh",
                prefix / "contrib" / "configure-global-search-shortcut.sh",
            )
            (home / ".config" / "systemd" / "user" / "chrome-codex-switcher.service").write_text(
                "fixture", encoding="utf-8"
            )
            (home / ".local" / "bin" / "context-twin").write_text(
                "fixture", encoding="utf-8"
            )

            fixture_bin.mkdir()
            gsettings = fixture_bin / "gsettings"
            gsettings.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = get ]; then "
                "echo \"['/fixture/', '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/chrome-codex-switcher/']\"; fi\n",
                encoding="utf-8",
            )
            systemctl = fixture_bin / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gsettings.chmod(0o755)
            systemctl.chmod(0o755)

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fixture_bin}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", str(root / "uninstall.sh")],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertFalse(prefix.exists())
            self.assertFalse((home / ".local" / "bin" / "context-twin").exists())
            self.assertFalse(
                (home / ".config" / "systemd" / "user" / "chrome-codex-switcher.service").exists()
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "fixture-state")
            self.assertIn("Persistent state", result.stdout)



if __name__ == "__main__":
    unittest.main()
