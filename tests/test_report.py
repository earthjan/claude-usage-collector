import argparse
import contextlib
import io
import os
import unittest

from usage_collector import analyze, report

from tests import support

DEMO = os.path.join(support.FIXTURES, "demo-store")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITTED_REPORT = os.path.join(REPO, "docs", "demo", "report.html")
COMMITTED_PREVIEW = os.path.join(REPO, "docs", "img", "report-preview.svg")
SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'
FORBIDDEN_DASHES = (chr(0x2013), chr(0x2014))


def demo_summary(config=None, streams=True):
    loaded = analyze.load_streams(DEMO)[0] if streams else None
    return analyze.summarize(analyze.load(DEMO), analyze.Meter.from_config(config), loaded)


class RenderHtmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = report.render_html(demo_summary())

    def test_is_one_self_contained_static_page(self):
        lowered = self.html.lower()
        for banned in ("<script", "http://", "https://", "src=", "@import", "url(", "<link"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, lowered)
        self.assertTrue(lowered.startswith("<!doctype html>"))

    def test_supports_light_and_dark(self):
        self.assertIn("prefers-color-scheme: dark", self.html)
        self.assertIn("color-scheme: light dark", self.html)

    def test_has_the_seven_charts_and_kpi_tiles(self):
        self.assertEqual(self.html.count("<svg"), 7)
        for label in ("Effective tokens", "Turns", "Sessions", "Subagent share"):
            self.assertIn(label, self.html)
        for title in ("by day", "by model", "by agent type", "Context per turn"):
            self.assertIn(title, self.html)

    def test_starting_context_cards(self):
        for title in ("Starting context", "What a thread starts with",
                      "Median starting context by week", "Top skills",
                      "Listed but never used"):
            self.assertIn(title, self.html)
        for label in ("System prompt", "Instructions", "Memory", "Skill listing",
                      "Agent listing", "Tools and MCP", "Hook context",
                      "Rest of first turn", "forked or continued"):
            self.assertIn(label, self.html)

    def test_never_used_names_are_listed(self):
        for name in ("slides", "spreadsheet", "statusline-setup"):
            self.assertIn("<code>%s</code>" % name, self.html)
        self.assertNotIn("<code>tdd</code>", self.html)

    def test_without_stream_rows_the_new_cards_are_left_out(self):
        html = report.render_html(demo_summary(streams=False))
        self.assertEqual(html.count("<svg"), 4)
        self.assertNotIn("Starting context", html)
        self.assertNotIn("Listed but never used", html)

    def test_charts_have_accessible_labels_and_native_tooltips(self):
        self.assertGreaterEqual(self.html.count('role="img"'), 7)
        self.assertIn("<title>", self.html)

    def test_tables_list_top_agents_and_sessions(self):
        summary = demo_summary()
        self.assertIn(summary["top_agents"][0]["agent"], self.html)
        self.assertIn(summary["top_sessions"][0]["session"], self.html)

    def test_rendering_is_deterministic(self):
        self.assertEqual(report.render_html(demo_summary()), self.html)

    def test_cost_column_only_with_prices(self):
        self.assertNotIn("$", self.html)
        prices = {"claude": {"input": 1, "cache_read": 0.1, "cache_write": 1, "output": 5}}
        self.assertIn("$", report.render_html(demo_summary({"prices": prices})))

    def test_empty_summary_renders_a_notice(self):
        empty = analyze.summarize([], analyze.Meter.from_config(None))
        html = report.render_html(empty)
        self.assertIn("No rows", html)
        self.assertNotIn("<svg", html)

    def test_no_dashes_that_the_repo_forbids(self):
        for dash in FORBIDDEN_DASHES:
            self.assertNotIn(dash, self.html)


class DailyChartSvgTest(unittest.TestCase):
    def test_standalone_svg_carries_namespace_and_its_own_theme(self):
        svg = report.daily_chart_svg(demo_summary()["days"], standalone=True)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn(SVG_NAMESPACE, svg)
        self.assertIn("prefers-color-scheme: dark", svg)
        self.assertNotIn("<script", svg)

    def test_one_bar_per_non_zero_segment(self):
        days = [{"date": "2026-04-01", "main": 10, "sub": 5},
                {"date": "2026-04-02", "main": 0, "sub": 7}]
        svg = report.daily_chart_svg(days)
        self.assertEqual(svg.count('class="bar main"'), 1)
        self.assertEqual(svg.count('class="bar sub"'), 2)


