import csv
import json
import os
import subprocess
import unittest
from datetime import date

from usage_collector import audit, guard, sinks

from tests import support
from tests.test_guard import catalog_row, context_row, skill_row, valid_row

TODAY = date(2026, 3, 10)
CANARY = "CANARY_SYNC_7f3"
TOTALS = {"2026-03-01": {"claude-opus-5-5": {
    "input": 1, "output": 2, "cache_read": 3, "cache_write": 4}}}


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.reader(handle))


def isolated_git_env(tmp):
    empty = tmp.join("gitconfig")
    open(empty, "w").close()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=empty, GIT_CONFIG_NOSYSTEM="1")
    env.pop("GIT_DIR", None)
    return env


class FolderSinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.join("store")
        self.sink = sinks.FolderSink(self.store, "work")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_grouped_by_utc_day_with_one_header(self):
        rows = [
            valid_row(ts="2026-03-01T10:00:00Z"),
            valid_row(ts="2026-03-02T00:00:01Z", output="7", context="240"),
        ]
        self.sink.write_rows(rows)
        self.sink.write_rows([valid_row(ts="2026-03-01T11:00:00Z")])

        first = read_csv(os.path.join(self.store, "work/2026/03/2026-03-01.csv"))
        self.assertEqual(first[0], list(guard.COLUMNS))
        self.assertEqual([line[0] for line in first[1:]],
                         ["2026-03-01T10:00:00Z", "2026-03-01T11:00:00Z"])
        second = read_csv(os.path.join(self.store, "work/2026/03/2026-03-02.csv"))
        self.assertEqual(len(second), 2)

    def test_invalid_rows_write_nothing(self):
        with self.assertRaises(sinks.GuardError):
            self.sink.write_rows([valid_row(), valid_row(model="free text")])
        self.assertFalse(os.path.exists(self.store))

    def test_write_check_validates_and_writes_one_file_per_machine(self):
        path = self.sink.write_check("2026-03", TOTALS, "laptop-1")
        self.assertEqual(path, os.path.join(self.store, "checks/work/laptop-1/2026-03.json"))
        with open(path) as handle:
            self.assertEqual(json.load(handle), TOTALS)
        with self.assertRaises(sinks.GuardError):
            self.sink.write_check("2026-03", {"x": {}}, "laptop-1")
        with self.assertRaises(sinks.GuardError):
            self.sink.write_check("2026-03", TOTALS, "../x")

    def test_write_check_takes_a_week_and_rejects_other_names(self):
        path = self.sink.write_check("2026-W09", TOTALS, "laptop-1")
        self.assertEqual(path, os.path.join(self.store, "checks/work/laptop-1/2026-W09.json"))
        for bad in ("2026-W99", "2026-13", "../2026-03"):
            with self.subTest(bad=bad), self.assertRaises(sinks.GuardError):
                self.sink.write_check(bad, TOTALS, "laptop-1")

    def test_failed_write_leaves_no_rows_so_a_retry_writes_each_once(self):
        rows = [valid_row(ts="2026-03-01T10:00:00Z"), valid_row(ts="2026-03-02T10:00:00Z")]
        blocker = os.path.join(self.store, "work/2026/03/2026-03-02.csv")
        os.makedirs(blocker)
        with self.assertRaises(OSError):
            self.sink.write_rows(rows)
        self.assertEqual(os.listdir(os.path.dirname(blocker)), ["2026-03-02.csv"])
        os.rmdir(blocker)
        self.sink.write_rows(rows)
        for day in ("01", "02"):
            lines = read_csv(os.path.join(self.store, "work/2026/03/2026-03-%s.csv" % day))
            self.assertEqual([line[0] for line in lines[1:]], ["2026-03-%sT10:00:00Z" % day])

    def test_each_stream_gets_its_own_day_file_and_header(self):
        written = self.sink.write_streams({
            "usage": [valid_row(ts="2026-03-01T10:00:00Z")],
            "context": [context_row(ts="2026-03-01T10:00:00Z")],
            "skills": [skill_row(ts="2026-03-02T09:00:00Z")],
            "catalog": [catalog_row(date="2026-03-01"),
                        catalog_row(date="2026-03-01", kind="agent", name="Explore")],
        })
        day = os.path.join(self.store, "work/2026/03")
        self.assertEqual(sorted(os.path.relpath(path, day) for path in written), [
            "2026-03-01.catalog.csv", "2026-03-01.context.csv",
            "2026-03-01.csv", "2026-03-02.skills.csv",
        ])
        catalog = read_csv(os.path.join(day, "2026-03-01.catalog.csv"))
        self.assertEqual(catalog[0], list(guard.CATALOG_COLUMNS))
        self.assertEqual([line[3:] for line in catalog[1:]],
                         [["skill", "plugin-x:review"], ["agent", "Explore"]])
        context = read_csv(os.path.join(day, "2026-03-01.context.csv"))
        self.assertEqual(context[0], list(guard.CONTEXT_COLUMNS))

    def test_a_problem_in_any_stream_writes_no_stream(self):
        with self.assertRaises(sinks.GuardError) as caught:
            self.sink.write_streams({
                "usage": [valid_row()],
                "context": [context_row(event="free text")],
            })
        self.assertEqual(caught.exception.problems,
                         ["context row 0: column event is invalid"])
        self.assertFalse(os.path.exists(self.store))

    def test_a_sink_that_hashes_names_refuses_slug_names(self):
        sink = sinks.FolderSink(self.store, "work", hashed_names=True)
        with self.assertRaises(sinks.GuardError) as caught:
            sink.write_streams({"skills": [skill_row()]})
        self.assertEqual(caught.exception.problems,
                         ["skills row 0: column skill is invalid"])
        self.assertFalse(os.path.exists(self.store))
        sink.write_streams({"skills": [skill_row(skill="0123456789ab")]})

    def test_sync_is_a_no_op(self):
        self.assertIn("nothing to sync", self.sink.sync(TODAY))


class GitSinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.env = isolated_git_env(self.tmp)
        self.remote = self.tmp.join("remote.git")
        self.git("init", "--bare", "-b", "main", self.remote)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(
            ("git",) + args, cwd=cwd, env=self.env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout

    def clone(self, name, account):
        path = self.tmp.join(name)
        self.git("clone", "-q", self.remote, path)
        return sinks.GitSink(path, account, "origin", "main", True, env=self.env)

    def remote_file(self, path):
        return self.git("--git-dir", self.remote, "show", "main:" + path)

    def test_sync_commits_and_pushes_rows(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row(ts="2026-03-01T10:00:00Z")])
        sink.sync(TODAY)
        self.assertIn("2026-03-01T10:00:00Z", self.remote_file("work/2026/03/2026-03-01.csv"))
        message = self.git("--git-dir", self.remote, "log", "-1", "--format=%s", "main")
        self.assertEqual(message.strip(), "usage: work 2026-03-10")

    def test_sync_without_changes_makes_no_commit(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row()])
        sink.sync(TODAY)
        sink.sync(TODAY)
        count = self.git("--git-dir", self.remote, "rev-list", "--count", "main")
        self.assertEqual(count.strip(), "1")

    def test_two_accounts_push_without_conflict(self):
        work, personal = self.clone("a", "work"), self.clone("b", "personal")
        work.write_rows([valid_row()])
        personal.write_rows([valid_row(account="personal")])
        work.sync(TODAY)
        personal.sync(TODAY)
        self.assertIn("work", self.remote_file("work/2026/01/2026-01-02.csv"))
        self.assertIn("personal", self.remote_file("personal/2026/01/2026-01-02.csv"))

    def test_two_machines_appending_same_day_file_merge(self):
        first, second = self.clone("a", "work"), self.clone("b", "work")
        first.write_rows([valid_row(ts="2026-01-02T01:00:00Z")])
        first.sync(TODAY)
        second.write_rows([valid_row(ts="2026-01-02T02:00:00Z")])
        second.sync(TODAY)
        first.write_rows([valid_row(ts="2026-01-02T03:00:00Z")])
        first.sync(TODAY)
        lines = self.remote_file("work/2026/01/2026-01-02.csv").splitlines()
        self.assertEqual([line.split(",")[0] for line in lines], [
            "ts", "2026-01-02T01:00:00Z", "2026-01-02T02:00:00Z", "2026-01-02T03:00:00Z",
        ])

    def test_two_machines_on_one_account_both_push_their_checks(self):
        first, second = self.clone("a", "work"), self.clone("b", "work")
        first.write_check("2026-03", TOTALS, "laptop-1")
        first.sync(TODAY)
        second.write_check("2026-03", TOTALS, "laptop-2")
        second.write_rows([valid_row()])
        second.sync(TODAY)
        for machine in ("laptop-1", "laptop-2"):
            self.assertIn("claude-opus-5-5",
                          self.remote_file("checks/work/%s/2026-03.json" % machine))
        self.assertIn("work", self.remote_file("work/2026/01/2026-01-02.csv"))

    def test_conflict_aborts_rebase_keeps_local_commit_and_raises(self):
        first, second = self.clone("a", "work"), self.clone("b", "work")
        first.write_check("2026-03", TOTALS, "laptop-1")
        first.sync(TODAY)
        changed = json.loads(json.dumps(TOTALS))
        changed["2026-03-01"]["claude-opus-5-5"]["input"] = 9
        path = second.write_check("2026-03", changed, "laptop-1")
        with self.assertRaises(sinks.SyncError):
            second.sync(TODAY)
        git_dir = os.path.join(second.path, ".git")
        self.assertFalse(os.path.exists(os.path.join(git_dir, "rebase-merge")))
        self.assertFalse(os.path.exists(os.path.join(git_dir, "rebase-apply")))
        subject = self.git("log", "-1", "--format=%s", cwd=second.path)
        self.assertEqual(subject.strip(), "usage: work 2026-03-10")
        with open(path) as handle:
            self.assertEqual(json.load(handle), changed)

    def test_sync_commits_only_collector_paths(self):
        sink = self.clone("a", "work")
        other = os.path.join(sink.path, "personal", "2026", "01", "2026-01-02.csv")
        sinks.FolderSink(sink.path, "personal").write_rows([valid_row(account="personal")])
        self.git("add", "personal", cwd=sink.path)
        sink.write_rows([valid_row()])
        sink.sync(TODAY)
        listed = self.git("--git-dir", self.remote, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("personal", listed)
        staged = self.git("diff", "--cached", "--name-only", cwd=sink.path)
        self.assertEqual(staged.strip(), os.path.relpath(other, sink.path))

    def assert_nothing_left(self, sink):
        branches = self.git("--git-dir", self.remote, "branch", "--list")
        self.assertEqual(branches.strip(), "")
        local = subprocess.run(("git", "rev-parse", "--verify", "-q", "HEAD"), cwd=sink.path,
                               env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(local.returncode, 0)

    def test_an_untracked_stray_file_stops_the_commit_and_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row()])
        os.makedirs(os.path.join(sink.path, "drafts"))
        with open(os.path.join(sink.path, "drafts", "notes.txt"), "w") as handle:
            handle.write(CANARY + "\n")
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems,
                         ["drafts/notes.txt: not part of the store layout"])
        self.assert_nothing_left(sink)

    def test_a_file_staged_by_hand_stops_the_commit_and_push(self):
        sink = self.clone("a", "work")
        with open(os.path.join(sink.path, "notes.txt"), "w") as handle:
            handle.write("staged by hand\n")
        self.git("add", "notes.txt", cwd=sink.path)
        sink.write_rows([valid_row()])
        with self.assertRaises(audit.AuditError):
            sink.sync(TODAY)
        self.assert_nothing_left(sink)

    def test_a_tampered_committed_file_is_audited_in_full_before_the_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row(ts="2026-01-02T01:00:00Z")])
        sink.sync(TODAY)
        path = os.path.join(sink.path, "work/2026/01/2026-01-02.csv")
        with open(path) as handle:
            text = handle.read()
        with open(path, "w") as handle:
            handle.write(text.replace("claude-opus-5-5", CANARY + " private", 1))
        sink.write_rows([valid_row(ts="2026-01-02T02:00:00Z")])
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        problems = "\n".join(caught.exception.problems)
        self.assertEqual(problems, "work/2026/01/2026-01-02.csv: row 0: column model is invalid")
        self.assertNotIn(CANARY, problems + str(caught.exception))
        count = self.git("--git-dir", self.remote, "rev-list", "--count", "main")
        self.assertEqual(count.strip(), "1")
        local = self.git("rev-list", "--count", "HEAD", cwd=sink.path)
        self.assertEqual(local.strip(), "1")

    def test_slug_names_stop_the_push_when_names_are_hashed(self):
        path = self.tmp.join("a")
        self.git("clone", "-q", self.remote, path)
        sinks.FolderSink(path, "work").write_streams({"skills": [skill_row()]})
        sink = sinks.GitSink(path, "work", "origin", "main", True,
                             env=self.env, hashed_names=True)
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems, [
            "work/2026/01/2026-01-02.skills.csv: skills row 0: column skill is invalid"])
        self.assert_nothing_left(sink)

    def commit_by_hand(self, sink, relative, text=None, link=None):
        path = os.path.join(sink.path, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if link:
            os.symlink(link, path)
        else:
            with open(path, "w") as handle:
                handle.write(text)
        self.git("add", "--", relative, cwd=sink.path)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "-m", "by hand", cwd=sink.path)

    def remote_count(self):
        return self.git("--git-dir", self.remote, "rev-list", "--count", "main").strip()

    def test_a_local_commit_made_by_hand_stops_the_first_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row()])
        self.commit_by_hand(sink, "notes.txt", CANARY + "\n")
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems, ["notes.txt: not part of the store layout"])
        self.assertEqual(self.git("--git-dir", self.remote, "branch", "--list").strip(), "")

    def test_a_local_commit_made_by_hand_stops_a_later_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row(ts="2026-01-02T01:00:00Z")])
        sink.sync(TODAY)
        self.commit_by_hand(sink, "drafts/notes.txt", CANARY + "\n")
        sink.write_rows([valid_row(ts="2026-01-02T02:00:00Z")])
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems,
                         ["drafts/notes.txt: not part of the store layout"])
        self.assertEqual(self.remote_count(), "1")

    def test_a_committed_link_stops_the_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row()])
        self.commit_by_hand(sink, "work/2026/01/2026-01-03.csv", link="/etc/hosts")
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems,
                         ["work/2026/01/2026-01-03.csv: is a link, not a file"])

    def test_the_push_audit_reads_what_git_commits_not_the_working_tree(self):
        sink = self.clone("a", "work")
        with open(os.path.join(sink.path, ".git", "info", "attributes"), "w") as handle:
            handle.write("*.csv filter=leak\n")
        self.git("config", "filter.leak.clean",
                 "sed -e 's/claude-opus-5-5/%s private/'" % CANARY, cwd=sink.path)
        sink.write_rows([valid_row()])
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        problems = "\n".join(caught.exception.problems)
        self.assertEqual(problems, "work/2026/01/2026-01-02.csv: row 0: column model is invalid")
        self.assertNotIn(CANARY, problems + str(caught.exception))
        self.assertEqual(self.git("--git-dir", self.remote, "branch", "--list").strip(), "")

    def test_a_modified_gitattributes_stops_the_push(self):
        sink = self.clone("a", "work")
        sink.write_rows([valid_row()])
        with open(os.path.join(sink.path, ".gitattributes"), "w") as handle:
            handle.write("*.csv merge=union\n*.txt -diff\n")
        with self.assertRaises(audit.AuditError) as caught:
            sink.sync(TODAY)
        self.assertEqual(caught.exception.problems,
                         [".gitattributes: content is not what the tool writes"])
        self.assert_nothing_left(sink)

    def test_destination_that_is_not_a_repo_raises(self):
        sink = sinks.GitSink(self.tmp.join("plain"), "work", "origin", "main", True, env=self.env)
        sink.write_rows([valid_row()])
        with self.assertRaises(sinks.SyncError):
            sink.sync(TODAY)


