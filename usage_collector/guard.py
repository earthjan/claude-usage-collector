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
CONTEXT_PARTS = (
    "system_prompt_chars", "instr_user_chars", "instr_project_chars",
    "instr_local_chars", "instr_memory_chars", "instr_other_chars",
    "instr_files", "skill_listing_chars", "skill_count",
    "agent_listing_chars", "agent_count", "deferred_tools_chars",
    "deferred_tool_count", "mcp_chars", "mcp_server_count",
    "hook_context_chars",
)
CONTEXT_COLUMNS = (
    "ts", "account", "machine", "project", "session", "agent",
    "is_subagent", "agent_type", "event",
) + CONTEXT_PARTS + ("first_turn_context",)
SKILL_COLUMNS = ("ts", "account", "machine", "project", "session", "agent", "skill")
CATALOG_COLUMNS = ("date", "account", "machine", "kind", "name")
EVENTS = ("start", "compact", "fork")
CATALOG_KINDS = ("skill", "agent")
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
    "event": lambda value: value in EVENTS,
    "first_turn_context": is_count,
    "skill": is_slug,
    "date": is_date,
    "kind": lambda value: value in CATALOG_KINDS,
    "name": is_slug,
}
VALIDATORS.update((part, is_count) for part in CONTEXT_PARTS)


def _usage_mismatches(row: dict) -> list:
    parts = int(row["input"]) + int(row["cache_read"]) + int(row["cache_write"])
    mismatches = [] if int(row["context"]) == parts else ["context"]
    return mismatches + _agent_mismatches(row)


def _agent_mismatches(row: dict) -> list:
    return ["agent"] if row["is_subagent"] == "0" and row["agent"] != "" else []


class Schema:
    """The columns of one stream and the cross-column rules its rows must meet."""

    def __init__(self, columns, label, mismatches=lambda row: []):
        self.columns = columns
        self.label = label
        self.mismatches = mismatches


SCHEMAS = {
    "usage": Schema(COLUMNS, "row", _usage_mismatches),
    "context": Schema(CONTEXT_COLUMNS, "context row", _agent_mismatches),
    "skills": Schema(SKILL_COLUMNS, "skills row"),
    "catalog": Schema(CATALOG_COLUMNS, "catalog row"),
}
STREAMS = tuple(SCHEMAS)


def validate_rows(rows) -> list:
    return validate_stream("usage", rows)


def validate_streams(batches: dict) -> list:
    problems = []
    for stream, rows in batches.items():
        problems.extend(validate_stream(stream, rows))
    return problems


def validate_stream(stream: str, rows) -> list:
    schema = SCHEMAS[stream]
    problems = []
    for index, row in enumerate(rows):
        where = "%s %d" % (schema.label, index)
        problems.extend(_row_problems(schema, where, row))
    return problems


def _row_problems(schema: Schema, where: str, row) -> list:
    if not isinstance(row, dict):
        return ["%s: not a row" % where]
    problems = []
    if any(key not in schema.columns for key in row):
        problems.append("%s: unknown column" % where)
    for column in schema.columns:
        if column not in row:
            problems.append("%s: column %s is missing" % (where, column))
        elif not VALIDATORS[column](row[column]):
            problems.append("%s: column %s is invalid" % (where, column))
    if not problems:
        problems.extend("%s: column %s is invalid" % (where, column)
                        for column in schema.mismatches(row))
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
