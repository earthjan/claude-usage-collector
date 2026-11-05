"""One policy for every name that leaves: agent types, skills, catalog names, tools."""
from . import guard
from .hashing import hash_value

MCP_PREFIX = "mcp__"


class NamePolicy:
    def __init__(self, salt: bytes, hash_all: bool):
        self.salt = salt
        self.hash_all = hash_all
        self.hashed = {}

    def name(self, value) -> str:
        """A slug is kept unless every name is hashed; anything else is hashed."""
        if not isinstance(value, str) or not value:
            return ""
        if self.hash_all or not guard.is_slug(value):
            return self._hash(value)
        return value

    def tool(self, value: str) -> str:
        if value.startswith(MCP_PREFIX):
            return "mcp:" + self._hash(value)
        return self.name(value)

    def _hash(self, value: str) -> str:
        """Hashes a name and remembers the pair for the local name map."""
        hashed = hash_value(self.salt, value)
        self.hashed[hashed] = value
        return hashed
