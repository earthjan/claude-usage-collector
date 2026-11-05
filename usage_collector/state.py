"""Local-only bookkeeping: read offsets per transcript and seen message ids."""
import json
import os
from datetime import date, timedelta

KEEP_SEEN_DAYS = 60
MARK_KINDS = ("stream", "skill", "catalog")


class StateError(Exception):
    pass


class State:
    def __init__(self, data: dict):
        self._files = data.get("files", {})
        self._seen = data.get("seen", {})
        marks = data.get("marks", {})
        self._marks = {kind: marks.get(kind, {}) for kind in MARK_KINDS}
        self.last_run = data.get("last_run")
        self.last_rows = data.get("last_rows", 0)
        self.total_rows = data.get("total_rows", 0)
        self.crosscheck_month = data.get("crosscheck_month")
        self.crosscheck_week = data.get("crosscheck_week")

    @classmethod
    def load(cls, path: str) -> "State":
        try:
            with open(path) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return cls({})
        except (OSError, ValueError):
            raise StateError("state file is unreadable") from None
        if not isinstance(data, dict):
            raise StateError("state file is unreadable")
        return cls(data)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temp = path + ".tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self._as_dict(), handle)
        os.replace(temp, path)

    def _as_dict(self) -> dict:
        return {
            "files": self._files,
            "seen": self._seen,
            "marks": self._marks,
            "last_run": self.last_run,
            "last_rows": self.last_rows,
            "total_rows": self.total_rows,
            "crosscheck_month": self.crosscheck_month,
            "crosscheck_week": self.crosscheck_week,
        }

    @property
    def files_tracked(self) -> int:
        return len(self._files)

    def offset_for(self, path: str) -> int:
        return self._files.get(path, {}).get("offset", 0)

    def compact_pending(self, path: str) -> bool:
        return self._files.get(path, {}).get("compact", False)

    def stream_offset(self, path: str, stream: str) -> int:
        """Files from before per-stream offsets start every stream at 0."""
        return self._files.get(path, {}).get("streams", {}).get(stream, 0)

    def context_fork_pending(self, path: str) -> bool:
        """A forked file whose own first turn has not been read yet."""
        return self._files.get(path, {}).get("streams", {}).get("context_fork") is True

    def set_file(self, path: str, offset: int, compact_pending: bool,
                 streams=None) -> None:
        self._files[path] = {
            "offset": offset, "compact": compact_pending,
            "streams": dict(streams or {}),
        }

    def has_seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark_seen(self, message_ids, today: date) -> None:
        for message_id in message_ids:
            self._seen[message_id] = today.isoformat()

    def has_mark(self, kind: str, key: str) -> bool:
        return key in self._marks[kind]

    def add_marks(self, kind: str, keys, today: date) -> None:
        for key in keys:
            self._marks[kind][key] = today.isoformat()

    def prune(self, today: date) -> None:
        cutoff = (today - timedelta(days=KEEP_SEEN_DAYS)).isoformat()
        self._seen = _since(self._seen, cutoff)
        self._marks = {kind: _since(marks, cutoff) for kind, marks in self._marks.items()}

    def record_run(self, when: str, rows: int) -> None:
        self.last_run = when
        self.last_rows = rows
        self.total_rows += rows


def _since(dated: dict, cutoff: str) -> dict:
    return {key: day for key, day in dated.items() if day >= cutoff}
