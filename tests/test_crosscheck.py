import argparse
import contextlib
import io
import json
import os
import unittest
from datetime import date

from usage_collector import crosscheck, guard
from usage_collector.hashing import hash_value
from usage_collector.sinks import FolderSink

from tests import support

DAILY = {
    "daily": [
        {
            "date": "2026-02-03",
            "inputTokens": 999,
            "unknownField": "CANARY_PROMPT_7f3",
            "modelBreakdowns": [
                {"modelName": "claude-opus-5-5", "inputTokens": 10,
                 "outputTokens": 20, "cacheCreationTokens": 30,
                 "cacheReadTokens": 40, "cost": 1.5,
                 "note": "CANARY_REPLY_7f3"},
                {"modelName": "claude-haiku-4-5", "inputTokens": 1,
                 "outputTokens": 2, "cacheCreationTokens": 3,
                 "cacheReadTokens": 4},
                {"modelName": "<synthetic>", "inputTokens": 5,
                 "outputTokens": 5, "cacheCreationTokens": 5,
                 "cacheReadTokens": 5},
            ],
        },
        {"date": "2026-02-04", "modelBreakdowns": "not a list"},
        "not an entry",
        {"modelBreakdowns": []},
        {"date": "2026-03-01", "modelBreakdowns": [
            {"modelName": "claude-opus-5-5", "inputTokens": 1,
             "outputTokens": 1, "cacheCreationTokens": 1,
             "cacheReadTokens": 1},
        ]},
    ],
    "totals": {"inputTokens": 1},
}
EXPECTED = {
    "2026-02-03": {
        "claude-opus-5-5": {"input": 10, "output": 20, "cache_write": 30, "cache_read": 40},
        "claude-haiku-4-5": {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4},
    }
}


