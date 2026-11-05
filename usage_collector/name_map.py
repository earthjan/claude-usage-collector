"""A local-only map from hashed names back to plain names, for display.

The map lives beside the config, never in the destination, so plain names
stay on this machine. analyze and report read it to show names people
recognise; nothing else uses it.
"""
import json
import os
import tempfile

NAME_FIELDS = {"usage": ("agent_type",), "context": ("agent_type",),
               "skills": ("skill",), "catalog": ("name",)}


class NameMapError(Exception):
    pass


class NameMap:
    def __init__(self, names=None):
        self.names = dict(names or {})

    @classmethod
    def read(cls, path: str):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise NameMapError("name map not found") from None
        except (OSError, ValueError):
            raise NameMapError("name map is not valid JSON") from None
        names = data.get("names") if isinstance(data, dict) else None
        if not _is_string_map(names):
            raise NameMapError("name map must hold a names object of strings")
        return cls(names)

    @classmethod
    def read_or_empty(cls, path: str):
        return cls.read(path) if os.path.exists(path) else cls()

    def merge(self, pairs: dict) -> None:
        self.names.update(pairs)

    def display(self, value: str) -> str:
        return self.names.get(value, value)

    def resolve(self, stream: str, rows: list) -> list:
        """Copies of rows with every name field shown as its plain name when known."""
        fields = NAME_FIELDS.get(stream, ())
        if not self.names or not fields:
            return rows
        return [dict(row, **{field: self.display(row[field]) for field in fields})
                for row in rows]

    def save(self, path: str) -> None:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=".names.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"names": self.names}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except BaseException:
            if os.path.exists(temp):
                os.remove(temp)
            raise


def _is_string_map(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items())


def is_inside(path: str, folder: str) -> bool:
    """True when the file at path would sit in folder or below it."""
    parent = os.path.realpath(os.path.dirname(os.path.abspath(os.path.expanduser(path))))
    root = os.path.realpath(os.path.abspath(os.path.expanduser(folder)))
    return os.path.commonpath([parent, root]) == root


def ensure_outside(map_path: str, destination: str) -> None:
    """Refuses a map path that would sit inside the destination folder."""
    if is_inside(map_path, destination):
        raise NameMapError(
            "the name map would be written inside the destination folder, "
            "move the config folder outside it")
