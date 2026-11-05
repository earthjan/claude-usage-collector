# Analysis

**`analyze` prints the breakdowns as text. `report` draws the main
breakdowns from `analyze` as charts on one static HTML page.** The per-token
totals, the per-model cost column and the per-agent-type average are in
`analyze` only (the report shows some of them in hover tooltips). Both read any store folder and need no config,
so they run on whichever machine holds a copy of the data.

```sh
claude-usage analyze --store ~/usage-data
claude-usage analyze --store ~/usage-data --since 2026-01-01 --account work
claude-usage report  --store ~/usage-data --out report.html --since 2026-01-01
```

| Flag | Command | Meaning |
|---|---|---|
| `--store PATH` | both | The store folder to read. Defaults to the destination in your config, so it is needed only when there is no config. |
| `--since YYYY-MM-DD` | both | Only rows on or after this UTC day. |
| `--account LABEL` | `analyze` | Only rows for one account. |
| `--out PATH` | `report` | Where to write the HTML. Default `report.html`. |
| `--svg PATH` | `report` | Also write the daily chart as a standalone SVG file. |

A demo report built from synthetic data is in
[demo/report.html](demo/report.html). The demo store holds two accounts and
every stream. You can rebuild it, and the preview image `docs/img/report-preview.svg`,
with:

```sh
python3 -m usage_collector --config /nonexistent report \
  --store tests/fixtures/demo-store \
  --out docs/demo/report.html --svg docs/img/report-preview.svg
```

`--config /nonexistent` makes sure your own `weights` and `prices` do not
change the demo.

## What the report answers

**The breakdowns are built to show where tokens go, and especially how much
goes to subagents and to large contexts.**

- **Totals:** each token type, the weighted figure, and cost when prices
  are set.
- **Main thread versus subagents:** what share of usage each takes.
- **By model.**
- **By agent type:** how many agents ran, their total, and the average per
  agent.
- **Context bands:** how many turns ran with a context under 50K tokens,
  50K to 150K, 150K to 300K, and over 300K, split into main thread and
  subagents. Large contexts are where cache reads pile up.
- **Top 15 agents by effective (weighted) usage:** agent hash, type, model, number of turns and
  peak context.
- **Top 10 sessions by effective (weighted) usage.**
- **Two subagent warning signs:** the share of subagents whose peak context
  went over 200K, and the share that finished in 10 turns or fewer. Many
  short agents with huge contexts usually mean a lot of context was handed
  over for a small job.
- **Starting context:** how big a context is before any work starts, read
  from the context size files.
  - The median and 90th percentile of `first_turn_context`, for main
    sessions and for subagents.
  - The average makeup of a `start` event, part by part (system prompt,
    each kind of instruction file, skill listing, agent listing, deferred
    tools, MCP instructions, hook context), as estimated tokens.
  - The median starting context per ISO week, so you can see whether your
    setup grows over time.
  - How many `compact` events there were.
  - How many `fork` events there were. A forked or continued session, or
    a forked subagent, starts with copied history, so its first turn is
    not a fresh start. These are counted but left out of the median, the
    90th percentile, the makeup and the weekly trend.
- **Skills and agents:** the skills used most, from the skill use files.
  Then the names the catalog listed during the period that were never
  used: skills that were never invoked, and agent types that were never
  spawned (checked against the usage rows' `agent_type`). A listed name
  that is not a slug, for example one containing `/` or longer than 64
  characters, is stored as a hash, so it shows as 12 hex characters here.

The report adds three pieces for these: a stacked bar of the average
starting context by part, a weekly starting-context trend chart, and a
"listed but never used" table.

### Estimated tokens

**Context sizes are stored as characters, and `analyze` turns them into
estimated tokens by dividing by 4.** That is a rough rule of thumb for
English text and code, not a tokenizer. Use it to compare parts with each
other and over time. `first_turn_context` is a real token count from the
API, so the gap between it and the sum of the estimated parts is roughly
the first prompt, the built-in tool definitions and anything else that is
not attached as one of the measured parts.

A big instruction file or a long skill listing is paid for on every turn
of every session, mostly as cache reads. The starting-context section is
there to make that cost visible.

The report is one HTML file with inline SVG charts. It makes no network
requests, needs no JavaScript, and follows your system's light or dark
setting. You can attach it to an email or open it offline.

## Weighted usage

**Raw token counts treat a cache read the same as an output token, which
hides where the cost is.** So both commands also show a weighted
"effective" figure:

```
effective = input x 1 + cache_read x 0.1 + cache_write x 2 + output x 5
```

The default weights roughly follow how prompt caching and output are
priced relative to plain input. Change them in the config:

```json
"weights": {"input": 1, "cache_read": 0.1, "cache_write": 2, "output": 5}
```

## Dollar cost

**No prices ship with the tool, so no cost is shown until you add them.**
Prices change, and a wrong built-in number would be worse than none. Copy
current prices from the official Anthropic pricing page into the config, in
dollars per million tokens, keyed by the start of the model name:

```json
"prices": {
  "claude-opus":   {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0},
  "claude-sonnet": {"input": 0.0, "cache_read": 0.0, "cache_write": 0.0, "output": 0.0}
}
```

Replace every `0.0` with the real price. A row's model is matched against
these prefixes, and the longest matching prefix wins. A token kind left out
of a price table counts as free. Rows whose model matches no prefix get no
cost, rather than a guessed one.

The result is an estimate. It is what the tokens would cost at the prices
you entered. It is not your bill: plans, discounts and batch pricing are
not taken into account.

## Working with the CSV directly

The store is plain CSV, so any tool can read it. For example, with Python:

```python
import csv, glob

def read(pattern):
    rows = []
    for path in glob.glob("usage-data/*/????/??/" + pattern):
        with open(path) as handle:
            rows.extend(csv.DictReader(handle))
    return rows

usage = read("????-??-??.csv")
starts = [r for r in read("*.context.csv") if r["event"] == "start"]
print(sum(int(r["output"]) for r in usage))
print(sum(int(r["first_turn_context"]) for r in starts) / max(len(starts), 1))
```

Match usage files with `????-??-??.csv`, not `*.csv`, or the context,
skills and catalog files are read as usage rows too. The patterns skip
`checks/`, which holds JSON. Column meanings are in
[schema.md](schema.md).