class ReduceDailyTest(unittest.TestCase):
    def test_keeps_only_allowlisted_numbers_for_the_month(self):
        self.assertEqual(crosscheck.reduce_daily(DAILY, "2026-02", support.SALT), EXPECTED)

    def test_sums_repeated_models_on_one_day(self):
        entry = {"modelName": "claude-opus-5-5", "inputTokens": 1,
                 "outputTokens": 1, "cacheCreationTokens": 1, "cacheReadTokens": 1}
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [entry, entry]}]}
        totals = crosscheck.reduce_daily(data, "2026-02", support.SALT)
        self.assertEqual(totals["2026-02-03"]["claude-opus-5-5"]["input"], 2)

    def test_missing_or_bad_numbers_count_as_zero(self):
        entry = {"modelName": "claude-opus-5-5", "inputTokens": "12",
                 "outputTokens": True, "cacheReadTokens": -3}
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [entry]}]}
        counts = crosscheck.reduce_daily(data, "2026-02", support.SALT)["2026-02-03"]["claude-opus-5-5"]
        self.assertEqual(counts, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

    def test_non_object_input_gives_no_totals(self):
        for data in ([], "text", {"daily": "text"}, None):
            with self.subTest(data=data):
                self.assertEqual(crosscheck.reduce_daily(data, "2026-02", support.SALT), {})


class MonthTest(unittest.TestCase):
    def test_month_bounds(self):
        self.assertEqual(crosscheck.month_bounds("2026-02"), ("20260201", "20260228"))
        self.assertEqual(crosscheck.month_bounds("2024-12"), ("20241201", "20241231"))

    def test_previous_month(self):
        self.assertEqual(crosscheck.previous_month(date(2026, 3, 1)), "2026-02")
        self.assertEqual(crosscheck.previous_month(date(2026, 1, 15)), "2025-12")


class CrosscheckCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.bin = self.tmp.join("bin")
        os.makedirs(self.bin)
        self.store = self.tmp.join("store")
        self.argv_log = self.tmp.join("argv.txt")
        self.env_log = self.tmp.join("env.txt")
        self.environ = {"PATH": self.bin + ":/usr/bin:/bin"}
        self.config = {
            "account": "work",
            "destination": {"type": "folder", "path": self.store},
            "crosscheck": {"command": "ccusage", "enabled": True},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fake_ccusage(self, stdout: str, code: int = 0):
        payload = self.tmp.join("payload.json")
        with open(payload, "w") as handle:
            handle.write(stdout)
        script = self.tmp.join("bin", "ccusage")
        with open(script, "w") as handle:
            handle.write(
                '#!/bin/sh\necho "$@" > "%s"\necho "$CLAUDE_CONFIG_DIR" > "%s"\n'
                'cat "%s"\nexit %d\n'
                % (self.argv_log, self.env_log, payload, code)
            )
        os.chmod(script, 0o755)

    def run_check(self, month="2026-02", today=date(2026, 3, 10), week=None):
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(month=month, week=week)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crosscheck.cmd_crosscheck(
                args, self.config, environ=self.environ, today=today,
                machine="laptop-1", salt=support.SALT,
            )
        return code, out.getvalue(), err.getvalue()

    def check_file(self, month="2026-02"):
        return os.path.join(self.store, "checks", "work", "laptop-1", month + ".json")

    def test_writes_guarded_totals(self):
        self.fake_ccusage(json.dumps(DAILY))
        code, _out, err = self.run_check()
        self.assertEqual(code, 0, err)
        with open(self.check_file()) as handle:
            self.assertEqual(json.load(handle), EXPECTED)
        self.assertNotIn(b"CANARY", support.read_all_bytes(self.store))

    def test_asks_ccusage_for_the_daily_report_of_the_month(self):
        self.fake_ccusage(json.dumps(DAILY))
        self.run_check()
        with open(self.argv_log) as handle:
            argv = handle.read().split()
        self.assertEqual(argv, [
            "daily", "--json", "--since", "20260201", "--until", "20260228",
            "--timezone", "UTC",
        ])

    def test_points_ccusage_at_the_configured_sources(self):
        self.fake_ccusage(json.dumps(DAILY))
        first, second = self.tmp.join("one"), self.tmp.join("two")
        self.config["sources"] = [first + "/projects", second + "/projects/"]
        code, _out, err = self.run_check()
        self.assertEqual(code, 0, err)
        with open(self.env_log) as handle:
            self.assertEqual(handle.read().strip(), first + "," + second)

    def test_source_that_is_not_a_projects_folder_is_an_error(self):
        self.fake_ccusage(json.dumps(DAILY))
        self.config["sources"] = [self.tmp.join("transcripts")]
        code, _out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("projects", err)
        self.assertFalse(os.path.exists(self.check_file()))

    def test_month_period_defaults_to_previous_month(self):
        self.config["crosscheck"]["period"] = "month"
        self.fake_ccusage(json.dumps({"daily": []}))
        code, _out, _err = self.run_check(month=None, today=date(2026, 1, 3))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.check_file("2025-12")))

    def test_defaults_to_the_last_completed_week(self):
        self.fake_ccusage(json.dumps({"daily": []}))
        code, _out, err = self.run_check(month=None, today=date(2026, 3, 4))
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(self.check_file("2026-W09")))

    def test_week_asks_ccusage_for_monday_to_sunday_and_keeps_only_those_days(self):
        day = {"modelName": "claude-opus-5-5", "inputTokens": 1, "outputTokens": 1,
               "cacheCreationTokens": 1, "cacheReadTokens": 1}
        data = {"daily": [{"date": d, "modelBreakdowns": [day]}
                          for d in ("2026-02-22", "2026-02-23", "2026-03-01", "2026-03-02")]}
        self.fake_ccusage(json.dumps(data))
        code, _out, err = self.run_check(month=None, week="2026-W09")
        self.assertEqual(code, 0, err)
        with open(self.argv_log) as handle:
            argv = handle.read().split()
        self.assertEqual(argv[3:6], ["20260223", "--until", "20260301"])
        with open(self.check_file("2026-W09")) as handle:
            self.assertEqual(sorted(json.load(handle)), ["2026-02-23", "2026-03-01"])

    def test_rejects_bad_week_and_both_flags(self):
        self.fake_ccusage(json.dumps(DAILY))
        code, _out, err = self.run_check(month=None, week="2026-W60")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-Www", err)
        code, _out, err = self.run_check(month="2026-02", week="2026-W09")
        self.assertEqual(code, 1)
        self.assertIn("not both", err)

    def test_non_claude_model_name_is_hashed_and_never_written(self):
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [
            {"modelName": "my secret prompt", "inputTokens": 1},
            {"modelName": "acme-private-finetune-v2", "inputTokens": 1}]}]}
        self.fake_ccusage(json.dumps(data))
        code, out, err = self.run_check()
        self.assertEqual(code, 0, err)
        stored = support.read_all_bytes(self.store)
        for secret in ("secret", "acme"):
            self.assertNotIn(secret.encode(), stored)
            self.assertNotIn(secret, out + err)

    def test_out_of_range_total_fails_closed(self):
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [
            {"modelName": "claude-opus-5-5", "inputTokens": 10 ** 14}]}]}
        self.fake_ccusage(json.dumps(data))
        code, _out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("guard", err)
        self.assertFalse(os.path.exists(self.check_file()))

    def test_failing_or_garbled_ccusage_writes_nothing(self):
        for stdout, status in (("{}", 3), ("not json", 0)):
            with self.subTest(stdout=stdout, status=status):
                self.fake_ccusage(stdout, status)
                code, _out, err = self.run_check()
                self.assertEqual(code, 1)
                self.assertIn("ccusage", err)
                self.assertFalse(os.path.exists(self.check_file()))

    def test_missing_command_is_an_error(self):
        code, _out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_rejects_bad_month(self):
        self.fake_ccusage(json.dumps(DAILY))
        code, _out, err = self.run_check(month="2026-13")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM", err)


