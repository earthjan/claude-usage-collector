# Privacy

**Session content never leaves the machine. Only timestamps, token counts,
short labels and salted hashes do, and only after a strict check of every
value.** This page explains how that is enforced, what the hashes do and do
not hide, and how to check it yourself.

![Transcript content, names, paths and the salt stay on the machine. Only timestamps, counts, slugs and salted hashes pass the guard.](img/privacy-boundary.svg)

## What leaves, and what never does

These are the only values written to the store. The full column list is in
[schema.md](schema.md).

| Leaves the machine | Form |
|---|---|
| When a message happened | UTC timestamp, optionally rounded to the hour |
| Token counts | input, cache read, cache write, output, and their sum |
| Web search and fetch counts | integers |
| Model and effort | a slug such as `claude-opus-5-5`, and `low` to `max` |
| Tool names | slugs such as `Read`; MCP tools become `mcp:<hash>` |
| Agent type, spawn depth, fork flag | a slug such as `Explore` (or a hash), an integer, 0 or 1 |
| Project, session, agent, machine | 12 character salted hashes |
| Account | the label you chose, such as `work` |

These never leave, because the extractor never reads them into a row:

- prompts, replies and thinking
- code, tool inputs and tool outputs
- session titles and subagent descriptions
- file paths, project folder names and git branch names
- hostnames, raw session ids and raw agent ids
- the salt, the config and the state file

## How it is enforced

**Three independent layers each block content on their own.**

### 1. The extractor reads an allowlist

`extract.py` reads named fields from each transcript line: `type`,
`subtype`, `timestamp`, `effort`, and inside `message`: `id`, `model`,
`usage` and the `name` of each `tool_use` item. It never touches anything
else. New fields that Claude Code adds later are ignored without any code
change. From a subagent's `.meta.json` it reads only `agentType`,
`spawnDepth` and `isFork`.

Values that might be free text are reduced before they reach a row:

- A tool name that is not a simple slug becomes a hash. Every MCP tool name
  (`mcp__<server>__<tool>`) becomes `mcp:<hash>`, because server names can
  be private.
- An agent type that is not a simple slug becomes a hash.
- An `effort` value outside `low`, `medium`, `high`, `xhigh`, `max` becomes
  empty.

### 2. Names become salted hashes

Project folder, session id, agent id and hostname are hashed with
HMAC-SHA256, keyed with a 32 byte random salt, and cut to 12 hex
characters. The salt is created by `init` in your config folder with mode
600. It is never written to the store.

### 3. The guard fails closed

`guard.py` checks every row before anything is written:

- Each of the 21 columns has a strict rule: a timestamp pattern, a slug
  pattern of at most 64 characters with no spaces, a 12 hex hash pattern,
  a fixed list of values, or a non-negative integer up to 10^9.
- An unknown column or a missing column is a problem.
- `context` must equal input plus cache read plus cache write, and a main
  thread row must have an empty agent.

**If any row has any problem, nothing is written, the command exits 1, and
the read offsets do not move.** The error names the row number and column,
never the value, so a rejected value cannot leak through the log either.

The same guard checks `ccusage` totals before they are stored.

## Threat model

**The design assumes someone you do not fully trust might read the store.**
It is built so that reading the store tells them how much you used Claude
Code, and when, but not what you worked on.

What a reader of the store can learn:

- **Your usage pattern.** Timestamps show when you work. Set
  `round_ts_to_hour` to blur this to the hour.
- **Which models, tools and agent types you use.** Agent types and
  tool names that are simple slugs are written as is. That covers the
  built-in ones (`Explore`, `general-purpose`, `Read`, `Bash`), but also
  agents you define yourself in `.claude/agents`, plugin agents such as
  `plugin:agent`, and custom non-MCP tool names. If you name agents after
  projects, clients or teams, those names reach the store. Set
  `hash_agent_types` to hash every agent type; it is the safe choice for
  anyone with project-named agents. MCP tool names are always hashed.
- **Model ids.** A model id containing `claude-` is kept from that point
  on (a provider prefix or an `@version` suffix is dropped). Any other
  model id, such as a custom gateway name, is hashed, and a missing one
  becomes `unknown`.
- **How many projects and sessions you have,** and which rows belong
  together, through the hashes.
- **Your labels.** The account label, and `machine_label` if you set one,
  are written as is. Pick neutral labels.

What a reader cannot learn without the salt:

- **The names behind the hashes.** Without the salt, a hash cannot be
  checked against a guessed project name.

What the hashes do not protect against:

- **Someone with your salt.** They can hash guessed names and compare. Keep
  the salt as private as your SSH keys. Never commit it.
- **Someone with access to your machine.** They can read the transcripts
  directly. This tool does not change that.

Keep the store in a **private** repository. Never push it to the public tool
repo. See [multi-account.md](multi-account.md).

## How to audit it yourself

**Run a dry run and read what would be written.**

```sh
claude-usage collect --dry-run
```

It prints the number of new rows per day and the first three rows as CSV.
It writes nothing and does not move the offsets.

Then search the real store for words that must never appear. Pick words
from your own work: a project name, a branch name, a phrase from a prompt.

```sh
grep -rIl -e 'my-secret-project' -e 'feature/some-branch' ~/usage-data
```

No output means no match. You can also open any CSV in a spreadsheet: every
cell is a number, a hash, a timestamp or a short label.

The test suite checks the same thing on synthetic transcripts that contain
marker strings in titles, paths, branches, prompts, tool inputs, replies and
agent descriptions. It asserts that none of them appears anywhere in the
written store.
