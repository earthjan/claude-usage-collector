import os
import unittest

from usage_collector import store as store_module

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

    def test_refuses_to_replace_a_folder_that_is_not_a_demo_store(self):
        out = self.tmp.join("other")
        os.makedirs(os.path.join(out, "keep-me"))
        with self.assertRaises(SystemExit):
            make_demo_store.write(out, replace=True)
        self.assertTrue(os.path.isdir(os.path.join(out, "keep-me")))


if __name__ == "__main__":
    unittest.main()
