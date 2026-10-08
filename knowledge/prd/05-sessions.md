# Sessions

## Starting a child

`agent_spawn(caller, child_name)` refuses unless the caller is the parent named in the
child's unacknowledged brief. It also refuses a Manager before a `repo_check`, a child
whose phase is paused, a child that is already registered, a session name
a running session holds, and a start past `parallelism_cap` when the user set one. It
then runs, in the repo root:

```
claude "You are <name>. Read your brief from the swarm ledger and follow it." --bg \
  --name <session name> --agent swarm-<role> --model <brief model> \
  --permission-mode <file value> --strict-mcp-config --mcp-config <config> \
  --allowedTools <file tools> --settings '{"worktree":{"bgIsolation":"none"}}'
```

- The prompt goes before the options, because `--allowedTools` takes a space-separated
  list and swallows a prompt that follows it.
- `<config>` holds the ledger's HTTP URL, the agent file's `mcpServers`, and the a11y
  servers when the host has a11y installed. A Driver's config also holds cartographer's
  and each installed driver plugin's servers. Every plugin server is the shim's stdio
  entry. See "Plugin servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- It waits up to 15 seconds for the session to appear in `claude agents --json`, then
  records the child's row: `agent_id` = the session id, `session_name`, `bg_id`, state
  `registered`. A session that does not appear is stopped and the call fails.
- The parent ends its turn after its spawns. A child's message wakes it.
- The claude binary is `SENTINEL_SWARM_CLAUDE` when set, else `claude`.
- Development channels: when the environment variable `CLAUDE_DEV_CHANNELS` is not empty,
  every session launch appends `--dangerously-load-development-channels` and its entries as
  the last arguments. That covers `agent_spawn`, `agent_resume`, the watchdog's Oracle
  resume, and the Oracle's launcher in every mode. The entries are separated by spaces
  or commas, each `plugin:<name>@<marketplace>` or `server:<name>`. The flag goes last
  because it takes several values and swallows anything after it. This matches Alex's
  `claude` wrapper, which the launcher bypasses by calling the binary directly. The
  variable is empty today. Claude Code 2.1.283 reads the flag only in an interactive session, so today it
  takes effect only for an interactive Oracle; see
  [13-platform-facts.md](13-platform-facts.md). An interactive Oracle launch that
  carries the `swarm-events` channel adds `server:swarm-events` to the same flag, once.
  See "Wake-up delivery" below.
- The Oracle starts a Driver with `drive_request(focus)` instead of `brief_create` and
  `agent_spawn`: the tool performs both steps itself, under the child name
  `driver-e<ordinal>`, and returns the loop's status alongside the spawned agent. It is
  the only path that starts a Driver: `brief_create` refuses `child_role="driver"`, so
  every Driver has the request row its findings need. See "Explorations" in
  [02-run-lifecycle.md](02-run-lifecycle.md).

## Identity

- Every tool takes `caller`, the agent's name, and `agent_id`. The `pre_ledger` hook
  stamps `agent_id` with the session id through `updatedInput`, over any value the
  agent passes. `ledger_info`, `brief_get`, `who_owns`, `directive_submit`, and
  `events` take no identity.
- `brief_ack` binds a name to a session. After that, every call resolves `agent_id` to
  its row and refuses a `caller` that does not match, or an agent that has ended.
- The Oracle is bound at `run_start`.

## Start calls

A Manager, Lead, Coder, or Driver makes no start calls of its own. Its `session_start`
hook makes them before the model's first turn, because the hook already posts to the
ledger and knows the session id, which is the child's `agent_id`.

- For a `registered` child, the hook calls `brief_ack` under the agent's spawned name.
  It then returns, as `additionalContext`, the results of `ledger_info`, `brief_get`,
  `guidelines_get`, and, for a Manager or Lead, `run_status` without the PRD text.
- For a child already bound, on any `source` (`resume`, `compact`, `clear`), it
  returns the same results and does not bind again.
- A refused bind returns the refusal as the context. The tools stay allowed, so the
  child calls `brief_ack` itself. Until it succeeds, `pre_ledger` denies the child
  every ledger tool except `brief_ack` and the five that take no identity. See
  "What each hook does" in [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).
- Claude Code caps a hook's `additionalContext` at 10,000 characters and saves a
  longer one to a file that the model is not told to read. The hook keeps its context
  under 9,500 characters: a result that does not fit is left out, and the context
  names the call for the child to make itself.
