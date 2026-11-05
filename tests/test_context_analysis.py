import unittest

from usage_collector import context_analysis as ca

from tests.test_guard import catalog_row, context_row, skill_row

PARTS_ZERO = {part: "0" for part in (
    "system_prompt_chars", "instr_user_chars", "instr_project_chars",
    "instr_local_chars", "instr_memory_chars", "instr_other_chars", "instr_files",
    "skill_listing_chars", "skill_count", "agent_listing_chars", "agent_count",
    "deferred_tools_chars", "deferred_tool_count", "mcp_chars", "mcp_server_count",
    "hook_context_chars",
)}
AGENT = "a" * 12


def event(first, ts="2026-04-06T10:00:00Z", sub=False, kind="start", **parts):
    row = context_row(ts=ts, event=kind, first_turn_context=str(first), **PARTS_ZERO)
    if sub:
        row.update(agent=AGENT, is_subagent="1", agent_type="Explore")
    row.update((key, str(value)) for key, value in parts.items())
    typed = dict(row)
    for key in ("is_subagent", "first_turn_context") + tuple(PARTS_ZERO):
        typed[key] = int(row[key])
    return typed


class StartingContextTest(unittest.TestCase):
    def test_median_and_p90_per_thread(self):
        rows = [event(n * 1000) for n in range(1, 11)] + [event(5000, sub=True),
                                                          event(9000, sub=True)]
        threads = ca.starting_context(rows)["threads"]
        self.assertEqual(threads["main"], {"count": 10, "median": 5500, "p90": 9000})
        self.assertEqual(threads["sub"], {"count": 2, "median": 7000, "p90": 9000})

    def test_threads_without_events_have_no_figures(self):
        threads = ca.starting_context([event(4000)])["threads"]
        self.assertEqual(threads["sub"], {"count": 0, "median": None, "p90": None})

    def test_composition_averages_chars_over_four_and_keeps_the_rest(self):
        rows = [
            event(10_000, system_prompt_chars=8000, instr_project_chars=4000),
            event(20_000, system_prompt_chars=12_000, hook_context_chars=400),
        ]
        main = dict(ca.starting_context(rows)["composition"]["main"])
        # system prompt (8000 + 12000) / 2 / 4, project 4000 / 2 / 4, hooks 400 / 2 / 4
        self.assertEqual(main["system_prompt"], 2500)
        self.assertEqual(main["instr_project"], 500)
        self.assertEqual(main["hook_context"], 50)
        self.assertEqual(main["instr_user"], 0)
        # 15000 average first turn minus the 3050 attributed
        self.assertEqual(main["rest"], 11_950)

    def test_composition_lists_every_part_in_a_fixed_order(self):
        keys = [key for key, _value in
                ca.starting_context([event(100)])["composition"]["main"]]
        self.assertEqual(keys, [key for key, _label in ca.PARTS])
        self.assertEqual(keys[-1], "rest")

    def test_rest_never_goes_below_zero(self):
        main = dict(ca.starting_context(
            [event(100, system_prompt_chars=4000)])["composition"]["main"])
        self.assertEqual(main["rest"], 0)

    def test_compact_events_are_counted_not_measured_as_starts(self):
        rows = [event(4000), event(90_000, kind="compact"),
                event(80_000, kind="compact", sub=True)]
        summary = ca.starting_context(rows)
        self.assertEqual(summary["compactions"], {"main": 1, "sub": 1})
        self.assertEqual(summary["threads"]["main"]["median"], 4000)
        self.assertEqual(summary["starts"], 1)

    def test_fork_events_are_counted_apart_and_left_out_of_every_start_figure(self):
        rows = [event(4000, system_prompt_chars=8000),
                event(190_000, kind="fork", hook_context_chars=40),
                event(150_000, kind="fork", sub=True)]
        summary = ca.starting_context(rows)
        self.assertEqual(summary["forks"], {"main": 1, "sub": 1})
        self.assertEqual(summary["starts"], 1)
        self.assertEqual(summary["threads"]["main"], {"count": 1, "median": 4000, "p90": 4000})
        self.assertEqual(dict(summary["composition"]["main"])["system_prompt"], 2000)
        self.assertEqual(dict(summary["composition"]["main"])["hook_context"], 0)
        self.assertEqual([week["main"] for week in summary["weeks"]], [4000])

    def test_weekly_trend_uses_iso_weeks_and_the_median(self):
        rows = [
            event(1000, ts="2026-04-05T23:00:00Z"),
            event(2000, ts="2026-04-06T01:00:00Z"),
            event(4000, ts="2026-04-12T22:00:00Z"),
            event(7000, ts="2026-04-08T10:00:00Z", sub=True),
        ]
        weeks = ca.starting_context(rows)["weeks"]
        self.assertEqual(weeks, [
            {"week": "2026-W14", "main": 1000, "sub": None},
            {"week": "2026-W15", "main": 3000, "sub": 7000},
        ])

    def test_no_events(self):
        summary = ca.starting_context([])
        self.assertEqual(summary["starts"], 0)
        self.assertEqual(summary["weeks"], [])


class SkillsAndAgentsTest(unittest.TestCase):
    def usage(self, agent_type):
        return {"agent_type": agent_type}

    def test_top_skills_by_uses_then_name(self):
        rows = [skill_row(skill="tdd"), skill_row(skill="tdd", session="1" * 12),
                skill_row(skill="pdf"), skill_row(skill="charts")]
        top = ca.skills_and_agents(rows, [], [])["top_skills"]
        self.assertEqual([(e["name"], e["uses"]) for e in top],
                         [("tdd", 2), ("charts", 1), ("pdf", 1)])

    def test_listed_but_never_used_counts_distinct_days_across_machines(self):
        catalog = [
            catalog_row(name="tdd"), catalog_row(name="slides"),
            catalog_row(name="slides", date="2026-01-03"),
            catalog_row(name="slides", date="2026-01-03", machine="9" * 12),
            catalog_row(kind="agent", name="Explore"),
            catalog_row(kind="agent", name="docs-writer"),
        ]
        result = ca.skills_and_agents(
            [skill_row(skill="tdd")], catalog,
            [self.usage(""), self.usage("Explore")])
        self.assertEqual(result["unused"], [
            {"kind": "agent", "name": "docs-writer", "days": 1},
            {"kind": "skill", "name": "slides", "days": 2},
        ])
        self.assertEqual(result["listed"], {"skill": 2, "agent": 2})

    def test_nothing_collected(self):
        result = ca.skills_and_agents([], [], [])
        self.assertEqual(result, {"top_skills": [], "unused": [],
                                  "listed": {"skill": 0, "agent": 0}})


if __name__ == "__main__":
    unittest.main()
