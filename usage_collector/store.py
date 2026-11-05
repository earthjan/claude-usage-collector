"""Read rows and cross-check files back out of a store folder."""
import csv
import json
import os

from . import guard

INT_COLUMNS = (
    "input", "cache_read", "cache_write", "output", "context",
    "web_search", "web_fetch", "is_subagent", "is_fork", "compacted",
)
CHECKS_DIR = "checks"


class StoreError(Exception):
    pass


def resolve_store(explicit, config) -> str:
    if explicit:
        path = explicit
    elif config:
        path = config["destination"]["path"]
    else:
        raise StoreError("no store given, pass --store PATH or run init")
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        raise StoreError("store folder does not exist")
    return path


class RowReader:
    """Loads guarded rows, skipping any row the guard rejects."""

    def __init__(self, store: str, since=None, account=None):
        if since is not None and not guard.is_date(since):
            raise StoreError("--since must be YYYY-MM-DD")
        self.store = store
        self.since = since
        self.account = account
        self.skipped = 0

    def read(self) -> list:
        rows = []
        for path in self._csv_paths():
            rows.extend(self._read_file(path))
        rows.sort(key=lambda row: row["ts"])
        return rows

    def _csv_paths(self) -> list:
        paths = []
        for account in self._accounts():
            top = os.path.join(self.store, account)
            for folder, dirs, files in os.walk(top):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                paths.extend(os.path.join(folder, n) for n in sorted(files) if n.endswith(".csv"))
        return paths

    def _accounts(self) -> list:
        names = sorted(
            name for name in os.listdir(self.store)
            if name != CHECKS_DIR and not name.startswith(".")
            and os.path.isdir(os.path.join(self.store, name))
        )
        return [name for name in names if self.account in (None, name)]

    def _read_file(self, path: str) -> list:
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        kept = []
        for row in rows:
            if guard.validate_rows([row]):
                self.skipped += 1
            elif self._in_range(row):
                kept.append(_typed(row))
        return kept

    def _in_range(self, row: dict) -> bool:
        return self.since is None or row["ts"][:10] >= self.since


def _typed(row: dict) -> dict:
    typed = dict(row)
    for column in INT_COLUMNS:
        typed[column] = int(row[column])
    typed["spawn_depth"] = int(row["spawn_depth"]) if row["spawn_depth"] else None
    return typed


def read_checks(store: str, month=None) -> dict:
    """Returns {(account, month): totals}, adding up the files of every machine."""
    found = {}
    top = os.path.join(store, CHECKS_DIR)
    for account in _subfolders(top):
        folder = os.path.join(top, account)
        for path in _check_paths(folder):
            key = os.path.basename(path)[:-5]
            if month not in (None, key):
                continue
            totals = _load_check(path)
            if totals is not None:
                _add_totals(found.setdefault((account, key), {}), totals)
    return found


def _subfolders(folder: str) -> list:
    if not os.path.isdir(folder):
        return []
    return [name for name in sorted(os.listdir(folder))
            if os.path.isdir(os.path.join(folder, name))]


def _check_paths(account_folder: str) -> list:
    """Per-machine files, plus month files written straight into the account folder."""
    folders = [account_folder] + [
        os.path.join(account_folder, machine) for machine in _subfolders(account_folder)
    ]
    return [
        os.path.join(folder, name) for folder in folders
        for name in sorted(os.listdir(folder))
        if name.endswith(".json") and os.path.isfile(os.path.join(folder, name))
    ]


def _add_totals(into: dict, totals: dict) -> None:
    for day, models in totals.items():
        for model, counts in models.items():
            target = into.setdefault(day, {}).setdefault(
                model, dict.fromkeys(guard.CHECK_KEYS, 0)
            )
            for key in guard.CHECK_KEYS:
                target[key] += counts[key]


def _load_check(path: str):
    try:
        with open(path) as handle:
            totals = json.load(handle)
    except (OSError, ValueError):
        return None
    return None if guard.validate_check_totals(totals) else totals