- When the brief is in the context, the hook records `briefs.last_read_by_child_at`,
  as `pre_ledger` does for a `brief_get`. A child whose session restarts after a
  return therefore has its brief re-read for `handoff_submit`: the brief text is in
  its fresh context.
- The Oracle makes its own start calls. It is one session, and its start tools need
  the user's answers.

Each role's first tool call is then one `ToolSearch` `select:` of its working set,
without the start-call tools. A tool a role calls only at the end of its work stays
out of that set: the Coder loads `score_record` and `handoff_submit`, and the Driver
loads `drive_done`, with a second `select:` just before the first call.

## Agent states

`registered` → `working` ↔ `idle` → `handed_up` → `released`.

- `brief_ack`, which the `session_start` hook calls, sets `working`. The Stop hook sets `working` to `idle` when a turn ends.
  The next tool use or session start sets it back to `working`.
- `handoff_submit` sets the Coder to `handed_up`. `return_work` sets it to `idle`.
- Release sets `released` and `ended_at`.

## Wake-ups owed

The ledger cannot send a message to a session: `SendMessage` is a session tool, and the
CLI has no command for it. So each step that leaves work for another agent records a
wake-up the caller owes, and returns a `next` field with the exact call to make.

- Steps that owe a wake-up: `handoff_submit`, `return_work`, `message_post`,
  `phase_update(handed_up)`, a returned `module_review` or `phase_review`, `cr_open`,
  `cr_accept`, `cr_complete`, a failed `cr_verify`, each level of a departure pushback
  chain, `issue_escalate`, an `attempt_record` that advances an issue's round, and, for
  the Driver, `drive_issue` and `drive_checkin` (both owe the Oracle).
  `drive_done`, and `drive_unavailable` when the Driver calls it, owe the Oracle too, so
  the Oracle is woken directly when an exploration ends or the Driver cannot load. See
  "Explorations" in [02-run-lifecycle.md](02-run-lifecycle.md).
- A wake-up is owed only to a live agent with a session name, never to the caller
  itself.
- `next` is `SendMessage(to="<session name>", message="<one-line pointer>")` when the
  recipient's session runs, and `agent_resume(target_name=...)` when it does not. When
  `claude agents --json` fails, `next` names both: the `SendMessage` call, or
  `agent_resume` if that session is not running. When the recipient has
  a channel, the ledger pushes the wake-up itself instead. See "Wake-up delivery" below.
- The `post_any` hook clears the debt when it sees a `SendMessage` to that session name.
  When the sender is a Driver whose exploration is closed and it owes nothing more, the
  hook then releases it, which stops its session.
- The Stop hook blocks a Manager, Lead, Coder, or Driver that still owes a wake-up and
  names each call. It also blocks one that has unread messages in its run, and names
  `message_inbox`. When both apply, one block names the owed calls first and the unread
  count last. It does not block a stop that follows its own block
  (`stop_hook_active`), so a Driver that ignores the block twice goes idle, live, with
  its wake-up unsent. The watchdog reports that as `driver_unsent`. See "Detection" in
  [08-watchdog.md](08-watchdog.md).
- A role session wakes the agent its step leaves work for, the Oracle included. The
  Oracle's watchdog `Monitor` stays for the events no role session sends: watchdog
  findings, and the `PushNotification` calls the Oracle owes the user. See "How a report
  reaches the Oracle" in [08-watchdog.md](08-watchdog.md).

## Wake-up delivery

`wake_transport` in the settings file picks how a wake-up reaches its target. The
default is `channel`. `sendmessage` restores the `SendMessage` behavior above exactly,
and the launcher then loads no channel.

- `route_wakeup` in `mcp/src/swarm_ledger/wake.py` builds every wake instruction:
  `next`, the member Stop hook's owed calls, the Oracle Stop hook's wake hints, and the
  watchdog's stall step. It decides in this order:
  1. The setting is `sendmessage`: the `SendMessage` or `agent_resume` call above.
  2. The target's session is not running: `agent_resume`.
  3. The target supports channels: the ledger pushes the wake-up through the target's
     `swarm-events` channel, and `next` reads "Nothing to send: the ledger delivered
     this wake-up to <name> through its channel."
  4. Otherwise: the `SendMessage` call. A target with no session name gets
     `agent_resume`, and a target whose liveness is unknown gets both calls, as for
     `next` above.
- A target supports channels when its `agents.channel` is `launched` or `confirmed` and
  its bridge holds the ledger's `/events` stream open. Only a wake-up with a `wakeups`
  row is pushed, so the Oracle's wake hints and the watchdog's stall step name a call.
