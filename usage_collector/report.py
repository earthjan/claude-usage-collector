"""Render a static, self-contained HTML usage report.

The page uses inline SVG and CSS only: no JavaScript, fonts or images are
loaded, so it opens offline and can be attached or committed as one file.
"""
import math
import os
import sys
from html import escape

from . import formatting as fmt
from .analyze import AnalysisError, build
from .name_map import NameMapError
from .store import StoreError

SVG_NS = "http://www.w3.org/2000/svg"
FONT_STACK = ('-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
              '"Helvetica Neue", Arial, sans-serif')
MONO_STACK = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

THEME_LIGHT = {
    "bg": "#f6f6f4", "surface": "#fcfcfb", "border": "#e4e3de",
    "text": "#1b1b1a", "muted": "#52514e", "faint": "#8a8984",
    "grid": "#ecebe7", "series-1": "#2a78d6", "series-2": "#eb6834",
    "series-3": "#1baf7a", "series-4": "#eda100", "series-5": "#e87ba4",
    "series-6": "#008300", "series-7": "#6250d6", "rest": "#c9c8c2",
}
THEME_DARK = {
    "bg": "#121211", "surface": "#1a1a19", "border": "#2e2e2b",
    "text": "#f0efec", "muted": "#c3c2b7", "faint": "#8f8e86",
    "grid": "#272725", "series-1": "#3987e5", "series-2": "#d95926",
    "series-3": "#199e70", "series-4": "#c98500", "series-5": "#d55181",
    "series-6": "#008300", "series-7": "#9085e9", "rest": "#4a4a46",
}
CHART_CSS = """
.bar.main, .swatch.main, .dot.main { fill: var(--series-1); }
.bar.sub, .swatch.sub, .dot.sub { fill: var(--series-2); }
.seg-1 { fill: var(--series-1); } .seg-2 { fill: var(--series-2); }
.seg-3 { fill: var(--series-3); } .seg-4 { fill: var(--series-4); }
.seg-5 { fill: var(--series-5); } .seg-6 { fill: var(--series-6); }
.seg-7 { fill: var(--series-7); } .seg-rest { fill: var(--rest); }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.line.main { stroke: var(--series-1); }
.line.sub { stroke: var(--series-2); }
.dot { stroke: var(--surface); stroke-width: 2; }
.hit { fill: transparent; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--faint); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.label { fill: var(--text); font-size: 12px; }
.value { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.legend-text { fill: var(--muted); font-size: 12px; }
"""


def _theme_vars(theme: dict) -> str:
    return " ".join("--%s: %s;" % item for item in theme.items())


def _themed(selector: str) -> str:
    return (
        "%s { %s }\n@media (prefers-color-scheme: dark) { %s { %s } }\n"
        % (selector, _theme_vars(THEME_LIGHT), selector, _theme_vars(THEME_DARK))
    )


def nice_step(value: float) -> float:
    if value <= 0:
        return 1
    exponent = 10 ** math.floor(math.log10(value))
    for multiple in (1, 2, 2.5, 5, 10):
        if multiple * exponent >= value:
            return multiple * exponent
    return 10 * exponent


def axis_ticks(top_value: float, count: int = 4) -> list:
    step = nice_step(top_value / count)
    steps = max(1, math.ceil(top_value / step))
    return [step * i for i in range(steps + 1)]


def _num(value: float) -> str:
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _top_rounded_bar(css: str, x, y, width, height, radius=3) -> str:
    radius = min(radius, width / 2, height)
    return (
        '<path class="%s" d="M%s %sv%sa%s %s 0 0 1 %s %sh%sa%s %s 0 0 1 %s %sv%sz"/>'
        % (css, _num(x), _num(y + height), _num(-(height - radius)),
           _num(radius), _num(radius), _num(radius), _num(-radius),
           _num(width - 2 * radius), _num(radius), _num(radius), _num(radius),
           _num(radius), _num(height - radius))
    )


