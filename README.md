# claude-usage-collector

Turn your Claude Code transcripts into number-only usage rows and context
sizes, and combine several machines and accounts into one private store you
can study.

It costs zero tokens. It is plain Python 3.9 with no dependencies. It runs
once a day on each machine, reads only new transcript lines, and writes CSV.

Three readers fill the store:

- **Usage rows:** one row per assistant message, with its token counts,
  model, tools and agent.
- **Context sizes:** how big each context was before its first turn, and
  what it was made of (system prompt, instruction files, skill and agent
  listings, deferred tools, MCP instructions, hook output), as character
  lengths and counts. Two small files beside it record which skills were
  used and which skill and agent names were offered.
- **A weekly ccusage cross-check:** daily totals from
  [ccusage](https://github.com/ryoppippi/ccusage), stored so `compare` can
  check the rows against an independent reading.

## Privacy guarantee

**Only numbers, short labels and salted hashes ever leave the machine.**
Prompts, replies, thinking, code, tool inputs and outputs, session titles,
agent descriptions, file paths, project names and branch names stay where
they were written. So does the text of your instruction files, skills,
listings, hook output and system prompt: only its length is counted.

Three things make that hold:

- **An allowlist.** The extractor reads a fixed list of fields from each
  transcript line and ignores everything else, including fields Claude Code
  adds in future versions.
- **Salted hashes.** Project, session, agent and machine names become a
  12 character HMAC-SHA256 hash. The key (the salt) is created on your
  machine and never written to the store.
- **A fail-closed guard.** Every row of every file passes a strict check
  of every column before it is written. Each file kind has its own schema.
  One bad value anywhere means nothing is written and the run exits
  non-zero. Error messages name the file kind, row and column, never the
  value.

You can see exactly what would leave with `claude-usage collect --dry-run`.
The full details, including what the hashes do not protect, are in
[docs/privacy.md](docs/privacy.md).

![Several machines each run three readers and a guard, push number-only files to one private store, and a research machine reads it](docs/img/architecture.svg)

## What you get

Once rows are in a store, two commands turn them into findings:

- `claude-usage analyze` prints a text report: totals, main thread versus
  subagents, usage by model and by agent type, context size bands, the
  agents and sessions with the most weighted usage, how big a context is
  before any work starts and what it is made of, the skills used most, and
  the skills and agent types that were offered but never used.
- `claude-usage report` draws the main breakdowns from `analyze` as charts
  on one static HTML page. It works offline and needs no JavaScript.

See a report built from synthetic demo data in
[docs/demo/report.html](docs/demo/report.html) (download it and open it in a
browser). Here is a preview:

![Preview of the HTML report built from synthetic data](docs/img/report-preview.svg)

## Quickstart

You need macOS or Linux, `git`, and Python 3.9 or newer. The macOS system
Python at `/usr/bin/python3` works.

```sh
git clone https://github.com/<you>/claude-usage-collector ~/claude-usage-collector
cd ~/claude-usage-collector

# 1. Write a config and a fresh salt for this machine.
./bin/claude-usage init --account work

# 2. Look at what would be collected. Nothing is written.
./bin/claude-usage collect --dry-run

# 3. Link claude-usage into ~/.local/bin and schedule a daily run (launchd on macOS).
./install/install.sh

# 4. Check the install.
claude-usage doctor
```

That writes rows to `~/usage-data`, a plain folder. To combine several
machines, point each one at a clone of a **private** git repo instead. The
steps are in [docs/multi-account.md](docs/multi-account.md).

On Linux, `install.sh` links the wrapper but does not schedule anything.
Run `claude-usage run` from cron or a systemd timer.

## Commands

All commands take `--config PATH` before the command name, for example
`claude-usage --config ~/other.json collect`. Without it, the config comes
from `$CLAUDE_USAGE_CONFIG`, then `~/.config/claude-usage-collector/config.json`.

| Command | What it does |
|---|---|
| `init --account LABEL [--dest-type folder\|git] [--dest PATH] [--schedule HH:MM] [--force]` | Write the config and the salt. Refuses to replace a config without `--force`, and keeps the salt even then. |
| `collect [--dry-run]` | Read new transcript lines, guard the rows of every stream, append them to the store, save read positions. `--dry-run` prints usage row counts per day, the number of context events, skill uses and catalog names, and three sample rows, and writes nothing. |
| `sync` | For a git store: commit, `pull --rebase`, push. Does nothing for a folder store. |
| `run` | `collect`, then the weekly cross-check for each missed completed week up to 21 days back (see [docs/cross-check.md](docs/cross-check.md)), then `sync`. This is what the daily schedule calls. |
| `crosscheck [--week YYYY-Www] [--month YYYY-MM]` | Ask `ccusage` for daily totals of one ISO week (default: the last completed week, or month when `crosscheck.period` is `month`) or one month, and store them under `checks/`. |
| `compare [--store PATH] [--week YYYY-Www] [--month YYYY-MM] [--tolerance 0.02]` | Compare summed rows with the `ccusage` totals. Without `--week` or `--month` it checks every stored period. Exits non-zero if any gap is over the tolerance. |
| `analyze [--store PATH] [--since YYYY-MM-DD] [--account LABEL]` | Print a text usage report. |
| `report [--store PATH] [--out report.html] [--since YYYY-MM-DD] [--svg PATH]` | Write a static HTML report. `--svg` also writes the daily chart as an SVG file. |
| `doctor` | Check config, salt, store, git remote, schedule, `ccusage` and transcript retention. Changes nothing. |
| `status` | Show the last run time, rows collected and files tracked. |

`compare`, `analyze` and `report` also work without a config, so a research
machine only needs a clone of the store:
`claude-usage report --store ~/usage-data`.

## Documentation

- [Architecture](docs/architecture.md): the modules and one daily run, step by step.
- [Privacy](docs/privacy.md): the allowlist, the guard, the threat model, and how to audit.
- [Schema](docs/schema.md): every column of every file, and the store layout.
- [Configuration](docs/configuration.md): every config key and file location.
- [Multiple machines and accounts](docs/multi-account.md): setting up a shared private store.
- [Cross-check](docs/cross-check.md): how `ccusage` is used to check the numbers.
- [Analysis](docs/analysis.md): `analyze`, `report`, starting context, weights and prices.

## FAQ

### Why not just use ccusage?

`ccusage` is great for "how much did I use this month on this machine". It
reads the transcripts on demand and prints totals. It does not keep a
history once Claude Code deletes old transcripts, and it does not combine
several machines or accounts. It also does not split usage by subagent,
agent type or context size, or show what your contexts are made of, which
is the point of this tool.

So this tool uses `ccusage` as an independent check instead of a
replacement. Once a week it stores the `ccusage` daily totals next to the
rows, and `compare` shows any gap. See [docs/cross-check.md](docs/cross-check.md).

### Why not OpenTelemetry?

Claude Code can export metrics and events over OpenTelemetry. That needs a
collector and a backend running somewhere, it only covers sessions from the
moment you turn it on, and you have to set it up on every machine. Some
event options can also include prompt text, so you must trust the whole
pipeline with content.

This tool works from files already on disk, needs no server, and can only
ever write the allowlisted columns. If you already run OpenTelemetry, keep
it. The two answer different questions.

### Will I lose data if I do not run it for a while?

Yes, if you wait too long. **Claude Code deletes transcripts older than
`cleanupPeriodDays`, which defaults to 30 days.** Anything deleted before a
run is gone for good.

The default is enough for the daily run and the weekly cross-check, so you
do not have to change it. Set a longer period in `~/.claude/settings.json`
if you expect to leave a machine off for weeks:

```json
{
  "cleanupPeriodDays": 365
}
```

`claude-usage doctor` warns when it is set below 22 (the weekly check can
catch up on a week that started 21 days ago), or below 62 when
`crosscheck.period` is `"month"`.

### What happens when I upgrade an existing install?

Pull the repo and keep your config and state. Old config keys keep
working, and a config without `crosscheck.period` uses the weekly check.
`hash_agent_types` is now an alias of `hash_names`, so with it set to
`true` tool names in usage rows are hashed too, as are skill and catalog
names. Rows written before the upgrade keep their plain tool names, so a
per-tool breakdown splits at the upgrade date. The first run after the
upgrade reads context sizes, skill uses and listed names from the start of
every transcript still on disk, and carries on with usage rows where it
left off, so no usage row is written twice. That first run is slower and
uses more memory than a daily run, and the state file grows, because it
remembers every message id it reads for 60 days to tell copied history
apart. See
[docs/architecture.md](docs/architecture.md#separate-offsets-per-stream).

### Does it cost tokens or call any API?

No. It only reads local files. The optional cross-check runs your local
`ccusage` command, which also reads local files.

### Can I run it on Windows?

Not tested. The collector is plain Python, but the installer and schedule
are for macOS, and Linux needs cron or systemd.

### What if a run fails?

It depends on where it failed.

- **Before or while writing rows** (the guard rejects a row, the store
  folder cannot be written, a git destination is not a clone yet): nothing
  is written and the read positions do not move, so the next run tries the
  same lines again. A guard failure names the transcript file the bad row
  came from.
- **During sync** (offline, a push is refused, a rebase conflict): the
  rows are already in the store and committed locally, and the read
  positions have moved. The next run pushes them.

Run `claude-usage doctor` and check the log at
`~/Library/Logs/claude-usage-collector.log` on macOS. An unexpected error
prints only its type; run the command again with `--debug` to see the full
traceback (it can contain local file paths, so check it before sharing).

## License

MIT. See [LICENSE](LICENSE).
