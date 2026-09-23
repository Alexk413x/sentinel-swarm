# sentinel-swarm hooks

Twelve hooks, all wired in `hooks.json`, all calling `python -m swarm_ledger.hooks
<event>` from `mcp/`. Each hook reads one JSON object from stdin and, when it has an
opinion, writes one JSON object to stdout. The dispatch logic lives in
`mcp/src/swarm_ledger/hooks/__main__.py`; the per-event behavior lives in
`mcp/src/swarm_ledger/hooks/events.py`, as functions `handle_<event>(ledger, data)`
that tests call directly, in-process.

| # | Event | Matcher | What it does | Blocks |
|---|---|---|---|---|
| 1 | `SessionStart` | none | Reports an unfinished run and its Oracle, a missing `knowledge/code_graph.db`, and a `.git/info/exclude` that lacks `.sentinel-swarm/` (and fixes that last one) | No |
| 2 | `PreToolUse` | `Agent` | Spawn gate: the caller's role may create this child role, the model is approved, an unacked brief exists, and the parallelism cap has room | Yes |
| 3 | `SubagentStart` | the four roles | Registers the agent in the ledger. Marks an idle agent working again when it is resumed | No |
| 4 | `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | Role gate: only a Coder writes. Ownership gate: the path must be the caller's claimed file or test file. Protected paths: nobody hand-writes `.sentinel-swarm/` | Yes |
| 5 | `PreToolUse` | `Bash\|PowerShell` | Shell gate for Coders: allows the project profile's test, build, and lint commands, and read-only git. Denies the rest, and denies any shell use by a non-Coder | Yes |
| 6 | `PreToolUse` | the swarm-ledger MCP tools | Stamps the caller's real `agent_id` onto every call, overwriting whatever the agent passed. Denies `override_grant` for a non-Oracle | Yes |
| 7 | `PostToolUse` | none | Heartbeat to the registry. Marks an idle agent working again. After a Coder's edit, marks the file's claim stale, so a handoff needs a fresh self review | No |
| 8 | `PostToolUse` | `Bash\|PowerShell` | Compares `git status --porcelain` against the Coder's claim. A change outside the claim is logged as a violation and reported to the Lead | No |
| 9 | `PreCompact` | none | Adds one to the agent's context overflow count | No |
| 10 | `SubagentStop` | the four roles | Fires each time a subagent's turn ends. Records the transcript path and token totals. Blocks a working Coder once, until it calls `handoff_submit` or messages its Lead. Otherwise marks a working agent idle | Once per agent |
| 11 | `Stop` | none | Records the Oracle's token totals from its transcript. Allows the stop when the run is paused, a directive needs the user, or any agent is working. Otherwise, while the run has unlocked phases, a submitted handoff, a live claim, or a live agent, blocks and names each idle agent to resume and its agent id | Yes |
| 12 | `SessionEnd` | none | Closes the registry entry for the session and records its token totals | No |

## The two rules every hook follows

- **A hook never exits non-zero and never blocks through exit code 2.** It blocks by
  printing a JSON decision (`permissionDecision: "deny"` for `PreToolUse`, `decision:
  "block"` for `Stop` and `SubagentStop`) and exiting 0.
- **A broken hook allows.** Any exception is caught at the top level, written as one
  line to stderr, and the hook prints nothing and exits 0. A hook can never lock the
  user out.

## No-op by default

Every hook is a no-op (allow, print nothing) unless an active run exists in the
ledger and the caller is a registered, live swarm agent. A paused run counts as
active here: every gate still applies while the run is paused. Exceptions: `SessionStart`
always runs its checks, `SubagentStart` always registers the agent, hook 4's
protected-records rule applies to everyone whenever a run is active, and hook 6
always stamps identity, because a ledger call can arrive before its caller is a
registered agent (`run_start` itself, for instance).

## Why `uv run`, not `python3 || python`

The sibling plugins' hooks are single-file scripts with no third-party imports, so
they run as `python3 ... || python ...` to work on a host with only one of those
names. These hooks import the `swarm_ledger` package and its dependencies
(`fastmcp`, `pyyaml`), so they run through `uv run --project
"${CLAUDE_PLUGIN_ROOT}/mcp" --frozen --no-dev`, which resolves the locked
environment before running.
