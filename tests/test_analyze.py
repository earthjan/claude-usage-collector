import argparse
import contextlib
import io
import os
import unittest

from usage_collector import analyze
from usage_collector.sinks import FolderSink

from tests import support

AGENT_A = "a1" * 6
AGENT_B = "b2" * 6
SESSION_1 = "51" * 6
SESSION_2 = "52" * 6


def row(ts="2026-04-01T10:00:00Z", account="work", session=SESSION_1, agent="",
        agent_type="", model="claude-opus-5-5", input_=100, cache_read=1000,
        cache_write=10, output=20):
    return {
        "ts": ts, "account": account, "machine": "0" * 12, "project": "1" * 12,
        "session": session, "agent": agent, "is_subagent": "1" if agent else "0",
        "agent_type": agent_type, "spawn_depth": "1" if agent else "",
        "is_fork": "0", "model": model, "effort": "",
        "input": str(input_), "cache_read": str(cache_read),
        "cache_write": str(cache_write), "output": str(output),
        "context": str(input_ + cache_read + cache_write),
        "web_search": "0", "web_fetch": "0", "tools": "", "compacted": "0",
    }


def typed(*rows):
    store = support.TempDir()
    try:
        for account in sorted({r["account"] for r in rows}):
            FolderSink(store.path, account).write_rows([r for r in rows if r["account"] == account])
        return analyze.load(store.path)
    finally:
        store.cleanup()