- `agents.channel` is `none`, `launched`, or `confirmed`. Only a launch that carries the
  channel lists `swarm-events` in its MCP config, so a bridge that connects records
  `launched` on its session's row. `run_start` and `agent_spawn` record it too, for a
  session whose bridge connected before its row existed. The first confirmed push
  records `confirmed`.
- Only an interactive Oracle started by the launcher carries the channel. Every `--bg`
  role, and a `--bg` or headless Oracle, keeps `channel` at `none`, and its wake-ups go
  by `SendMessage`. See "Launch" in [11-setup-and-settings.md](11-setup-and-settings.md).
- The bridge is `swarm-events`, a standard-library stdio MCP server,
  `python -m swarm_ledger.bridge`, run as `hook.py channel`. It declares only the
  `claude/channel` capability and has no tools. After `notifications/initialized` it
  reads `CLAUDE_CODE_SESSION_ID`, takes the ledger's port from `server.json`, and holds
  `GET /events?session=<session id>` open. It reconnects 2 seconds after a drop, and
  reads the port again each time. It writes each event, except a ping, as one
  `notifications/claude/channel` with the event's `content` and the `meta` keys that are
  identifiers. Its instructions tell the session to follow the pointer in each event.
  See `/events` in [06-ledger-server.md](06-ledger-server.md).
- A pushed wake-up records `pushed_at`. The ledger confirms it from the target's
  transcript: a user turn, outside a sidechain, from the channel (origin kind `channel`,
  a `turnOrigin` or `promptSource` of `channel`, or a `<channel source="swarm-events">`
  tag) that carries the wake-up's pointer or its `wakeup_id`, stamped no earlier than 5
  seconds before the push. Confirmation sets `sent_at` and `channel = confirmed`, and
  costs no model turn. The watchdog checks on every pass, and the sender's Stop hook
  checks before it decides. The transcript shape of a channel turn is not
  verified live yet.
- The member Stop hook waits, polling each second, until each pushed wake-up it owes is
  confirmed or 30 seconds old. A confirmed push is settled. An unconfirmed push blocks
  the sender with the `SendMessage` call, or `agent_resume` when the target stopped.
  That fallback call never pushes, so a lost event never strands a target.
- The watchdog reports a push that stays unconfirmed and unsent. See `wake_unconfirmed`
  in [08-watchdog.md](08-watchdog.md).
- A channel event carries a `kind`. A wake-up is kind `wakeup`, with the pointer as its
  content and `wakeup_id` and `reason` as its meta. The stream also sends a `ping` event
  after 15 seconds without one. A future device-queue broker adds a second kind on
  `swarm-events`, not a separate server. **(needs implementation: the event `kind` and
  the `ping` are built; the device-queue broker is not)**
- The Coder's Stop hook also blocks once, per Coder, when the Coder stops while working
  with no handoff, and names `handoff_submit` or a `message_post` to its Lead.

## Time signal

Opus 5.5 paces its work to the elapsed time it sees, and a time budget mostly keeps more
agents working in parallel. Sonnet 5.5 can read text that arrives after a tool result as
a possible injection. So only the Oracle and the Managers see the elapsed time.

- Every wake-up whose target is the Oracle or a Manager ends with the run's elapsed
  time, measured from `runs.started_at`: `elapsed <n>s`, or `elapsed <n>s / <budget>s`
  when `time_budget_minutes` is set. See "Settings file" in
  [11-setup-and-settings.md](11-setup-and-settings.md).
- The suffix goes on the delivered text only: the pushed channel event's content, the
  `SendMessage` message in `next` and in the Stop hook's owed calls, and the message
  `agent_resume` sends. The `wakeups` row keeps the bare pointer, so transcript
  confirmation still finds it.
- `wake.signal_for` gives the suffix only for a target whose role is `oracle` or
  `manager`. A wake-up for a Lead, a Coder, or a Driver carries none, and no tool
  result carries a countdown for them.
- Each line the watchdog listener prints, and the watchdog's own resume message to the
  Oracle, ends with the same suffix. See [08-watchdog.md](08-watchdog.md).
- The Oracle and Manager templates explain the suffix where they describe wake-ups, with
  the sentence "Time matters here: do not spend time that can be avoided, and the
  earlier a correct result is obtained, the better." Saving time never skips a review,
  a score, or a test, and every ledger gate still holds.
- The budget is advisory. Nothing stops a run at the limit; the watchdog and the
  escalation budget stay the only controls on a runaway agent.