def _right_rounded_bar(css: str, x, y, width, height, radius=3) -> str:
    radius = min(radius, height / 2, width)
    return (
        '<path class="%s" d="M%s %sh%sa%s %s 0 0 1 %s %sv%sa%s %s 0 0 1 %s %sh%sz"/>'
        % (css, _num(x), _num(y), _num(width - radius), _num(radius), _num(radius),
           _num(radius), _num(radius), _num(height - 2 * radius), _num(radius),
           _num(radius), _num(-radius), _num(radius), _num(-(width - radius)))
    )


def _svg_open(width: int, height: int, label: str, standalone: bool) -> str:
    namespace = ' xmlns="%s"' % SVG_NS if standalone else ""
    return ('<svg%s viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'aria-label="%s" font-family=\'%s\'>'
            % (namespace, width, height, width, height, escape(label), FONT_STACK))


def _legend(x: int, y: int) -> str:
    items = []
    for offset, css, text in ((0, "main", "Main thread"), (110, "sub", "Subagents")):
        items.append(
            '<rect class="swatch %s" x="%d" y="%d" width="10" height="10" rx="2"/>'
            '<text class="legend-text" x="%d" y="%d">%s</text>'
            % (css, x + offset, y - 9, x + offset + 16, y, text)
        )
    return "".join(items)


class DailyChart:
    """Stacked columns of effective usage per day, main thread below subagents."""

    WIDTH, HEIGHT = 960, 300
    LEFT, RIGHT, TOP, BOTTOM = 56, 12, 36, 30
    GAP = 2
    STANDALONE_PAD = 20

    def __init__(self, days: list):
        self.days = days
        self.plot_w = self.WIDTH - self.LEFT - self.RIGHT
        self.plot_h = self.HEIGHT - self.TOP - self.BOTTOM
        peak = max([d["main"] + d["sub"] for d in days] or [0])
        self.ticks = axis_ticks(peak or 1)
        self.band = self.plot_w / max(len(days), 1)

    def svg(self, standalone: bool) -> str:
        label = "Effective tokens by day, main thread and subagents stacked"
        if standalone:
            return self._standalone(label)
        return "".join([_svg_open(self.WIDTH, self.HEIGHT, label, False)]
                       + self._body() + ["</svg>"])

    def _standalone(self, label: str) -> str:
        pad = self.STANDALONE_PAD
        parts = [
            _svg_open(self.WIDTH + 2 * pad, self.HEIGHT + 2 * pad + 28, label, True),
            "<style>%s%s.bg { fill: var(--surface); stroke: var(--border); }"
            ".heading { fill: var(--text); font-size: 15px; font-weight: 600; }</style>"
            % (_themed("svg"), CHART_CSS),
            '<rect class="bg" x="0.5" y="0.5" width="%d" height="%d" rx="12"/>'
            % (self.WIDTH + 2 * pad - 1, self.HEIGHT + 2 * pad + 27),
            '<text class="heading" x="%d" y="%d">Effective tokens by day</text>'
            % (pad + 4, pad + 14),
            '<g transform="translate(%d %d)">' % (pad, pad + 28),
        ]
        return "".join(parts + self._body() + ["</g></svg>"])

    def _body(self) -> list:
        parts = [_legend(self.LEFT, 18)]
        parts.extend(self._grid())
        parts.extend(self._column(i, day) for i, day in enumerate(self.days))
        parts.extend(self._x_labels())
        return parts

    def _y(self, value: float) -> float:
        return self.TOP + self.plot_h - value / self.ticks[-1] * self.plot_h

    def _grid(self) -> list:
        lines = []
        for tick in self.ticks:
            y = self._y(tick)
            css = "baseline" if tick == 0 else "gridline"
            lines.append('<line class="%s" x1="%d" x2="%d" y1="%s" y2="%s"/>'
                         % (css, self.LEFT, self.WIDTH - self.RIGHT, _num(y), _num(y)))
            lines.append('<text class="tick" x="%d" y="%s" text-anchor="end">%s</text>'
                         % (self.LEFT - 8, _num(y + 4), fmt.tokens(tick)))
        return lines

    def _column(self, index: int, day: dict) -> str:
        width = max(self.band * 0.64, 1)
        x = self.LEFT + index * self.band + (self.band - width) / 2
        base = self._y(0)
        main_h = base - self._y(day["main"])
        sub_h = base - self._y(day["sub"])
        title = "%s: main thread %s, subagents %s" % (
            day["date"], fmt.tokens(day["main"]), fmt.tokens(day["sub"]))
        parts = ["<g><title>%s</title>" % escape(title)]
        if day["main"]:
            shape = _top_rounded_bar if not day["sub"] else self._plain_bar
            parts.append(shape("bar main", x, base - main_h, width, main_h))
        if day["sub"]:
            gap = self.GAP if day["main"] else 0
            top = base - main_h - gap - sub_h
            parts.append(_top_rounded_bar("bar sub", x, top, width, sub_h))
        parts.append("</g>")
        return "".join(parts)

    @staticmethod
    def _plain_bar(css, x, y, width, height) -> str:
        return ('<rect class="%s" x="%s" y="%s" width="%s" height="%s"/>'
                % (css, _num(x), _num(y), _num(width), _num(height)))

    def _x_labels(self) -> list:
        every = max(1, math.ceil(len(self.days) / 10))
        labels = []
        for index in range(0, len(self.days), every):
            x = self.LEFT + (index + 0.5) * self.band
            labels.append('<text class="tick" x="%s" y="%d" text-anchor="middle">%s</text>'
                          % (_num(x), self.HEIGHT - 10, self.days[index]["date"][5:]))
        return labels


