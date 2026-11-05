"""Cross-check collected rows against ccusage daily totals.

Only the four token counts per day and model are kept from ccusage output.
Everything else it prints (costs, project names, unknown fields) is dropped
before the guard sees it.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from . import guard, periods
from .config import DEFAULTS
from .models import model_slug
from .periods import is_month, previous_month
from .sinks import GuardError, make_sink
from .store import RowReader, StoreError, read_checks, resolve_store

CCUSAGE_FIELDS = (
    ("input", "inputTokens"), ("output", "outputTokens"),
    ("cache_write", "cacheCreationTokens"), ("cache_read", "cacheReadTokens"),
)
SKIPPED_MODELS = ("<synthetic>",)
TIMEOUT_SECONDS = 600


class CrosscheckError(Exception):
    pass


def month_bounds(month: str) -> tuple:
    return ccusage_bounds(month)


def ccusage_bounds(period: str) -> tuple:
    return tuple(day.strftime("%Y%m%d") for day in periods.bounds(period))


def reduce_daily(data, period: str, salt: bytes) -> dict:
    first, last = (day.isoformat() for day in periods.bounds(period))
    totals = {}
    entries = data.get("daily") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if not _entry_in_range(entry, first, last):
            continue
        for breakdown in _breakdowns(entry):
            _add_breakdown(totals.setdefault(entry["date"], {}), breakdown, salt)
    return {day: models for day, models in totals.items() if models}


def _entry_in_range(entry, first: str, last: str) -> bool:
    if not isinstance(entry, dict) or not guard.is_date(entry.get("date")):
        return False
    return first <= entry["date"] <= last


def _breakdowns(entry: dict) -> list:
    found = entry.get("modelBreakdowns")
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def _add_breakdown(models: dict, breakdown: dict, salt: bytes) -> None:
    name = breakdown.get("modelName")
    if not isinstance(name, str) or name in SKIPPED_MODELS:
        return
    counts = models.setdefault(model_slug(name, salt), {key: 0 for key, _source in CCUSAGE_FIELDS})
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


def run_ccusage(command: str, period: str, environ, sources) -> dict:
    base = os.environ if environ is None else environ
    env = dict(base, CLAUDE_CONFIG_DIR=claude_config_dirs(sources))
    path = shutil.which(command, path=env.get("PATH"))
    if not path:
        raise CrosscheckError("ccusage command not found on PATH")
    since, until = ccusage_bounds(period)
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


def cmd_crosscheck(args, config, machine, salt, environ=None, today=None) -> int:
    today = today or datetime.now(timezone.utc).date()
    try:
        period = chosen_period(args, config, today)
        command = config["crosscheck"].get("command", "ccusage")
        sources = config.get("sources", DEFAULTS["sources"])
        data = run_ccusage(command, period, environ, sources)
        totals = reduce_daily(data, period, salt)
        path = make_sink(config, env=environ).write_check(period, totals, machine)
    except CrosscheckError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    except GuardError as error:
        _print_guard(error)
        return 1
    print("wrote %d day(s) of cross-check totals for %s to %s" % (len(totals), period, path))
    return 0


def chosen_period(args, config, today) -> str:
    """The --month or --week asked for, else the last completed one of the configured period."""
    month, week = getattr(args, "month", None), getattr(args, "week", None)
    if month and week:
        raise CrosscheckError("give --month or --week, not both")
    if month and not is_month(month):
        raise CrosscheckError("--month must be YYYY-MM")
    if week and not periods.is_week(week):
        raise CrosscheckError("--week must be YYYY-Www")
    if month or week:
        return month or week
    if config["crosscheck"].get("period", "week") == "month":
        return previous_month(today)
    return periods.previous_week(today)


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
        self._rows = {}

    def run(self, checks: dict) -> None:
        for (account, period), totals in sorted(checks.items()):
            sums = self._row_sums(account, period)
            for day in sorted(set(totals) | set(sums)):
                self._compare_day(account, day, totals.get(day, {}), sums.get(day, {}))

    def _row_sums(self, account: str, period: str) -> dict:
        first, last = (day.isoformat() for day in periods.bounds(period))
        sums = {}
        for row in self._account_rows(account):
            if not first <= row["ts"][:10] <= last:
                continue
            day = sums.setdefault(row["ts"][:10], {})
            counts = day.setdefault(row["model"], dict.fromkeys(guard.CHECK_KEYS, 0))
            for key in guard.CHECK_KEYS:
                counts[key] += row[key]
        return sums

    def _account_rows(self, account: str) -> list:
        if account not in self._rows:
            self._rows[account] = RowReader(self.store, account=account).read()
        return self._rows[account]

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
        period = _compare_filter(args)
    except StoreError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    checks = read_checks(store, period)
    if not checks:
        print("error: no cross-check files found, run crosscheck first", file=sys.stderr)
        return 1
    comparison = Comparison(store, args.tolerance)
    comparison.run(checks)
    comparison.print_table()
    return 1 if comparison.over else 0


def _compare_filter(args):
    month, week = args.month, getattr(args, "week", None)
    if month is not None and week is not None:
        raise StoreError("give --month or --week, not both")
    if month is not None and not is_month(month):
        raise StoreError("--month must be YYYY-MM")
    if week is not None and not periods.is_week(week):
        raise StoreError("--week must be YYYY-Www")
    return month if month is not None else week
