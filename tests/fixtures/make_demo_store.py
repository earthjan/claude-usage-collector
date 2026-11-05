"""Build the synthetic demo store used by the docs and the report tests.

Every number is invented by a seeded random generator, so running this twice
produces byte-identical files. The context, skills and catalog streams are
derived from the usage rows with a second generator. Usage, from the repo root:

    python3 tests/fixtures/make_demo_store.py [OUT_DIR] [--replace]
"""
import hashlib
import os
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from usage_collector import guard, periods  # noqa: E402
from usage_collector.sinks import FolderSink  # noqa: E402

DEFAULT_OUT = os.path.join(REPO, "tests", "fixtures", "demo-store")
SEED = 20260401
START = datetime(2026, 4, 1, tzinfo=timezone.utc)
DAYS = 30
MONTH = "2026-04"
ACCOUNTS = {
    "work": {"machines": 1, "sessions": (1, 3), "weekend_sessions": (0, 1)},
    "personal": {"machines": 2, "sessions": (0, 2), "weekend_sessions": (1, 3)},
}
MAIN_MODELS = (("claude-opus-5-5", 6), ("claude-sonnet-5-5", 3))
AGENT_TYPES = (
    ("Explore", "claude-haiku-4-5", 5),
    ("general-purpose", "claude-sonnet-5-5", 3),
    ("Plan", "claude-opus-5-5", 1),
    ("review-kit:code-reviewer", "claude-sonnet-5-5", 2),
    ("fork", None, 1),
)
EFFORTS = ("", "low", "medium", "high", "high", "xhigh")
TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "Write")
COMPACT_AT = 300_000
KNOWN_TOP_DIRS = {"work", "personal", "checks"}
STREAM_SEED = SEED + 1
# (name, weight of being used, first day listed); weight 0 is listed but never used
SKILLS = (
    ("tdd", 6, 0), ("code-review", 5, 0), ("diagnose", 3, 0),
    ("review-kit:review", 2, 0), ("write-docs", 2, 0), ("pdf", 1, 0),
    ("slides", 0, 0), ("spreadsheet", 0, 0),
    ("release-notes", 2, 11), ("db-migrate", 0, 19),
)
LISTED_AGENTS = (
    "Explore", "general-purpose", "Plan", "statusline-setup",
    "review-kit:code-reviewer", "docs-kit:docs-writer",
)
CHARS_PER_TOKEN = 4
# Estimated tokens per part at the start of a thread, before jitter.
MAIN_PARTS = {
    "system_prompt": 2900, "instr_user": 900, "instr_project": 1800,
    "agent_listing": 480, "deferred_tools": 1080, "mcp": 700, "hook_context": 220,
}
SUB_PARTS = {
    "system_prompt": 1400, "instr_user": 900, "instr_project": 1800,
    "deferred_tools": 1080, "mcp": 700,
}
FORK_PARTS = {"deferred_tools": 180, "hook_context": 120}
COMPACT_PARTS = {"instr_user": 900, "instr_project": 1800, "hook_context": 220}
SKILL_TOKENS = 85


def fake_hash(*parts) -> str:
    return hashlib.sha256(":".join(("demo",) + parts).encode()).hexdigest()[:12]


def weighted(rng: random.Random, options):
    return rng.choices([o[:-1] for o in options], [o[-1] for o in options])[0]


