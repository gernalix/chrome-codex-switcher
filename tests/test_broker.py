import unittest
from chrome_codex_switcher.broker import EventBroker


class BrokerTests(unittest.TestCase):
    def test_optional_pn_change_hook_does_not_block_event_transport(self):
        types = []
        broker = EventBroker(on_event=types.append)
        result = broker.emit("note_changed", {"context_id": "ctx"})
        self.assertEqual(types, ["note_changed"])
        self.assertEqual(result["type"], "note_changed")
        self.assertEqual(broker.wait_after(0, 0)[1][0], result)

    def test_cursor_resets_after_daemon_restart(self):
        broker = EventBroker()
        broker.emit("focus_chrome", {"context_id": "x"})
        seq, events = broker.wait_after(999, 0)
        self.assertEqual(seq, 1)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
