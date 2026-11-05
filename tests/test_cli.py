import contextlib
import csv
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

from usage_collector import cli, crosscheck, extract, guard, sinks
from usage_collector import config as config_module
from usage_collector.hashing import load_salt
from usage_collector.names import NamePolicy

from tests import support
from tests.test_sinks import isolated_git_env

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=timezone.utc)
FIRST_OF_MONTH = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.projects = self.tmp.copy_projects()
        self.store = self.tmp.join("store")
        self.config_path = self.tmp.join("cfg", "config.json")
        home = self.tmp.join("home")
        os.makedirs(home)
        self.bin = self.tmp.join("bin")
        os.makedirs(self.bin)
        self.environ = isolated_git_env(self.tmp)
        self.environ.update(HOME=home, PATH=self.bin + ":/usr/bin:/bin")
        self.environ.pop("CLAUDE_USAGE_CONFIG", None)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["--config", self.config_path] + list(argv),
                environ=self.environ, now=now,
            )
        return code, out.getvalue(), err.getvalue()

    def init(self, *extra):
        code, _out, err = self.cli("init", "--account", "work", "--dest", self.store, *extra)
        self.assertEqual(code, 0, err)
        self.edit_config(sources=[self.projects])

    def edit_config(self, **changes):
        with open(self.config_path) as handle:
            data = json.load(handle)
        data.update(changes)
        with open(self.config_path, "w") as handle:
            json.dump(data, handle)

    def store_files(self):
        found = []
        for folder, _dirs, files in os.walk(self.store):
            found.extend(os.path.relpath(os.path.join(folder, n), self.store) for n in files)
        return sorted(found)

    def write_settings(self, data):
        folder = os.path.join(self.environ["HOME"], ".claude")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "settings.json"), "w") as handle:
            json.dump(data, handle)

    def fake_command(self, name):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)


class InitTest(CliTestCase):
    def test_init_writes_config_and_salt(self):
        self.init("--schedule", "07:15")
        with open(self.config_path) as handle:
            data = json.load(handle)
        self.assertEqual((data["account"], data["schedule"]), ("work", "07:15"))
        salt = os.path.join(os.path.dirname(self.config_path), "salt")
        self.assertEqual(stat.S_IMODE(os.stat(salt).st_mode), 0o600)

    def test_second_init_needs_force(self):
        self.init()
        code, _out, err = self.cli("init", "--account", "personal")
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.cli("init", "--account", "personal", "--force")[0], 0)

    def test_env_var_selects_config(self):
        self.environ["CLAUDE_USAGE_CONFIG"] = self.config_path
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["init", "--account", "work"], environ=self.environ, now=NOW)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.config_path))


