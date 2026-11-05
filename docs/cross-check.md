# Cross-check with ccusage

**`ccusage` reads the same transcripts in its own way, so its daily totals
are an independent check on the collector.** It is pointed at the folders
in your config's `sources`, so it sees the same files the collector reads. Once a month the collector
stores those totals next to the rows. `compare` then sums the rows the same
way and shows any gap.

![The collector and ccusage read the same transcripts independently. compare sums rows per day and model and checks them against ccusage totals.](img/cross-check.svg)

[ccusage](https://github.com/ryoppippi/ccusage) is a separate open source
tool. Install it yourself if you want the cross-check. Everything else works
without it.

## Why a cross-check helps

A collector that silently drops or doubles rows would give wrong research
results that look fine. Two things make that a real risk:

- Claude Code's transcript format is not a documented API and can change.
- Forked sessions copy their parent's messages, so naive counting double
  counts.

If both tools agree day by day and model by model, both are probably
reading the files right. If they disagree, `compare` tells you where to
look.

## What is stored

`claude-usage crosscheck` runs:

```sh
CLAUDE_CONFIG_DIR=<folder holding each projects source> \
  ccusage daily --json --since YYYYMMDD --until YYYYMMDD --timezone UTC
```

for one calendar month. It never runs the `ccusage session` report, which
includes project paths.

- `CLAUDE_CONFIG_DIR` is the parent of each folder in `sources`, joined
  with commas. With the default source `~/.claude/projects` that is
  `~/.claude`. Every source must end in `projects`, or the cross-check
  stops with an error.
- `--timezone UTC` makes `ccusage` group days in UTC, the same way rows are
  grouped. Without it, `ccusage` uses your system time zone and every day
  would be shifted against the rows. It needs a `ccusage` version that has
  the `--timezone` option.

From the JSON it keeps, per day and per model, only four integers: input,
output, cache read and cache write tokens. Costs, project names and any
other field are dropped. The result goes through the same fail-closed guard
as rows, then into `checks/<account>/<machine>/YYYY-MM.json` in the store.
Each machine has its own file, so two machines sharing one account label
never edit the same file, and `compare` adds their totals up. The format
is in [schema.md](schema.md#cross-check-totals).

## When it runs

- **Automatically,** as part of `claude-usage run`, for the previous UTC
  month, until that month has been stored once. Because both the rows and
  `ccusage` use UTC days, the month is complete as soon as it is over in
  UTC, even if it is still the last evening of the month on your clock. A machine that was asleep on the
  first of the month catches up on its next run. It runs between collect
  and sync, so the totals are pushed in the same run.
- **By hand,** for any month:

```sh
claude-usage crosscheck                  # the previous month
claude-usage crosscheck --month 2025-12
```

The automatic run is skipped quietly when `crosscheck.enabled` is `false`
or the command is not on `PATH`. Set `crosscheck.command` to a full path if
`ccusage` lives somewhere the schedule cannot see.

`ccusage` only sees transcripts that still exist. Run the cross-check before
Claude Code's `cleanupPeriodDays` removes a month's files, or the totals
will be low.

## Comparing

```sh
claude-usage compare --store ~/usage-data --month 2025-12
claude-usage compare --store ~/usage-data --month 2025-12 --tolerance 0.05
```

For each account, day and model, `compare` sums the rows and sets them
against the stored totals, one token kind at a time. Without `--month` it
checks every month that has a check file. `--store` defaults to the
destination in your config. It prints a table of the gaps and exits non-zero
if any gap is larger than the tolerance. The default tolerance is `0.02`,
which means 2 percent.

## Reading a gap

Small gaps are normal. Larger ones usually have a plain cause:

- **Rows below the totals for the last days of a month:** the month was
  checked before the collector caught up. Run `collect`, then compare again.
- **Rows below the totals across a whole day:** a run was blocked by the
  guard, or transcripts were deleted before collection. Run
  `claude-usage doctor`.
- **Rows above the totals:** messages counted twice. Check whether
  `state.json` was deleted while the store already had rows.
- **A model missing on one side:** the two tools may name or group models
  differently. Look at the model names in the check file and in the rows
  for that day.

The two tools are independent, so neither is the truth by definition. A
gap means "look here", not "the collector is wrong".
