"""Read rows from a store and compute usage breakdowns."""
import sys
from collections import Counter

from . import context_analysis
from . import formatting as fmt
from .config import DEFAULTS, TOKEN_KEYS, is_token_number_map
from .name_map import NameMap, NameMapError
from .store import RowReader, StoreError, StreamReader, resolve_store

CONTEXT_BANDS = (
    ("<50K", 50_000), ("50-150K", 150_000), ("150-300K", 300_000), ("300K+", None),
)
STREAMS = ("context", "skills", "catalog")
MAIN_TYPE = "(main)"
TOP_AGENTS = 15
TOP_SESSIONS = 10
BIG_CONTEXT = 200_000
SHORT_AGENT_TURNS = 10
PER_MILLION = 1_000_000


class AnalysisError(Exception):
    pass


def load(store: str, since=None, account=None) -> list:
    return RowReader(store, since=since, account=account).read()


class Meter:
    """Turns a row's token counts into weighted effective usage and dollars."""

    def __init__(self, weights: dict, prices: dict):
        self.weights = weights
        self.prices = prices

    @classmethod
    def from_config(cls, config):
        config = config or {}
        weights = dict(DEFAULTS["weights"])
        weights.update(_number_map(config.get("weights", {}), "weights"))
        prices = config.get("prices", {})
        if not isinstance(prices, dict):
            raise AnalysisError("prices must be an object keyed by model prefix")
        checked = {prefix: _number_map(table, "prices")
                   for prefix, table in prices.items()}
        return cls(weights, checked)

    @property
    def priced(self) -> bool:
        return bool(self.prices)

    def effective(self, row: dict) -> float:
        return sum(row[key] * self.weights[key] for key in TOKEN_KEYS)

    def cost(self, row: dict):
        table = self._price_table(row["model"])
        if table is None:
            return None
        return sum(row[key] * table.get(key, 0) for key in TOKEN_KEYS) / PER_MILLION

    def _price_table(self, model: str):
        matches = [prefix for prefix in self.prices if model.startswith(prefix)]
        return self.prices[max(matches, key=len)] if matches else None


