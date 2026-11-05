"""Summaries of the context, skills and catalog streams.

Context sizes are stored as character counts. They are turned into
estimated tokens at CHARS_PER_TOKEN, a rough rule of thumb for English text.
"""
import math
import statistics
from collections import Counter
from datetime import date

from . import periods

CHARS_PER_TOKEN = 4
TOP_SKILLS = 15
THREADS = ("main", "sub")
PARTS = (
    ("system_prompt", "System prompt"),
    ("instr_user", "User instructions"),
    ("instr_project", "Project instructions"),
    ("instr_local", "Local instructions"),
    ("instr_memory", "Memory"),
    ("instr_other", "Other instructions"),
    ("skill_listing", "Skill listing"),
    ("agent_listing", "Agent listing"),
    ("deferred_tools", "Deferred tools"),
    ("mcp", "MCP instructions"),
    ("hook_context", "Hook context"),
    ("rest", "Rest of first turn"),
)
MEASURED = tuple(key for key, _label in PARTS if key != "rest")


def _thread(row: dict) -> str:
    return "sub" if row["is_subagent"] else "main"


def median(values: list):
    return round(statistics.median(values)) if values else None


def p90(values: list):
    """Nearest-rank 90th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def starting_context(rows: list) -> dict:
    return StartingContext(rows).result()


class StartingContext:
    """Sizes of what a thread carries into its first turn.

    Fork events (a session or subagent that opens with copied history) are
    only counted: their first turn holds the copied conversation, so it
    says nothing about how big a fresh start is.
    """

    def __init__(self, rows: list):
        self.starts = [row for row in rows if row["event"] == "start"]
        self.compacts = [row for row in rows if row["event"] == "compact"]
        self.forks = [row for row in rows if row["event"] == "fork"]

    def result(self) -> dict:
        return {
            "starts": len(self.starts),
            "threads": {name: self._figures(self._sizes(name)) for name in THREADS},
            "composition": {name: self._composition(name) for name in THREADS},
            "weeks": self._weeks(),
            "compactions": self._per_thread(self.compacts),
            "forks": self._per_thread(self.forks),
        }

    @staticmethod
    def _per_thread(rows: list) -> dict:
        return {name: sum(_thread(row) == name for row in rows) for name in THREADS}

    def _sizes(self, thread: str, rows=None) -> list:
        return [row["first_turn_context"] for row in (self.starts if rows is None else rows)
                if _thread(row) == thread]

    @staticmethod
    def _figures(sizes: list) -> dict:
        return {"count": len(sizes), "median": median(sizes), "p90": p90(sizes)}

    def _composition(self, thread: str) -> list:
        rows = [row for row in self.starts if _thread(row) == thread]
        count = len(rows) or 1
        parts = [(key, round(sum(row[key + "_chars"] for row in rows)
                             / count / CHARS_PER_TOKEN)) for key in MEASURED]
        first_turn = sum(row["first_turn_context"] for row in rows) / count
        rest = max(0, round(first_turn - sum(value for _key, value in parts)))
        return parts + [("rest", rest)]

    def _weeks(self) -> list:
        by_week = {}
        for row in self.starts:
            week = periods.week_of(date.fromisoformat(row["ts"][:10]))
            by_week.setdefault(week, []).append(row)
        return [dict({"week": week},
                     **{name: median(self._sizes(name, by_week[week])) for name in THREADS})
                for week in sorted(by_week)]


def skills_and_agents(skill_rows: list, catalog_rows: list, usage_rows: list) -> dict:
    uses = Counter(row["skill"] for row in skill_rows)
    used = {"skill": set(uses),
            "agent": {row["agent_type"] for row in usage_rows if row["agent_type"]}}
    listed = {"skill": {}, "agent": {}}
    for row in catalog_rows:
        listed[row["kind"]].setdefault(row["name"], set()).add(row["date"])
    unused = [{"kind": kind, "name": name, "days": len(days)}
              for kind in sorted(listed) for name, days in sorted(listed[kind].items())
              if name not in used[kind]]
    top = sorted(uses.items(), key=lambda item: (-item[1], item[0]))[:TOP_SKILLS]
    return {
        "top_skills": [{"name": name, "uses": count} for name, count in top],
        "unused": unused,
        "listed": {kind: len(names) for kind, names in listed.items()},
    }