class Thread:
    """One conversation thread whose context grows turn by turn."""

    def __init__(self, rng, base: dict, model: str, start_context: int):
        self.rng = rng
        self.base = base
        self.model = model
        self.context = start_context
        self.compact_next = False

    def turns(self, count: int, ts: datetime) -> list:
        rows = []
        for _ in range(count):
            ts += timedelta(seconds=self.rng.randint(15, 150))
            rows.append(self._turn(ts))
        return rows

    def _turn(self, ts: datetime) -> dict:
        rng = self.rng
        cache_write = rng.randint(600, 15000)
        input_ = rng.randint(1, 60)
        cache_read = max(self.context - cache_write // 3, 0)
        row = dict(self.base)
        row.update({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "model": self.model,
            "effort": rng.choice(EFFORTS), "input": str(input_),
            "cache_read": str(cache_read), "cache_write": str(cache_write),
            "output": str(int(rng.lognormvariate(6.3, 0.9))),
            "context": str(input_ + cache_read + cache_write),
            "web_search": str(int(rng.random() < 0.03)),
            "web_fetch": str(int(rng.random() < 0.05)),
            "tools": ";".join(sorted(set(rng.sample(TOOLS, rng.randint(0, 3))))),
            "compacted": "1" if self.compact_next else "0",
        })
        self.compact_next = False
        self.context = int(row["context"])
        if self.context > COMPACT_AT:
            self.context, self.compact_next = rng.randint(30_000, 60_000), True
        return row


class DemoStore:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.rows = {account: [] for account in ACCOUNTS}

    def build(self) -> dict:
        for day in range(DAYS):
            date = START + timedelta(days=day)
            for account in ACCOUNTS:
                for number in range(self._session_count(account, date)):
                    self._session(account, date, number)
        return self.rows

    def _session_count(self, account: str, date: datetime) -> int:
        spec = ACCOUNTS[account]
        low, high = spec["weekend_sessions" if date.weekday() >= 5 else "sessions"]
        return self.rng.randint(low, high)

    def _session(self, account: str, date: datetime, number: int) -> None:
        rng = self.rng
        session = fake_hash(account, date.date().isoformat(), str(number))
        base = {
            "account": account,
            "machine": fake_hash(account, "machine", str(rng.randrange(ACCOUNTS[account]["machines"]))),
            "project": fake_hash(account, "project", str(rng.randrange(4))),
            "session": session, "agent": "", "is_subagent": "0", "agent_type": "",
            "spawn_depth": "", "is_fork": "0",
        }
        start = date + timedelta(hours=rng.randint(7, 19), minutes=rng.randint(0, 59))
        main = Thread(rng, base, weighted(rng, MAIN_MODELS)[0], rng.randint(18_000, 30_000))
        length = rng.randint(6, 30) if rng.random() < 0.75 else rng.randint(31, 75)
        rows = main.turns(length, start)
        for index in range(rng.choice((0, 0, 1, 1, 2, 3))):
            rows += self._subagent(base, session, index, start, main.model)
        self.rows[account].extend(rows)

    def _subagent(self, base: dict, session: str, index: int, start, main_model) -> list:
        rng = self.rng
        agent_type, model = weighted(rng, AGENT_TYPES)
        is_fork = agent_type == "fork"
        agent = dict(base, agent=fake_hash(session, "agent", str(index)),
                     is_subagent="1", agent_type=agent_type,
                     spawn_depth=str(2 if rng.random() < 0.15 else 1),
                     is_fork="1" if is_fork else "0")
        context = rng.randint(80_000, 180_000) if is_fork else rng.randint(12_000, 22_000)
        thread = Thread(rng, agent, model or main_model, context)
        length = rng.randint(2, 9) if rng.random() < 0.6 else rng.randint(10, 40)
        return thread.turns(length, start + timedelta(minutes=rng.randint(2, 40)))


def check_totals(rows: list, rng: random.Random) -> dict:
    totals = {}
    for row in rows:
        day = totals.setdefault(row["ts"][:10], {})
        counts = day.setdefault(row["model"], dict.fromkeys(guard.CHECK_KEYS, 0))
        for key in guard.CHECK_KEYS:
            counts[key] += int(row[key])
    for day in totals.values():
        for counts in day.values():
            if rng.random() < 0.05:
                counts["output"] += max(1, counts["output"] // 400)
    return {day: models for day, models in totals.items() if day.startswith(MONTH)}


def weekly(totals: dict) -> dict:
    """The month's totals split into one file per ISO week, as a weekly install writes them."""
    weeks = {}
    for day, models in totals.items():
        week = periods.week_of(datetime.strptime(day, "%Y-%m-%d").date())
        weeks.setdefault(week, {})[day] = models
    return weeks


class DemoStreams:
    """Context events, skill uses and listed names that fit one account's usage rows."""

    def __init__(self, rng: random.Random, rows: list):
        self.rng = rng
        self.rows = rows
        self.threads = {}
        for row in rows:
            self.threads.setdefault((row["session"], row["agent"]), []).append(row)

    def build(self) -> dict:
        context, skills = [], []
        for (_session, agent), rows in self.threads.items():
            first = "fork" if rows[0]["is_fork"] == "1" else "start"
            context.append(self._event(rows[0], first, self._start_parts(rows[0])))
            context.extend(self._event(row, "compact", self._compact_parts(row))
                           for row in rows if row["compacted"] == "1")
            skills.extend(self._skill_uses(rows, agent))
        context.sort(key=lambda row: row["ts"])
        skills.sort(key=lambda row: row["ts"])
        return {"context": context, "skills": skills, "catalog": self._catalog()}

    def _start_parts(self, row: dict) -> dict:
        if row["is_fork"] == "1":
            return self._jittered(FORK_PARTS)
        parts = self._jittered(SUB_PARTS if row["agent"] else MAIN_PARTS)
        parts.update(self._skill_listing(row["ts"][:10]))
        if not row["agent"]:
            parts["instr_memory"] = self.rng.randint(300, 1100)
            parts["instr_local"] = self.rng.choice((0, 0, 0, 250))
        return parts

    def _compact_parts(self, row: dict) -> dict:
        parts = self._jittered(COMPACT_PARTS)
        if not row["agent"]:
            parts["instr_memory"] = self.rng.randint(300, 1100)
        return parts

    def _jittered(self, parts: dict) -> dict:
        return {key: round(value * self.rng.uniform(0.9, 1.1)) for key, value in parts.items()}

    def _skill_listing(self, day: str) -> dict:
        return {"skill_listing": SKILL_TOKENS * len(listed_skills(day))}

    def _event(self, row: dict, kind: str, parts: dict) -> dict:
        event = {key: row[key] for key in guard.CONTEXT_COLUMNS[:8]}
        event["event"] = kind
        for column in guard.CONTEXT_PARTS:
            event[column] = "0"
        for key, tokens in parts.items():
            event[key + "_chars"] = str(tokens * CHARS_PER_TOKEN)
        event.update(self._counts(row, parts))
        event["first_turn_context"] = row["context"]
        return event

    def _counts(self, row: dict, parts: dict) -> dict:
        instr = [key for key in parts if key.startswith("instr_") and parts[key]]
        counts = {"instr_files": len(instr)}
        if parts.get("skill_listing"):
            counts["skill_count"] = len(listed_skills(row["ts"][:10]))
        if parts.get("agent_listing"):
            counts["agent_count"] = len(LISTED_AGENTS)
        if parts.get("deferred_tools"):
            counts["deferred_tool_count"] = 24 if parts["deferred_tools"] > 500 else 4
        if parts.get("mcp"):
            counts["mcp_server_count"] = 2
        return {key: str(value) for key, value in counts.items()}

    def _skill_uses(self, rows: list, agent: str) -> list:
        rng = self.rng
        count = rng.choice((0, 0, 1, 1, 1, 2, 3)) if not agent else int(rng.random() < 0.1)
        options = [s for s in listed_skills(rows[0]["ts"][:10]) if s[1]]
        names = set()
        for _ in range(count):
            names.add(rng.choices([s[0] for s in options], [s[1] for s in options])[0])
        uses = []
        for name in sorted(names):
            row = rng.choice(rows)
            uses.append({key: row[key] for key in guard.SKILL_COLUMNS[:6]})
            uses[-1]["skill"] = name
        return uses

    def _catalog(self) -> list:
        seen, catalog = set(), []
        for row in self.rows:
            day = row["ts"][:10]
            if (day, row["machine"]) in seen:
                continue
            seen.add((day, row["machine"]))
            base = {"date": day, "account": row["account"], "machine": row["machine"]}
            catalog += [dict(base, kind="skill", name=s[0]) for s in listed_skills(day)]
            catalog += [dict(base, kind="agent", name=name) for name in LISTED_AGENTS]
        return catalog


def listed_skills(day: str) -> list:
    offset = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) - START).days
    return [skill for skill in SKILLS if offset >= skill[2]]


def prepare(out: str, replace: bool) -> None:
    if os.path.exists(out) and os.listdir(out):
        if not replace:
            raise SystemExit("output folder is not empty, pass --replace")
        if not set(os.listdir(out)) <= KNOWN_TOP_DIRS:
            raise SystemExit("output folder does not look like a demo store")
        shutil.rmtree(out)


def write(out: str, replace: bool = False) -> None:
    prepare(out, replace)
    store = DemoStore()
    stream_rng = random.Random(STREAM_SEED)
    for account, rows in store.build().items():
        rows.sort(key=lambda row: row["ts"])
        sink = FolderSink(out, account)
        sink.write_rows(rows)
        for week, totals in sorted(weekly(check_totals(rows, store.rng)).items()):
            sink.write_check(week, totals, rows[0]["machine"])
        sink.write_streams(DemoStreams(stream_rng, rows).build())


def main(argv) -> int:
    paths = [arg for arg in argv if not arg.startswith("--")]
    write(paths[0] if paths else DEFAULT_OUT, replace="--replace" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
