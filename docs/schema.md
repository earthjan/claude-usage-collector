# Schema

**Each row is one assistant message: when it happened, who made it (as
hashes), which model, and how many tokens it used.** Rows are CSV with a
header, 21 columns, in the order below. Cross-check totals are JSON.

## Row columns

| Column | Type | Meaning |
|---|---|---|
| `ts` | `YYYY-MM-DDTHH:MM:SSZ` | When the message was written, in UTC. With `round_ts_to_hour` the minutes and seconds are `00`. |
| `account` | slug | The account label from your config, such as `work`. |
| `machine` | hash or slug | Salted hash of the hostname, or `machine_label` if you set one. |
| `project` | hash | Salted hash of the project folder name. |
| `session` | hash | Salted hash of the session id. |
| `agent` | hash or empty | Salted hash of the subagent id. Empty on the main thread. |
| `is_subagent` | 0 or 1 | 1 when the row came from a file under `subagents/`. |
| `agent_type` | slug, hash or empty | `agentType` from the subagent's meta file, such as `Explore`. Hashed when `hash_agent_types` is on, or when it is not a simple slug. Empty on the main thread. |
| `spawn_depth` | integer or empty | `spawnDepth` from the meta file: how deep in a chain of agents this one was started. Empty on the main thread. |
| `is_fork` | 0 or 1 | 1 when the meta file says `isFork: true`. |
| `model` | slug or hash | The model name, such as `claude-opus-5-5`. Ids from other providers are cut to the part from `claude-` on, so `us.anthropic.claude-opus-4-1-v1:0` becomes `claude-opus-4-1-v1:0` and `claude-opus-4-1@20250805` becomes `claude-opus-4-1`. Any other id is hashed. A missing id is `unknown`. |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max` or empty | The reasoning effort, when Claude Code recorded one. |
| `input` | integer | Input tokens not served from cache. |
| `cache_read` | integer | Input tokens read from the prompt cache. |
| `cache_write` | integer | Input tokens written to the prompt cache. |
| `output` | integer | Output tokens. |
| `context` | integer | `input + cache_read + cache_write`: the size of the context this message saw. |
| `web_search` | integer | Server-side web search requests. |
| `web_fetch` | integer | Server-side web fetch requests. |
| `tools` | list or empty | Tool names called in this message, sorted, unique, joined with `;`. MCP tools appear as `mcp:<hash>`. |
| `compacted` | 0 or 1 | 1 on the first row after a context compaction in that file. |

Missing token fields count as 0.

### Value rules

The guard enforces these. Anything else stops the run.

- **slug:** starts with a letter or digit, then up to 63 letters, digits,
  `.`, `_`, `:` or `-`. No spaces.
- **hash:** exactly 12 lowercase hex characters (`0` to `9`, `a` to `f`).
- **integer:** no sign, no leading zeros, at most 10^9.
- **list:** every item is a slug.

### Example row

All values below are made up.

```csv
ts,account,machine,project,session,agent,is_subagent,agent_type,spawn_depth,is_fork,model,effort,input,cache_read,cache_write,output,context,web_search,web_fetch,tools,compacted
2026-01-14T09:30:12Z,work,3f9a0c1d2e4b,a1b2c3d4e5f6,0f1e2d3c4b5a,9c8b7a6f5e4d,1,Explore,1,0,claude-opus-5-5,high,12,48000,1500,620,49512,0,0,Grep;Read,0
```

## Store layout

**A store has one folder per account, one CSV per UTC day, and a `checks/`
folder for monthly cross-check totals.**

![A store has one folder per account, one CSV per UTC day, and a checks folder for monthly ccusage totals](img/store-layout.svg)

```
<store>/
  .gitattributes                         git stores only: *.csv merge=union
  <account>/YYYY/MM/YYYY-MM-DD.csv       rows, grouped by the UTC day of ts
  checks/<account>/<machine>/YYYY-MM.json   ccusage totals for one month
```

- Day files are append only. The header is written once, when the file is
  created.
- Each machine writes only inside its own account folder and its own
  `checks/<account>/<machine>/` folder, where `<machine>` is the same value
  as the `machine` column.
- Two machines can share one account label. Their rows land in the same day
  files and are told apart by the `machine` column.

## Cross-check totals

`checks/<account>/<machine>/YYYY-MM.json` holds, per day and per model, the
four token totals that `ccusage` reported on that machine. Nothing else from
`ccusage` is kept. `compare` adds up the files of every machine in an
account. A file written straight into `checks/<account>/` by an older
version is still read.

```json
{
  "2025-12-01": {
    "claude-opus-5-5": {
      "cache_read": 900000,
      "cache_write": 40000,
      "input": 1200,
      "output": 30000
    }
  }
}
```

The guard checks this file too: dates must be real dates, model names must
be slugs, and each model has exactly the four keys, each an integer from 0
to 10^13. The cap is higher than for rows because a whole day of cache reads
can pass 10^9.

## Hashes across machines

**The same project gets a different hash on each machine unless the
machines share a salt.** Each `init` creates its own salt. If you want
project and session hashes to line up across machines, copy the salt file
before running `init` on the next machine. See
[multi-account.md](multi-account.md).