def daily_chart_svg(days: list, standalone: bool = False) -> str:
    return DailyChart(days).svg(standalone)


class BarList:
    """Horizontal bars, one row per entry, value printed at the bar end."""

    WIDTH = 460
    LABEL_W, VALUE_W, ROW_H, BAR_H, TOP = 172, 64, 30, 14, 8

    def __init__(self, label: str, entries: list):
        self.label = label
        self.entries = entries
        self.peak = max([value for _name, value, _tip in entries] or [1]) or 1

    def svg(self) -> str:
        height = self.TOP * 2 + self.ROW_H * len(self.entries)
        parts = [_svg_open(self.WIDTH, height, self.label, False)]
        parts.extend(self._row(i, entry) for i, entry in enumerate(self.entries))
        parts.append("</svg>")
        return "".join(parts)

    def _row(self, index: int, entry: tuple) -> str:
        name, value, tip = entry
        y = self.TOP + index * self.ROW_H
        bar_y = y + (self.ROW_H - self.BAR_H) / 2
        span = self.WIDTH - self.LABEL_W - self.VALUE_W
        width = max(value / self.peak * span, 2)
        return (
            '<g><title>%s</title>'
            '<text class="label" x="0" y="%s">%s</text>%s'
            '<text class="value" x="%s" y="%s">%s</text></g>'
            % (escape(tip), _num(bar_y + 11), escape(name),
               _right_rounded_bar("bar main", self.LABEL_W, bar_y, width, self.BAR_H),
               _num(self.LABEL_W + width + 8), _num(bar_y + 11), fmt.tokens(value))
        )


class BandChart:
    """Paired horizontal bars per context band: main thread and subagents."""

    WIDTH = 960
    LABEL_W, VALUE_W, BAR_H, PAIR_GAP, ROW_H, TOP = 84, 56, 12, 2, 40, 34

    def __init__(self, bands: list):
        self.bands = bands
        self.peak = max([max(c["main"], c["sub"]) for _n, c in bands] or [1]) or 1

    def svg(self) -> str:
        height = self.TOP + self.ROW_H * len(self.bands)
        parts = [_svg_open(self.WIDTH, height, "Turns per context band, main thread "
                           "and subagents", False), _legend(self.LABEL_W, 16)]
        parts.extend(self._band(i, band) for i, band in enumerate(self.bands))
        parts.append("</svg>")
        return "".join(parts)

    def _band(self, index: int, band: tuple) -> str:
        name, counts = band
        y = self.TOP + index * self.ROW_H
        span = self.WIDTH - self.LABEL_W - self.VALUE_W
        tip = "%s context: %d main thread turns, %d subagent turns" % (
            name, counts["main"], counts["sub"])
        parts = ['<g><title>%s</title><text class="label" x="0" y="%s">%s</text>'
                 % (escape(tip), _num(y + self.BAR_H + 2), escape(name))]
        for offset, key in ((0, "main"), (self.BAR_H + self.PAIR_GAP, "sub")):
            width = max(counts[key] / self.peak * span, 2)
            bar_y = y + offset
            parts.append(_right_rounded_bar("bar " + key, self.LABEL_W, bar_y, width,
                                            self.BAR_H))
            parts.append('<text class="value" x="%s" y="%s">%d</text>'
                         % (_num(self.LABEL_W + width + 8), _num(bar_y + 10), counts[key]))
        parts.append("</g>")
        return "".join(parts)