class CollectTest(CliTestCase):
    def test_collect_writes_day_files_and_no_canary(self):
        self.init()
        code, out, err = self.cli("collect")
        self.assertEqual(code, 0, err)
        self.assertIn("11 new rows, 7 context events, 3 skill uses, 9 catalog names", out)
        self.assertEqual(self.store_files(), [
            "work/2026/03/2026-03-01.catalog.csv",
            "work/2026/03/2026-03-01.context.csv",
            "work/2026/03/2026-03-01.csv",
            "work/2026/03/2026-03-01.skills.csv",
            "work/2026/03/2026-03-02.catalog.csv",
            "work/2026/03/2026-03-02.context.csv",
            "work/2026/03/2026-03-02.csv",
            "work/2026/03/2026-03-02.skills.csv",
            "work/2026/03/2026-03-05.context.csv",
            "work/2026/03/2026-03-05.csv",
        ])
        data = support.read_all_bytes(self.store)
        for canary in support.CANARIES:
            self.assertNotIn(canary.encode(), data)
        self.assertNotIn(b"example-user", data)

    def test_v1_hash_agent_types_config_hashes_skill_and_catalog_names(self):
        self.init()
        with open(self.config_path) as handle:
            data = json.load(handle)
        data.pop("hash_names")
        data["hash_agent_types"] = True
        with open(self.config_path, "w") as handle:
            json.dump(data, handle)
        self.assertEqual(self.cli("collect")[0], 0)
        salt = load_salt(config_module.salt_path(self.config_path))
        policy = NamePolicy(salt, True)
        names = {stream: self.column_values(stream, column)
                 for stream, column in (("skills", "skill"), ("catalog", "name"))}
        self.assertEqual(names["skills"], {policy.name("tdd"), policy.name(
            "Odd Skill CANARY_SKILLNAME_7f3")})
        self.assertIn(policy.name("Explore"), names["catalog"])
        self.assertNotIn("tdd", names["catalog"])

    def column_values(self, stream: str, column: str) -> set:
        found = set()
        for name in self.store_files():
            if name.endswith("." + stream + ".csv"):
                with open(os.path.join(self.store, name)) as handle:
                    found |= {row[column] for row in csv.DictReader(handle)}
        return found

    def test_state_from_before_the_new_streams_backfills_them_without_usage_rows(self):
        self.init()
        self.cli("collect")
        state_path = os.path.join(os.path.dirname(self.config_path), "state.json")
        with open(state_path) as handle:
            state = json.load(handle)
        state.pop("marks")
        for entry in state["files"].values():
            entry.pop("streams")
        with open(state_path, "w") as handle:
            json.dump(state, handle)
        shutil.rmtree(self.store)
        code, out, err = self.cli("collect")
        self.assertEqual(code, 0, err)
        self.assertIn("0 new rows, 7 context events, 3 skill uses, 9 catalog names", out)
        self.assertFalse([path for path in self.store_files()
                          if path.endswith(("01.csv", "02.csv", "05.csv"))])

    def test_guard_problem_in_a_new_stream_writes_nothing_and_echoes_nothing(self):
        self.init()
        with mock.patch.object(NamePolicy, "name", lambda _self, value: value or ""):
            code, out, err = self.cli("collect")
        self.assertEqual(code, 1)
        self.assertIn("catalog row 2: column name is invalid", err)
        self.assertIn("skills row 1: column skill is invalid", err)
        for canary in support.CANARIES:
            self.assertNotIn(canary, out + err)
        self.assertFalse(os.path.exists(self.store))
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        self.assertFalse(os.path.exists(state))

    def test_second_collect_appends_only_new_rows(self):
        self.init()
        self.cli("collect")
        support.complete_partial_line(self.projects)
        code, out, _err = self.cli("collect")
        self.assertEqual(code, 0)
        self.assertIn("1 new rows", out)
        path = os.path.join(self.store, "work/2026/03/2026-03-05.csv")
        with open(path) as handle:
            self.assertEqual(len(handle.read().splitlines()), 3)

    def test_dry_run_prints_counts_and_sample_and_writes_nothing(self):
        self.init()
        code, out, _err = self.cli("collect", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("2026-03-01: 7", out)
        self.assertIn("2026-03-02: 3", out)
        for line in ("context events: 7", "skill uses: 3", "catalog names: 9"):
            self.assertIn(line, out)
        self.assertIn(",".join(guard.COLUMNS), out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("2026-03-01T")]), 3)
        self.assertFalse(os.path.exists(self.store))
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        self.assertFalse(os.path.exists(state))
        for canary in support.CANARIES:
            self.assertNotIn(canary, out)

    def append_line(self, line):
        path = os.path.join(self.projects, support.BETA, support.S3 + ".jsonl")
        with open(path, "a") as handle:
            handle.write(line + "\n")
        support.mark_idle(path)
        return path

    def test_odd_line_before_a_good_line_still_collects_and_moves_on(self):
        self.init()
        self.append_line(support.B2_LINE.replace("claude-opus-5-5", "claude opus")
                         .replace('"input_tokens": 6', '"input_tokens": 2000000000'))
        self.append_line(support.B2_LINE.replace("msg_B2", "msg_ODD")
                         .replace("claude-opus-5-5", "a custom/model name"))
        self.append_line(support.B2_LINE.replace("msg_B2", "msg_GOOD"))
        code, out, err = self.cli("collect")
        self.assertEqual(code, 0, err)
        self.assertIn("13 new rows", out)
        self.assertIn("0 new rows", self.cli("collect")[1])
        data = support.read_all_bytes(self.store)
        self.assertNotIn(b"custom", data)

    def test_guard_failure_names_the_file_writes_nothing_and_keeps_offsets(self):
        self.init()
        bad = support.B2_LINE.replace("claude-opus-5-5", "SECRET model name")
        path = self.append_line(bad)
        with mock.patch.object(extract, "model_slug", lambda model, _salt: model):
            code, out, err = self.cli("collect")
        self.assertEqual(code, 1)
        self.assertIn("column model is invalid", err)
        self.assertIn(path, err)
        self.assertNotIn("SECRET", out + err)
        self.assertFalse(os.path.exists(self.store))
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        self.assertFalse(os.path.exists(state))

    def test_unwritable_destination_fails_cleanly_and_a_retry_writes_once(self):
        self.init()
        blocker = os.path.join(self.store, "work/2026/03/2026-03-02.csv")
        os.makedirs(blocker)
        code, out, err = self.cli("collect")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("error:", err)
        os.rmdir(blocker)
        self.assertEqual(self.cli("collect")[0], 0)
        with open(os.path.join(self.store, "work/2026/03/2026-03-01.csv")) as handle:
            self.assertEqual(len(handle.read().splitlines()), 8)

    def test_unexpected_error_prints_no_traceback_or_paths(self):
        self.init()
        with mock.patch.object(extract, "extract", side_effect=RuntimeError("/private/x")):
            code, out, err = self.cli("collect")
            self.assertEqual(code, 1)
            self.assertNotIn("/private/x", out + err)
            self.assertIn("--debug", err)
            with self.assertRaises(RuntimeError):
                self.cli("--debug", "collect")

    def test_git_destination_that_is_not_a_clone_writes_nothing(self):
        self.init("--dest-type", "git")
        code, _out, err = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("not a git clone", err)
        self.assertFalse(os.path.exists(self.store))
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        self.assertFalse(os.path.exists(state))

    def test_collect_without_config_fails_cleanly(self):
        code, _out, err = self.cli("collect")
        self.assertEqual(code, 1)
        self.assertIn("init", err)


