import argparse
import contextlib
import io
import json
import os
import unittest
from datetime import date

from usage_collector import crosscheck, guard
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
        self.assertEqual(crosscheck.reduce_daily(DAILY, "2026-02"), EXPECTED)

    def test_sums_repeated_models_on_one_day(self):
        entry = {"modelName": "claude-opus-5-5", "inputTokens": 1,
                 "outputTokens": 1, "cacheCreationTokens": 1, "cacheReadTokens": 1}
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [entry, entry]}]}
        totals = crosscheck.reduce_daily(data, "2026-02")
        self.assertEqual(totals["2026-02-03"]["claude-opus-5-5"]["input"], 2)

    def test_missing_or_bad_numbers_count_as_zero(self):
        entry = {"modelName": "claude-opus-5-5", "inputTokens": "12",
                 "outputTokens": True, "cacheReadTokens": -3}
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [entry]}]}
        counts = crosscheck.reduce_daily(data, "2026-02")["2026-02-03"]["claude-opus-5-5"]
        self.assertEqual(counts, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

    def test_non_object_input_gives_no_totals(self):
        for data in ([], "text", {"daily": "text"}, None):
            with self.subTest(data=data):
                self.assertEqual(crosscheck.reduce_daily(data, "2026-02"), {})


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

    def run_check(self, month="2026-02", today=date(2026, 3, 10)):
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(month=month)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crosscheck.cmd_crosscheck(
                args, self.config, environ=self.environ, today=today,
                machine="laptop-1",
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

    def test_defaults_to_previous_month(self):
        self.fake_ccusage(json.dumps({"daily": []}))
        code, _out, _err = self.run_check(month=None, today=date(2026, 1, 3))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.check_file("2025-12")))

    def test_free_text_model_fails_closed_without_echoing_it(self):
        data = {"daily": [{"date": "2026-02-03", "modelBreakdowns": [
            {"modelName": "my secret prompt", "inputTokens": 1}]}]}
        self.fake_ccusage(json.dumps(data))
        code, out, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.check_file()))
        self.assertNotIn("secret", out + err)

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

    def compare(self, month=None, tolerance=0.02, config=None):
        args = argparse.Namespace(store=self.store, month=month, tolerance=tolerance)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crosscheck.cmd_compare(args, config)
        return code, out.getvalue(), err.getvalue()

    def opus(self, **changes):
        counts = {"input": 10, "output": 40, "cache_write": 60, "cache_read": 80}
        counts.update(changes)
        return counts

    def write_check(self, opus, machine="laptop-1"):
        self.sink.write_check("2026-02", {"2026-02-03": {
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


if __name__ == "__main__":
    unittest.main()
