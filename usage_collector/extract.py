"""Read Claude Code transcripts and keep only allowlisted numbers and slugs.

Only the fields named in this module are ever read from a transcript line.
Everything else (prompts, replies, tool input, titles, paths) is ignored.
"""
import json
import os
import time

from . import guard
from .hashing import hash_value
from .models import model_slug
from .names import NamePolicy
from .records import count as _count
from .records import format_ts as _format_ts
from .records import is_compact_boundary as _is_compact_boundary
from .records import usage_message as _usage_message
from .streams import EXTRA_STREAMS, StreamRun

SETTLE_SECONDS = 600


class Settings:
    def __init__(self, salt, account, hostname, machine_label=None,
                 round_ts_to_hour=False, hash_agent_types=False,
                 hash_names=False, settle_seconds=SETTLE_SECONDS,
                 clock=time.time):
        self.salt = salt
        self.account = account
        self.hostname = hostname
        self.machine_label = machine_label
        self.round_ts_to_hour = round_ts_to_hour
        self.names = NamePolicy(salt, hash_names or hash_agent_types)
        self.settle_seconds = settle_seconds
        self.clock = clock

    @property
    def machine(self) -> str:
        return self.machine_label or hash_value(self.salt, self.hostname)


class FileProgress:
    def __init__(self, offset: int, compact: bool, streams: dict):
        self.offset = offset
        self.compact = compact
        self.streams = streams


class Extraction:
    def __init__(self, rows, files, seen_ids, sources, extra: StreamRun):
        self.rows = rows
        self.files = files
        self.seen_ids = seen_ids
        self.sources = sources
        self.extra = extra

    @property
    def streams(self) -> dict:
        """Rows of every stream by name, usage first."""
        found = {"usage": self.rows}
        found.update(self.extra.rows)
        return found

    def files_of_rejected_rows(self) -> list:
        """Local transcript paths of rows the guard rejects, so the user can find them."""
        sources = dict(self.extra.sources, usage=self.sources)
        return sorted({
            source for stream, rows in self.streams.items()
            for row, source in zip(rows, sources[stream])
            if guard.validate_stream(stream, [row])
        })


def extract(sources, state, settings) -> Extraction:
    return _Extractor(state, settings).run(sources)


def apply(extraction, state, today) -> None:
    for path, progress in extraction.files.items():
        state.set_file(path, progress.offset, progress.compact, progress.streams)
    state.mark_seen(extraction.seen_ids, today)
    for kind, keys in extraction.extra.marks.items():
        state.add_marks(kind, keys, today)
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

    @property
    def born(self) -> float:
        """When the file was created, where the platform records it, else 0."""
        try:
            return getattr(os.stat(self.path), "st_birthtime", 0.0)
        except OSError:
            return 0.0

    def read_order(self) -> tuple:
        # Forked sessions can hold copies of a subagent file without its
        # meta.json, so the copy beside the meta must win the dedupe. A
        # forked or continued session copies its parent's history, and the
        # parent always exists first, so older files claim shared ids.
        return (self.is_subagent, not self.has_meta, self.born, self.path)


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
        self.extra = StreamRun(state, settings)

    def run(self, sources) -> Extraction:
        files = [found for root in sources for found in discover(root)]
        for transcript in sorted(files, key=TranscriptFile.read_order):
            self._read_file(transcript)
        return Extraction(
            self.rows, self.files, list(self.rows_by_id), self.sources, self.extra
        )

    def _read_file(self, transcript) -> None:
        path = transcript.path
        offsets, compact = self._start(path)
        positions, records, end = self._finished_records(path, min(offsets.values()))
        identity = _FileIdentity(transcript, self.settings).columns()
        streams = self.extra.file_streams(path, identity, offsets)
        for position, record in zip(positions, records):
            if record is None:
                continue
            if position >= offsets["usage"]:
                compact = self._take(record, identity, compact, path)
            streams.take(record, position)
        self.files[path] = FileProgress(end, compact, streams.offsets(end))

    def _start(self, path: str) -> tuple:
        offsets = {stream: self.state.stream_offset(path, stream) for stream in EXTRA_STREAMS}
        offsets["usage"] = self.state.offset_for(path)
        if os.path.getsize(path) < max(offsets.values()):
            return dict.fromkeys(offsets, 0), False
        return offsets, self.state.compact_pending(path)

    def _finished_records(self, path: str, offset: int) -> tuple:
        """Returns each line's byte position, its record, and where the next read starts."""
        lines, end = read_new_lines(path, offset)
        positions = _line_positions(lines, offset)
        records = [parse_line(line) for line in lines]
        if self._is_idle(path):
            return positions, records, end
        kept = _finished_count(records)
        if kept < len(lines):
            end = positions[kept]
        return positions[:kept], records[:kept], end

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
        index = self.rows_by_id.get(message["id"])
        if index is not None and not _is_empty(self.rows[index]):
            self._merge(self.rows[index], message)
            return False
        row = _RowBuilder(record, message, self.settings).build(identity, compact)
        if row is None:
            return compact
        if index is None:
            self.rows_by_id[message["id"]] = len(self.rows)
            self.rows.append(row)
            self.sources.append(path)
        elif not _is_empty(row):
            # The first copy carried no counts, so this occurrence is the real turn.
            row["tools"] = _join_tools(_tool_set(row) | _tool_set(self.rows[index]))
            self.rows[index], self.sources[index] = row, path
        return False

    def _merge(self, row: dict, message: dict) -> None:
        later = _RowBuilder.usage_columns(message)
        if later is None:
            return
        later["tools"] = _join_tools(
            _tool_set(row) | set(filter(None, _tool_names(message, self.settings).split(";")))
        )
        row.update(later)


def _is_empty(row: dict) -> bool:
    return row["context"] == "0" and row["output"] == "0"


def _line_positions(lines: list, offset: int) -> list:
    positions = []
    for line in lines:
        positions.append(offset)
        offset += len(line) + 1
    return positions


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
        return self.settings.names.name(self.meta.get("agentType"))

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
        row["model"] = model_slug(self.message.get("model"), self.settings.salt)
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


def _tool_names(message: dict, settings) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    names = {
        settings.names.tool(item["name"]) for item in content
        if isinstance(item, dict) and item.get("type") == "tool_use"
        and isinstance(item.get("name"), str) and item["name"]
    }
    return _join_tools(names)


def _tool_set(row: dict) -> set:
    return set(filter(None, row["tools"].split(";")))


def _join_tools(names) -> str:
    return ";".join(sorted(names))
