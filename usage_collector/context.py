"""Measure what Claude Code attached to a turn: character lengths and counts only.

Every text value read here (instructions, listings, tool descriptions, MCP
blocks, hook output, the system prompt, paths) is passed to len() and never
stored, so no attached text can reach a row.
"""
import json

from . import guard

INSTRUCTION_PARTS = {
    "User": "instr_user_chars",
    "Project": "instr_project_chars",
    "Local": "instr_local_chars",
    "AutoMem": "instr_memory_chars",
}
INSTRUCTION_COLUMNS = tuple(INSTRUCTION_PARTS.values()) + ("instr_other_chars", "instr_files")


class Composition:
    """The sizes of everything attached before one turn."""

    def __init__(self):
        self.parts = dict.fromkeys(guard.CONTEXT_PARTS, 0)
        self._agents = set()
        self._tools = set()
        self._servers = set()

    def add(self, attachment) -> None:
        if not isinstance(attachment, dict):
            return
        handler = _HANDLERS.get(kind_of(attachment))
        if handler is not None:
            handler(self, attachment)

    def fits(self) -> bool:
        return all(value <= guard.ROW_INT_LIMIT for value in self._counts().values())

    def columns(self) -> dict:
        return {part: str(value) for part, value in self._counts().items()}

    def _counts(self) -> dict:
        counts = dict(self.parts)
        counts["agent_count"] = len(self._agents)
        counts["deferred_tool_count"] = len(self._tools)
        counts["mcp_server_count"] = len(self._servers)
        return counts

    def _prompt_snapshot(self, attachment: dict) -> None:
        self.parts["system_prompt_chars"] = _total_length(attachment.get("systemPrompt"))

    def _instructions(self, attachment: dict) -> None:
        for column in INSTRUCTION_COLUMNS:
            self.parts[column] = 0
        for item in _dicts(attachment.get("files")):
            part = INSTRUCTION_PARTS.get(kind_of(item), "instr_other_chars")
            self.parts[part] += _length(item.get("content"))
            self.parts["instr_files"] += 1

    def _skill_listing(self, attachment: dict) -> None:
        self.parts["skill_listing_chars"] += _length(attachment.get("content"))
        count = attachment.get("skillCount")
        if not _is_count(count):
            count = len(_strings(attachment.get("names")))
        self.parts["skill_count"] += count

    def _agent_listing(self, attachment: dict) -> None:
        self.parts["agent_listing_chars"] += _total_length(attachment.get("addedLines"))
        self._agents.update(_strings(attachment.get("addedTypes")))

    def _deferred_delta(self, attachment: dict) -> None:
        self.parts["deferred_tools_chars"] += _total_length(attachment.get("addedLines"))
        self._tools.update(_strings(attachment.get("addedNames")))

    def _deferred_record(self, attachment: dict) -> None:
        for entry in _dicts(attachment.get("entries")):
            self.parts["deferred_tools_chars"] += (
                _length(entry.get("name")) + _length(entry.get("description"))
                + _schema_length(entry.get("input_schema"))
            )
            self._tools.update(_strings([entry.get("name")]))

    def _mcp(self, attachment: dict) -> None:
        self.parts["mcp_chars"] += _total_length(attachment.get("addedBlocks"))
        self._servers.update(_strings(attachment.get("addedNames")))

    def _hook(self, attachment: dict) -> None:
        self.parts["hook_context_chars"] += _total_length(attachment.get("content"))


_HANDLERS = {
    "prompt_snapshot": Composition._prompt_snapshot,
    "instructions": Composition._instructions,
    "skill_listing": Composition._skill_listing,
    "agent_listing_delta": Composition._agent_listing,
    "deferred_tools_delta": Composition._deferred_delta,
    "deferred_tools_record": Composition._deferred_record,
    "mcp_instructions_delta": Composition._mcp,
    "hook_additional_context": Composition._hook,
}


def kind_of(item: dict):
    """The item's type when it is a string, so a list or object never reaches a lookup."""
    kind = item.get("type")
    return kind if isinstance(kind, str) else None


def _length(value) -> int:
    return len(value) if isinstance(value, str) else 0


def _total_length(values) -> int:
    return sum(map(len, _strings(values)))


def _strings(values) -> list:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _dicts(values) -> list:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _schema_length(schema) -> int:
    if schema is None:
        return 0
    return len(json.dumps(schema, separators=(",", ":"), sort_keys=True))


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