COMPOSITION_GROUPS = (
    ("1", "System prompt", ("system_prompt",)),
    ("2", "Instructions", ("instr_user", "instr_project", "instr_local", "instr_other")),
    ("3", "Memory", ("instr_memory",)),
    ("4", "Skill listing", ("skill_listing",)),
    ("5", "Agent listing", ("agent_listing",)),
    ("6", "Tools and MCP", ("deferred_tools", "mcp")),
    ("7", "Hook context", ("hook_context",)),
    ("rest", "Rest of first turn", ("rest",)),
)
THREAD_LABELS = (("main", "Main thread"), ("sub", "Subagents"))


def grouped_composition(parts: list) -> list:
    """[(css, label, tokens)] for each composition group, from [(part, tokens)]."""
    values = dict(parts)
    return [(css, label, sum(values.get(key, 0) for key in keys))
            for css, label, keys in COMPOSITION_GROUPS]


class CompositionChart:
    """One stacked horizontal bar per thread: the average start, split by part."""

    WIDTH = 960
    LABEL_W, VALUE_W, BAR_H, ROW_H, GAP = 96, 64, 22, 40, 2
    LEGEND_ROW, CHAR_W, SWATCH_GAP, ITEM_GAP = 20, 6.6, 16, 22

    def __init__(self, composition: dict):
        self.rows = [(name, grouped_composition(composition[key]))
                     for key, name in THREAD_LABELS]
        self.peak = max(sum(v for _c, _l, v in groups) for _n, groups in self.rows) or 1
        self.legend, self.top = self._legend_layout()

    def svg(self) -> str:
        height = self.top + self.ROW_H * len(self.rows)
        parts = [_svg_open(self.WIDTH, height, "Average starting context by part, "
                           "main thread and subagents", False)]
        parts.extend(self.legend)
        parts.extend(self._row(i, row) for i, row in enumerate(self.rows))
        parts.append("</svg>")
        return "".join(parts)

    def _legend_layout(self) -> tuple:
        items, x, y = [], 0.0, 16
        for css, label, _keys in COMPOSITION_GROUPS:
            width = self.SWATCH_GAP + len(label) * self.CHAR_W
            if x and x + width > self.WIDTH:
                x, y = 0.0, y + self.LEGEND_ROW
            items.append('<rect class="seg-%s" x="%s" y="%d" width="10" height="10" rx="2"/>'
                         '<text class="legend-text" x="%s" y="%d">%s</text>'
                         % (css, _num(x), y - 9, _num(x + self.SWATCH_GAP), y, label))
            x += width + self.ITEM_GAP
        return items, y + 18

    def _row(self, index: int, row: tuple) -> str:
        name, groups = row
        y = self.top + index * self.ROW_H
        total = sum(value for _c, _l, value in groups)
        parts = ['<text class="label" x="0" y="%s">%s</text>'
                 % (_num(y + self.BAR_H / 2 + 4), name)]
        parts.extend(self._segments(name, y, groups, total))
        end = self.LABEL_W + total / self.peak * self._span()
        parts.append('<text class="value" x="%s" y="%s">%s</text>'
                     % (_num(end + 8), _num(y + self.BAR_H / 2 + 4), fmt.tokens(total)))
        return "<g>%s</g>" % "".join(parts)

    def _span(self) -> float:
        return self.WIDTH - self.LABEL_W - self.VALUE_W

    def _segments(self, name: str, y: float, groups: list, total: int) -> list:
        shown = [group for group in groups if group[2]]
        x, segments = float(self.LABEL_W), []
        for index, (css, label, value) in enumerate(shown):
            width = value / self.peak * self._span()
            last = index == len(shown) - 1
            drawn = width if last else max(width - self.GAP, 0.5)
            tip = "%s, %s: about %s tokens (%s)" % (
                name, label, fmt.tokens(value), fmt.percent(value, total))
            shape = (_right_rounded_bar("seg seg-" + css, x, y, drawn, self.BAR_H)
                     if last else
                     '<rect class="seg seg-%s" x="%s" y="%s" width="%s" height="%s"/>'
                     % (css, _num(x), _num(y), _num(drawn), _num(self.BAR_H)))
            segments.append("<g><title>%s</title>%s</g>" % (escape(tip), shape))
            x += width
        return segments