class StatusTest(CliTestCase):
    def test_status_before_and_after_collect(self):
        self.init()
        self.assertIn("last run: never", self.cli("status")[1])
        self.cli("collect")
        out = self.cli("status")[1]
        self.assertIn("last run: 2026-03-10T18:00:00Z", out)
        self.assertIn("rows collected: 11 last run, 11 total", out)
        self.assertIn("files tracked: 6", out)


class SyncAndRunTest(CliTestCase):
    def test_sync_on_folder_is_a_no_op(self):
        self.init()
        code, out, _err = self.cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("nothing to sync", out)

    def test_run_collects_and_syncs_into_git_store(self):
        remote = self.tmp.join("remote.git")
        git = lambda *a: subprocess.run(("git",) + a, env=self.environ, check=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        git("init", "-q", "--bare", "-b", "main", remote)
        git("clone", "-q", remote, self.store)
        self.init("--dest-type", "git")
        code, _out, err = self.cli("run")
        self.assertEqual(code, 0, err)
        shown = git("--git-dir", remote, "show", "main:work/2026/03/2026-03-01.csv").stdout
        self.assertIn("2026-03-01T10:00:05Z", shown)

    def test_sync_conflict_exits_one_and_leaves_state_alone(self):
        remote = self.tmp.join("remote.git")
        git = lambda *a, cwd=None: subprocess.run(
            ("git",) + a, env=self.environ, check=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        git("init", "-q", "--bare", "-b", "main", remote)
        git("clone", "-q", remote, self.store)
        other = self.tmp.join("other")
        git("clone", "-q", remote, other)
        self.init("--dest-type", "git")
        self.edit_config(machine_label="laptop-1")
        totals = {"2026-03-01": {"claude-opus-5-5": {
            "input": 1, "output": 2, "cache_read": 3, "cache_write": 4}}}
        sinks.FolderSink(other, "work").write_check("2026-03", totals, "laptop-1")
        git("add", "-A", cwd=other)
        git("-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "other", cwd=other)
        git("push", "-q", "origin", "HEAD:main", cwd=other)
        self.assertEqual(self.cli("collect")[0], 0)
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        with open(state) as handle:
            before = handle.read()
        totals["2026-03-01"]["claude-opus-5-5"]["input"] = 9
        sinks.FolderSink(self.store, "work").write_check("2026-03", totals, "laptop-1")
        code, _out, err = self.cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("rebase aborted", err)
        with open(state) as handle:
            self.assertEqual(handle.read(), before)

    def test_run_skips_crosscheck_quietly_without_ccusage(self):
        self.init()
        with mock.patch.object(crosscheck, "cmd_crosscheck") as check:
            code, _out, _err = self.cli("run", now=FIRST_OF_MONTH)
        self.assertEqual(code, 0)
        check.assert_not_called()

    def test_run_crosschecks_previous_month_once(self):
        self.init()
        self.edit_config(crosscheck={"command": "ccusage", "enabled": True, "period": "month"})
        self.fake_command("ccusage")
        with mock.patch.object(crosscheck, "cmd_crosscheck", return_value=0) as check:
            self.assertEqual(self.cli("run", now=FIRST_OF_MONTH)[0], 0)
            self.assertEqual(self.cli("run", now=NOW)[0], 0)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args[0][0].month, "2026-02")

    def test_run_crosschecks_the_last_completed_week_once(self):
        self.init()
        self.fake_command("ccusage")
        with mock.patch.object(crosscheck, "cmd_crosscheck", return_value=0) as check:
            self.assertEqual(self.cli("run", now=NOW)[0], 0)
            self.assertEqual(self.cli("run", now=NOW)[0], 0)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args[0][0].week, "2026-W10")
        self.assertIsNone(check.call_args[0][0].month)

    def test_run_catches_up_missed_weeks_and_logs_the_too_old_ones(self):
        self.init()
        self.fake_command("ccusage")
        with mock.patch.object(crosscheck, "cmd_crosscheck", return_value=0) as check:
            self.cli("run", now=datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc))
            code, out, _err = self.cli("run", now=NOW)
        self.assertEqual(code, 0)
        weeks = [call[0][0].week for call in check.call_args_list]
        self.assertEqual(weeks, ["2026-W06", "2026-W09", "2026-W10"])
        self.assertIn("skipped cross-check for 2026-W07", out)
        self.assertIn("skipped cross-check for 2026-W08", out)

    def test_run_after_a_v1_monthly_state_checks_only_the_last_completed_week(self):
        self.init()
        self.fake_command("ccusage")
        state_path = os.path.join(os.path.dirname(self.config_path), "state.json")
        with open(state_path, "w") as handle:
            json.dump({"crosscheck_month": "2026-02"}, handle)
        with mock.patch.object(crosscheck, "cmd_crosscheck", return_value=0) as check:
            self.assertEqual(self.cli("run", now=NOW)[0], 0)
        self.assertEqual([call[0][0].week for call in check.call_args_list], ["2026-W10"])

    def test_run_retries_a_failed_week_on_the_next_run(self):
        self.init()
        self.fake_command("ccusage")
        with mock.patch.object(crosscheck, "cmd_crosscheck", side_effect=[1, 0]) as check:
            self.assertEqual(self.cli("run", now=NOW)[0], 1)
            self.assertEqual(self.cli("run", now=NOW)[0], 0)
        weeks = [call[0][0].week for call in check.call_args_list]
        self.assertEqual(weeks, ["2026-W10", "2026-W10"])

    def test_run_respects_disabled_crosscheck(self):
        self.init()
        self.edit_config(crosscheck={"command": "ccusage", "enabled": False})
        self.fake_command("ccusage")
        with mock.patch.object(crosscheck, "cmd_crosscheck") as check:
            self.cli("run", now=FIRST_OF_MONTH)
        check.assert_not_called()