def row(ts, model, account="work", input_=10, output=20, cache_read=40, cache_write=30):
    return {
        "ts": ts, "account": account, "machine": "a" * 12, "project": "b" * 12,
        "session": "c" * 12, "agent": "", "is_subagent": "0", "agent_type": "",
        "spawn_depth": "", "is_fork": "0", "model": model, "effort": "",
        "input": str(input_), "cache_read": str(cache_read),
        "cache_write": str(cache_write), "output": str(output),
        "context": str(input_ + cache_read + cache_write),
        "web_search": "0", "web_fetch": "0", "tools": "", "compacted": "0",
    }


class CompareCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.join("store")
        self.sink = FolderSink(self.store, "work")
        rows = [
            row("2026-02-03T10:00:00Z", "claude-opus-5-5", input_=4),
            row("2026-02-03T11:00:00Z", "claude-opus-5-5", input_=6),
            row("2026-02-03T12:00:00Z", "claude-haiku-4-5",
                input_=1, output=2, cache_read=4, cache_write=3),
        ]
        self.assertEqual(guard.validate_rows(rows), [])
        self.sink.write_rows(rows)

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self, month=None, tolerance=0.02, config=None, week=None):
        args = argparse.Namespace(
            store=self.store, month=month, week=week, tolerance=tolerance,
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crosscheck.cmd_compare(args, config)
        return code, out.getvalue(), err.getvalue()

    def opus(self, **changes):
        counts = {"input": 10, "output": 40, "cache_write": 60, "cache_read": 80}
        counts.update(changes)
        return counts

    def write_check(self, opus, machine="laptop-1", period="2026-02"):
        self.sink.write_check(period, {"2026-02-03": {
            "claude-opus-5-5": opus,
            "claude-haiku-4-5": {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4},
        }}, machine)

    def test_matching_totals_pass(self):
        self.write_check(self.opus())
        code, out, err = self.compare()
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 over tolerance", out)

    def test_gap_over_tolerance_fails_and_is_listed(self):
        self.write_check(self.opus(output=50))
        code, out, _err = self.compare()
        self.assertEqual(code, 1)
        self.assertIn("2026-02-03", out)
        self.assertIn("claude-opus-5-5", out)
        self.assertIn("output", out)
        self.assertIn("-20.0%", out)

    def test_checks_from_two_machines_on_one_account_are_added(self):
        self.write_check(self.opus(input=4, output=15, cache_write=20, cache_read=30))
        self.sink.write_check("2026-02", {"2026-02-03": {
            "claude-opus-5-5": self.opus(input=6, output=25, cache_write=40, cache_read=50),
        }}, "laptop-2")
        code, out, err = self.compare()
        self.assertEqual(code, 0, out + err)

    def test_older_check_file_without_a_machine_folder_is_still_read(self):
        legacy = os.path.join(self.store, "checks", "work", "2026-02.json")
        os.makedirs(os.path.dirname(legacy))
        with open(legacy, "w") as handle:
            json.dump({"2026-02-03": {"claude-opus-5-5": self.opus(output=50)}}, handle)
        code, out, _err = self.compare()
        self.assertEqual(code, 1)
        self.assertIn("-20.0%", out)

    def test_gap_within_tolerance_passes(self):
        self.write_check(self.opus(output=41))
        self.assertEqual(self.compare(tolerance=0.05)[0], 0)

    def test_model_missing_from_checks_is_a_gap(self):
        self.sink.write_check(
            "2026-02", {"2026-02-03": {"claude-opus-5-5": self.opus()}}, "laptop-1"
        )
        code, out, _err = self.compare()
        self.assertEqual(code, 1)
        self.assertIn("claude-haiku-4-5", out)

    def test_month_filter_and_no_checks(self):
        self.write_check(self.opus())
        self.assertEqual(self.compare(month="2026-02")[0], 0)
        code, _out, err = self.compare(month="2026-01")
        self.assertEqual(code, 1)
        self.assertIn("no cross-check", err)

    def test_week_check_sums_only_rows_inside_the_week(self):
        self.sink.write_rows([row("2026-02-09T10:00:00Z", "claude-opus-5-5")])
        self.write_check(self.opus(), period="2026-W06")
        code, out, err = self.compare(week="2026-W06")
        self.assertEqual(code, 0, out + err)
        self.assertIn("compared 2 day/model pair(s), 0 gap(s)", out)

    def test_weekly_and_monthly_files_are_both_compared(self):
        self.write_check(self.opus(), period="2026-W06")
        self.write_check(self.opus(output=50), period="2026-02")
        code, out, _err = self.compare()
        self.assertEqual(code, 1)
        self.assertIn("compared 4 day/model pair(s), 1 gap(s)", out)
        self.assertEqual(self.compare(week="2026-W06")[0], 0)

    def test_bad_week_filter_is_an_error(self):
        self.write_check(self.opus(), period="2026-W06")
        code, _out, err = self.compare(week="2026-W6")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-Www", err)

    def test_check_file_with_an_unknown_name_is_ignored(self):
        self.write_check(self.opus(), period="2026-W06")
        stray = os.path.join(self.store, "checks", "work", "laptop-1", "notes.json")
        with open(stray, "w") as handle:
            json.dump({"2026-02-03": {"claude-opus-5-5": self.opus(output=1)}}, handle)
        code, out, err = self.compare()
        self.assertEqual(code, 0, out + err)

    def test_store_comes_from_config_when_not_given(self):
        self.write_check(self.opus())
        args = argparse.Namespace(store=None, month=None, tolerance=0.02)
        config = {"destination": {"type": "folder", "path": self.store}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(crosscheck.cmd_compare(args, config), 0)

    def test_no_store_and_no_config_is_an_error(self):
        args = argparse.Namespace(store=None, month=None, tolerance=0.02)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(crosscheck.cmd_compare(args, None), 1)
        self.assertIn("--store", err.getvalue())




class VersionManagedCommandTest(unittest.TestCase):
    """A command whose interpreter sits beside it, run with launchd's bare PATH."""

    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.join("store")
        self.environ = {"PATH": "/usr/bin:/bin", "HOME": self.tmp.join("home")}
        self.payload = self.tmp.join("payload.json")
        with open(self.payload, "w") as handle:
            json.dump(DAILY, handle)

    def tearDown(self):
        self.tmp.cleanup()

    def write_script(self, path: str, text: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, 0o755)
        return path

    def fake_tool(self, folder: str) -> str:
        return self.write_script(os.path.join(folder, "ccusage"), "#!/usr/bin/env fakeinterp\n")

    def fake_interpreter(self, folder: str) -> None:
        self.write_script(
            os.path.join(folder, "fakeinterp"), '#!/bin/sh\ncat "%s"\n' % self.payload)

    def run_check(self, command: str):
        config = {
            "account": "work",
            "destination": {"type": "folder", "path": self.store},
            "crosscheck": {"command": command, "enabled": True},
        }
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(month="2026-02", week=None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crosscheck.cmd_crosscheck(
                args, config, environ=self.environ, today=date(2026, 3, 10),
                machine="laptop-1", salt=support.SALT,
            )
        return code, err.getvalue()

    def stored(self):
        path = os.path.join(self.store, "checks", "work", "laptop-1", "2026-02.json")
        with open(path) as handle:
            return json.load(handle)

    def test_absolute_path_finds_the_interpreter_beside_it(self):
        folder = self.tmp.join("node", "bin")
        self.fake_interpreter(folder)
        code, err = self.run_check(self.fake_tool(folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), EXPECTED)

    def test_tilde_path_is_expanded_against_home(self):
        folder = os.path.join(self.environ["HOME"], "node", "bin")
        self.fake_interpreter(folder)
        self.fake_tool(folder)
        code, err = self.run_check("~/node/bin/ccusage")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), EXPECTED)

    def test_symlink_finds_the_interpreter_beside_the_real_file(self):
        real = self.tmp.join("node", "lib")
        self.fake_interpreter(real)
        link = self.tmp.join("links", "ccusage")
        os.makedirs(os.path.dirname(link))
        os.symlink(self.fake_tool(real), link)
        code, err = self.run_check(link)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), EXPECTED)

    def test_symlink_finds_the_interpreter_beside_the_link(self):
        bin_folder = self.tmp.join("node", "bin")
        self.fake_interpreter(bin_folder)
        link = os.path.join(bin_folder, "ccusage")
        os.symlink(self.fake_tool(self.tmp.join("node", "lib", "pkg")), link)
        code, err = self.run_check(link)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(), EXPECTED)

    def test_missing_absolute_path_is_an_error_that_does_not_echo_it(self):
        missing = self.tmp.join("nowhere", "ccusage")
        code, err = self.run_check(missing)
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.assertNotIn(missing, err)


class ResolveCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.home = self.tmp.join("home")

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, *parts) -> str:
        path = self.tmp.join(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_a_tilde_path_resolves_against_home(self):
        path = self.tool("home", "bin", "ccusage")
        environ = {"HOME": self.home, "PATH": "/usr/bin"}
        self.assertEqual(crosscheck.resolve_command("~/bin/ccusage", environ), path)

    def test_a_tilde_user_path_is_not_glued_onto_home(self):
        self.tool("homenosuchuser-7f3", "bin", "ccusage")
        environ = {"HOME": self.home, "PATH": "/usr/bin"}
        command = "~nosuchuser-7f3/bin/ccusage"
        self.assertIsNone(crosscheck.resolve_command(command, environ))


if __name__ == "__main__":
    unittest.main()
