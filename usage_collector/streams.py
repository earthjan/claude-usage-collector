"""Context sizes, skill uses and listed names, read beside the usage rows.

Each stream keeps its own read offset per transcript, so an install that
started before a stream existed reads that stream from the start of every
file still on disk while the usage rows carry on where they were.

Forked sessions repeat their parent's history with the same message ids and
record uuids. Every id these readers pass is remembered (as a "stream" mark),
so copied history never yields a second event, skill use or listing.
"""
from . import guard
from .context import Composition, kind_of
from .records import attachment_of, count, format_ts, is_compact_boundary, usage_message
from .state import MARK_KINDS

EXTRA_STREAMS = ("context", "skills", "catalog")
CONTEXT_IDENTITY = (
    "account", "machine", "project", "session", "agent", "is_subagent", "agent_type",
)
CATALOG_SOURCES = {
    "skill_listing": ("skill", ("names",)),
    "agent_listing_delta": ("agent", ("addedTypes", "builtInTypes")),
}


class StreamRun:
    """The rows and marks of one extract run, across every transcript."""

    def __init__(self, state, settings):
        self.state = state
        self.settings = settings
        self.rows = {stream: [] for stream in EXTRA_STREAMS}
        self.sources = {stream: [] for stream in EXTRA_STREAMS}
        self.marks = {kind: {} for kind in MARK_KINDS}

    def file_streams(self, path: str, identity: dict, offsets: dict) -> "FileStreams":
        return FileStreams(self, path, identity, offsets)

    def claim(self, kind: str, key: str) -> bool:
        """Marks the key and reports whether it was new to this run and the state."""
        if key in self.marks[kind] or self.state.has_mark(kind, key):
            return False
        self.marks[kind][key] = True
        return True

    def emit(self, stream: str, row: dict, source: str) -> None:
        self.rows[stream].append(row)
        self.sources[stream].append(source)


class FileStreams:
    def __init__(self, run: StreamRun, path: str, identity: dict, offsets: dict):
        self.readers = {
            "context": _ContextReader(
                run, path, identity, offsets["context"],
                offsets["context"] > 0 and run.state.context_fork_pending(path)),
            "skills": _SkillReader(run, path, identity, offsets["skills"]),
            "catalog": _CatalogReader(run, path, identity, offsets["catalog"]),
        }

    def take(self, record: dict, position: int) -> None:
        for reader in self.readers.values():
            if position >= reader.offset:
                reader.take(record, position)

    def offsets(self, end: int) -> dict:
        found = {name: reader.next_offset(end) for name, reader in self.readers.items()}
        found["context_fork"] = self.readers["context"].fork_pending
        return found


class _Reader:
    def __init__(self, run: StreamRun, path: str, identity: dict, offset: int):
        self.run = run
        self.path = path
        self.identity = identity
        self.offset = offset

    def next_offset(self, end: int) -> int:
        return end

    def _ts(self, record: dict):
        return format_ts(record.get("timestamp"), self.run.settings.round_ts_to_hour)


class _ContextReader(_Reader):
    """Sums what is attached before a file's first turn and after each compaction.

    An open event waits for its turn across runs: the offset stays at the
    point the event opened, so the next run measures it again in full.

    A file that opens with copied history (a forked or continued session)
    gets a `fork` event instead of a `start`, measured from what was
    attached after the last copied turn. A copied turn never closes an
    event and is never read again: the offset moves past it.
    """

    def __init__(self, run, path, identity, offset, fork_pending=False):
        super().__init__(run, path, identity, offset)
        self.event = "start" if offset == 0 else ("fork" if fork_pending else None)
        self.opened_at = offset
        self.parts = Composition()

    def next_offset(self, end: int) -> int:
        if self.event is None or self.opened_at is None:
            return end
        return self.opened_at

    @property
    def fork_pending(self) -> bool:
        return self.event == "fork"

    def take(self, record: dict, position: int) -> None:
        if self.opened_at is None:
            self.opened_at = position
        if is_compact_boundary(record):
            self._boundary(position)
        elif record.get("type") == "attachment":
            if self.event:
                self.parts.add(record.get("attachment"))
        else:
            message = usage_message(record)
            if message is not None:
                self._turn(record, message)

    def _boundary(self, position: int) -> None:
        self.parts = Composition()
        if self.event not in ("start", "fork"):
            self.event = "compact"
            self.opened_at = position

    def _turn(self, record: dict, message: dict) -> None:
        # A turn that carried no context tokens says nothing about the
        # context, so the event waits for the next real turn.
        if _context_tokens(message["usage"]) == 0:
            return
        if not self.run.claim("stream", message["id"]):
            self._copied_turn()
            return
        if self.event is None:
            return
        row = self._row(record, message["usage"])
        if row is not None:
            self.run.emit("context", row, self.path)
        self.event = None

    def _copied_turn(self) -> None:
        """Copied history: the compaction it carries was counted where it happened."""
        self.parts = Composition()
        self.event = "fork" if self.event in ("start", "fork") else None
        self.opened_at = None

    def _row(self, record: dict, usage: dict):
        ts = self._ts(record)
        first_turn = _context_tokens(usage)
        if ts is None or first_turn > guard.ROW_INT_LIMIT or not self.parts.fits():
            return None
        row = {"ts": ts, "event": self.event, "first_turn_context": str(first_turn)}
        row.update((key, self.identity[key]) for key in CONTEXT_IDENTITY)
        row.update(self.parts.columns())
        return {column: row[column] for column in guard.CONTEXT_COLUMNS}


class _SkillReader(_Reader):
    def take(self, record: dict, position: int) -> None:
        attachment = attachment_of(record, "invoked_skills")
        if attachment is None or _is_copy(self.run, record):
            return
        ts = self._ts(record)
        skills = attachment.get("skills")
        if ts is None or not isinstance(skills, list):
            return
        for skill in skills:
            if isinstance(skill, dict):
                self._use(ts, self.run.settings.names.name(skill.get("name")))

    def _use(self, ts: str, skill: str) -> None:
        identity = self.identity
        key = "|".join((identity["session"], identity["agent"], skill))
        if not skill or not self.run.claim("skill", key):
            return
        row = {"ts": ts, "skill": skill}
        row.update((column, identity[column]) for column in guard.SKILL_COLUMNS[1:-1])
        self.run.emit("skills", {c: row[c] for c in guard.SKILL_COLUMNS}, self.path)


class _CatalogReader(_Reader):
    def take(self, record: dict, position: int) -> None:
        if record.get("type") != "attachment":
            return
        attachment = record.get("attachment")
        source = CATALOG_SOURCES.get(kind_of(attachment)) if isinstance(
            attachment, dict) else None
        ts = format_ts(record.get("timestamp"), False)
        if source is None or ts is None:
            return
        kind, fields = source
        for field in fields:
            for name in _list(attachment.get(field)):
                self._list_name(ts[:10], kind, self.run.settings.names.name(name))

    def _list_name(self, day: str, kind: str, name: str) -> None:
        if not name or not self.run.claim("catalog", "|".join((day, kind, name))):
            return
        row = {"date": day, "account": self.identity["account"],
               "machine": self.identity["machine"], "kind": kind, "name": name}
        self.run.emit("catalog", row, self.path)


def _is_copy(run: StreamRun, record: dict) -> bool:
    uuid = record.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return False
    return not run.claim("stream", "uuid:" + uuid)


def _context_tokens(usage: dict) -> int:
    return sum(count(usage.get(key)) for key in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def _list(value) -> list:
    return value if isinstance(value, list) else []
