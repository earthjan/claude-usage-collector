import json
import os
import stat
import tempfile
import unittest
from datetime import date

from usage_collector.state import State, StateError


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_empty_state(self):
        state = State.load(self.path)
        self.assertEqual(state.offset_for("/a.jsonl"), 0)
        self.assertFalse(state.compact_pending("/a.jsonl"))
        self.assertFalse(state.has_seen("msg_1"))
        self.assertIsNone(state.last_run)

    def test_round_trip_keeps_offsets_seen_ids_and_runs(self):
        state = State.load(self.path)
        state.set_file("/a.jsonl", 120, compact_pending=True)
        state.mark_seen(["msg_1", "msg_2"], date(2026, 3, 1))
        state.record_run("2026-03-01T18:00:00Z", 7)
        state.record_run("2026-03-02T18:00:00Z", 3)
        state.save(self.path)

        loaded = State.load(self.path)
        self.assertEqual(loaded.offset_for("/a.jsonl"), 120)
        self.assertTrue(loaded.compact_pending("/a.jsonl"))
        self.assertTrue(loaded.has_seen("msg_2"))
        self.assertEqual(loaded.last_run, "2026-03-02T18:00:00Z")
        self.assertEqual(loaded.last_rows, 3)
        self.assertEqual(loaded.total_rows, 10)
        self.assertEqual(loaded.files_tracked, 1)

    def test_saved_file_is_private(self):
        State.load(self.path).save(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_prune_drops_ids_older_than_60_days(self):
        state = State.load(self.path)
        state.mark_seen(["old"], date(2026, 1, 1))
        state.mark_seen(["edge"], date(2026, 1, 2))
        state.prune(date(2026, 3, 3))
        self.assertFalse(state.has_seen("old"))
        self.assertTrue(state.has_seen("edge"))

    def test_crosscheck_month_round_trips(self):
        state = State.load(self.path)
        self.assertIsNone(state.crosscheck_month)
        state.crosscheck_month = "2026-02"
        state.save(self.path)
        self.assertEqual(State.load(self.path).crosscheck_month, "2026-02")

    def test_crosscheck_week_round_trips_beside_the_month(self):
        state = State.load(self.path)
        self.assertIsNone(state.crosscheck_week)
        state.crosscheck_month = "2026-02"
        state.crosscheck_week = "2026-W09"
        state.save(self.path)
        loaded = State.load(self.path)
        self.assertEqual((loaded.crosscheck_month, loaded.crosscheck_week), ("2026-02", "2026-W09"))

    def test_stream_offsets_round_trip_beside_the_usage_offset(self):
        state = State.load(self.path)
        state.set_file("/a.jsonl", 120, False, {"context": 80, "skills": 120})
        state.save(self.path)
        loaded = State.load(self.path)
        self.assertEqual(loaded.offset_for("/a.jsonl"), 120)
        self.assertEqual(loaded.stream_offset("/a.jsonl", "context"), 80)
        self.assertEqual(loaded.stream_offset("/a.jsonl", "skills"), 120)
        self.assertEqual(loaded.stream_offset("/a.jsonl", "catalog"), 0)

    def test_v1_state_file_reads_with_stream_offsets_at_zero(self):
        with open(self.path, "w") as handle:
            json.dump({"files": {"/a.jsonl": {"offset": 500, "compact": True}},
                       "seen": {"msg_1": "2026-03-01"}, "last_run": None,
                       "last_rows": 0, "total_rows": 9}, handle)
        state = State.load(self.path)
        self.assertEqual(state.offset_for("/a.jsonl"), 500)
        self.assertTrue(state.compact_pending("/a.jsonl"))
        self.assertEqual(state.stream_offset("/a.jsonl", "context"), 0)
        self.assertFalse(state.has_mark("catalog", "2026-03-01|skill|tdd"))

    def test_marks_round_trip_per_kind_and_are_pruned_after_60_days(self):
        state = State.load(self.path)
        state.add_marks("skill", ["old"], date(2026, 1, 1))
        state.add_marks("skill", ["edge"], date(2026, 1, 2))
        state.add_marks("catalog", ["edge"], date(2026, 1, 2))
        state.save(self.path)
        loaded = State.load(self.path)
        loaded.prune(date(2026, 3, 3))
        self.assertFalse(loaded.has_mark("skill", "old"))
        self.assertTrue(loaded.has_mark("skill", "edge"))
        self.assertTrue(loaded.has_mark("catalog", "edge"))
        self.assertFalse(loaded.has_mark("stream", "edge"))

    def test_corrupt_file_raises(self):
        with open(self.path, "w") as handle:
            handle.write("not json")
        with self.assertRaises(StateError):
            State.load(self.path)


if __name__ == "__main__":
    unittest.main()
