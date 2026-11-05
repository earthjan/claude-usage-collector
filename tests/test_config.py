import json
import os
import stat
import tempfile
import unittest

from usage_collector import config


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "cfg", "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as handle:
            json.dump(data, handle)


class ResolvePathTest(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(
            config.resolve_config_path("/x/c.json", {"CLAUDE_USAGE_CONFIG": "/y"}),
            "/x/c.json",
        )

    def test_env_used_when_no_explicit_path(self):
        self.assertEqual(
            config.resolve_config_path(None, {"CLAUDE_USAGE_CONFIG": "/y/c.json"}),
            "/y/c.json",
        )

    def test_default_under_dot_config(self):
        path = config.resolve_config_path(None, {})
        self.assertTrue(
            path.endswith("/.config/claude-usage-collector/config.json")
        )


class InitTest(ConfigTestCase):
    def test_init_writes_config_and_private_salt(self):
        config.init_config(self.path, account="work")
        loaded = config.load_config(self.path)
        self.assertEqual(loaded["account"], "work")
        self.assertEqual(loaded["destination"], {"type": "folder", "path": "~/usage-data"})
        self.assertEqual(loaded["schedule"], "18:00")
        salt = config.salt_path(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(salt).st_mode), 0o600)

    def test_init_git_destination_gets_git_defaults(self):
        config.init_config(
            self.path, account="personal", dest_type="git",
            dest="~/data", schedule="07:30",
        )
        loaded = config.load_config(self.path)
        self.assertEqual(loaded["destination"], {
            "type": "git", "path": "~/data", "remote": "origin",
            "branch": "main", "push": True,
        })
        self.assertEqual(loaded["schedule"], "07:30")

    def test_init_writes_hash_names_not_the_old_alias(self):
        config.init_config(self.path, account="work")
        with open(self.path) as handle:
            written = json.load(handle)
        self.assertIs(written["hash_names"], True)
        self.assertNotIn("hash_agent_types", written)

    def test_init_refuses_to_overwrite_without_force(self):
        config.init_config(self.path, account="work")
        with self.assertRaises(config.ConfigError):
            config.init_config(self.path, account="personal")
        self.assertEqual(config.load_config(self.path)["account"], "work")

    def test_force_overwrites_config_but_keeps_salt(self):
        config.init_config(self.path, account="work")
        with open(config.salt_path(self.path)) as handle:
            salt = handle.read()
        config.init_config(self.path, account="personal", force=True)
        self.assertEqual(config.load_config(self.path)["account"], "personal")
        with open(config.salt_path(self.path)) as handle:
            self.assertEqual(handle.read(), salt)

    def test_init_rejects_bad_account(self):
        with self.assertRaises(config.ConfigError):
            config.init_config(self.path, account="my work account")
        self.assertFalse(os.path.exists(self.path))


class LoadTest(ConfigTestCase):
    def test_missing_keys_get_defaults(self):
        self.write({"account": "work"})
        loaded = config.load_config(self.path)
        self.assertEqual(loaded["sources"], ["~/.claude/projects"])
        self.assertIs(loaded["round_ts_to_hour"], False)
        self.assertIs(loaded["hash_names"], True)
        self.assertIsNone(loaded["machine_label"])
        self.assertEqual(
            loaded["crosscheck"], {"command": "ccusage", "enabled": True, "period": "week"},
        )

    def test_hash_names_precedence(self):
        cases = (
            ({}, True),
            ({"hash_agent_types": False}, True),
            ({"hash_agent_types": True}, True),
            ({"hash_names": False, "hash_agent_types": True}, True),
            ({"hash_names": True, "hash_agent_types": False}, True),
            ({"hash_names": False}, False),
            ({"hash_names": False, "hash_agent_types": False}, False),
        )
        for keys, expected in cases:
            with self.subTest(keys=keys):
                self.write(dict({"account": "work"}, **keys))
                self.assertIs(config.load_config(self.path)["hash_names"], expected)

    def test_name_map_lives_beside_config(self):
        self.assertEqual(config.name_map_path(self.path),
                         os.path.join(os.path.dirname(self.path), "names.json"))

    def test_crosscheck_period_can_be_month_and_old_objects_still_load(self):
        for crosscheck in ({"command": "ccusage", "enabled": True, "period": "month"},
                           {"command": "ccusage", "enabled": True}):
            with self.subTest(crosscheck=crosscheck):
                self.write({"account": "work", "crosscheck": crosscheck})
                self.assertEqual(config.load_config(self.path)["crosscheck"], crosscheck)

    def test_missing_file_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(self.path)

    def test_invalid_values_raise(self):
        bad = [
            {"account": "has space"},
            {"account": "work", "schedule": "25:00"},
            {"account": "work", "destination": {"type": "s3", "path": "x"}},
            {"account": "work", "destination": {"type": "folder"}},
            {"account": "work", "machine_label": "my laptop"},
            {"account": "work", "sources": "~/.claude/projects"},
            {"account": "work", "round_ts_to_hour": "yes"},
            {"account": "work", "hash_names": "yes"},
            {"account": "work", "crosscheck": False},
            {"account": "work", "crosscheck": {"command": "", "enabled": True}},
            {"account": "work", "crosscheck": {"command": "ccusage", "enabled": "no"}},
            {"account": "work", "crosscheck": {"command": "ccusage", "period": "day"}},
            {"account": "work", "weights": {"output": "five"}},
            {"account": "work", "weights": {"thinking": 1}},
            {"account": "work", "weights": []},
            {"account": "work", "prices": {"claude-opus": {"output": -1}}},
            {"account": "work", "prices": {"claude-opus": 3}},
            {"account": "work", "prices": []},
        ]
        for data in bad:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(config.ConfigError):
                    config.load_config(self.path)

    def test_not_json_raises(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("{nope")
        with self.assertRaises(config.ConfigError):
            config.load_config(self.path)

    def test_state_and_salt_live_beside_config(self):
        folder = os.path.dirname(self.path)
        self.assertEqual(config.salt_path(self.path), os.path.join(folder, "salt"))
        self.assertEqual(config.state_path(self.path), os.path.join(folder, "state.json"))


if __name__ == "__main__":
    unittest.main()