def _number_map(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise AnalysisError("%s must be an object" % name)
    if not is_token_number_map(value):
        raise AnalysisError("%s entries must be token kinds with numbers >= 0" % name)
    return dict(value)


class Tally:
    def __init__(self):
        self.turns = 0
        self.tokens = dict.fromkeys(TOKEN_KEYS, 0)
        self.effective = 0.0
        self.cost = 0.0
        self.unpriced = 0
        self.peak_context = 0
        self.models = Counter()

    def add(self, row: dict, effective: float, cost) -> None:
        self.turns += 1
        for key in TOKEN_KEYS:
            self.tokens[key] += row[key]
        self.effective += effective
        self.unpriced += cost is None
        self.cost += cost or 0.0
        self.peak_context = max(self.peak_context, row["context"])
        self.models[row["model"]] += 1

    def as_dict(self, priced: bool) -> dict:
        data = dict(self.tokens)
        data.update(
            turns=self.turns, effective=self.effective,
            cost=self.cost if priced and self.turns > self.unpriced else None,
            unpriced=self.unpriced, peak_context=self.peak_context,
            model=self.models.most_common(1)[0][0] if self.models else "",
        )
        return data


class Summary:
    """Groups every row once into all the tallies the reports need."""

    def __init__(self, meter: Meter):
        self.meter = meter
        self.total = Tally()
        self.threads = {"main": Tally(), "sub": Tally()}
        self.groups = {name: {} for name in ("day_main", "day_sub", "model",
                                             "agent_type", "agent", "session")}
        self.agent_types = {}
        self.session_agents = {}
        self.bands = {name: {"main": 0, "sub": 0} for name, _top in CONTEXT_BANDS}

    def add(self, row: dict) -> None:
        effective, cost = self.meter.effective(row), self.meter.cost(row)
        thread = "sub" if row["is_subagent"] else "main"
        session = (row["account"], row["session"])
        keys = {
            "day_" + thread: row["ts"][:10], "model": row["model"],
            "agent_type": row["agent_type"] or MAIN_TYPE, "session": session,
        }
        if row["agent"]:
            keys["agent"] = row["agent"]
            self.agent_types[row["agent"]] = row["agent_type"]
            self.session_agents.setdefault(session, set()).add(row["agent"])
        for tally in [self.total, self.threads[thread]] + [
                self.groups[group].setdefault(key, Tally()) for group, key in keys.items()]:
            tally.add(row, effective, cost)
        self.bands[_band(row["context"])][thread] += 1

    def result(self) -> dict:
        priced = self.meter.priced
        return {
            "turns": self.total.turns,
            "priced": priced,
            "weights": dict(self.meter.weights),
            "totals": self.total.as_dict(priced),
            "main": self.threads["main"].as_dict(priced),
            "sub": self.threads["sub"].as_dict(priced),
            "accounts": sorted({account for account, _s in self.groups["session"]}),
            "sessions": len(self.groups["session"]),
            "days": self._days(),
            "by_model": self._ranked("model"),
            "by_agent_type": self._agent_types(),
            "context_bands": [(name, self.bands[name]) for name, _top in CONTEXT_BANDS],
            "top_agents": self._top_agents(),
            "top_sessions": self._top_sessions(),
            "subagent_shares": self._subagent_shares(),
        }

    def _days(self) -> list:
        main, sub = self.groups["day_main"], self.groups["day_sub"]
        return [
            {"date": day,
             "main": main[day].effective if day in main else 0,
             "sub": sub[day].effective if day in sub else 0}
            for day in sorted(set(main) | set(sub))
        ]

    def _ranked(self, group: str) -> list:
        entries = [dict(tally.as_dict(self.meter.priced), name=name)
                   for name, tally in self.groups[group].items()]
        return sorted(entries, key=lambda e: (-e["effective"], str(e["name"])))

    def _agent_types(self) -> list:
        counts = Counter(self.agent_types.values())
        entries = self._ranked("agent_type")
        for entry in entries:
            entry["agents"] = counts.get(entry["name"], 0)
            entry["avg"] = entry["effective"] / entry["agents"] if entry["agents"] else None
        return entries

    def _top_agents(self) -> list:
        entries = self._ranked("agent")[:TOP_AGENTS]
        for entry in entries:
            entry["agent"] = entry.pop("name")
            entry["agent_type"] = self.agent_types[entry["agent"]]
        return entries

    def _top_sessions(self) -> list:
        entries = self._ranked("session")[:TOP_SESSIONS]
        for entry in entries:
            key = entry.pop("name")
            entry["account"], entry["session"] = key
            entry["agents"] = len(self.session_agents.get(key, ()))
        return entries

    def _subagent_shares(self) -> dict:
        agents = self.groups["agent"].values()
        return {
            "agents": len(agents),
            "peak_over_200k": sum(t.peak_context > BIG_CONTEXT for t in agents),
            "short": sum(t.turns <= SHORT_AGENT_TURNS for t in agents),
        }


def _band(context: int) -> str:
    for name, upper in CONTEXT_BANDS:
        if upper is None or context < upper:
            return name
    return CONTEXT_BANDS[-1][0]


def summarize(rows, meter: Meter, streams=None) -> dict:
    """Summarizes usage rows, plus the context, skills and catalog rows when given."""
    streams = streams or {}
    summary = Summary(meter)
    for row in rows:
        summary.add(row)
    result = summary.result()
    result["first_day"] = rows[0]["ts"][:10] if rows else None
    result["last_day"] = rows[-1]["ts"][:10] if rows else None
    result["starting_context"] = context_analysis.starting_context(
        streams.get("context", []))
    result["skills_agents"] = context_analysis.skills_and_agents(
        streams.get("skills", []), streams.get("catalog", []), rows)
    return result


def load_streams(store: str, since=None, account=None) -> tuple:
    """Returns ({stream: rows} for the context, skills and catalog streams, skipped count)."""
    streams, skipped = {}, 0
    for stream in STREAMS:
        reader = StreamReader(store, stream, since=since, account=account)
        streams[stream] = reader.read()
        skipped += reader.skipped
    return streams, skipped


def build(args, config):
    """Loads and summarizes a store; returns (summary, skipped row count)."""
    store = resolve_store(args.store, config)
    account = getattr(args, "account", None)
    names = _name_map(getattr(args, "names", None))
    reader = RowReader(store, since=args.since, account=account)
    rows = names.resolve("usage", reader.read())
    streams, skipped = load_streams(store, since=args.since, account=account)
    streams = {stream: names.resolve(stream, found) for stream, found in streams.items()}
    summary = summarize(rows, Meter.from_config(config), streams)
    return summary, reader.skipped + skipped


def _name_map(path) -> NameMap:
    return NameMap.read(path) if path else NameMap()


def cmd_analyze(args, config) -> int:
    try:
        summary, skipped = build(args, config)
    except (StoreError, AnalysisError, NameMapError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print(render_text(summary, skipped), end="")
    return 0


def render_text(summary: dict, skipped: int = 0) -> str:
    lines = TextReport(summary).lines()
    if skipped:
        lines.append("note: skipped %d row(s) that failed the guard" % skipped)
    return "\n".join(lines) + "\n"


THREAD_NAMES = (("main thread", "main"), ("subagents", "sub"))


class TextReport:
    def __init__(self, summary: dict):
        self.s = summary
        self.out = []

    def lines(self) -> list:
        if not self.s["turns"]:
            return ["no rows found for this selection"]
        for section in (self._totals, self._threads, self._models, self._types,
                        self._bands, self._agents, self._sessions, self._shape,
                        self._starting, self._skills):
            section()
            self.out.append("")
        return self.out

    def _heading(self, title: str) -> None:
        self.out += [title, "-" * len(title)]

    def _table(self, header: tuple, rows: list) -> None:
        table = [header] + [tuple(str(v) for v in r) for r in rows]
        widths = [max(len(r[i]) for r in table) for i in range(len(header))]
        for r in table:
            self.out.append("  " + "  ".join(
                v.ljust(w) if i == 0 else v.rjust(w)
                for i, (v, w) in enumerate(zip(r, widths))).rstrip())

    def _cost(self, entry: dict) -> list:
        return [fmt.dollars(entry["cost"])] if self.s["priced"] else []

    def _cost_head(self) -> tuple:
        return ("cost",) if self.s["priced"] else ()

    def _totals(self) -> None:
        t = self.s["totals"]
        self._heading("Totals (%s to %s)" % (self.s["first_day"], self.s["last_day"]))
        self.out.append("  turns: %d, sessions: %d, accounts: %s" % (
            t["turns"], self.s["sessions"], ", ".join(self.s["accounts"])))
        self.out.append("  input %s, cache read %s, cache write %s, output %s" % tuple(
            fmt.tokens(t[k]) for k in TOKEN_KEYS))
        self.out.append("  effective (weighted): %s" % fmt.tokens(t["effective"]))
        if self.s["priced"]:
            self.out.append("  approximate cost: %s (%d turn(s) had no price)" % (
                fmt.dollars(t["cost"]), t["unpriced"]))

    def _threads(self) -> None:
        self._heading("Main vs subagent")
        whole = self.s["totals"]["effective"]
        rows = [(name, self.s[key]["turns"], fmt.tokens(self.s[key]["effective"]),
                 fmt.percent(self.s[key]["effective"], whole))
                for name, key in (("main thread", "main"), ("subagents", "sub"))]
        self._table(("thread", "turns", "effective", "share"), rows)

    def _models(self) -> None:
        self._heading("By model")
        rows = [[e["name"], e["turns"], fmt.tokens(e["effective"])] + self._cost(e)
                for e in self.s["by_model"]]
        self._table(("model", "turns", "effective") + self._cost_head(), rows)

    def _types(self) -> None:
        self._heading("By agent type")
        rows = [(e["name"], e["agents"], e["turns"], fmt.tokens(e["effective"]),
                 fmt.average(e["avg"])) for e in self.s["by_agent_type"]]
        self._table(("agent type", "agents", "turns", "effective", "avg/agent"), rows)

    def _bands(self) -> None:
        self._heading("Context per turn")
        rows = [(name, c["main"], c["sub"]) for name, c in self.s["context_bands"]]
        self._table(("band", "main", "subagent"), rows)

    def _agents(self) -> None:
        self._heading("Top agents by effective usage")
        rows = [[e["agent"], e["agent_type"], e["model"], e["turns"],
                 fmt.tokens(e["peak_context"]), fmt.tokens(e["effective"])] + self._cost(e)
                for e in self.s["top_agents"]]
        self._table(("agent", "type", "model", "turns", "peak ctx", "effective")
                    + self._cost_head(), rows)

    def _sessions(self) -> None:
        self._heading("Top sessions by effective usage")
        rows = [[e["session"], e["account"], e["turns"], e["agents"],
                 fmt.tokens(e["peak_context"]), fmt.tokens(e["effective"])] + self._cost(e)
                for e in self.s["top_sessions"]]
        self._table(("session", "account", "turns", "agents", "peak ctx", "effective")
                    + self._cost_head(), rows)

    def _shape(self) -> None:
        shares = self.s["subagent_shares"]
        self._heading("Subagent shape")
        self.out.append("  subagents: %d" % shares["agents"])
        self.out.append("  peak context over 200K: %s" % fmt.percent(
            shares["peak_over_200k"], shares["agents"]))
        self.out.append("  10 turns or fewer: %s" % fmt.percent(
            shares["short"], shares["agents"]))

    def _starting(self) -> None:
        start = self.s["starting_context"]
        self._heading("Starting context")
        if not start["starts"]:
            self.out.append("  no context events collected yet")
            return
        threads = start["threads"]
        self._table(("thread", "starts", "median", "p90"), [
            (name, threads[key]["count"], fmt.average(threads[key]["median"]),
             fmt.average(threads[key]["p90"])) for name, key in THREAD_NAMES])
        compactions = start["compactions"]
        self.out.append("  compactions: main thread %d, subagents %d"
                        % (compactions["main"], compactions["sub"]))
        forks = start["forks"]
        self.out.append("  forked or continued: main thread %d, subagents %d"
                        " (left out of the figures above)" % (forks["main"], forks["sub"]))
        self._composition(start["composition"])
        self._weeks(start["weeks"])

    def _composition(self, composition: dict) -> None:
        self.out += ["", "  Average start, estimated tokens (chars / 4)"]
        main, sub = dict(composition["main"]), dict(composition["sub"])
        self._table(("part", "main thread", "subagents"), [
            (label, fmt.tokens(main[key]), fmt.tokens(sub[key]))
            for key, label in context_analysis.PARTS])

    def _weeks(self, weeks: list) -> None:
        self.out += ["", "  Median starting context by ISO week"]
        self._table(("week", "main thread", "subagents"), [
            (w["week"], fmt.average(w["main"]), fmt.average(w["sub"])) for w in weeks])

    def _skills(self) -> None:
        found = self.s["skills_agents"]
        self._heading("Skills and agents")
        if not (found["top_skills"] or any(found["listed"].values())):
            self.out.append("  no skill or catalog rows collected yet")
            return
        self.out.append("  listed: %d skill(s), %d agent type(s)"
                        % (found["listed"]["skill"], found["listed"]["agent"]))
        self.out += ["", "  Top skills by uses"]
        self._table(("skill", "uses"), [(e["name"], e["uses"])
                                        for e in found["top_skills"]])
        self.out += ["", "  Listed but never used"]
        if not found["unused"]:
            self.out.append("  none")
            return
        self._table(("name", "kind", "days listed"), [
            (e["name"], e["kind"], e["days"]) for e in found["unused"]])