class TrendChart:
    """Median starting context per ISO week, one line per thread kind."""

    WIDTH, HEIGHT = 460, 240
    LEFT, RIGHT, TOP, BOTTOM = 48, 16, 34, 28

    def __init__(self, weeks: list):
        self.weeks = weeks
        peak = max([w[key] or 0 for w in weeks for key in ("main", "sub")] or [0])
        self.ticks = axis_ticks(peak or 1)
        self.plot_w = self.WIDTH - self.LEFT - self.RIGHT
        self.plot_h = self.HEIGHT - self.TOP - self.BOTTOM
        self.step = self.plot_w / max(len(weeks), 1)

    def svg(self) -> str:
        parts = [_svg_open(self.WIDTH, self.HEIGHT, "Median starting context by ISO week, "
                           "main thread and subagents", False), _legend(self.LEFT, 16)]
        parts.extend(self._grid())
        for key in ("main", "sub"):
            parts.append(self._line(key))
        parts.extend(self._week(i, week) for i, week in enumerate(self.weeks))
        parts.append("</svg>")
        return "".join(parts)

    def _x(self, index: int) -> float:
        return self.LEFT + (index + 0.5) * self.step

    def _y(self, value: float) -> float:
        return self.TOP + self.plot_h - value / self.ticks[-1] * self.plot_h

    def _grid(self) -> list:
        lines = []
        for tick in self.ticks:
            y = self._y(tick)
            lines.append('<line class="%s" x1="%d" x2="%d" y1="%s" y2="%s"/>'
                         % ("baseline" if tick == 0 else "gridline", self.LEFT,
                            self.WIDTH - self.RIGHT, _num(y), _num(y)))
            lines.append('<text class="tick" x="%d" y="%s" text-anchor="end">%s</text>'
                         % (self.LEFT - 8, _num(y + 4), fmt.tokens(tick)))
        return lines

    def _line(self, key: str) -> str:
        path, pen = [], "M"
        for index, week in enumerate(self.weeks):
            if week[key] is None:
                pen = "M"
                continue
            path.append("%s%s %s" % (pen, _num(self._x(index)), _num(self._y(week[key]))))
            pen = "L"
        return '<path class="line %s" d="%s"/>' % (key, "".join(path)) if path else ""

    def _week(self, index: int, week: dict) -> str:
        x = self._x(index)
        tip = "%s: main thread %s, subagents %s" % (
            week["week"], fmt.average(week["main"]), fmt.average(week["sub"]))
        parts = ["<g><title>%s</title>" % escape(tip),
                 '<rect class="hit" x="%s" y="%d" width="%s" height="%d"/>'
                 % (_num(x - self.step / 2), self.TOP, _num(self.step), self.plot_h)]
        for key in ("main", "sub"):
            if week[key] is not None:
                parts.append('<circle class="dot %s" cx="%s" cy="%s" r="4"/>'
                             % (key, _num(x), _num(self._y(week[key]))))
        parts.append('<text class="tick" x="%s" y="%d" text-anchor="middle">%s</text></g>'
                     % (_num(x), self.HEIGHT - 8, escape(week["week"][5:])))
        return "".join(parts)


PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 %(font)s; -webkit-font-smoothing: antialiased; }
.page { max-width: 1040px; margin: 0 auto; padding: 48px 16px 64px; }
header { margin-bottom: 28px; }
.eyebrow { margin: 0 0 6px; color: var(--muted); font-size: 12px; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 0; font-size: 30px; line-height: 1.2; letter-spacing: -.02em; font-weight: 650; }
.meta { margin: 8px 0 0; color: var(--muted); }
h2 { margin: 0 0 4px; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
.sub { margin: 0 0 18px; color: var(--muted); font-size: 13px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; }
.tile { padding: 16px 18px; }
.tile .k { color: var(--muted); font-size: 12px; font-weight: 500; }
.tile .v { margin-top: 4px; font-size: 26px; font-weight: 600; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; }
.tile .n { margin-top: 2px; color: var(--faint); font-size: 12px; }
.card { padding: 20px 22px; margin-bottom: 16px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 16px; }
.grid2 .card { margin-bottom: 0; }
.grid2 + .card, .grid2 { margin-bottom: 16px; }
.chart { overflow-x: auto; }
.chart svg { display: block; width: 100%%; height: auto; }
.chart.wide svg { min-width: 560px; }
.chart text { font-family: inherit; }
table { width: 100%%; border-collapse: collapse; font-size: 13px;
  font-variant-numeric: tabular-nums; }
.table { overflow-x: auto; }
th { color: var(--muted); font-weight: 500; text-align: left; padding: 0 10px 8px 0;
  border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--grid); white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; }