class DoctorTest(CliTestCase):
    def test_healthy_setup_has_no_failures(self):
        self.init()
        os.makedirs(self.store)
        self.write_settings({"cleanupPeriodDays": 365})
        self.fake_command("ccusage")
        code, out, _err = self.cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertIn("OK   ccusage", out)

    def retention(self):
        lines = self.cli("doctor")[1].splitlines()
        return next(line for line in lines if "cleanupPeriodDays" in line)

    def test_default_or_three_week_retention_is_enough_for_the_weekly_check(self):
        self.init()
        for settings in ({}, {"cleanupPeriodDays": 30}, {"cleanupPeriodDays": 22}):
            with self.subTest(settings=settings):
                self.write_settings(settings)
                line = self.retention()
                self.assertTrue(line.startswith("OK   "), line)
                self.assertIn("weekly", line)

    def test_warns_when_retention_is_shorter_than_the_weekly_catch_up(self):
        self.init()
        for days in (13, 14, 21):
            with self.subTest(days=days):
                self.write_settings({"cleanupPeriodDays": days})
                line = self.retention()
                self.assertTrue(line.startswith("WARN "), line)
                self.assertIn("below 22", line)
                self.assertNotIn("90", line)

    def test_month_period_needs_two_months_of_retention(self):
        self.init()
        self.edit_config(crosscheck={"command": "ccusage", "enabled": True, "period": "month"})
        for settings, level in (({}, "WARN "), ({"cleanupPeriodDays": 30}, "WARN "),
                                ({"cleanupPeriodDays": 61}, "WARN "),
                                ({"cleanupPeriodDays": 62}, "OK   ")):
            with self.subTest(settings=settings):
                self.write_settings(settings)
                line = self.retention()
                self.assertTrue(line.startswith(level), line)
                self.assertIn("monthly", line)

    def test_unreadable_retention_setting_warns_instead_of_reading_as_unset(self):
        self.init()
        folder = os.path.join(self.environ["HOME"], ".claude")
        os.makedirs(folder, exist_ok=True)
        for text in ("{not json", "[]", '{"cleanupPeriodDays": "7"}',
                     '{"cleanupPeriodDays": 7.5}', '{"cleanupPeriodDays": true}'):
            with self.subTest(text=text):
                with open(os.path.join(folder, "settings.json"), "w") as handle:
                    handle.write(text)
                line = self.retention()
                self.assertTrue(line.startswith("WARN "), line)
                self.assertIn("could not read", line)
                self.assertNotIn("7", line.replace("cleanupPeriodDays", ""))

    def test_missing_config_fails(self):
        code, out, _err = self.cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL config", out)

    def test_invalid_hand_edit_fails_cleanly(self):
        self.init()
        self.edit_config(weights={"output": "five"}, crosscheck=False)
        code, out, err = self.cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL config", out)
        self.assertNotIn("Traceback", out + err)

    def test_doctor_is_read_only(self):
        self.init()
        self.cli("doctor")
        self.assertFalse(os.path.exists(self.store))
        state = os.path.join(os.path.dirname(self.config_path), "state.json")
        self.assertFalse(os.path.exists(state))

    def test_git_destination_that_is_not_a_clone_fails(self):
        self.init("--dest-type", "git")
        os.makedirs(self.store)
        out = self.cli("doctor")[1]
        self.assertIn("FAIL git", out)


