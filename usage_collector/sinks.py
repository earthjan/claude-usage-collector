"""Where guarded rows go: a plain folder, or a folder that is a git clone."""
import csv
import json
import os
import subprocess

from . import audit, guard, periods
from .audit import GIT_ATTRIBUTES

FILE_SUFFIXES = {
    "usage": ".csv", "context": ".context.csv",
    "skills": ".skills.csv", "catalog": ".catalog.csv",
}
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
    hashed_names = config.get("hash_names", True)
    if destination["type"] == "git":
        return GitSink(
            path, config["account"], destination.get("remote", "origin"),
            destination.get("branch", "main"), destination.get("push", True),
            env=env, hashed_names=hashed_names,
        )
    return FolderSink(path, config["account"], hashed_names)


class FolderSink:
    def __init__(self, path: str, account: str, hashed_names=False):
        self.path = path
        self.account = account
        self.hashed_names = hashed_names

    def check_ready(self) -> None:
        pass

    def write_rows(self, rows) -> list:
        return self.write_streams({"usage": rows})

    def write_streams(self, batches: dict) -> list:
        """Writes every day file of every stream or none, so a retry never repeats rows."""
        problems = guard.validate_streams(batches, self.hashed_names)
        if problems:
            raise GuardError(problems)
        staged = []
        try:
            for stream, rows in batches.items():
                for day, day_rows in sorted(_group_by_day(rows).items()):
                    staged.append(self._stage_day(stream, day, day_rows))
        except OSError:
            _remove_temps(staged)
            raise
        for path, temp in staged:
            os.replace(temp, path)
        return [path for path, _temp in staged]

    def _stage_day(self, stream: str, day: str, rows) -> tuple:
        year, month = day[:4], day[5:7]
        name = day + FILE_SUFFIXES[stream]
        path = os.path.join(self.path, self.account, year, month, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        existing = _read_bytes(path)
        with open(temp, "wb") as handle:
            handle.write(existing)
        with open(temp, "a", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=guard.SCHEMAS[stream].columns, lineterminator="\n"
            )
            if not existing:
                writer.writeheader()
            writer.writerows(rows)
        return path, temp

    def write_check(self, period: str, totals: dict, machine: str) -> str:
        problems = guard.validate_check_totals(totals)
        if not periods.is_period(period):
            problems.append("period is invalid")
        if not guard.is_slug(machine):
            problems.append("machine is invalid")
        if problems:
            raise GuardError(problems)
        path = os.path.join(self.path, "checks", self.account, machine, period + ".json")
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
        day = row["date"] if "date" in row else row["ts"][:10]
        days.setdefault(day, []).append(row)
    return days


class GitSink(FolderSink):
    def __init__(self, path, account, remote, branch, push, env=None, hashed_names=False):
        super().__init__(path, account, hashed_names)
        self.remote = remote
        self.branch = branch
        self.push = push
        self.env = env

    def check_ready(self) -> None:
        if not os.path.isdir(os.path.join(self.path, ".git")):
            raise SyncError("destination is not a git clone, clone your data repo there")

    def sync(self, today) -> str:
        self.check_ready()
        self._audit()
        self._ensure_union_merge()
        committed = self._commit(today)
        base = self._pull() if self._remote_branch_exists() else None
        if self.push:
            self._audit_outgoing(base)
            self._git_or_fail("push failed", "push", "-q", self.remote, "HEAD:" + self.branch)
        return "committed new rows" if committed else "no new rows to commit"

    def _audit_outgoing(self, base) -> None:
        """Checks every file the push carries, as git stored it, not as it sits on disk."""
        if self._git("rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            return
        entries = self._tree_entries()
        if base:
            changed = set(self._changed_since(base))
            entries = [entry for entry in entries if entry[1] in changed]
        problems = audit.audit_committed(entries, self._read_blob, self.hashed_names)
        if problems:
            raise audit.AuditError(problems)

    def _tree_entries(self) -> list:
        result = self._git_or_fail("git ls-tree failed", "ls-tree", "-r", "-z", "HEAD")
        entries = []
        for line in result.stdout.split("\0"):
            if line:
                meta, relative = line.split("\t", 1)
                mode, _kind, object_id = meta.split(" ")
                entries.append((mode, relative, object_id))
        return entries

    def _changed_since(self, base: str) -> list:
        result = self._git_or_fail(
            "git diff failed", "diff", "--name-only", "-z", "--no-renames",
            "--diff-filter=d", base, "HEAD",
        )
        return [path for path in result.stdout.split("\0") if path]

    def _read_blob(self, object_id: str) -> bytes:
        result = subprocess.run(
            ("git", "cat-file", "blob", object_id), cwd=self.path, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise SyncError("git cat-file failed")
        return result.stdout

    def _audit(self) -> None:
        """Checks every file a commit could carry, in full, before anything is committed."""
        problems = audit.audit_paths(self.path, self._pending_paths(), self.hashed_names)
        if problems:
            raise audit.AuditError(problems)

    def _pending_paths(self) -> list:
        result = self._git_or_fail(
            "git status failed", "status", "--porcelain", "-z", "-uall", "--no-renames"
        )
        return [entry[3:] for entry in result.stdout.split("\0") if entry]

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

    def _pull(self) -> str:
        """Rebases onto the remote branch and returns the remote tip it fetched."""
        result = self._git(*GIT_IDENTITY, "pull", "-q", "--rebase", self.remote, self.branch)
        if result.returncode != 0:
            self._git("rebase", "--abort")
            raise SyncError("pull --rebase failed, rebase aborted, will retry next run")
        tip = self._git_or_fail("git rev-parse failed", "rev-parse", "FETCH_HEAD")
        return tip.stdout.strip()

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
