"""Fail-closed validation of everything that leaves the machine.

Problems describe where a value failed (row index and column) and never
include the value itself, so a rejected row cannot leak through the error.
"""
import re
from datetime import datetime

COLUMNS = (
    "ts", "account", "machine", "project", "session", "agent",
    "is_subagent", "agent_type", "spawn_depth", "is_fork", "model",
    "effort", "input", "cache_read", "cache_write", "output", "context",
    "web_search", "web_fetch", "tools", "compacted",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROW_INT_LIMIT = 10 ** 9
TOTAL_INT_LIMIT = 10 ** 13
CHECK_KEYS = ("input", "output", "cache_read", "cache_write")

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
HASH = re.compile(r"[0-9a-f]{12}")
INT = re.compile(r"0|[1-9][0-9]{0,9}")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_slug(value) -> bool:
    return isinstance(value, str) and SLUG.fullmatch(value) is not None


def is_hash(value) -> bool:
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def is_count(value) -> bool:
    if not isinstance(value, str) or not INT.fullmatch(value):
        return False
    return int(value) <= ROW_INT_LIMIT


def is_timestamp(value) -> bool:
    if not isinstance(value, str) or not TS.fullmatch(value):
        return False
    return _parses(value, "%Y-%m-%dT%H:%M:%SZ")


def is_date(value) -> bool:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return False
    return _parses(value, "%Y-%m-%d")


def _parses(value: str, pattern: str) -> bool:
    try:
        datetime.strptime(value, pattern)
    except ValueError:
        return False
    return True


def _flag(value) -> bool:
    return value in ("0", "1")


def _optional(check):
    return lambda value: value == "" or check(value)


def _tools(value) -> bool:
    if value == "":
        return True
    return isinstance(value, str) and all(map(is_slug, value.split(";")))


VALIDATORS = {
    "ts": is_timestamp,
    "account": is_slug,
    "machine": is_slug,
    "project": is_hash,
    "session": is_hash,
    "agent": _optional(is_hash),
    "is_subagent": _flag,
    "agent_type": _optional(is_slug),
    "spawn_depth": _optional(is_count),
    "is_fork": _flag,
    "model": is_slug,
    "effort": lambda value: value == "" or value in EFFORTS,
    "input": is_count,
    "cache_read": is_count,
    "cache_write": is_count,
    "output": is_count,
    "context": is_count,
    "web_search": is_count,
    "web_fetch": is_count,
    "tools": _tools,
    "compacted": _flag,
}


def validate_rows(rows) -> list:
    problems = []
    for index, row in enumerate(rows):
        problems.extend(_row_problems(index, row))
    return problems


def _row_problems(index: int, row) -> list:
    if not isinstance(row, dict):
        return ["row %d: not a row" % index]
    problems = []
    if any(key not in VALIDATORS for key in row):
        problems.append("row %d: unknown column" % index)
    for column in COLUMNS:
        if column not in row:
            problems.append("row %d: column %s is missing" % (index, column))
        elif not VALIDATORS[column](row[column]):
            problems.append("row %d: column %s is invalid" % (index, column))
    if not problems:
        problems.extend(_consistency_problems(index, row))
    return problems


def _consistency_problems(index: int, row: dict) -> list:
    problems = []
    parts = int(row["input"]) + int(row["cache_read"]) + int(row["cache_write"])
    if int(row["context"]) != parts:
        problems.append("row %d: column context is invalid" % index)
    if row["is_subagent"] == "0" and row["agent"] != "":
        problems.append("row %d: column agent is invalid" % index)
    return problems


def validate_check_totals(totals) -> list:
    if not isinstance(totals, dict):
        return ["totals: not an object"]
    problems = []
    for day_index, (date, models) in enumerate(totals.items()):
        where = "day %d" % day_index
        if not is_date(date):
            problems.append("%s: date is invalid" % where)
        if not isinstance(models, dict):
            problems.append("%s: models is not an object" % where)
            continue
        for model_index, (model, counts) in enumerate(models.items()):
            entry = "%s model %d" % (where, model_index)
            problems.extend(_check_entry_problems(entry, model, counts))
    return problems


def _check_entry_problems(entry: str, model, counts) -> list:
    problems = []
    if not is_slug(model):
        problems.append("%s: model is invalid" % entry)
    if not isinstance(counts, dict) or set(counts) != set(CHECK_KEYS):
        return problems + ["%s: counts have the wrong keys" % entry]
    for key in CHECK_KEYS:
        if not _is_total(counts[key]):
            problems.append("%s: %s is invalid" % (entry, key))
    return problems


def _is_total(value) -> bool:
    is_int = isinstance(value, int) and not isinstance(value, bool)
    return is_int and 0 <= value <= TOTAL_INT_LIMIT
