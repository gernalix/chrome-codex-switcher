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


if __name__ == "__main__":
    unittest.main()
