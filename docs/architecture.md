# Architecture

**Each machine runs a small collector once a day. It reads only the new
lines of each transcript, keeps an allowlist of numbers and labels, checks
every row, and appends the rows to a store.** The store is a plain folder or
a clone of a private git repo. Research happens on any machine with a copy of
the store.

![Several machines each run a collector and guard, push number-only rows to one private store, and a research machine reads it](img/architecture.svg)

The collector never sends anything over the network itself. The only thing
that leaves a machine is a `git push` of the store, and only when you choose
a git store.

## One daily run

`claude-usage run` is what the schedule calls. It does five things in order.

![Read, extract, guard, write, push. A guard failure writes nothing and keeps offsets.](img/daily-run.svg)

1. **Read.** For each transcript file it seeks to the byte offset saved by
   the last run and reads up to the last newline. A line Claude Code is
   still writing is left for next time. If a file got shorter than the saved
   offset, it is read again from the start. While a file is still active
   (changed in the last 10 minutes) and ends with an assistant message, the
   lines of that last message are left for the next run too, because more
   lines with a larger output count may still be added to it.
2. **Extract.** Each line is parsed as JSON. Malformed lines are skipped.
   Only assistant lines that carry `message.usage` become rows, and only the
   allowlisted fields are read from them. Names become salted hashes.
3. **Guard.** Every row is checked column by column. Any problem stops the
   run before anything is written.
4. **Write.** Rows are grouped by UTC day and appended to one CSV per day.
   Only after the write succeeds are the new offsets and seen message ids
   saved.
5. **Push.** For a git store: add, commit, `pull --rebase`, push.

Between write and push, `run` also stores last month's `ccusage` totals if
that has not been done yet. See [cross-check.md](cross-check.md).

## The modules

The package is `usage_collector/`. Each module has one job.

| Module | Job |
|---|---|
| `cli.py` | The `argparse` commands and the wiring between modules. |
| `config.py` | Load and validate the JSON config, fill defaults, write it at `init`. |
| `hashing.py` | Create and load the salt. Hash a value to 12 hex characters with HMAC-SHA256. |
| `state.py` | Local bookkeeping: byte offset per file, seen message ids, last run. |
| `extract.py` | Find transcript files, read new lines, build rows from allowlisted fields. |
| `guard.py` | The fail-closed validator for rows and for cross-check totals. |
| `sinks.py` | Write rows to a folder, and for a git store, sync it. |
| `doctor.py` | Read-only health checks for `claude-usage doctor`. |
| `crosscheck.py` | Run `ccusage`, keep only its totals, compare them with rows. |
| `analyze.py` | Read rows from a store and compute breakdowns. |
| `report.py` | Render those breakdowns as a static HTML page. |

`bin/claude-usage` is a small shell wrapper that runs
`python3 -m usage_collector` with the repo on `PYTHONPATH`.
`install/install.sh` links it into `~/.local/bin` and, on macOS, loads a
launchd agent built from `install/launchd.plist.template`.

## Finding transcripts

Claude Code keeps one folder per project under each source (by default
`~/.claude/projects`):

```
<source>/<project-folder>/<session-id>.jsonl
<source>/<project-folder>/<session-id>/subagents/agent-<agent-id>.jsonl
<source>/<project-folder>/<session-id>/subagents/agent-<agent-id>.meta.json
```

Subagent files are found by searching everything under `subagents/`, so
nested agents are included. From a subagent's `.meta.json` file the
collector reads only `agentType`, `spawnDepth` and `isFork`. Every other key,
including the free-text `description`, is never read into a row.

## Counting each message once

**A message is counted once even when it appears in several files or on
several lines.** Two things cause repeats:

- Claude Code can write one assistant message as several lines that share a
  `message.id`. Within one run these lines are merged: tool names are
  combined and the latest usage numbers win. The last message of an active
  file is held back until it is finished (see step 1 above), so its lines
  are not split across two runs.
- A forked session copies its parent's history, so the same `message.id`
  shows up in more than one file. The first file read keeps the row. Main
  session files are read before subagent files, so a subagent's copy of
  parent messages never replaces the parent's row. A subagent file that
  has its `meta.json` beside it is read before a copy of the same file
  without one, so the row keeps its agent type and original session.
  Between two main session files that share messages, the file whose path
  sorts first keeps the row, whichever one is the original. Ids from
  earlier runs are remembered in the state file for 60 days, then pruned.

Lines with no `message.id` and rows from the `<synthetic>` model are
skipped.

## Compaction

When Claude Code compacts a conversation it writes a `compact_boundary`
line. The next usage row in that file gets `compacted=1`. If the boundary is
the last thing in a file, that flag waits in the state file for the next
run. If a message that was already counted follows the boundary, the
boundary is part of history copied from a parent session, so the flag is
dropped: the compaction was already counted in the parent.

## Git sync

**The git store syncs in the order add, commit, `pull --rebase`, push.**
Pulling first would fail, because `collect` has already written files into
the working tree by then.

- The sink writes a `.gitattributes` file with `*.csv merge=union`. Two
  machines on the same account that append to the same day file then merge
  cleanly, with no repeated header.
- Commits use a neutral identity (`claude-usage-collector`), so no personal
  email lands in the data repo. The message is `usage: <account> <date>`.
- If the rebase fails, it is aborted and the run exits non-zero. The local
  commit stays, so the next run tries again.

## State and local files

These files live next to the config, by default in
`~/.config/claude-usage-collector/`. None of them is ever written to the
store.

| File | Holds |
|---|---|
| `config.json` | Your settings. Mode 600. |
| `salt` | 32 random bytes, hex encoded. Mode 600. |
| `state.json` | Offsets per transcript file, seen message ids, last run, last cross-checked month. Mode 600. |

Deleting `state.json` makes the next run read every transcript from the
start. Rows already in the store would then be written again, so only do
that with an empty store.

## Known limits

- **A new model name that is not a simple slug blocks collection.** The
  guard rejects it on purpose (fail closed). Collection stays blocked until
  the extractor learns the name.
- **A message split across two runs loses its tail.** If Claude Code writes
  the later lines of a message after a run has already read the first one,
  those later tool names and the final output count are not added.