class ResearchCommandsTest(CliTestCase):
    def fake_ccusage(self, payload):
        path = os.path.join(self.bin, "ccusage")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % json.dumps(payload))
        os.chmod(path, 0o755)

    def test_crosscheck_uses_the_session_path_and_last_completed_week(self):
        self.init()
        self.edit_config(machine_label="laptop-1")
        self.fake_ccusage({"daily": [{"date": "2026-03-04", "modelBreakdowns": [
            {"modelName": "claude-opus-5-5", "inputTokens": 3}]}]})
        code, _out, err = self.cli("crosscheck")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store_files(), ["checks/work/laptop-1/2026-W10.json"])

    def test_crosscheck_and_compare_take_week_and_month_flags(self):
        self.init()
        self.edit_config(machine_label="laptop-1")
        self.fake_ccusage({"daily": []})
        self.assertEqual(self.cli("crosscheck", "--week", "2026-W08")[0], 0)
        self.assertEqual(self.cli("crosscheck", "--month", "2026-01")[0], 0)
        self.assertEqual(self.store_files(), [
            "checks/work/laptop-1/2026-01.json", "checks/work/laptop-1/2026-W08.json",
        ])
        code, out, err = self.cli("compare", "--week", "2026-W08")
        self.assertEqual(code, 0, out + err)
        self.assertIn("compared 0 day/model pair(s)", out)

    def test_run_crosscheck_reaches_ccusage_on_the_session_path(self):
        self.init()
        self.edit_config(machine_label="laptop-1")
        self.fake_ccusage({"daily": []})
        code, _out, err = self.cli("run", now=FIRST_OF_MONTH)
        self.assertEqual(code, 0, err)
        self.assertIn("checks/work/laptop-1/2026-W08.json", self.store_files())

    def test_run_just_after_utc_midnight_checks_the_whole_utc_month(self):
        self.init()
        self.edit_config(machine_label="laptop-1", crosscheck={
            "command": "ccusage", "enabled": True, "period": "month"})
        log = self.tmp.join("argv.log")
        path = os.path.join(self.bin, "ccusage")
        with open(path, "w") as handle:
            handle.write('#!/bin/sh\necho "$@" > "%s"\necho \'{"daily": []}\'\n' % log)
        os.chmod(path, 0o755)
        now = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(self.cli("run", now=now)[0], 0)
        with open(log) as handle:
            self.assertEqual(handle.read().split(), [
                "daily", "--json", "--since", "20261001", "--until", "20261031",
                "--timezone", "UTC",
            ])

    def test_report_and_analyze_work_without_a_config(self):
        self.init()
        self.cli("collect")
        os.remove(self.config_path)
        out_file = self.tmp.join("out.html")
        self.assertEqual(self.cli("analyze", "--store", self.store)[0], 0)
        code, _out, err = self.cli("report", "--store", self.store, "--out", out_file)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out_file))


class EntryPointTest(unittest.TestCase):
    def test_python_dash_m_and_wrapper_show_help(self):
        for command in (
            [sys.executable, "-m", "usage_collector", "--help"],
            [os.path.join(REPO, "bin", "claude-usage"), "--help"],
        ):
            with self.subTest(command=command[0]):
                result = subprocess.run(
                    command, cwd=REPO if "-m" in command else "/",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("collect", result.stdout)


if __name__ == "__main__":
    unittest.main()
