"""Read Claude Code transcripts and keep only allowlisted numbers and slugs.

Only the fields named in this module are ever read from a transcript line.
Everything else (prompts, replies, tool input, titles, paths) is ignored.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from . import guard
from .hashing import hash_value

SYNTHETIC_MODEL = "<synthetic>"
SETTLE_SECONDS = 600
UNKNOWN_MODEL = "unknown"
CLAUDE_MODEL = re.compile(r"claude-[A-Za-z0-9._:-]*")
TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(Z|[+-]\d{2}:\d{2})?"
)


class Settings:
    def __init__(self, salt, account, hostname, machine_label=None,
                 round_ts_to_hour=False, hash_agent_types=False,
                 settle_seconds=SETTLE_SECONDS, clock=time.time):
        self.salt = salt
        self.account = account
        self.hostname = hostname
        self.machine_label = machine_label
        self.round_ts_to_hour = round_ts_to_hour
        self.hash_agent_types = hash_agent_types
        self.settle_seconds = settle_seconds
        self.clock = clock

    @property
    def machine(self) -> str:
        return self.machine_label or hash_value(self.salt, self.hostname)


class Extraction:
    def __init__(self, rows, files, seen_ids, sources=None):
        self.rows = rows
        self.files = files
        self.seen_ids = seen_ids
        self.sources = sources or []

    def files_of_rejected_rows(self) -> list:
        """Local transcript paths of rows the guard rejects, so the user can find them."""
        return sorted({
            source for row, source in zip(self.rows, self.sources)
            if guard.validate_rows([row])
        })


def extract(sources, state, settings) -> Extraction:
    return _Extractor(state, settings).run(sources)


def apply(extraction, state, today) -> None:
    for path, (offset, compact) in extraction.files.items():
        state.set_file(path, offset, compact)
    state.mark_seen(extraction.seen_ids, today)
    state.prune(today)


class TranscriptFile:
    def __init__(self, path, project, session, agent_id=None):
        self.path = path
        self.project = project
        self.session = session
        self.agent_id = agent_id

    @property
    def is_subagent(self) -> bool:
        return self.agent_id is not None

    @property
    def meta_path(self) -> str:
        return self.path[: -len(".jsonl")] + ".meta.json"

    @property
    def has_meta(self) -> bool:
        return self.is_subagent and os.path.isfile(self.meta_path)

    def read_order(self) -> tuple:
        # Forked sessions can hold copies of a subagent file without its
        # meta.json, so the copy beside the meta must win the dedupe.
        return (self.is_subagent, not self.has_meta, self.path)


def discover(root: str) -> list:
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return []
    found = []
    for project in sorted(os.listdir(root)):
        project_path = os.path.join(root, project)
        if os.path.isdir(project_path):
            found.extend(_project_files(project_path, project))
    return found


def _project_files(project_path: str, project: str) -> list:
    found = []
    for entry in sorted(os.listdir(project_path)):
        path = os.path.join(project_path, entry)
        if entry.endswith(".jsonl") and os.path.isfile(path):
            found.append(TranscriptFile(path, project, entry[: -len(".jsonl")]))
        elif os.path.isdir(os.path.join(path, "subagents")):
            found.extend(_subagent_files(path, project, entry))
    return found


def _subagent_files(session_path: str, project: str, session: str) -> list:
    found = []
    for folder, _dirs, files in os.walk(os.path.join(session_path, "subagents")):
        for name in files:
            if name.startswith("agent-") and name.endswith(".jsonl"):
                agent_id = name[len("agent-"): -len(".jsonl")]
                path = os.path.join(folder, name)
                found.append(TranscriptFile(path, project, session, agent_id))
    return found


def read_new_lines(path: str, offset: int):
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[:end].split(b"\n"), offset + end + 1


def parse_line(line: bytes):
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


class _Extractor:
    def __init__(self, state, settings):
        self.state = state
        self.settings = settings
        self.rows = []
        self.sources = []
        self.rows_by_id = {}
        self.files = {}

    def run(self, sources) -> Extraction:
        files = [found for root in sources for found in discover(root)]
        for transcript in sorted(files, key=TranscriptFile.read_order):
            self._read_file(transcript)
        return Extraction(self.rows, self.files, list(self.rows_by_id), self.sources)

    def _read_file(self, transcript) -> None:
        path = transcript.path
        offset, compact = self._start(path)
        records, end = self._finished_records(path, offset)
        identity = _FileIdentity(transcript, self.settings).columns()
        for record in filter(None, records):
            compact = self._take(record, identity, compact, path)
        self.files[path] = (end, compact)

    def _start(self, path: str) -> tuple:
        offset = self.state.offset_for(path)
        if os.path.getsize(path) < offset:
            return 0, False
        return offset, self.state.compact_pending(path)

    def _finished_records(self, path: str, offset: int) -> tuple:
        lines, end = read_new_lines(path, offset)
        records = [parse_line(line) for line in lines]
        if self._is_idle(path):
            return records, end
        kept = _finished_count(records)
        consumed = sum(len(line) + 1 for line in lines[:kept])
        return records[:kept], offset + consumed

    def _is_idle(self, path: str) -> bool:
        settled_before = self.settings.clock() - self.settings.settle_seconds
        return os.path.getmtime(path) <= settled_before

    def _take(self, record: dict, identity: dict, compact: bool, path: str) -> bool:
        if _is_compact_boundary(record):
            return True
        message = _usage_message(record)
        if message is None:
            return compact
        # A repeated id after a boundary means the boundary is copied
        # history whose compaction was already counted where it happened.
        if self.state.has_seen(message["id"]):
            return False
        if message["id"] in self.rows_by_id:
            self._merge(self.rows_by_id[message["id"]], message)
            return False
        row = _RowBuilder(record, message, self.settings).build(identity, compact)
        if row is None:
            return compact
        self.rows.append(row)
        self.sources.append(path)
        self.rows_by_id[message["id"]] = row
        return False

    def _merge(self, row: dict, message: dict) -> None:
        later = _RowBuilder.usage_columns(message)
        if later is None:
            return
        later["tools"] = _join_tools(
            set(filter(None, row["tools"].split(";")))
            | set(filter(None, _tool_names(message, self.settings).split(";")))
        )
        row.update(later)


def _finished_count(records: list) -> int:
    """Count the leading records that end before the file's last message.

    Claude Code writes one line per content block of a message, so while a
    file is still active its last message may gain more lines and a larger
    output count. Those lines are left for a later run.
    """
    ids = [_usage_id(record) for record in records]
    if not ids or ids[-1] is None:
        return len(records)
    start = len(ids) - 1
    for index in range(len(ids) - 2, -1, -1):
        if ids[index] not in (None, ids[-1]):
            break
        if ids[index] == ids[-1]:
            start = index
    return start


def _usage_id(record):
    message = _usage_message(record) if record else None
    return message["id"] if message else None


def _is_compact_boundary(record: dict) -> bool:
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


def _usage_message(record: dict):
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    if not isinstance(message.get("id"), str) or not message["id"]:
        return None
    if message.get("model") == SYNTHETIC_MODEL:
        return None
    return message


class _FileIdentity:
    def __init__(self, transcript, settings):
        self.transcript = transcript
        self.settings = settings
        self.meta = _read_meta(transcript.meta_path) if transcript.is_subagent else {}

    def columns(self) -> dict:
        transcript = self.transcript
        return {
            "account": self.settings.account,
            "machine": self.settings.machine,
            "project": self._hash(transcript.project),
            "session": self._hash(transcript.session),
            "agent": self._hash(transcript.agent_id) if transcript.is_subagent else "",
            "is_subagent": "1" if transcript.is_subagent else "0",
            "agent_type": self._agent_type(),
            "spawn_depth": self._spawn_depth(),
            "is_fork": "1" if self.meta.get("isFork") is True else "0",
        }

    def _hash(self, value: str) -> str:
        return hash_value(self.settings.salt, value)


    def _agent_type(self) -> str:
        agent_type = self.meta.get("agentType")
        if not isinstance(agent_type, str) or not agent_type:
            return ""
        if self.settings.hash_agent_types or not guard.is_slug(agent_type):
            return self._hash(agent_type)
        return agent_type

    def _spawn_depth(self) -> str:
        depth = self.meta.get("spawnDepth")
        return str(depth) if _is_count(depth) else ""


def _read_meta(path: str) -> dict:
    try:
        with open(path) as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    return {key: meta.get(key) for key in ("agentType", "spawnDepth", "isFork")}


class _RowBuilder:
    def __init__(self, record: dict, message: dict, settings):
        self.record = record
        self.message = message
        self.settings = settings

    def build(self, identity: dict, compact: bool):
        ts = _format_ts(self.record.get("timestamp"), self.settings.round_ts_to_hour)
        if ts is None:
            return None
        usage = self.usage_columns(self.message)
        if usage is None:
            return None
        row = {"ts": ts}
        row.update(identity)
        row["model"] = _model_slug(self.message.get("model"), self.settings)
        effort = self.record.get("effort")
        row["effort"] = effort if effort in guard.EFFORTS else ""
        row.update(usage)
        row["tools"] = _tool_names(self.message, self.settings)
        row["compacted"] = "1" if compact else "0"
        return {column: row[column] for column in guard.COLUMNS}

    @staticmethod
    def usage_columns(message: dict):
        """Returns the count columns, or None when a count is out of range."""
        usage = message["usage"]
        server = usage.get("server_tool_use")
        server = server if isinstance(server, dict) else {}
        counts = {
            "input": _count(usage.get("input_tokens")),
            "cache_read": _count(usage.get("cache_read_input_tokens")),
            "cache_write": _count(usage.get("cache_creation_input_tokens")),
            "output": _count(usage.get("output_tokens")),
        }
        counts["context"] = counts["input"] + counts["cache_read"] + counts["cache_write"]
        counts["web_search"] = _count(server.get("web_search_requests"))
        counts["web_fetch"] = _count(server.get("web_fetch_requests"))
        if any(value > guard.ROW_INT_LIMIT for value in counts.values()):
            return None
        return {key: str(value) for key, value in counts.items()}


def _is_count(value) -> bool:
    is_int = isinstance(value, int) and not isinstance(value, bool)
    return is_int and 0 <= value <= guard.ROW_INT_LIMIT


def _count(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _model_slug(model, settings) -> str:
    """Keeps Claude model ids (minus provider prefixes and @versions); hashes the rest."""
    if not isinstance(model, str) or not model:
        return UNKNOWN_MODEL
    found = CLAUDE_MODEL.search(model)
    if found and guard.is_slug(found.group()[:64]):
        return found.group()[:64]
    return hash_value(settings.salt, model)


def _tool_names(message: dict, settings) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    names = {
        _tool_slug(item["name"], settings) for item in content
        if isinstance(item, dict) and item.get("type") == "tool_use"
        and isinstance(item.get("name"), str) and item["name"]
    }
    return _join_tools(names)


def _tool_slug(name: str, settings) -> str:
    if name.startswith("mcp__"):
        return "mcp:" + hash_value(settings.salt, name)
    if guard.is_slug(name):
        return name
    return hash_value(settings.salt, name)


def _join_tools(names) -> str:
    return ";".join(sorted(names))


def _format_ts(value, round_to_hour: bool):
    if not isinstance(value, str):
        return None
    match = TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    try:
        moment = _to_utc(match)
    except (ValueError, OverflowError):
        return None
    if round_to_hour:
        moment = moment.replace(minute=0, second=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_utc(match) -> datetime:
    parts = [int(part) for part in match.groups()[:6]]
    moment = datetime(*parts, tzinfo=timezone.utc)
    zone = match.group(7)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        moment -= sign * timedelta(hours=hours, minutes=minutes)
    return moment