class MeterTest(unittest.TestCase):
    def test_default_weights_worked_example(self):
        meter = analyze.Meter.from_config(None)
        # 100 x1 + 1000 x0.1 + 10 x2 + 20 x5
        self.assertEqual(meter.effective(typed(row())[0]), 320)

    def test_weights_override_from_config(self):
        meter = analyze.Meter.from_config({"weights": {"output": 1}})
        self.assertEqual(meter.effective(typed(row())[0]), 240)

    def test_cost_is_none_without_prices(self):
        meter = analyze.Meter.from_config({"prices": {}})
        self.assertIsNone(meter.cost(typed(row())[0]))

    def test_cost_uses_longest_matching_model_prefix(self):
        prices = {
            "claude-opus": {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1},
            "claude-opus-5": {"input": 10, "cache_read": 1, "cache_write": 20, "output": 50},
        }
        meter = analyze.Meter.from_config({"prices": prices})
        # (100 x10 + 1000 x1 + 10 x20 + 20 x50) / 1e6
        self.assertAlmostEqual(meter.cost(typed(row())[0]), 0.0032)
        self.assertIsNone(meter.cost(typed(row(model="claude-haiku-4-5"))[0]))

    def test_bad_weights_are_rejected(self):
        for config in ({"weights": {"input": -1}}, {"weights": {"input": "x"}},
                       {"weights": []}, {"prices": {"m": {"input": "1"}}}):
            with self.subTest(config=config):
                with self.assertRaises(analyze.AnalysisError):
                    analyze.Meter.from_config(config)


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.rows = typed(
            row(input_=10, cache_read=40_000, cache_write=0),
            row(ts="2026-04-01T11:00:00Z", input_=0, cache_read=60_000, cache_write=0),
            row(ts="2026-04-02T10:00:00Z", agent=AGENT_A, agent_type="Explore",
                model="claude-haiku-4-5", input_=0, cache_read=149_999, cache_write=1),
            row(ts="2026-04-02T11:00:00Z", agent=AGENT_A, agent_type="Explore",
                model="claude-haiku-4-5", input_=0, cache_read=250_000, cache_write=0),
            row(ts="2026-04-03T10:00:00Z", account="personal", session=SESSION_2,
                agent=AGENT_B, agent_type="general-purpose", input_=0,
                cache_read=300_000, cache_write=0),
        )
        self.summary = analyze.summarize(self.rows, analyze.Meter.from_config(None))

    def test_totals_and_turns(self):
        totals = self.summary["totals"]
        self.assertEqual(self.summary["turns"], 5)
        self.assertEqual(totals["input"], 10)
        self.assertEqual(totals["cache_read"], 799_999)
        self.assertEqual(totals["output"], 100)

    def test_main_and_subagent_split(self):
        self.assertEqual(self.summary["main"]["turns"], 2)
        self.assertEqual(self.summary["sub"]["turns"], 3)

    def test_context_bands_split_main_and_sub(self):
        bands = dict((name, counts) for name, counts in self.summary["context_bands"])
        self.assertEqual(bands["<50K"], {"main": 1, "sub": 0})
        self.assertEqual(bands["50-150K"], {"main": 1, "sub": 0})
        self.assertEqual(bands["150-300K"], {"main": 0, "sub": 2})
        self.assertEqual(bands["300K+"], {"main": 0, "sub": 1})

    def test_by_agent_type_counts_agents_and_averages(self):
        types = {entry["name"]: entry for entry in self.summary["by_agent_type"]}
        self.assertEqual(types["Explore"]["agents"], 1)
        self.assertEqual(types["Explore"]["turns"], 2)
        explore_effective = (149_999 * 0.1 + 2) + (250_000 * 0.1) + 2 * 100
        self.assertAlmostEqual(types["Explore"]["avg"], explore_effective)
        self.assertEqual(types["(main)"]["agents"], 0)

    def test_agent_type_average_is_per_agent_not_the_total(self):
        rows = typed(
            row(agent=AGENT_A, agent_type="Explore", input_=100, cache_read=0,
                cache_write=0, output=0),
            row(agent=AGENT_B, agent_type="Explore", input_=300, cache_read=0,
                cache_write=0, output=0),
        )
        summary = analyze.summarize(rows, analyze.Meter.from_config(None))
        explore = {e["name"]: e for e in summary["by_agent_type"]}["Explore"]
        self.assertEqual((explore["agents"], explore["effective"]), (2, 400))
        self.assertEqual(explore["avg"], 200)

    def test_by_model_sorted_by_effective(self):
        names = [entry["name"] for entry in self.summary["by_model"]]
        self.assertEqual(names, ["claude-opus-5-5", "claude-haiku-4-5"])

    def test_top_agents_carry_peak_context_and_model(self):
        top, second = self.summary["top_agents"]
        self.assertEqual(top["agent"], AGENT_A)
        self.assertEqual(top["peak_context"], 250_000)
        self.assertEqual(top["model"], "claude-haiku-4-5")
        self.assertEqual(top["turns"], 2)
        self.assertEqual(second["agent"], AGENT_B)
        self.assertEqual(second["peak_context"], 300_000)

    def test_top_sessions_include_subagent_turns(self):
        sessions = {s["session"]: s for s in self.summary["top_sessions"]}
        self.assertEqual(sessions[SESSION_1]["turns"], 4)
        self.assertEqual(sessions[SESSION_1]["agents"], 1)
        self.assertEqual(sessions[SESSION_2]["account"], "personal")

    def test_subagent_shares(self):
        shares = self.summary["subagent_shares"]
        self.assertEqual(shares["agents"], 2)
        self.assertEqual(shares["peak_over_200k"], 2)
        self.assertEqual(shares["short"], 2)

    def test_days_split_main_and_sub(self):
        days = self.summary["days"]
        self.assertEqual([d["date"] for d in days], ["2026-04-01", "2026-04-02", "2026-04-03"])
        self.assertEqual(days[0]["sub"], 0)
        self.assertEqual(days[1]["main"], 0)

    def test_empty_rows(self):
        summary = analyze.summarize([], analyze.Meter.from_config(None))
        self.assertEqual(summary["turns"], 0)
        self.assertEqual(summary["days"], [])


class AnalyzeCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.store = self.tmp.path
        FolderSink(self.store, "work").write_rows([
            row(), row(ts="2026-04-05T10:00:00Z", agent=AGENT_A, agent_type="Explore"),
        ])
        FolderSink(self.store, "personal").write_rows([row(ts="2026-04-02T10:00:00Z")])

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, since=None, account=None, config=None, store=None):
        args = argparse.Namespace(store=store or self.store, since=since, account=account)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = analyze.cmd_analyze(args, config)
        return code, out.getvalue(), err.getvalue()

    def test_prints_every_section(self):
        code, out, err = self.analyze()
        self.assertEqual(code, 0, err)
        for heading in ("Totals", "Main vs subagent", "By model", "By agent type",
                        "Context per turn", "Top agents", "Top sessions", "Subagent shape"):
            self.assertIn(heading, out)
        self.assertIn("turns: 3", out)
        self.assertNotIn("$", out)

    def test_cost_appears_when_prices_are_set(self):
        prices = {"claude-opus": {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1}}
        _code, out, _err = self.analyze(config={"prices": prices})
        self.assertIn("$", out)

    def test_since_and_account_filters(self):
        self.assertIn("turns: 2", self.analyze(since="2026-04-02")[1])
        self.assertIn("turns: 2", self.analyze(account="work")[1])

    def test_rows_failing_the_guard_are_skipped_and_counted(self):
        path = os.path.join(self.store, "work", "2026", "04", "2026-04-01.csv")
        with open(path, "a") as handle:
            handle.write("not,a,valid,row\n")
        _code, out, _err = self.analyze()
        self.assertIn("turns: 3", out)
        self.assertIn("skipped 1", out)

    def test_context_skill_and_catalog_files_are_not_read_as_usage_rows(self):
        from tests.test_guard import catalog_row, context_row, skill_row
        FolderSink(self.store, "work").write_streams({
            "context": [context_row(ts="2026-04-01T10:00:00Z")],
            "skills": [skill_row(ts="2026-04-01T10:00:00Z")],
            "catalog": [catalog_row(date="2026-04-01")],
        })
        _code, out, _err = self.analyze()
        self.assertIn("turns: 3", out)
        self.assertNotIn("skipped", out)

    def write_streams(self):
        from tests.test_guard import catalog_row, context_row, skill_row
        FolderSink(self.store, "work").write_streams({
            "context": [
                context_row(ts="2026-04-01T10:00:00Z", first_turn_context="24000"),
                context_row(ts="2026-04-01T12:00:00Z", event="compact"),
                context_row(ts="2026-04-02T10:00:00Z", event="fork",
                            first_turn_context="180000"),
                context_row(ts="2026-04-05T10:00:00Z", agent=AGENT_A, is_subagent="1",
                            agent_type="Explore", first_turn_context="9000"),
            ],
            "skills": [skill_row(ts="2026-04-01T10:00:00Z", skill="tdd")],
            "catalog": [catalog_row(date="2026-04-01", name="tdd"),
                        catalog_row(date="2026-04-01", name="slides"),
                        catalog_row(date="2026-04-01", kind="agent", name="Explore"),
                        catalog_row(date="2026-04-01", kind="agent", name="docs-writer")],
        })

    def test_starting_context_and_skills_sections(self):
        self.write_streams()
        code, out, err = self.analyze()
        self.assertEqual(code, 0, err)
        section = out[out.index("Starting context"):]
        for text in ("24.0K", "9.0K", "compactions: main thread 1, subagents 0",
                     "forked or continued: main thread 1, subagents 0",
                     "System prompt", "2026-W14", "chars / 4"):
            self.assertIn(text, section)
        self.assertNotIn("180.0K", section)
        skills = out[out.index("Skills and agents"):]
        self.assertIn("tdd", skills)
        never = skills[skills.index("Listed but never used"):]
        self.assertIn("slides", never)
        self.assertIn("docs-writer", never)
        self.assertNotIn("Explore", never)

    def test_stream_sections_respect_since(self):
        self.write_streams()
        out = self.analyze(since="2026-04-02")[1]
        section = out[out.index("Starting context"):]
        self.assertNotIn("24.0K", section)
        self.assertIn("9.0K", section)

    def test_store_without_streams_says_none_were_collected(self):
        out = self.analyze()[1]
        self.assertIn("no context events collected", out)
        self.assertIn("no skill or catalog rows collected", out)

    def test_bad_stream_rows_are_counted_as_skipped(self):
        self.write_streams()
        path = os.path.join(self.store, "work", "2026", "04", "2026-04-01.skills.csv")
        with open(path, "a") as handle:
            handle.write("CANARY_SKILLBODY_7f3\n")
        out = self.analyze()[1]
        self.assertIn("skipped 1", out)
        self.assertNotIn("CANARY", out)

    def test_bad_since_and_missing_store_are_errors(self):
        self.assertEqual(self.analyze(since="April")[0], 1)
        code, _out, err = self.analyze(store=os.path.join(self.store, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_empty_store_says_so(self):
        code, out, _err = self.analyze(since="2030-01-01")
        self.assertEqual(code, 0)
        self.assertIn("no rows", out)


if __name__ == "__main__":
    unittest.main()
