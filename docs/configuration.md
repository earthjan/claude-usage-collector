# Configuration

**One JSON file per machine holds every setting. `init` writes it for you,
and the defaults suit most people.** You only need to edit it to add a
transcript folder, switch to a git store, or tune privacy and analysis.

## Where the files are

The config is found in this order:

1. `--config PATH`, given before the command: `claude-usage --config ~/alt.json collect`
2. the `CLAUDE_USAGE_CONFIG` environment variable
3. `~/.config/claude-usage-collector/config.json`

The salt (`salt`), the state file (`state.json`) and the local name map
(`names.json`) live in the same folder as the config. A second config in another folder therefore has its own salt
and its own read positions.

## Creating it

```sh
claude-usage init --account work
claude-usage init --account work --dest-type git --dest ~/usage-data --schedule 07:30
```

| Flag | Default | Meaning |
|---|---|---|
| `--account LABEL` | required | Account label written into every row. |
| `--dest-type folder\|git` | `folder` | Plain folder, or a clone of a git repo. |
| `--dest PATH` | `~/usage-data` | Where the store lives. |
| `--schedule HH:MM` | `18:00` | Daily run time, read by `install/install.sh`. |
| `--force` | off | Replace an existing config. The existing salt is kept, so hashes do not change. |

## A full example

This is what `init --account work` writes (shown compactly):

```json
{
  "machine_label": null,
  "sources": ["~/.claude/projects"],
  "round_ts_to_hour": false,
  "hash_names": true,
  "crosscheck": {"command": "ccusage", "enabled": true, "period": "week"},
  "account": "work",
  "destination": {"type": "folder", "path": "~/usage-data"},
  "schedule": "18:00"
}
```

A git store looks like this:

```json
"destination": {
  "type": "git",
  "path": "~/usage-data",
  "remote": "origin",
  "branch": "main",
  "push": true
}
```

## Every key

