import os
import unittest

from usage_collector import store as store_module
from usage_collector.sinks import FolderSink

from tests import support
from tests.test_guard import catalog_row, context_row, skill_row


class StreamReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.path
        FolderSink(self.store, "work").write_streams({
            "context": [context_row(ts="2026-04-01T10:00:00Z"),
                        context_row(ts="2026-04-03T10:00:00Z", event="compact")],
            "skills": [skill_row(ts="2026-04-02T10:00:00Z")],
            "catalog": [catalog_row(date="2026-04-02")],
        })
        FolderSink(self.store, "personal").write_streams({
            "context": [context_row(ts="2026-04-02T09:00:00Z", account="personal")],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, stream, **kwargs):
        reader = store_module.StreamReader(self.store, stream, **kwargs)
        return reader.read(), reader.skipped

    def test_context_rows_come_back_typed_and_sorted(self):
        rows, skipped = self.read("context")
        self.assertEqual(skipped, 0)
        self.assertEqual([r["ts"][:10] for r in rows],
                         ["2026-04-01", "2026-04-02", "2026-04-03"])
        self.assertEqual(rows[0]["first_turn_context"], 1210)
        self.assertEqual(rows[0]["system_prompt_chars"], 1500)
        self.assertEqual(rows[0]["is_subagent"], 0)
        self.assertEqual(rows[2]["event"], "compact")

    def test_skill_and_catalog_rows_are_read_from_their_own_files(self):
        skills, _ = self.read("skills")
        catalog, _ = self.read("catalog")
        self.assertEqual([r["skill"] for r in skills], ["plugin-x:review"])
        self.assertEqual([(r["date"], r["kind"]) for r in catalog], [("2026-04-02", "skill")])

    def test_since_and_account_filter_every_stream(self):
        rows, _ = self.read("context", since="2026-04-02", account="work")
        self.assertEqual([r["ts"][:10] for r in rows], ["2026-04-03"])
        catalog, _ = self.read("catalog", since="2026-04-03")
        self.assertEqual(catalog, [])

    def test_rows_failing_the_guard_are_skipped_and_counted(self):
        path = os.path.join(self.store, "work", "2026", "04", "2026-04-01.context.csv")
        with open(path, "a") as handle:
            handle.write("CANARY_INSTRUCTION_7f3\n")
        rows, skipped = self.read("context")
        self.assertEqual((len(rows), skipped), (3, 1))

    def test_usage_files_are_not_read_as_a_stream(self):
        FolderSink(self.store, "work").write_rows([])
        rows, skipped = self.read("skills")
        self.assertEqual((len(rows), skipped), (1, 0))

    def test_store_without_stream_files_reads_nothing(self):
        empty = support.TempDir()
        try:
            reader = store_module.StreamReader(empty.path, "context")
            self.assertEqual(reader.read(), [])
        finally:
            empty.cleanup()


if __name__ == "__main__":
    unittest.main()
