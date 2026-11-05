"""Local-only bookkeeping: read offsets per transcript and seen message ids."""
import json
import os
from datetime import date, timedelta

KEEP_SEEN_DAYS = 60


class StateError(Exception):
    pass


class State:
    def __init__(self, data: dict):
        self._files = data.get("files", {})
        self._seen = data.get("seen", {})
        self.last_run = data.get("last_run")
        self.last_rows = data.get("last_rows", 0)
        self.total_rows = data.get("total_rows", 0)
        self.crosscheck_month = data.get("crosscheck_month")

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
            "last_run": self.last_run,
            "last_rows": self.last_rows,
            "total_rows": self.total_rows,
            "crosscheck_month": self.crosscheck_month,
        }

    @property
    def files_tracked(self) -> int:
        return len(self._files)

    def offset_for(self, path: str) -> int:
        return self._files.get(path, {}).get("offset", 0)

    def compact_pending(self, path: str) -> bool:
        return self._files.get(path, {}).get("compact", False)

    def set_file(self, path: str, offset: int, compact_pending: bool) -> None:
        self._files[path] = {"offset": offset, "compact": compact_pending}

    def has_seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark_seen(self, message_ids, today: date) -> None:
        for message_id in message_ids:
            self._seen[message_id] = today.isoformat()

    def prune(self, today: date) -> None:
        cutoff = (today - timedelta(days=KEEP_SEEN_DAYS)).isoformat()
        self._seen = {
            key: seen_on for key, seen_on in self._seen.items()
            if seen_on >= cutoff
        }

    def record_run(self, when: str, rows: int) -> None:
        self.last_run = when
        self.last_rows = rows
        self.total_rows += rows
