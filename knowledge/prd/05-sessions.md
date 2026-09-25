# Sessions

## Starting a child

`agent_spawn(caller, child_name)` refuses unless the caller is the parent named in the
child's unacknowledged brief. It also refuses a Manager before a `repo_check`, a child
that is already registered, a session name a running session holds, and a start past
`parallelism_cap` when the user set one. It then runs, in the repo root:

```
claude "You are <name>. Read your brief from the swarm ledger and follow it." --bg \
  --name <session name> --agent swarm-<role> --model <brief model> \
  --permission-mode <file value> --strict-mcp-config --mcp-config <config> \
  --allowedTools <file tools> --settings '{"worktree":{"bgIsolation":"none"}}'
```

- The prompt goes before the options, because `--allowedTools` takes a space-separated
  list and swallows a prompt that follows it.
- `<config>` holds the ledger's HTTP URL, the agent file's `mcpServers`, and the a11y
  servers when the host has a11y installed. A server with a shared HTTP URL in
  `server.json` that answers is listed by that URL; any other server keeps the shim's
  stdio entry. **(proposed)** See "Shared HTTP servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- It waits up to 15 seconds for the session to appear in `claude agents --json`, then
  records the child's row: `agent_id` = the session id, `session_name`, `bg_id`, state
  `registered`. A session that does not appear is stopped and the call fails.
- The parent ends its turn after its spawns. A child's message wakes it.
- The claude binary is `SENTINEL_SWARM_CLAUDE` when set, else `claude`.

## Identity

- Every tool takes `caller`, the agent's name, and `agent_id`. The `pre_ledger` hook
  stamps `agent_id` with the session id through `updatedInput`, over any value the
  agent passes. `ledger_info`, `brief_get`, `who_owns`, `directive_submit`, and
  `events` take no identity.
- `brief_ack` binds a name to a session. After that, every call resolves `agent_id` to
  its row and refuses a `caller` that does not match, or an agent that has ended.
- The Oracle is bound at `run_start`.

## Agent states

`registered` → `working` ↔ `idle` → `handed_up` → `released`.

- `brief_ack` sets `working`. The Stop hook sets `working` to `idle` when a turn ends.
  The next tool use or session start sets it back to `working`.
- `handoff_submit` sets the Coder to `handed_up`. `return_work` sets it to `idle`.
- Release sets `released` and `ended_at`.

## Wake-ups owed

The ledger cannot send a message to a session: `SendMessage` is a session tool, and the
CLI has no command for it. So each step that leaves work for another agent records a
wake-up the caller owes, and returns a `next` field with the exact call to make.

- Steps that owe a wake-up: `handoff_submit`, `return_work`, `message_post`,
  `phase_update(handed_up)`, a returned `module_review` or `phase_review`, `cr_open`,
  `cr_accept`, `cr_complete`, a failed `cr_verify`, and each level of a departure
  pushback chain.
- `next` is `SendMessage(to="<session name>", message="<one-line pointer>")` when the
  recipient's session runs, and `agent_resume(target_name=...)` when it does not.
- The `post_any` hook clears the debt when it sees a `SendMessage` to that session name.
- The Stop hook blocks a Manager, Lead, or Coder that still owes a wake-up and names each
  call. **(proposed)**
- The Coder's Stop hook also blocks once, per Coder, when the Coder stops while working
  with no handoff, and names `handoff_submit` or a `message_post` to its Lead.

## Messages

- `message_post(to_name, body)` sends to any agent registered in the run and refuses
  any other name, listing the registered ones. Any agent may message any other.
- `message_inbox()` returns and marks read the caller's unread messages. Every role
  reads it at the start of each turn after a wake-up.
- A `SendMessage` body is one line that points at the ledger record, for example
  "Handoff 1 for hello.py is waiting in the ledger." The detail lives in the ledger.

## The Oracle's Stop hook

- It never blocks while the run is paused.
- It does not block for pending work while any Manager, Lead, or Coder is working:
  waiting on running children is a valid stop. **(proposed)**
- It does not block for pending work while a directive waits on the user, with outcome
  `needs_user`. **(proposed)**
- Otherwise it blocks while the run has unlocked phases, submitted handoffs, live file
  claims, handed-up phases without an accepted review, or live agents none of which is
  working. The reason names each idle agent to wake and the call to wake it, and
  suggests `run_pause` when only the user can unblock the run.
- It also blocks while the run is active and the watchdog listener is not armed, even
  while children work. See "Watchdog" in [08-watchdog.md](08-watchdog.md).
- It does not block a stop that follows its own block (`stop_hook_active`).

## Liveness, resume, and release

- A session is running when `claude agents --json` shows a `pid` and its status and
  state are not `stopped`, `exited`, `crashed`, `failed`, `killed`, `dead`, or
  `completed`. State `done` counts as running.
- `agent_resume(target_name)` resumes a live agent of the run whose session is not
  running, with `claude --resume <session id> --bg "<pointer>"`. The pointer is the text
  of every wake-up the caller owes the target, or "Re-read your brief and your inbox in
  the ledger." It refuses a running session, because a resume of a running session
  starts a second copy. Any live agent of the run may call it, except on itself.
- Release sets the row `released` and stops the session with `claude stop <bg_id>`,
  which frees its memory. The Oracle's session is never stopped this way.
- `agent_release(target_agent_id)` releases a child of the caller. The other releases
  are automatic.

## What stops when

| Event | What stops |
|---|---|
| `approve` or `accept_incomplete` | The Coder's session |
| `agent_release` by a Manager after `module_review` accepts | The Lead's session |
| `phase_update(approved)` | The phase's Manager and every live agent under it |
| `run_finish` | Every agent still live except the Oracle |
| About 3 seconds after `run_finish` | The server waits up to 5 minutes for the Oracle's last turn to end, stops the Oracle's background session, and exits. An interactive Oracle is the user's terminal and keeps running |
| `idle_exit_minutes` with no active run, or a paused run, and no session of the run running | The ledger server |
| The ledger server's exit, by any path | Every shared MCP server's process tree **(proposed)** |
| A run that is not `active`, or a newer listener | The watchdog listener |

## Parallelism

- No limit by default. `agent_spawn` starts every session the plan calls for.
- When the user sets `parallelism_cap`, `agent_spawn` refuses at the cap, counting the
  run's live agents, the Oracle included. The parent tries again after a release.
  **(proposed)**
- A swarm session starts only its role's MCP servers, about 300 MB of memory each.
- With shared HTTP servers, a session starts no MCP server process: codebase-kg and the
  a11y servers run once per repo. **(proposed)**
