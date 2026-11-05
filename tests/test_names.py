import unittest

from usage_collector.hashing import hash_value
from usage_collector.names import NamePolicy

SALT = bytes(range(32))


def h(value):
    return hash_value(SALT, value)


class NamePolicyTest(unittest.TestCase):
    def test_slugs_are_kept_and_other_names_hashed(self):
        policy = NamePolicy(SALT, hash_all=False)
        self.assertEqual(policy.name("Explore"), "Explore")
        self.assertEqual(policy.name("plugin-x:review"), "plugin-x:review")
        self.assertEqual(policy.name("my private skill"), h("my private skill"))

    def test_hash_all_hashes_slugs_too(self):
        policy = NamePolicy(SALT, hash_all=True)
        self.assertEqual(policy.name("Explore"), h("Explore"))

    def test_missing_or_empty_names_give_an_empty_string(self):
        policy = NamePolicy(SALT, hash_all=False)
        for value in (None, "", 7, ["Explore"]):
            with self.subTest(value=value):
                self.assertEqual(policy.name(value), "")

    def test_tool_names_keep_the_mcp_marker_and_hash_the_rest(self):
        kept = NamePolicy(SALT, hash_all=False)
        hashed = NamePolicy(SALT, hash_all=True)
        self.assertEqual(kept.tool("Bash"), "Bash")
        self.assertEqual(hashed.tool("Bash"), h("Bash"))
        for policy in (kept, hashed):
            self.assertEqual(policy.tool("mcp__server__lookup"),
                             "mcp:" + h("mcp__server__lookup"))


if __name__ == "__main__":
    unittest.main()