class CompositionChartTest(unittest.TestCase):
    COMPOSITION = {
        "main": [("system_prompt", 3000), ("instr_user", 500), ("instr_project", 700),
                 ("instr_local", 0), ("instr_memory", 0), ("instr_other", 0),
                 ("skill_listing", 0), ("agent_listing", 0), ("deferred_tools", 300),
                 ("mcp", 200), ("hook_context", 0), ("rest", 5000)],
        "sub": [("system_prompt", 1000), ("instr_user", 0), ("instr_project", 0),
                ("instr_local", 0), ("instr_memory", 0), ("instr_other", 0),
                ("skill_listing", 0), ("agent_listing", 0), ("deferred_tools", 0),
                ("mcp", 0), ("hook_context", 0), ("rest", 0)],
    }

    def test_one_segment_per_non_zero_group_and_thread(self):
        svg = report.CompositionChart(self.COMPOSITION).svg()
        # main: system prompt, instructions, tools and MCP, rest; sub: system prompt
        self.assertEqual(svg.count('class="seg '), 5)
        self.assertIn("Instructions: about 1.2K tokens", svg)
        self.assertIn("Tools and MCP: about 500 tokens", svg)

    def test_segment_widths_share_one_scale(self):
        chart = report.CompositionChart(self.COMPOSITION)
        self.assertEqual(chart.peak, 9700)

    def test_legend_names_every_group_once(self):
        svg = report.CompositionChart(self.COMPOSITION).svg()
        self.assertEqual(svg.count('class="legend-text"'), len(report.COMPOSITION_GROUPS))


class TrendChartTest(unittest.TestCase):
    def test_lines_skip_weeks_without_a_value(self):
        weeks = [{"week": "2026-W14", "main": 1000, "sub": None},
                 {"week": "2026-W15", "main": 3000, "sub": 7000},
                 {"week": "2026-W16", "main": 2000, "sub": 6000}]
        svg = report.TrendChart(weeks).svg()
        self.assertEqual(svg.count('class="dot main"'), 3)
        self.assertEqual(svg.count('class="dot sub"'), 2)
        self.assertEqual(svg.count('class="line sub"'), 1)
        self.assertIn("2026-W15: main thread 3.0K, subagents 7.0K", svg)
        self.assertIn(">W14<", svg)


class ReportCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_report(self, **overrides):
        values = {"store": DEMO, "out": self.tmp.join("r.html"), "since": None, "svg": None}
        values.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = report.cmd_report(argparse.Namespace(**values), None)
        return code, out.getvalue(), err.getvalue()

    def test_writes_html_and_optional_preview_svg(self):
        svg_path = self.tmp.join("img", "preview.svg")
        code, _out, err = self.run_report(svg=svg_path)
        self.assertEqual(code, 0, err)
        with open(self.tmp.join("r.html")) as handle:
            self.assertIn("<svg", handle.read())
        with open(svg_path) as handle:
            self.assertIn(SVG_NAMESPACE, handle.read())

    def test_output_never_names_the_store_path(self):
        self.run_report()
        with open(self.tmp.join("r.html")) as handle:
            html = handle.read()
        self.assertNotIn(DEMO, html)
        self.assertNotIn(self.tmp.path, html)

    def test_since_narrows_the_range(self):
        self.run_report(since="2026-04-20")
        with open(self.tmp.join("r.html")) as handle:
            html = handle.read()
        self.assertIn("2026-04-20", html)
        self.assertNotIn("2026-04-19", html)

    def test_missing_store_is_an_error(self):
        code, _out, err = self.run_report(store=self.tmp.join("none"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


class CommittedDemoTest(unittest.TestCase):
    def test_committed_demo_report_and_preview_are_current(self):
        summary = demo_summary()
        with open(COMMITTED_REPORT) as handle:
            self.assertEqual(handle.read(), report.render_html(summary))
        with open(COMMITTED_PREVIEW) as handle:
            expected = report.daily_chart_svg(summary["days"], standalone=True) + "\n"
            self.assertEqual(handle.read(), expected)


if __name__ == "__main__":
    unittest.main()