- The Driver sees no elapsed time. `drive_issue` and `drive_checkin` return
  `next_checkin_due_at` and `next_checkin_in_s` instead. See "Explorations" in
  [02-run-lifecycle.md](02-run-lifecycle.md).
- The budget comes from the run's settings snapshot, `runs.settings_json`, taken at
  `run_start`, so a change to the settings file applies to the next run. An old run
  whose snapshot has no budget shows the elapsed time alone.

## Messages

- `message_post(to_name, body)` sends to an agent registered in the run and refuses any
  other name, listing the registered ones. The recipient must be the caller's parent,
  one of its children, or a sibling (the same parent); the refusal lists those names.
  A Manager reaches a Coder through the Coder's Lead, and the Oracle reaches a Lead or a
  Coder through its Manager. Messages the ledger writes itself, such as pushbacks,
  escalations, and violations, do not go through `message_post`. It refuses a `body`
  over 32,000 characters.
- `pre_send_message` applies the same relation to `SendMessage`, by session name, and
  also allows a session the caller owes an unsent wake-up: the ledger owes some
  wake-ups across levels, such as `cr_open` to a file's Coder, `cr_verify` by the
  nearest live ancestor, issue escalations, and disputes. The Oracle may also wake any
  live agent of the run that is not working, because its Stop hook names such agents
  when the run stalls. The text of a `SendMessage` is not checked.
- A change request is not direction: `cr_open` from a Manager or the Oracle routes to
  the file's live Coder, and owes it the wake-up.
- `message_inbox()` returns `messages` and `remaining`. It takes the caller's unread
  messages in its own run, matched by `run_id` and name, so an agent never reads mail
  addressed to the same name in an earlier run. It returns them oldest first, up to
  40,000 characters of bodies, and marks those read in the same transaction. Rows past
  the cap stay unread, and `remaining` counts them. The first unread message always
  comes back, even one the ledger wrote over the cap. Every role reads it at the start
  of each turn after a wake-up, and calls it again while `remaining` is above 0.
- An index on `messages (run_id, to_name, read_at)` serves the inbox and the unread
  counts. `connect` adds it to an older `ledger.db`.
- A `SendMessage` body is one line that points at the ledger record, for example
  "Handoff 1 for hello.py is waiting in the ledger." The detail lives in the ledger.

## The Oracle's Stop hook

- It blocks while the Oracle owes the user a `PushNotification` call for a Driver
  event, and names each call. This debt blocks even while the run is paused, while
  children work, while a directive waits on the user, and after `run_finish`, because
  the call ends at once. See "Driver notifications" in
  [02-run-lifecycle.md](02-run-lifecycle.md).
- Otherwise it never blocks while the run is paused.
- It does not block for pending work while any Manager, Lead, or Coder is working:
  waiting on running children is a valid stop.
- A child that is still `registered`, was spawned no more than 2 minutes ago
  (`REGISTER_GRACE`), and whose session is running counts as working, because it has
  not yet acknowledged its brief. The Oracle then stops instead of waking a child that
  is starting. A child that never starts is reported by the watchdog.
- It does not block for pending work while a directive waits on the user, with outcome
  `needs_user`.
- Otherwise it blocks while the run has unlocked phases, submitted handoffs, live file
  claims, handed-up phases without an accepted review, or live agents none of which is
  working. Phases under a scoped pause do not count. The reason names
  each idle agent to wake and the call to wake it, and suggests `run_pause` when only
  the user can unblock the run.
- It also blocks while the run is active and the watchdog listener is not armed, even
  while children work. See "Watchdog" in [08-watchdog.md](08-watchdog.md).
- It does not block a stop that follows its own block (`stop_hook_active`).

## Liveness, resume, and release

- A session is running when `claude agents --json` shows a `pid`, or lists it with
  `kind: "background"` (Claude Code 2.1.284 prints no `pid` for one), and its status and
  state are not `stopped`, `exited`, `crashed`, `failed`, `killed`, `dead`, or
  `completed`. State `done` counts as running.
- `agent_resume(target_name)` resumes a live agent of the run whose session is not
  running, with `claude --resume <session id> --bg "<pointer>" --name <session name>`
  and the role's launch options, because `--resume` restores the conversation but not
  the name or the launch flags. The pointer is the text
  of every wake-up the caller owes the target, or "Re-read your brief and your inbox in
  the ledger." The resume marks those wake-ups sent. It refuses a running session,
  because a resume of a running session starts a second copy. Any live agent of the run
  may call it, except on itself.
