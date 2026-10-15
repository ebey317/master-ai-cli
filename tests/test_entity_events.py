import json
import os
import shutil
import tempfile
import unittest

from scripts.entity_events import EntityEventBus


class TestEntityEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bus(self):
        return EntityEventBus("sess1", journal_dir=self.tmp)

    def test_flat_fields_and_seq(self):
        bus = self._bus()
        m = bus.emit("turn.start", {"state": "running"}, turn_id="t1")
        self.assertEqual(m["seq"], 1)
        self.assertEqual(m["type"], "turn.start")
        self.assertEqual(m["turn_id"], "t1")
        self.assertIn("session_id", m)
        for f in ("id", "v", "ts", "seq", "type"):
            self.assertIn(f, m)

    def test_append_only_history_replay(self):
        bus = self._bus()
        for i in range(3):
            bus.tool_call({"tool": "ls", "turn_id": "t1",
                           "tool_call_id": f"c{i}"})
        h = bus.history(after_seq=0)
        self.assertEqual(len(h["messages"]), 3)
        self.assertFalse(h["has_more"])
        # cold-fold replay for a late subscriber
        bus2 = EntityEventBus("sess1", journal_dir=self.tmp)
        h2 = bus2.history(after_seq=1)
        self.assertEqual(h2["messages"][0]["seq"], 2)

    def test_history_pages_by_turn(self):
        bus = self._bus()
        for t in ("t1", "t2"):
            for _ in range(5):
                bus.delta({"kind": "assistant", "text": "x" * 90000,
                           "turn_id": t})
        h = bus.history(after_seq=0)
        self.assertTrue(h["has_more"])
        turn_ids = {m["turn_id"] for m in h["messages"]}
        self.assertEqual(turn_ids, {"t1"})
        h2 = bus.history(after_seq=h["last_seq"])
        self.assertEqual({m["turn_id"] for m in h2["messages"]}, {"t2"})
        self.assertFalse(h2["has_more"])

    def test_empty_deltas_filtered(self):
        bus = self._bus()
        bus.delta({"kind": "thinking", "text": "   ", "turn_id": "t1"})
        self.assertEqual(len(bus.history()["messages"]), 0)

    def test_bad_subscriber_does_not_break_journal(self):
        bus = self._bus()
        bus.subscribe(lambda *a: 1 / 0)
        m = bus.emit("turn.start", {}, turn_id="t1")
        self.assertEqual(len(bus.history()["messages"]), 1)

    def test_seq_survives_reload(self):
        bus = self._bus()
        bus.emit("turn.start", {}, turn_id="t1")
        bus2 = EntityEventBus("sess1", journal_dir=self.tmp)
        m = bus2.emit("turn.start", {}, turn_id="t2")
        self.assertEqual(m["seq"], 2)


if __name__ == "__main__":
    unittest.main()