class ToolRepoSeparationTest(unittest.TestCase):
    """Usage data must never land in, or push to, the tool's own repo."""

    def setUp(self):
        self.tmp = support.TempDir()
        self.env = isolated_git_env(self.tmp)
        self.tool = self.tmp.join("tool")
        self.git("init", "-q", "-b", "main", self.tool)
        self.git("remote", "add", "origin",
                 "git@github.com:example-user/claude-usage-collector.git", cwd=self.tool)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        subprocess.run(("git",) + args, cwd=cwd, env=self.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def data_clone(self, remote_url, name="data"):
        path = self.tmp.join(name)
        self.git("init", "-q", "-b", "main", path)
        self.git("remote", "add", "origin", remote_url, cwd=path)
        return sinks.GitSink(path, "work", "origin", "main", True,
                             env=self.env, tool_root=self.tool)

    def test_folder_inside_the_tool_repo_is_refused(self):
        sink = sinks.FolderSink(os.path.join(self.tool, "data"), "work",
                                tool_root=self.tool)
        with self.assertRaises(sinks.SyncError) as caught:
            sink.check_ready()
        self.assertIn("tool", str(caught.exception))

    def test_git_destination_pushing_to_the_tool_repo_is_refused(self):
        urls = ("git@github.com:example-user/claude-usage-collector.git",
                "https://github.com/example-user/claude-usage-collector/")
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                sink = self.data_clone(url, name="data%d" % index)
                with self.assertRaises(sinks.SyncError) as caught:
                    sink.check_ready()
                self.assertIn("tool", str(caught.exception))

    def test_separate_private_repo_is_accepted(self):
        sink = self.data_clone("git@github.com:example-user/usage-data.git")
        sink.check_ready()


class MakeSinkTest(unittest.TestCase):
    def test_builds_sink_from_destination_config(self):
        folder = sinks.make_sink({"account": "work", "destination": {
            "type": "folder", "path": "~/usage-data"}})
        self.assertIsInstance(folder, sinks.FolderSink)
        self.assertEqual(folder.path, os.path.expanduser("~/usage-data"))
        git = sinks.make_sink({"account": "work", "destination": {
            "type": "git", "path": "/data", "remote": "up",
            "branch": "trunk", "push": False}})
        self.assertIsInstance(git, sinks.GitSink)
        self.assertEqual((git.remote, git.branch, git.push), ("up", "trunk", False))

    def test_sinks_hash_names_unless_the_config_turns_it_off(self):
        for destination in ({"type": "folder", "path": "/data"},
                            {"type": "git", "path": "/data"}):
            base = {"account": "work", "destination": destination}
            with self.subTest(destination=destination["type"]):
                self.assertIs(sinks.make_sink(base).hashed_names, True)
                off = dict(base, hash_names=False)
                self.assertIs(sinks.make_sink(off).hashed_names, False)


if __name__ == "__main__":
    unittest.main()
