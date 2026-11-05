"""Where guarded rows go: a plain folder, or a folder that is a git clone."""
import csv
import json
import os
import subprocess

from . import guard

GIT_ATTRIBUTES = "*.csv merge=union\n"
GIT_IDENTITY = (
    "-c", "user.name=claude-usage-collector",
    "-c", "user.email=claude-usage-collector@users.noreply.github.com",
)


class GuardError(Exception):
    def __init__(self, problems, files=()):
        super().__init__("%d problem(s) found, nothing written" % len(problems))
        self.problems = problems
        self.files = files


class SyncError(Exception):
    pass


def make_sink(config: dict, env=None):
    destination = config["destination"]
    path = os.path.expanduser(destination["path"])
    if destination["type"] == "git":
        return GitSink(
            path, config["account"], destination.get("remote", "origin"),
            destination.get("branch", "main"), destination.get("push", True),
            env=env,
        )
    return FolderSink(path, config["account"])


class FolderSink:
    def __init__(self, path: str, account: str):
        self.path = path
        self.account = account

    def check_ready(self) -> None:
        pass

    def write_rows(self, rows) -> list:
        """Writes every day file or none, so a failed run never leaves rows a retry repeats."""
        problems = guard.validate_rows(rows)
        if problems:
            raise GuardError(problems)
        staged = []
        try:
            for day, day_rows in sorted(_group_by_day(rows).items()):
                staged.append(self._stage_day(day, day_rows))
        except OSError:
            _remove_temps(staged)
            raise
        for path, temp in staged:
            os.replace(temp, path)
        return [path for path, _temp in staged]

    def _stage_day(self, day: str, rows) -> tuple:
        year, month = day[:4], day[5:7]
        path = os.path.join(self.path, self.account, year, month, day + ".csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        existing = _read_bytes(path)
        with open(temp, "wb") as handle:
            handle.write(existing)
        with open(temp, "a", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=guard.COLUMNS, lineterminator="\n"
            )
            if not existing:
                writer.writeheader()
            writer.writerows(rows)
        return path, temp

    def write_check(self, month: str, totals: dict, machine: str) -> str:
        problems = guard.validate_check_totals(totals)
        if not guard.is_date(month + "-01"):
            problems.append("month is invalid")
        if not guard.is_slug(machine):
            problems.append("machine is invalid")
        if problems:
            raise GuardError(problems)
        path = os.path.join(self.path, "checks", self.account, machine, month + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump(totals, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path

    def sync(self, today) -> str:
        return "nothing to sync for a folder destination"


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return b""


def _remove_temps(staged) -> None:
    for _path, temp in staged:
        try:
            os.remove(temp)
        except OSError:
            pass


def _group_by_day(rows) -> dict:
    days = {}
    for row in rows:
        days.setdefault(row["ts"][:10], []).append(row)
    return days


class GitSink(FolderSink):
    def __init__(self, path, account, remote, branch, push, env=None):
        super().__init__(path, account)
        self.remote = remote
        self.branch = branch
        self.push = push
        self.env = env

    def check_ready(self) -> None:
        if not os.path.isdir(os.path.join(self.path, ".git")):
            raise SyncError("destination is not a git clone, clone your data repo there")

    def sync(self, today) -> str:
        self.check_ready()
        self._ensure_union_merge()
        committed = self._commit(today)
        if self._remote_branch_exists():
            self._pull()
        if self.push:
            self._git_or_fail("push failed", "push", "-q", self.remote, "HEAD:" + self.branch)
        return "committed new rows" if committed else "no new rows to commit"

    def _ensure_union_merge(self) -> None:
        path = os.path.join(self.path, ".gitattributes")
        if os.path.exists(path):
            return
        with open(path, "w") as handle:
            handle.write(GIT_ATTRIBUTES)

    def _commit(self, today) -> bool:
        paths = [
            path for path in (
                ".gitattributes", self.account,
                os.path.join("checks", self.account),
            )
            if os.path.exists(os.path.join(self.path, path))
        ]
        self._git_or_fail("git add failed", "add", "--", *paths)
        if self._git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
            return False
        message = "usage: %s %s" % (self.account, today.isoformat())
        self._git_or_fail(
            "commit failed", *GIT_IDENTITY, "commit", "-q", "-m", message, "--", *paths
        )
        return True

    def _remote_branch_exists(self) -> bool:
        result = self._git_or_fail(
            "remote is not reachable", "ls-remote", "--heads", self.remote, self.branch
        )
        return bool(result.stdout.strip())

    def _pull(self) -> None:
        result = self._git(*GIT_IDENTITY, "pull", "-q", "--rebase", self.remote, self.branch)
        if result.returncode != 0:
            self._git("rebase", "--abort")
            raise SyncError("pull --rebase failed, rebase aborted, will retry next run")

    def _git_or_fail(self, reason: str, *args):
        result = self._git(*args)
        if result.returncode != 0:
            raise SyncError(reason)
        return result

    def _git(self, *args):
        return subprocess.run(
            ("git",) + args, cwd=self.path, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