code { font: 12px %(mono)s; color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; }
.stat .v { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stat .k { color: var(--muted); font-size: 13px; }
.split { margin-top: 18px; }
.key { display: inline-block; width: 10px; height: 10px; margin-right: 8px;
  border-radius: 2px; vertical-align: -1px; }
.key.seg-1 { background: var(--series-1); } .key.seg-2 { background: var(--series-2); }
.key.seg-3 { background: var(--series-3); } .key.seg-4 { background: var(--series-4); }
.key.seg-5 { background: var(--series-5); } .key.seg-6 { background: var(--series-6); }
.key.seg-7 { background: var(--series-7); } .key.seg-rest { background: var(--rest); }
footer { margin-top: 24px; color: var(--faint); font-size: 12px; }
@media (max-width: 520px) { .grid2 { grid-template-columns: 1fr; } h1 { font-size: 24px; } }
""" % {"font": FONT_STACK, "mono": MONO_STACK}


class HtmlReport:
    def __init__(self, summary: dict, skipped: int = 0):
        self.s = summary
        self.skipped = skipped

    def render(self) -> str:
        body = self._sections() if self.s["turns"] else [
            '<section class="card"><h2>No rows</h2>'
            '<p class="sub">No rows match this selection.</p></section>']
        return (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Claude usage report</title><style>%s%s%s</style></head>'
            '<body><main class="page">%s%s%s</main></body></html>\n'
            % (_themed(":root"), PAGE_CSS, CHART_CSS, self._header(),
               "".join(body), self._footer())
        )

    def _sections(self) -> list:
        sections = [self._kpis(), self._daily(), self._breakdowns(), self._bands(),
                    self._agents(), self._sessions(), self._shape()]
        if self.s["starting_context"]["starts"]:
            sections += [self._starting(), self._composition(), self._trend_and_skills()]
        if self.s["skills_agents"]["unused"]:
            sections.append(self._unused())
        return sections

    def _header(self) -> str:
        if not self.s["turns"]:
            meta = "No data"
        else:
            meta = "%s to %s, %d account%s: %s" % (
                self.s["first_day"], self.s["last_day"], len(self.s["accounts"]),
                "" if len(self.s["accounts"]) == 1 else "s",
                ", ".join(self.s["accounts"]))
        return ('<header><p class="eyebrow">Claude Code usage</p>'
                '<h1>Usage report</h1><p class="meta">%s</p></header>' % escape(meta))

    def _tile(self, key: str, value: str, note: str) -> str:
        return ('<div class="tile"><div class="k">%s</div><div class="v">%s</div>'
                '<div class="n">%s</div></div>' % (key, escape(value), escape(note)))

    def _kpis(self) -> str:
        t = self.s["totals"]
        context_in = t["input"] + t["cache_read"] + t["cache_write"]
        tiles = [
            self._tile("Effective tokens", fmt.tokens(t["effective"]), "weighted, see footer"),
            self._tile("Turns", "{:,}".format(t["turns"]), "assistant responses"),
            self._tile("Sessions", "{:,}".format(self.s["sessions"]),
                       "%d subagents" % self.s["subagent_shares"]["agents"]),
            self._tile("Subagent share", fmt.percent(self.s["sub"]["effective"], t["effective"]),
                       "of effective tokens"),
            self._tile("Cache read share", fmt.percent(t["cache_read"], context_in),
                       "of context tokens"),
        ]
        if self.s["priced"]:
            tiles.append(self._tile("Approximate cost", fmt.dollars(t["cost"]),
                                    "%d turns unpriced" % t["unpriced"]))
        else:
            tiles.append(self._tile("Peak context", fmt.tokens(t["peak_context"]),
                                    "largest single turn"))
        return '<section class="kpis">%s</section>' % "".join(tiles)

    def _card(self, title: str, note: str, inner: str, css: str = "chart") -> str:
        return ('<section class="card"><h2>%s</h2><p class="sub">%s</p>'
                '<div class="%s">%s</div></section>' % (title, note, css, inner))

    def _daily(self) -> str:
        return self._card("Effective tokens by day",
                          "Main thread and subagents stacked, UTC days.",
                          daily_chart_svg(self.s["days"]), "chart wide")

    def _breakdowns(self) -> str:
        models = [(e["name"], e["effective"], "%s: %s effective, %d turns"
                   % (e["name"], fmt.tokens(e["effective"]), e["turns"]))
                  for e in self.s["by_model"]]
        types = [(e["name"], e["effective"], "%s: %s effective, %d turns, %d agents"
                  % (e["name"], fmt.tokens(e["effective"]), e["turns"], e["agents"]))
                 for e in self.s["by_agent_type"]]
        return '<div class="grid2">%s%s</div>' % (
            self._card("Effective tokens by model", "All threads.",
                       BarList("Effective tokens by model", models).svg()),
            self._card("Effective tokens by agent type",
                       "(main) is the main thread of each session.",
                       BarList("Effective tokens by agent type", types).svg()))

    def _bands(self) -> str:
        return self._card("Context per turn",
                          "How many turns ran with each context size.",
                          BandChart(self.s["context_bands"]).svg(), "chart wide")

    def _table(self, header: list, rows: list) -> str:
        head = "".join('<th class="%s">%s</th>' % ("num" if numeric else "", label)
                       for label, numeric in header)
        body = "".join(
            "<tr>%s</tr>" % "".join('<td class="%s">%s</td>' % ("num" if numeric else "", cell)
                                    for cell, (_l, numeric) in zip(row, header))
            for row in rows)
        return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)

    def _cost_cells(self, entry: dict) -> list:
        return [fmt.dollars(entry["cost"])] if self.s["priced"] else []

    def _cost_head(self) -> list:
        return [("Cost", True)] if self.s["priced"] else []

    def _agents(self) -> str:
        header = [("Agent", False), ("Type", False), ("Model", False), ("Turns", True),
                  ("Peak context", True), ("Effective", True)] + self._cost_head()
        rows = [["<code>%s</code>" % escape(e["agent"]), escape(e["agent_type"]),
                 escape(e["model"]), str(e["turns"]), fmt.tokens(e["peak_context"]),
                 fmt.tokens(e["effective"])] + self._cost_cells(e)
                for e in self.s["top_agents"]]
        return self._card("Top subagents", "By effective tokens. Ids are salted hashes.",
                          self._table(header, rows), "table")

    def _sessions(self) -> str:
        header = [("Session", False), ("Account", False), ("Turns", True),
                  ("Subagents", True), ("Peak context", True),
                  ("Effective", True)] + self._cost_head()
        rows = [["<code>%s</code>" % escape(e["session"]), escape(e["account"]),
                 str(e["turns"]), str(e["agents"]), fmt.tokens(e["peak_context"]),
                 fmt.tokens(e["effective"])] + self._cost_cells(e)
                for e in self.s["top_sessions"]]
        return self._card("Top sessions", "Including their subagents.",
                          self._table(header, rows), "table")

    def _shape(self) -> str:
        shares = self.s["subagent_shares"]
        stats = [
            (str(shares["agents"]), "subagents"),
            (fmt.percent(shares["peak_over_200k"], shares["agents"]),
             "peaked above 200K context"),
            (fmt.percent(shares["short"], shares["agents"]), "finished in 10 turns or fewer"),
        ]
        inner = "".join('<div class="stat"><div class="v">%s</div><div class="k">%s</div></div>'
                        % (escape(v), k) for v, k in stats)
        return self._card("Subagent shape", "How big and how long subagents ran.",
                          inner, "stats")

    def _starting(self) -> str:
        start = self.s["starting_context"]
        threads = start["threads"]
        stats = []
        for key, name in THREAD_LABELS:
            stats += [(fmt.average(threads[key]["median"]), "%s median" % name.lower()),
                      (fmt.average(threads[key]["p90"]), "%s p90" % name.lower())]
        compactions = start["compactions"]
        stats.append((str(compactions["main"] + compactions["sub"]), "compactions"))
        forks = start["forks"]
        stats.append((str(forks["main"] + forks["sub"]), "forked or continued"))
        inner = "".join('<div class="stat"><div class="v">%s</div><div class="k">%s</div></div>'
                        % (escape(v), k) for v, k in stats)
        return self._card("Starting context",
                          "Tokens in the first turn of each session and subagent, "
                          "from %d start events. Forked or continued ones start with "
                          "copied history and are only counted." % start["starts"],
                          inner, "stats")

    def _composition(self) -> str:
        composition = self.s["starting_context"]["composition"]
        groups = {key: grouped_composition(composition[key]) for key, _n in THREAD_LABELS}
        header = [("Part", False), ("Main thread", True), ("Subagents", True)]
        rows = [['<span class="key seg-%s"></span>%s' % (css, label),
                 fmt.tokens(main), fmt.tokens(sub)]
                for (css, label, main), (_c, _l, sub) in zip(groups["main"], groups["sub"])]
        return ('<section class="card"><h2>What a thread starts with</h2>'
                '<p class="sub">Average start event, estimated tokens (characters / 4). '
                'Rest of first turn is everything else the first turn carried, '
                'such as tool definitions and the first prompt.</p>'
                '<div class="chart wide">%s</div><div class="table split">%s</div></section>'
                % (CompositionChart(composition).svg(), self._table(header, rows)))

    def _trend_and_skills(self) -> str:
        top = self.s["skills_agents"]["top_skills"]
        skills = [(e["name"], e["uses"], "%s: %d uses" % (e["name"], e["uses"]))
                  for e in top]
        trend = self._card("Median starting context by week",
                           "ISO weeks, UTC.", TrendChart(
                               self.s["starting_context"]["weeks"]).svg())
        if not skills:
            return trend
        return '<div class="grid2">%s%s</div>' % (trend, self._card(
            "Top skills", "Uses: first time a session or subagent invoked a skill.",
            BarList("Top skills by uses", skills).svg()))

    def _unused(self) -> str:
        header = [("Name", False), ("Kind", False), ("Days listed", True)]
        rows = [["<code>%s</code>" % escape(e["name"]), escape(e["kind"]), str(e["days"])]
                for e in self.s["skills_agents"]["unused"]]
        return self._card("Listed but never used",
                          "Skills no one invoked and agent types no one spawned, "
                          "though a listing offered them in this period.",
                          self._table(header, rows), "table")

    def _footer(self) -> str:
        w = self.s["weights"]
        text = ("Effective tokens weigh input x%s, cache read x%s, cache write x%s, "
                "output x%s." % tuple(_num(w[k]) for k in
                                      ("input", "cache_read", "cache_write", "output")))
        if self.skipped:
            text += " %d row(s) failed the guard and were skipped." % self.skipped
        if self.s["priced"]:
            text += " Cost is approximate, from the price table in your config."
        return "<footer>%s</footer>" % escape(text)


def render_html(summary: dict, skipped: int = 0) -> str:
    return HtmlReport(summary, skipped).render()


def _write(path: str, text: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def cmd_report(args, config) -> int:
    try:
        summary, skipped = build(args, config)
    except (StoreError, AnalysisError, NameMapError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    _write(args.out, render_html(summary, skipped))
    print("wrote report for %d turns to %s" % (summary["turns"], args.out))
    svg_path = getattr(args, "svg", None)
    if svg_path:
        _write(svg_path, daily_chart_svg(summary["days"], standalone=True) + "\n")
        print("wrote daily chart to %s" % svg_path)
    return 0
