# Privacy

**Session content never leaves the machine. Only timestamps, token counts,
character lengths, short labels and salted hashes do, and only after a
strict check of every value.** This page explains how that is enforced, what the hashes do and do
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
| Sizes of what was attached to a context | character lengths and counts, never the text (see below) |
| Skill names that were used | a slug such as `code-review` (or a hash) |
| Skill and agent names that were listed | slugs (or hashes), one per day |

These never leave, because the extractor never reads them into a row:

- prompts, replies and thinking
- code, tool inputs and tool outputs
- session titles and subagent descriptions
- file paths, project folder names and git branch names
- hostnames, raw session ids and raw agent ids
- the salt, the config and the state file
- the text of instruction files, skill listings and skill files, agent
  listings, deferred tool lines and descriptions, MCP instructions, hook
  output and the system prompt, and every path in them

### Context sizes: measured, never copied

**For what Claude Code attaches to a context, only the length of each text
leaves, never the text.** These attachment fields hold free text. The
collector passes each one to a length count and keeps only the number.

| Attachment | Field | What leaves |
|---|---|---|
| `prompt_snapshot` | `systemPrompt` | `system_prompt_chars` |
| `instructions` | each file's `content` | its length, added to `instr_user_chars`, `instr_project_chars`, `instr_local_chars`, `instr_memory_chars` or `instr_other_chars` by the file's `type` |
| `instructions` | each file's `path` | nothing; files are only counted in `instr_files` |
| `skill_listing` | `content` | `skill_listing_chars` |
| `skill_listing` | `skillCount` | `skill_count` |
| `skill_listing` | `names` | catalog rows under the name rule, and `skill_count` when `skillCount` is missing |
| `agent_listing_delta` | `addedLines` | `agent_listing_chars` |
| `agent_listing_delta` | `addedTypes` | `agent_count`, and catalog rows under the name rule |
| `agent_listing_delta` | `builtInTypes` | catalog rows under the name rule |
| `deferred_tools_delta` | `addedLines` | `deferred_tools_chars` |
| `deferred_tools_delta` | `addedNames` | `deferred_tool_count` only |
| `deferred_tools_record` | each entry's `name`, `description`, `input_schema` | their lengths, added to `deferred_tools_chars`, and `deferred_tool_count` |
| `mcp_instructions_delta` | `addedBlocks` | `mcp_chars` |
| `mcp_instructions_delta` | `addedNames` | `mcp_server_count` only |
| `hook_additional_context` | `content` | `hook_context_chars` |
| `invoked_skills` | each skill's `name` | a skill use row, under the name rule |
| `invoked_skills` | each skill's `path` and `content` | nothing |

Deferred tool names and MCP server names are counted, never written, not
even as hashes. Every other field of an attachment, such as `hookName` or
`digests`, is never read.

"The name rule" means a name that is a simple slug is written as is, and
anything else becomes a salted hash. With `hash_names` on, every name is
hashed.

## How it is enforced

**Three independent layers each block content on their own.**

### 1. The extractor reads an allowlist

`extract.py` reads named fields from each transcript line: `type`,
`subtype`, `timestamp`, `effort`, and inside `message`: `id`, `model`,
`usage` and the `name` of each `tool_use` item. From `attachment` lines,
`context.py` and `streams.py` read only the fields in the table above, plus
the line's `uuid`. The `uuid` is used only to notice history copied into a
forked session, and is never written. Nothing else is touched. New fields that Claude Code adds later are ignored without any code
change. From a subagent's `.meta.json` it reads only `agentType`,
`spawnDepth` and `isFork`.

Values that might be free text are reduced before they reach a row:

- A tool name that is not a simple slug becomes a hash. Every MCP tool name
  (`mcp__<server>__<tool>`) becomes `mcp:<hash>`, because server names can
  be private.
- An agent type, skill name or listed name that is not a simple slug
  becomes a hash. One shared name policy (`names.py`) does this for all of
  them, so `hash_names` covers them all at once.
- Attached text becomes its length, as described above.
- An `effort` value outside `low`, `medium`, `high`, `xhigh`, `max` becomes
  empty.

### 2. Names become salted hashes

Project folder, session id, agent id and hostname are hashed with
HMAC-SHA256, keyed with a 32 byte random salt, and cut to 12 hex
characters. The salt is created by `init` in your config folder with mode
600. It is never written to the store.

### 3. The guard fails closed

`guard.py` checks every row of every file before anything is written.
Usage rows, context sizes, skill uses and catalog rows each have their own
schema:

- Each column has a strict rule: a timestamp or date pattern, a slug
  pattern of at most 64 characters with no spaces, a 12 hex hash pattern,
  a fixed list of values, or a non-negative integer up to 10^9.
- An unknown column or a missing column is a problem.
- `context` must equal input plus cache read plus cache write, and a main
  thread row must have an empty agent.

**If any row of any file has any problem, nothing is written, the command
exits 1, and the read offsets do not move.** The error names the file kind,
row number and column (for example `context row 0: column event is
invalid`), never the value, so a rejected value cannot leak through the log
either.

The same guard checks `ccusage` totals before they are stored.

## Threat model

**The design assumes someone you do not fully trust might read the store.**
It is built so that reading the store tells them how much you used Claude
Code, and when, but not what you worked on.

What a reader of the store can learn:

- **Your usage pattern.** Timestamps show when you work. Set
  `round_ts_to_hour` to blur this to the hour.
- **Which models, tools, agent types and skills you use, and which ones
  you have installed.** Agent types, skill names and tool names that are
  simple slugs are written as is. That covers the built-in ones
  (`Explore`, `general-purpose`, `Read`, `Bash`), but also agents and
  skills you define yourself, plugin agents such as `plugin:agent`, and
  custom non-MCP tool names. The catalog file lists every skill and agent
  type that was offered on a day, even ones you never used. If you name
  agents or skills after projects, clients or teams, those names reach the
  store. Set `hash_names` to hash every name; it is the safe choice for
  anyone with project-named agents or skills. The older `hash_agent_types`
  key does the same. MCP tool names are always hashed.
- **How large your setup is.** Context sizes show how long your
  instruction files, listings and system prompt are, and how many skills,
  agents, deferred tools and MCP servers were offered. They do not show
  what any of them say.
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

It prints the number of new usage rows per day, the number of new context
events, skill uses and catalog names, and the first three usage rows as
CSV. It writes nothing and does not move the offsets.

Then search the real store for words that must never appear. Pick words
from your own work: a project name, a branch name, a phrase from a prompt.

```sh
grep -rIl -e 'my-secret-project' -e 'feature/some-branch' ~/usage-data
```

No output means no match. The search covers the context, skills and
catalog files too. A good extra test is a phrase from one of your own
`CLAUDE.md` files or skills. You can also open any CSV in a spreadsheet:
every cell is a number, a hash, a timestamp or a short label.

The test suite checks the same thing on synthetic transcripts that contain
marker strings in titles, paths, branches, prompts, tool inputs, replies and
agent descriptions, and in every free-text attachment field: instruction
content and paths, skill listings, skill content and paths, agent listing
lines, deferred tool lines and descriptions, MCP blocks, hook output and
the system prompt. Non-slug skill, agent, MCP and deferred tool names carry
markers too. It asserts that none of them appears anywhere in the written
store, in any file.
