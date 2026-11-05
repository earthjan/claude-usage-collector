"""Cross-check collected rows against ccusage daily totals.

Only the four token counts per day and model are kept from ccusage output.
Everything else it prints (costs, project names, unknown fields) is dropped
before the guard sees it.
"""
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from . import guard
from .config import DEFAULTS
from .sinks import GuardError, make_sink
from .store import RowReader, StoreError, read_checks, resolve_store

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
CCUSAGE_FIELDS = (
    ("input", "inputTokens"), ("output", "outputTokens"),
    ("cache_write", "cacheCreationTokens"), ("cache_read", "cacheReadTokens"),
)
SKIPPED_MODELS = ("<synthetic>",)
TIMEOUT_SECONDS = 600


class CrosscheckError(Exception):
    pass


def is_month(value) -> bool:
    return isinstance(value, str) and MONTH.fullmatch(value) is not None


def month_bounds(month: str) -> tuple:
    year, number = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, number)[1]
    return "%04d%02d01" % (year, number), "%04d%02d%02d" % (year, number, last)


def previous_month(today) -> str:
    if today.month == 1:
        return "%04d-12" % (today.year - 1)
    return "%04d-%02d" % (today.year, today.month - 1)


def reduce_daily(data, month: str) -> dict:
    totals = {}
    entries = data.get("daily") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if not _entry_in_month(entry, month):
            continue
        for breakdown in _breakdowns(entry):
            _add_breakdown(totals.setdefault(entry["date"], {}), breakdown)
    return {day: models for day, models in totals.items() if models}


def _entry_in_month(entry, month: str) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("date"), str):
        return False
    return entry["date"].startswith(month + "-")


def _breakdowns(entry: dict) -> list:
    found = entry.get("modelBreakdowns")
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def _add_breakdown(models: dict, breakdown: dict) -> None:
    model = breakdown.get("modelName")
    if not isinstance(model, str) or model in SKIPPED_MODELS:
        return
    counts = models.setdefault(model, {key: 0 for key, _source in CCUSAGE_FIELDS})
    for key, source in CCUSAGE_FIELDS:
        counts[key] += _count(breakdown.get(source))


def _count(value) -> int:
    is_int = isinstance(value, int) and not isinstance(value, bool)
    return value if is_int and value >= 0 else 0


def claude_config_dirs(sources) -> str:
    """Map each `<dir>/projects` source to `<dir>` for CLAUDE_CONFIG_DIR."""
    dirs = []
    for source in sources:
        folder = os.path.normpath(os.path.abspath(os.path.expanduser(source)))
        if os.path.basename(folder) != "projects":
            raise CrosscheckError(
                "crosscheck needs every source to be a Claude config's projects folder"
            )
        dirs.append(os.path.dirname(folder))
    return ",".join(dirs)


def run_ccusage(command: str, month: str, environ, sources) -> dict:
    base = os.environ if environ is None else environ
    env = dict(base, CLAUDE_CONFIG_DIR=claude_config_dirs(sources))
    path = shutil.which(command, path=env.get("PATH"))
    if not path:
        raise CrosscheckError("ccusage command not found on PATH")
    since, until = month_bounds(month)
    argv = [path, "daily", "--json", "--since", since, "--until", until,
            "--timezone", "UTC"]
    try:
        result = subprocess.run(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CrosscheckError("ccusage could not be run") from None
    if result.returncode != 0:
        raise CrosscheckError("ccusage exited with status %d" % result.returncode)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise CrosscheckError("ccusage did not print JSON") from None


def cmd_crosscheck(args, config, machine, environ=None, today=None) -> int:
    month = args.month or previous_month(today or datetime.now(timezone.utc).date())
    try:
        if not is_month(month):
            raise CrosscheckError("--month must be YYYY-MM")
        command = config["crosscheck"].get("command", "ccusage")
        sources = config.get("sources", DEFAULTS["sources"])
        totals = reduce_daily(run_ccusage(command, month, environ, sources), month)
        path = make_sink(config, env=environ).write_check(month, totals, machine)
    except CrosscheckError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    except GuardError as error:
        _print_guard(error)
        return 1
    print("wrote %d day(s) of cross-check totals for %s to %s" % (len(totals), month, path))
    return 0


def _print_guard(error: GuardError) -> None:
    for problem in error.problems:
        print("guard: %s" % problem, file=sys.stderr)
    print("error: %s" % error, file=sys.stderr)


class Comparison:
    """Compares summed rows with check totals per account, day and model."""

    HEADER = ("account", "date", "model", "field", "rows", "ccusage", "gap")

    def __init__(self, store: str, tolerance: float):
        self.store = store
        self.tolerance = tolerance
        self.lines = []
        self.compared = 0
        self.over = 0

    def run(self, checks: dict) -> None:
        for (account, month), totals in sorted(checks.items()):
            sums = self._row_sums(account, month)
            for day in sorted(set(totals) | set(sums)):
                self._compare_day(account, day, totals.get(day, {}), sums.get(day, {}))

    def _row_sums(self, account: str, month: str) -> dict:
        sums = {}
        for row in RowReader(self.store, account=account).read():
            if row["ts"][:7] != month:
                continue
            day = sums.setdefault(row["ts"][:10], {})
            counts = day.setdefault(row["model"], dict.fromkeys(guard.CHECK_KEYS, 0))
            for key in guard.CHECK_KEYS:
                counts[key] += row[key]
        return sums

    def _compare_day(self, account, day, checked: dict, summed: dict) -> None:
        empty = dict.fromkeys(guard.CHECK_KEYS, 0)
        for model in sorted(set(checked) | set(summed)):
            self.compared += 1
            expected = checked.get(model, empty)
            actual = summed.get(model, empty)
            for key in guard.CHECK_KEYS:
                where = (account, day, model, key)
                self._compare_value(where, actual[key], expected[key])

    def _compare_value(self, where: tuple, actual: int, expected: int) -> None:
        if actual == expected:
            return
        gap = (actual - expected) / expected if expected else 1.0
        over = abs(gap) > self.tolerance
        self.over += over
        mark = "%+.1f%%%s" % (gap * 100, " !" if over else "")
        self.lines.append(where + (str(actual), str(expected), mark))

    def print_table(self) -> None:
        if self.lines:
            table = [self.HEADER] + self.lines
            widths = [max(len(line[i]) for line in table) for i in range(len(self.HEADER))]
            for line in table:
                print("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip())
        print("compared %d day/model pair(s), %d gap(s), %d over tolerance (%.1f%%)"
              % (self.compared, len(self.lines), self.over, self.tolerance * 100))


def cmd_compare(args, config) -> int:
    try:
        store = resolve_store(args.store, config)
        if args.month is not None and not is_month(args.month):
            raise StoreError("--month must be YYYY-MM")
    except StoreError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    checks = read_checks(store, args.month)
    if not checks:
        print("error: no cross-check files found, run crosscheck first", file=sys.stderr)
        return 1
    comparison = Comparison(store, args.tolerance)
    comparison.run(checks)
    comparison.print_table()
    return 1 if comparison.over else 0
