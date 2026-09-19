import unittest
from chrome_codex_switcher.broker import EventBroker


class BrokerTests(unittest.TestCase):
    def test_cursor_resets_after_daemon_restart(self):
        broker = EventBroker()
        broker.emit("focus_chrome", {"context_id": "x"})
        seq, events = broker.wait_after(999, 0)
        self.assertEqual(seq, 1)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
