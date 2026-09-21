from pathlib import Path
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



if __name__ == "__main__":
    unittest.main()