| Key | Default | Meaning |
|---|---|---|
| `account` | none, required | Label for this account in every row. Must be a slug: starts with a letter or digit, then letters, digits, `.`, `_`, `:`, `-`, no spaces, at most 64 characters (see [schema.md](schema.md#value-rules)). |
| `machine_label` | `null` | When `null`, the `machine` column is a salted hash of the hostname. Set a slug such as `laptop` to use that instead. It is written as is, so keep it neutral. |
| `sources` | `["~/.claude/projects"]` | Folders that hold Claude Code project transcripts. Add one per Claude Code config folder you use. |
| `destination.type` | `"folder"` | `folder` or `git`. |
| `destination.path` | `"~/usage-data"` | The store folder. For `git`, a clone of your private data repo. |
| `destination.remote` | `"origin"` | Git only: remote to pull from and push to. |
| `destination.branch` | `"main"` | Git only: branch to pull from and push to. |
| `destination.push` | `true` | Git only: set `false` to commit locally without pushing. |
| `round_ts_to_hour` | `false` | Round every timestamp down to the hour, to hide exact working times. |
| `hash_names` | `true` | Hash every name that would otherwise be written as is: agent types, skill names, catalog names and tool names (MCP tools and names that are not slugs are always hashed). The plain names stay readable on your machine through the local name map (see [privacy.md](privacy.md#the-local-name-map)). Set it to `false` only if every agent and skill name you use is safe to share; `doctor` warns while it is off. See [privacy.md](privacy.md#threat-model). |
| `hash_agent_types` | not set | The older name of `hash_names`, kept so existing configs keep working. Only `true` counts: it turns hashing on whatever `hash_names` says. `false` is ignored, because older versions wrote it as a default. `init` no longer writes it. |
| `schedule` | `"18:00"` | Daily run time in 24 hour `HH:MM`. Used by `install/install.sh`. |
| `crosscheck.command` | `"ccusage"` | Command to run for the cross-check: a bare name found through `PATH`, or a path, which may start with `~/`. The launchd agent runs with the `PATH` of the shell that ran `install/install.sh`. If ccusage is not on it, or a version manager moves it after a Node upgrade, use the absolute path, for example `~/.nvm/versions/node/<version>/bin/ccusage`, or run the installer again. See [cross-check.md](cross-check.md#when-ccusage-comes-from-nvm-or-another-version-manager). It is pointed at this config's `sources`, which must each end in `projects`. |
| `crosscheck.enabled` | `true` | Set `false` to skip the cross-check. |
| `crosscheck.period` | `"week"` | `week` checks each completed ISO week, `month` checks each completed calendar month. Configs without this key behave as `week`. See [cross-check.md](cross-check.md#when-it-runs). |
| `weights` | see below | Multipliers for the weighted usage figure in `analyze` and `report`. |
| `prices` | `{}` | Dollar prices per million tokens, keyed by model prefix. Empty by default. |

### Which key decides whether names are hashed

**Every name is hashed unless `hash_names` is `false` and
`hash_agent_types` is not `true`.** A `true` on either key always hashes,
so no older config that asked for hashing loses it. An older
`"hash_agent_types": false` is ignored, because older versions wrote it
into every config as a default, not as a choice.

| Config has | Names are hashed |
|---|---|
| `"hash_agent_types": true` | yes, whatever `hash_names` says |
| `"hash_names": true` | yes |
| `"hash_names": false`, and `hash_agent_types` missing or `false` | no, and `doctor` warns |
| neither key, or only `"hash_agent_types": false` | yes |

**Upgrading:** a config written by an older `init` may hold
`"hash_names": false`. That was the old default, but the tool cannot tell
it from a choice, so it stays off and `claude-usage doctor` warns. Set it
to `true` (or delete the key) to hash every name from the next `collect`.

`weights` and `prices` are not written by `init`. Add them by hand when you
want them. Both are explained in [analysis.md](analysis.md).

```json
"weights": {"input": 1, "cache_read": 0.1, "cache_write": 2, "output": 5}
```

## Changing settings later

- **Schedule:** edit `schedule`, then run `install/install.sh` again, or
  pass `install/install.sh --schedule HH:MM`. The launchd agent only picks up
  a new time on install.
- **Privacy switches:** `round_ts_to_hour`, `hash_names` (or
  `hash_agent_types`) and `machine_label` apply to new rows only. Rows already in the store keep
  their old form.
- **Account label:** applies to new rows only, and they go into a new
  account folder.
- **Node upgrade:** if `crosscheck.command` points into a version manager
  such as `~/.nvm/versions/node/<version>/bin/ccusage`, the path changes
  when Node does. So does the `PATH` the installer gave the launchd
  agent. Update the command or run `install/install.sh` again, then run
  `claude-usage doctor`: it checks the command against the agent's
  `PATH`.

`claude-usage doctor` loads and validates the config, including
`crosscheck`, `weights` and `prices`, so run it after any hand edit.

## Installer options

```sh
install/install.sh [--schedule HH:MM] [--python /abs/path/to/python3] [--uninstall]
```

- It links `~/.local/bin/claude-usage` to the repo's wrapper. Make sure
  `~/.local/bin` is on your `PATH`.
- On macOS it writes `~/Library/LaunchAgents/io.github.claude-usage-collector.plist`
  and loads it. The agent runs `<python> -m usage_collector run` at the
  schedule time and logs to `~/Library/Logs/claude-usage-collector.log`.
- `--python` sets the interpreter. Without it, `/usr/bin/python3` is used
  when present.
- Running it again replaces what the last run installed.
- `--uninstall` unloads and removes the agent, and removes the link if it
  points at this repo. It says what it removed and what it kept. Your
  config, salt, state and log are always kept.
- Set `CLAUDE_USAGE_SKIP_LAUNCHCTL=1` to write or remove the plist without
  calling `launchctl`, for example when testing with a different `HOME`.
- The agent is given the config path from `CLAUDE_USAGE_CONFIG` at install
  time, or the default path.
- The `bin/claude-usage` wrapper itself runs `python3` from your `PATH`.
  Set `CLAUDE_USAGE_PYTHON` to an interpreter path to use another one, for
  example `CLAUDE_USAGE_PYTHON=/usr/bin/python3`.

## Transcript retention

**Claude Code deletes transcripts older than `cleanupPeriodDays` (30 days
by default).** This is a Claude Code setting, not a setting of this tool,
but it decides how much history can be collected. Set it in
`~/.claude/settings.json`:

```json
{"cleanupPeriodDays": 365}
```

The default 30 days is enough for the weekly cross-check, so leaving it
unset is fine. The weekly check can catch up on a week that started up to
21 days ago, so `claude-usage doctor` warns when the setting is below 22.
If you set `crosscheck.period` to `"month"`, it needs 62 days or more, and
doctor warns when it is unset or shorter. Doctor also warns when the
setting is there but is not a whole number, or `settings.json` cannot be
read. Keep it longer if you want older transcripts to stay collectable
after a long break.
