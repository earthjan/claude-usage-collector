import os
import unittest

from usage_collector import analyze, store as store_module

from tests import support
from tests.fixtures import make_demo_store

DEMO = os.path.join(support.FIXTURES, "demo-store")


def tree(root: str) -> dict:
    files = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                files[os.path.relpath(path, root)] = handle.read()
    return files


class DemoStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_generator_is_deterministic_and_matches_the_committed_store(self):
        out = self.tmp.join("demo")
        make_demo_store.write(out)
        self.assertEqual(tree(out), tree(DEMO))

    def test_store_covers_two_accounts_over_about_a_month(self):
        rows = store_module.RowReader(DEMO).read()
        self.assertEqual({row["account"] for row in rows}, {"work", "personal"})
        self.assertGreaterEqual(len({row["ts"][:10] for row in rows}), 28)
        self.assertGreaterEqual(len({row["agent_type"] for row in rows}), 5)
        self.assertGreaterEqual(len({row["model"] for row in rows}), 3)

    def test_every_committed_row_passes_the_guard(self):
        reader = store_module.RowReader(DEMO)
        reader.read()
        self.assertEqual(reader.skipped, 0)

    def test_store_covers_every_new_stream_for_both_accounts(self):
        context = store_module.StreamReader(DEMO, "context").read()
        starts = {(r["account"], r["is_subagent"]) for r in context if r["event"] == "start"}
        self.assertEqual(starts, {("work", 0), ("work", 1), ("personal", 0), ("personal", 1)})
        self.assertTrue(any(r["event"] == "compact" for r in context))
        skills = store_module.StreamReader(DEMO, "skills").read()
        self.assertEqual({r["account"] for r in skills}, {"work", "personal"})
        catalog = store_module.StreamReader(DEMO, "catalog").read()
        self.assertEqual({r["kind"] for r in catalog}, {"skill", "agent"})

    def test_every_session_and_subagent_has_one_start_or_fork_event(self):
        usage = store_module.RowReader(DEMO).read()
        threads = {(r["account"], r["session"], r["agent"]) for r in usage}
        context = store_module.StreamReader(DEMO, "context").read()
        starts = [(r["account"], r["session"], r["agent"])
                  for r in context if r["event"] in ("start", "fork")]
        self.assertEqual(sorted(starts), sorted(threads))

    def test_check_files_are_weekly_like_a_default_install(self):
        checks = store_module.read_checks(DEMO)
        self.assertEqual(sorted(checks), [
            (account, week) for account in ("personal", "work")
            for week in ("2026-W14", "2026-W15", "2026-W16", "2026-W17", "2026-W18")])
        self.assertTrue(checks[("work", "2026-W16")])

    def test_fork_subagents_get_a_fork_event_instead_of_a_start(self):
        usage = store_module.RowReader(DEMO).read()
        forks = {(r["account"], r["session"], r["agent"]) for r in usage if r["is_fork"]}
        context = store_module.StreamReader(DEMO, "context").read()
        self.assertTrue(forks)
        self.assertEqual({(r["account"], r["session"], r["agent"])
                          for r in context if r["event"] == "fork"}, forks)

    def test_some_listed_skills_and_agent_types_are_never_used(self):
        streams, _skipped = analyze.load_streams(DEMO)
        found = analyze.summarize(analyze.load(DEMO), analyze.Meter.from_config(None),
                                  streams)["skills_agents"]
        self.assertEqual({e["kind"] for e in found["unused"]}, {"skill", "agent"})

    def test_every_committed_stream_row_passes_the_guard(self):
        for stream in ("context", "skills", "catalog"):
            with self.subTest(stream=stream):
                reader = store_module.StreamReader(DEMO, stream)
                self.assertTrue(reader.read())
                self.assertEqual(reader.skipped, 0)

    def test_refuses_to_replace_a_folder_that_is_not_a_demo_store(self):
        out = self.tmp.join("other")
        os.makedirs(os.path.join(out, "keep-me"))
        with self.assertRaises(SystemExit):
            make_demo_store.write(out, replace=True)
        self.assertTrue(os.path.isdir(os.path.join(out, "keep-me")))


if __name__ == "__main__":
    unittest.main()
