"""An independent check of the files in a store, run before anything is pushed.

It reads each file back from disk, checks its path against the store layout
and its contents against the guard, and names problems by relative path,
never by value.
"""
import csv
import io
import json
import os
import re

from . import guard, periods

GIT_ATTRIBUTES = "*.csv merge=union\n"
GIT_FOLDER = ".git"
LINK_MODE = "120000"
REPO_MODE = "160000"
STREAM_SUFFIXES = {
    ".csv": "usage", ".context.csv": "context",
    ".skills.csv": "skills", ".catalog.csv": "catalog",
}
DAY_FILE = re.compile(
    r"(?P<account>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})/"
    r"(?P<day>(?P=year)-(?P=month)-\d{2})"
    r"(?P<suffix>\.csv|\.context\.csv|\.skills\.csv|\.catalog\.csv)"
)
CHECK_FILE = re.compile(
    r"checks/(?P<account>[^/]+)/(?P<machine>[^/]+)/(?P<period>[^/]+)\.json"
)
OLD_CHECK_FILE = re.compile(r"checks/(?P<account>[^/]+)/(?P<period>[^/]+)\.json")


class AuditError(Exception):
    def __init__(self, problems):
        super().__init__("%d problem(s) found, nothing committed or pushed" % len(problems))
        self.problems = problems


def audit_store(store: str, hashed_names=False) -> list:
    return audit_paths(store, store_files(store), hashed_names)


def audit_paths(store: str, relative_paths, hashed_names=False) -> list:
    problems = []
    for relative in sorted(set(relative_paths)):
        problems.extend(FileAudit(store, relative, hashed_names).problems())
    return problems


def store_files(store: str) -> list:
    """Every file and link in the store, as relative paths, leaving out the git folder."""
    found = []
    for folder, dirs, files in os.walk(store):
        if folder == store:
            dirs[:] = [name for name in dirs if name != GIT_FOLDER]
        dirs.sort()
        names = files + [name for name in dirs if os.path.islink(os.path.join(folder, name))]
        found.extend(_relative(store, os.path.join(folder, name)) for name in names)
    return found


def _relative(store: str, path: str) -> str:
    return os.path.relpath(path, store).replace(os.sep, "/")


def audit_committed(entries, read_blob, hashed_names=False) -> list:
    """Checks files as git will push them.

    entries are (mode, relative path, object id) from git ls-tree, and
    read_blob returns the bytes of an object id.
    """
    problems = []
    for mode, relative, object_id in sorted(entries, key=lambda entry: entry[1]):
        if mode == LINK_MODE:
            problems.append("%s: is a link, not a file" % relative)
        elif mode == REPO_MODE:
            problems.append("%s: is an embedded repository" % relative)
        else:
            content = ContentAudit(relative, read_blob(object_id), hashed_names)
            problems.extend(content.problems())
    return problems


class FileAudit:
    """Checks one file on disk: a link fails, anything else by its contents."""

    def __init__(self, store: str, relative: str, hashed_names=False):
        self.relative = relative
        self.hashed_names = hashed_names
        self.path = os.path.join(store, *relative.split("/"))

    def problems(self) -> list:
        if os.path.islink(self.path):
            return ["%s: is a link, not a file" % self.relative]
        if not os.path.exists(self.path):
            return []
        data = _read_bytes(self.path)
        return ContentAudit(self.relative, data, self.hashed_names).problems()


class ContentAudit:
    """Checks one file's bytes: its path first, then its contents by kind."""

    def __init__(self, relative: str, data: bytes, hashed_names=False):
        self.relative = relative
        self.data = data
        self.hashed_names = hashed_names

    def problems(self) -> list:
        check = self._content_check()
        if check is None:
            return [self._problem("not part of the store layout")]
        return [self._problem(problem) for problem in check()]

    def _problem(self, problem: str) -> str:
        return "%s: %s" % (self.relative, problem)

    def _content_check(self):
        if self.relative == ".gitattributes":
            return self._gitattributes_problems
        day = DAY_FILE.fullmatch(self.relative)
        if day and _is_day_file(day):
            stream = STREAM_SUFFIXES[day.group("suffix")]
            return lambda: self._csv_problems(stream)
        if _is_check_file(self.relative):
            return self._check_problems
        return None

    def _gitattributes_problems(self) -> list:
        if self.data == GIT_ATTRIBUTES.encode():
            return []
        return ["content is not what the tool writes"]

    def _csv_problems(self, stream: str) -> list:
        try:
            text = self.data.decode("utf-8")
            lines = list(csv.reader(io.StringIO(text, newline="")))
        except (UnicodeDecodeError, csv.Error):
            return ["could not be read as CSV"]
        columns = guard.SCHEMAS[stream].columns
        if not lines or tuple(lines[0]) != columns:
            return ["header does not match its schema"]
        return guard.validate_stream(stream, _rows(columns, lines[1:]), self.hashed_names)

    def _check_problems(self) -> list:
        try:
            totals = json.loads(self.data.decode("utf-8"), object_pairs_hook=_unique_keys)
        except DuplicateKey:
            return ["has a duplicate key"]
        except ValueError:
            return ["could not be read as JSON"]
        return guard.validate_check_totals(totals)


class DuplicateKey(Exception):
    pass


def _unique_keys(pairs) -> dict:
    """A JSON object that refuses a key seen twice, so no value hides behind another."""
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise DuplicateKey()
    return dict(pairs)


def _is_day_file(found) -> bool:
    return _is_account(found.group("account")) and guard.is_date(found.group("day"))


def _is_check_file(relative: str) -> bool:
    found = CHECK_FILE.fullmatch(relative)
    if found:
        return _is_account(found.group("account")) and guard.is_slug(
            found.group("machine")) and periods.is_period(found.group("period"))
    return _is_old_month_check(OLD_CHECK_FILE.fullmatch(relative))


def _is_old_month_check(found) -> bool:
    """A month file written straight into the account folder by an older version."""
    if found is None:
        return False
    return _is_account(found.group("account")) and periods.is_month(found.group("period"))


def _is_account(name: str) -> bool:
    return guard.is_slug(name) and name != "checks"


def _rows(columns, lines) -> list:
    """Each line as a row: surplus cells go under an unknown column, missing ones are absent."""
    rows = []
    for line in lines:
        row = dict(zip(columns, line))
        if len(line) > len(columns):
            row[None] = line[len(columns):]
        rows.append(row)
    return rows


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