- Release sets the row `released` and stops the session with `claude stop <bg_id>`,
  which frees its memory. The Oracle's session is never stopped this way.
- `agent_release(target_agent_id)` releases a child of the caller. The other releases
  are automatic.
- A Driver whose exploration is closed is released once it owes no unsent wake-up: after
  its `SendMessage` to the Oracle, after its own `agent_resume` of the Oracle, or at its
  Stop hook when a channel push was confirmed. `drive_request` and `run_finish` release
  one that is still live.

## What stops when

| Event | What stops |
|---|---|
| `approve` or `accept_incomplete` | The Coder's session |
| `agent_release` by a Manager after `module_review` accepts | The Lead's session |
| `phase_update(approved)` | The phase's Manager and every live agent under it |
| The Driver's wake-up to the Oracle after `drive_done`, or after its own `drive_unavailable`, is marked sent | The Driver's own session |
| `drive_done` or the Driver's own `drive_unavailable` with no live Oracle to wake | The Driver's own session, at once |
| `agent_release` of a Driver by the Oracle, or the Oracle's `drive_unavailable` | The Driver's session; its open exploration becomes `abandoned` |
| `drive_request` | An earlier Driver still live after its exploration ended |
| `run_finish` | Every agent still live except the Oracle |
| About 3 seconds after `run_finish` | The server waits up to 5 minutes for the Oracle's last turn to end, stops the Oracle's background session, and exits. An interactive Oracle is the user's terminal and keeps running |
| `idle_exit_minutes` with no active run, or a paused run, no session of the run running, and no ledger tool call | The ledger server |
| A run that is not `active`, or a newer listener | The watchdog listener |

## Parallelism

- No limit by default. `agent_spawn` starts every session the plan calls for.
- When the user sets `parallelism_cap`, `agent_spawn` refuses at the cap, counting the
  run's live agents, the Oracle included, plus other swarms' live sessions on the
  machine: entries from `claude agents --json` that are running, whose name matches the
  swarm session-name shape (`<slug>-r<run_id>-<name>`, from `looks_like_swarm_session`),
  and that are not one of this run's own session names. The parent tries again after a
  release.
- `role_parallelism_cap.<role>` caps that role's own live agents in the run, on top of
  `parallelism_cap`.
- `drive_request` starts the Driver through the same `agent_spawn` path, so both caps
  apply to it exactly as to any other role; in practice a run has at most one live
  Driver, since `drive_request` refuses while an exploration is already open.
- A swarm session starts only its role's MCP servers, about 300 MB of memory each.

## The multi-repo lock

- `run_start` takes a machine-level lock before it opens a new run, so two runs never
  work the same repo at once, including from a second clone path or a worktree of it.
  The lock key is the repo's resolved main checkout: `git rev-parse
  --path-format=absolute --git-common-dir` from the repo root, its parent, resolved;
  falls back to the repo root itself when git is unavailable. The lock file sits at
  `<CLAUDE_CONFIG_DIR or ~/.claude>/sentinel-swarm/locks/<sha256 of that path>.json` and
  records that path, the run id, and the ledger server's pid.
- `run_start` refuses a new run while the lock's server pid is still alive
  (`lock.pid_alive`, native on POSIX, `OpenProcess` on Windows), whatever run id the
  lock names. Resuming the run that already holds the lock is unaffected, because only
  opening a run takes the lock.
- The lock clears at `run_finish`, when the ledger server that holds it shuts down for
  any reason (`serve._shut_down` releases a lock its own pid owns), and, self-healing,
  the next `run_start` that finds a lock whose pid is no longer alive treats it as
  stale and takes it.

## Per-role settings

- `effort.<role>` passes `--effort <level>` to that role's `agent_spawn`, verified
  against the CLI's own `--effort` flag. A role with no entry runs at its model's
  default effort.
- `prompt_cache_ttl.<role>` sets `promptCacheTtl` in that role's session `--settings`
  JSON, verified against the `promptCacheTtl` settings key (Claude Code accepts only
  `"5m"` or `"1h"` and otherwise ignores the value). A role with no entry keeps Claude
  Code's own default TTL.
- `role_parallelism_cap.<role>`: see "Parallelism" above; a ledger-side cap, not a
  Claude Code mechanism.
- No CLI flag or setting controls per-session concurrency beyond what `parallelism_cap`
  and `role_parallelism_cap` already give the ledger, so no other per-role setting was
  added.
