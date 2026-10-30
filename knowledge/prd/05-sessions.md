# Sessions

## Starting a child

`agent_spawn(caller, child_name)` refuses unless the caller is the parent named in the
child's unacknowledged brief. It also refuses a Manager before a `repo_check`, a child
whose phase is paused, a child that is already registered, a session name
a running session holds, and a start past `parallelism_cap` when the user set one. A
start that would take free memory below `memory.headroom_mb` is queued, not refused;
see "Memory" below. It then runs, in the repo root:

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
  [13-platform-facts.md](13-platform-facts.md). The swarm adds no channel of its own.
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
- `run_start` and `brief_ack` need a session the mod checked in for. `pool.run_tool`
  calls `identity.require_mod_session`, which refuses either tool when no `mod_sessions`
  row exists for the stamped `agent_id` (for `run_start`, the call's `session_id`): "<tool>
  refuses session '<id>': the sentinel-swarm mod never checked in for it, so no hook
  gates this session. Run claude plugin list in the repo: sentinel-swarm must show
  enabled, not failed to load. Fix that, then start the session again". The mod's
  `session_start` post records the row. See "The mod" in
  [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).

## Start calls

A Manager, Lead, Coder, or Driver makes no start calls of its own. Its `session_start`
hook makes them before the model's first turn, because the hook already posts to the
ledger and knows the session id, which is the child's `agent_id`.

- For a `registered` child, the hook calls `brief_ack` under the agent's spawned name.
  The hook records the child's `mod_sessions` row first, so this call passes the mod
  check-in. It then returns, as `additionalContext`, the results of `ledger_info`, `brief_get`,
  `guidelines_get`, and, for a Manager or Lead, `run_status` without the PRD text.
- For a child already bound, on any `source` (`resume`, `compact`, `clear`), it
  returns the same results and does not bind again.
- A refused bind returns the refusal as the context. The tools stay allowed, so the
  child calls `brief_ack` itself. Until it succeeds, the ledger server refuses the
  child every ledger tool except `brief_ack` and the five that take no identity, and
  `pre_ledger` denies the same calls first. See "Tools" in
  [06-ledger-server.md](06-ledger-server.md) and "What each hook does" in
  [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).
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
loads `drive_done`, with a second `select:` just before the first call. Tool search is
on by default, so a session starts with tool names only and loads a schema when a
`select:` names it. A tool taken out of a role's set saves its schema bytes in every
session of that role. See "Measured baselines" in [12-testing.md](12-testing.md).

## Agent states

`registered` → `working` ↔ `idle` → `handed_up` → `released`, and `parked` from any
live state.

- `brief_ack`, which the `session_start` hook calls, sets `working`. The Stop hook sets `working` to `idle` when a turn ends.
  The next tool use or session start sets it back to `working`.
- `handoff_submit` sets the Coder to `handed_up`. `return_work` sets it to `idle`.
- Release sets `released` and `ended_at`.
- `parked` means the agent is live but its session is closed: the daemon retired it,
  or the ledger stopped it. The write that sets `parked_at` and `park_reason` also sets
  `state` to `parked` and keeps the state it replaced in `state_before_park`. Unparking
  puts that state back and clears all three, in one write. A parked agent is not
  ended: the ledger resumes it when a wake-up is owed to it. See "Parked agents" below.
- A change the workflow makes to a parked agent's state goes to `state_before_park`:
  the Stop hook's `working` to `idle`, and `return_work`'s `handed_up` to `idle`. The
  checks that ask what an agent waits on (the `crashed` finding, and the Oracle's Stop
  hook naming idle agents to wake) read `state_before_park` for a parked agent. The
  checks that ask whether a session works now count a parked agent as not working.

## Wake-ups owed

The ledger cannot send a message to a session: `SendMessage` is a session tool, and the
CLI has no command for it. So each step that leaves work for another agent records a
wake-up the caller owes, and returns a `next` field with the exact call to make.

- Steps that owe a wake-up: `handoff_submit`, `return_work`, `message_post`,
  `phase_update(handed_up)`, a returned `module_review` or `phase_review`, `cr_open`,
  `approve` and `accept_incomplete` for each live agent that called `wait_for` on the
  file,
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
  `agent_resume` if that session is not running. See "Wake-up delivery" below.
- When the caller has a `mod_sessions` row, `next` is instead "Nothing to call: the
  sentinel-swarm mod wakes <name> for you. Your Stop hook names a call only if that
  wake-up is not delivered." The mod's `session_start` post writes the row.
- The mod marks a wake-up paid through `wake_sent` once its send is delivered. The
  `post_any` hook clears the debt when it sees a `SendMessage` to that session name.
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

The sender's mod delivers every owed wake-up. The sender's model delivers a wake-up the
mod did not deliver: `SendMessage` to a running target, and `agent_resume` to a stopped
one. The ledger pushes nothing into a session.

### Mod wake-up delivery

- After each ledger tool call returns, the mod posts `owed` with the session id, off
  the hook's critical path. The ledger answers the caller's unsent wake-ups whose
  target has not ended, one entry per target: the target's session id, the pointers
  joined into one text, the time signal for an Oracle or Manager target, and the
  wake-up ids. It leaves out a target that `pre_send_message`'s recipient check would
  refuse; the mod's own sends do not raise its `session.send` hook, so the check runs
  in the ledger before the mod sends. They do raise its `SendMessage` gate, which
  passes a wake-up text the mod is sending (see "The mod" in
  [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md)).
- Before it answers, the ledger reads `claude agents --json` once for the targets. A
  target whose session is not running, or that is parked, is left out of the answer.
  When it has a session name and is not the Oracle, the ledger resumes it itself, after
  the owing call's transaction committed, with the pointers of every wake-up owed to it,
  and re-keys it; see "Re-keying a resumed session" below. A resume already in flight
  for the target, started in the last 2 minutes, starts no second one. When free memory
  minus the role's estimate is below `memory.headroom_mb` and another member session of
  the run still runs, the target is parked instead, "waiting for memory to resume it for
  a wake-up", and the watchdog resumes it later. A running entry under the target's
  session name with another id re-keys the agent, and the wake-up goes to the new id.
  When the listing fails, nothing is resumed and every target is answered as before.
- The Oracle is never resumed this way: an interactive Oracle is the user's terminal,
  and a background Oracle follows the watchdog's Oracle resume (see "How a report
  reaches the Oracle" in [08-watchdog.md](08-watchdog.md)).
- The mod sends each entry with `$.session.send({ to: { sessionId }, text })`, all
  targets in parallel. Every role is addressed by session id, the interactive Oracle
  included, never by name.
- A send that answers "no live session" is tried again after 2, 4, and 8 more seconds:
  a session registers 10 to 14 s after it starts. Any other refusal ends the attempt.
- The mod posts `wake_sent` with the ids whose send reported `isDelivered`. The ledger
  marks only the caller's own rows sent and releases a closed Driver, as `post_any`
  does. A wake-up whose send failed stays owed. `isDelivered` means the message is
  queued for the target, not read, as for a `SendMessage` call.
- The mod's `Stop` hook waits for the sends still in flight from earlier calls, pays
  what is still owed, and only then posts `stop`. So the Stop hook names `SendMessage` or `agent_resume` only for a
  wake-up the mod did not deliver: that is the fallback, and `agent_resume` stays the
  call for a stopped target.

- `route_wakeup` in `mcp/src/swarm_ledger/wake.py` builds every wake instruction:
  `next`, the member Stop hook's owed calls, the Oracle Stop hook's wake hints, and the
  watchdog's stall step. It decides in this order:
  1. The target has no session name, or its session is not running: `agent_resume`.
  2. The target's liveness is unknown: both calls, as for `next` above.
  3. Otherwise: the `SendMessage` call.
- The member Stop hook names each owed call at once. It does not wait.
- No role session runs a `Monitor` to receive wake-ups. Each one adds a process per
  session, keeps the session's status `busy` while it waits, leaves its children running
  on Windows when it stops, and puts every printed line into the session's context. Only
  the Oracle's watchdog `Monitor` exists.
- A device-queue broker that tells a session when a device frees up needs a new plan on
  the mod's delivery path. **(needs implementation)**
- The Coder's Stop hook also blocks once, per Coder, when the Coder stops while working
  with no handoff, and names `handoff_submit`, a `message_post` to its Lead, or
  `wait_for`. It does not block a Coder with an open file wait.

## Time signal

Opus 5.5 paces its work to the elapsed time it sees, and a time budget mostly keeps more
agents working in parallel. Sonnet 5.5 can read text that arrives after a tool result as
a possible injection. So only the Oracle and the Managers see the elapsed time.

- Every wake-up whose target is the Oracle or a Manager ends with the run's elapsed
  time, measured from `runs.started_at`: `elapsed <n>s`, or `elapsed <n>s / <budget>s`
  when `time_budget_minutes` is set. See "Settings file" in
  [11-setup-and-settings.md](11-setup-and-settings.md).
- The suffix goes on the delivered text only: the `SendMessage` message in `next` and
  in the Stop hook's owed calls, and the message `agent_resume` sends. The `wakeups` row
  keeps the bare pointer.
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
  of each turn after a wake-up, and calls it again while `remaining` is above 0. It
  skips a row the mod holds under a claim less than 2 minutes old.
- The inbox inside a wake-up: when a peer delivery reaches a role session, the mod's `session.receive` hook posts `inbox_take`. The ledger
  claims the caller's unread messages of its own run, up to the same 40,000-character
  cap, under a new claim id, and returns them with `remaining`. The mod appends them
  to the delivered text after `message_inbox() returned:`, then posts `inbox_ack`,
  which marks the claimed rows read, or `inbox_release` when the delivery was
  consumed or failed. A claim nobody settles counts as unread again after 2 minutes,
  and a late `inbox_ack` then marks nothing. The woken role calls `message_inbox`
  only when that text says more wait.
- An index on `messages (run_id, to_name, read_at)` serves the inbox and the unread
  counts.
- Message bodies live in `ledger.db`, not in a separate mailbox. A `message_post`
  commits the body, the recipient check, and the owed wake-up in one transaction. A
  second database would split run state and move the recipient check out of the
  ledger's gates. The ledger sets no rate limit and no duplicate filter on messages:
  the agents of one run are trusted peers, and the watchdog and the escalation budget
  control a runaway agent.
- A `SendMessage` body is one line that points at the ledger record, for example
  "Handoff 1 for hello.py is waiting in the ledger." The detail lives in the ledger.
- Agent-to-agent text goes through `message_post`, never a model's own `SendMessage`:
  the ledger checks the recipient, and the mod delivers the wake-up by session id at
  delivery time, so it reaches the live session even after a resume moved it to a new
  id. The role templates say so.

### Session names and SendMessage

`SendMessage` takes a session name. A swarm session that is also connected over Remote
Control is listed twice under one name, and after a resume the old session's Remote
Control entry stays listed, offline, under the same name. A send by bare name then
fails as ambiguous, or goes to the offline entry and waits there for good. See
[13-platform-facts.md](13-platform-facts.md).

- The mod's `session.send` hook takes every `SendMessage` the model of a swarm session
  makes and posts `send_target` with its `to`. When `to`, without a trailing
  ` [<ref>]`, is the session name of a live agent of the caller's run that
  `pre_send_message` allows, the ledger answers that agent's session id, and the mod
  sends the text to `{ sessionId }` itself and returns that result to the model.
  Any other name goes through unchanged.
- `pre_send_message`, `send_target`, and `post_any` read `to` without a trailing
  ` [<ref>]`, so a send to `"<name> [<ref>]"` is checked, and pays a wake-up, as a send
  to the name.
- Where a model-issued `SendMessage` stays the fallback, the text that names it adds
  the retry rule: "If a SendMessage answers that several agents carry the name, send
  it again with to="<name> [<ref>]", the ref of the session listed on this machine,
  never a Remote Control one." The member Stop hook's owed calls, the Oracle Stop
  hook's wake hints, and the watchdog's stall step carry it whenever they name a
  `SendMessage`, and every role template states it.

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
  the name or the launch flags. The pointer is the text of every unsent wake-up owed to
  the target, from any sender, or "Re-read your brief and your inbox in the ledger." It
  refuses a running session, because a resume of a running session starts a second
  copy. It refuses a target with no recorded `session_name`: "'<name>' has no recorded
  session, so agent_resume cannot wake it: a resume without one starts a fresh
  session". `claude --resume <id> --bg <message>` without `--name` comes back as a
  session named after the message. Any live agent of the run may call it, except on
  itself.
- When a session runs under the target's session name with another session id, for
  example one resumed earlier, `agent_resume` starts nothing: it re-keys the agent to
  that session and returns `rekeyed_from`.
- Release sets the row `released` and stops the session with `claude stop <bg_id>`,
  which frees its memory. The Oracle's session is never stopped this way.
- Release also stops the processes the session's tools started, which `claude stop`
  leaves running. Before the stop, the ledger reads the machine's process table
  (`procs.snapshot`: `CreateToolhelp32Snapshot` on Windows, `ps -A` elsewhere) and takes
  the session's process: the pid in `<config>/sessions/<pid>.json`, else the
  `session_pid` the watchdog last saw running. It keeps each child of that process that
  is a shell (`bash`, `sh`, `zsh`, `dash`, `fish`, `cmd`, `powershell`, `pwsh`) and
  every process under such a shell. After the stop, it terminates each of those still
  running and each new process under them (`TerminateProcess` on Windows, `SIGKILL`
  elsewhere), never the ledger server or the process making the call.
- The session's other children are its MCP servers. They exit with the session, and a
  plugin's relay among them can start the machine's one shared server that other
  sessions use, so a release leaves them alone.
- A process counts as a child only when it started after its parent, because a parent
  pid can name a dead process whose pid was reused. A process is terminated only while
  its start time still matches the one recorded.
- The release records the session's process and every process it found in
  `released_processes`, and logs how many it stopped in the release's agent event. The
  watchdog reports any of them still running, and any process started under them, as
  `stray_process`; see [08-watchdog.md](08-watchdog.md).
- `agent_release(target_agent_id)` releases a child of the caller. The other releases
  are automatic. Every release hands the released agent's `open` and `accepted` change
  requests to its nearest live ancestor; see "Change requests" in
  [04-agreements.md](04-agreements.md).
- A Driver whose exploration is closed is released once it owes no unsent wake-up: after
  its `SendMessage` to the Oracle, after its own `agent_resume` of the Oracle, or at its
  Stop hook when it owes nothing. `drive_request` and `run_finish` release one that is
  still live.

### Re-keying a resumed session

Every part of the ledger keys an agent by its current session id: the hooks stamp it as
`agent_id`, and liveness, the mod's sends, `mod_sessions`, and the transcript all use
it. A resumed background session can come back under a new session id (see
[13-platform-facts.md](13-platform-facts.md)), so the ledger re-keys the agent rather
than keep an alias.

- Before the resume, the ledger sets `agents.resume_started_at`. After it, it records
  the background id the CLI printed as `bg_id`, then polls `claude agents --json` for
  up to 15 seconds for the entry whose `id` is that background id, or else a running
  entry with the session name.
- When the entry's session id differs, one write transaction replaces the old id with
  the new one in every column that stores an agent id: `rekey.agent_id_columns` reads
  the schema for columns named `*agent_id`, columns that reference `agents`, and
  `mod_sessions.session_id`. It also sets `bg_id` to the new id's first 8 characters,
  points `transcript_path` at `<new id>.jsonl` in the same folder, clears
  `resume_started_at`, and logs a `rekeyed` agent event. The same id needs no re-key.
- The resumed session's own hooks can arrive before the poll ends. Each hook from a
  session id the ledger does not know re-keys the agent whose `resume_started_at` is set
  and whose `bg_id` equals the id's first 8 characters. `session_start` and `pre_ledger`
  also look the id up in `claude agents --json` and match its session name, while a
  resume started in the last 2 minutes is pending. Both paths and the poll are
  idempotent.
- While such a resume is pending, `pre_ledger` denies a swarm session the ledger does
  not know every tool that takes an identity, except `run_start`: "This session was just
  resumed, and the ledger has not matched it to its agent yet. Wait a few seconds, then
  make the same call again."
- A resume marks the wake-ups its pointer carried sent, and reports `reachable: true`,
  only once the poll found the resumed session. Otherwise they stay owed, and the call
  returns `reachable: false`. The agent is re-keyed when its first hook arrives, and the
  sender's mod then delivers the wake-ups to the new id.
- The watchdog's Oracle resume re-keys the Oracle the same way.

### Parked agents

- On each pass, the watchdog reconciles the run's live Managers, Leads, and Coders with
  the plain `claude agents --json` listing (`sessions.list_sessions`). One whose session
  is absent or not running, started more than 2 minutes ago and with no resume in
  flight, is parked. Its reason is the matching `bg retire <bg id>: <reason>` line of
  `~/.claude/daemon.log` when one exists ("the daemon retired its session: idle-prompt,
  idle 6m [low memory]"), else "its session is not in claude agents --json" or "its
  session is not running (state <state>)". A parked agent whose session is seen running
  again is unparked. A successful resume unparks it.
- A hook from a parked agent's session unparks it before the hook runs, because a
  session that sends a hook is running. `stop` and `session_end` do not, since a
  session's last hooks can arrive after the daemon retired it.
- The watchdog releases a parked Manager whose phase is `handed_up` or `approved`, and a
  parked Lead whose module is `approved` or whose latest `module_review` is accepted,
  once it has been parked for `idle_exit_minutes`. It releases only one with no live
  child, no unsent wake-up owed to it, and no unread message. The release's agent event
  names why: "the watchdog released it: parked over <n> minutes (<park reason>), and its
  phase is handed_up". If a review returns the work, the Oracle or the Manager briefs a
  fresh one: a returned review of an ended subject already reports
  `subject_ended`.
- When memory frees, the watchdog resumes parked agents that are owed a wake-up, each
  with that wake-up's pointers: first the parents none of whose children is still
  `registered` or `working`, then by phase ordinal, then by the oldest owed wake-up. It
  stops at the first one that does not fit under the headroom.
- A resume fits when free memory minus the role's estimate stays at or above
  `memory.headroom_mb`, or when no member session of the run runs, since then waiting
  frees nothing. Unknown free memory fits.
- `crashed` still reports a `working` or late `registered` agent whose session is gone,
  and the Oracle resumes it with `agent_resume`. A failed resume is logged as a
  `resume_failed` agent event, and the agent stays parked.

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

- `agent_spawn` refuses at `parallelism_cap`, counting the run's live agents, the
  Oracle included, plus other swarms' live sessions on the machine: entries from
  `claude agents --json` that are running, whose name matches the swarm session-name
  shape (`<slug>-r<run_id>-<name>`, from `looks_like_swarm_session`), and that are not
  one of this run's own session names. The parent tries again after a release.
- Left empty, `parallelism_cap` is set from total memory: half of it, at 600 MB a
  session, and at least 4 (27 on a 32 GB machine). Unknown total memory leaves no cap,
  and `0` turns the cap off. Setup prints the value and says that it counts other
  swarms' sessions.
- `role_parallelism_cap.<role>` caps that role's own live agents in the run, on top of
  `parallelism_cap`.
- `drive_request` starts the Driver through the same `agent_spawn` path, so both caps
  apply to it exactly as to any other role; in practice a run has at most one live
  Driver, since `drive_request` refuses while an exploration is already open.
- A swarm session starts only its role's MCP servers, about 300 MB of memory each.

## Memory

Claude Code's daemon retires idle background sessions when the machine runs low on
memory, and an idle parent waiting on its children is what it retires first. The
ledger treats memory as a run resource. See [13-platform-facts.md](13-platform-facts.md)
for the measured figures.

- **Measure.** `memory.free_mb` reads free system memory with the standard library:
  `GlobalMemoryStatusEx` on Windows, `vm_stat` (free, inactive, speculative, and
  purgeable pages) on macOS, and `MemAvailable` in `/proc/meminfo` on Linux. Each
  watchdog pass samples each running session's resident memory where it finds a pid,
  from the listing or from `<config>/sessions/<pid>.json`, with
  `GetProcessMemoryInfo` on Windows or `ps -o rss=` elsewhere, and keeps a running mean
  per role in `memory_estimates`. A role's estimate is that mean, or 300 MB with no
  sample, plus 300 MB for a Coder's test and type-check processes. The functions are
  replaceable, as `sessions._run` is, so tests fake them.
- **Admit.** Before `agent_spawn` starts a session, it checks the machine-wide
  estimate: free memory, less the memory reserved for sessions still starting on the
  machine by any swarm, minus the child role's estimate, must stay at or above
  `memory.headroom_mb` (1,500 by default; the daemon retires sessions at about 700 MB
  free). A spawn that does not fit, or that arrives while earlier spawns of the run
  wait, is queued in `spawn_queue` and returns `queued: true`, the reason, and a `next`
  that says not to call `agent_spawn` for it again. A second `agent_spawn` of a queued
  child returns the same entry. Unknown free memory, or an empty `headroom_mb`, admits
  the spawn.
- **Reserve.** Free memory does not show a session until it has started, about a
  minute, so two swarms, or two calls of one swarm, could each admit a session against
  the same free memory. Every admission therefore runs under the machine-wide file lock
  `<config>/sentinel-swarm/locks/memory.lock`, and each admitted spawn or resume records
  its estimate in `locks/memory.json` with its server's pid and the time. A reservation
  counts for 60 seconds, and only while its server runs. Other swarms' queued spawns do
  not count: two runs that each held back for the other's queue would both wait.
- **Start queued spawns.** Each watchdog pass starts queued spawns oldest first, while
  they fit. It skips one whose phase is paused or that a parallelism cap holds back,
  and drops one whose brief was acknowledged, whose parent ended, or whose start failed,
  with a ledger message to the parent that says why. A start that fails on an
  unreadable `~/.claude.json` stays queued; see "An unreadable ~/.claude.json" below. While a spawn is queued, the
  Oracle's Stop hook does not block for pending work, and the watchdog reports no stall.
- **Capacity.** `run_status` returns `capacity`: `free_mb`, `reserved_mb` (the
  reservations above), `other_swarms` (every other live swarm on the machine, from the
  multi-repo lock), `headroom_mb`, `estimate_mb` per role, `spawn_slots` per role (how
  many more sessions fit under the headroom after the reservations, within the
  parallelism caps, less the queued spawns), `queued_spawns`, and `parked` agents with
  their reasons. The `status` skill prints the queued spawns and the parked agents in
  its first lines. The Manager, Lead, and Oracle templates start at
  most `spawn_slots` children at once.
- **Park idle parents.** When `memory.park_idle_parents` is on (the default) and free
  memory is below the headroom plus the estimates of the queued spawns and the
  reservations, each watchdog
  pass stops idle Leads, then idle Managers, with `claude stop <bg_id>`, until the
  estimates it freed cover the shortfall. It stops only one whose session runs, that has
  a live child that is `registered` or `working`, and that is owed no unsent wake-up and
  has no unread messages, checked in the same transaction that sets `parked_at`, with
  the reason "the ledger stopped it under memory pressure while its children work". A
  failed stop clears the flag. The first wake-up owed to it resumes it.

## An unreadable ~/.claude.json

Many sessions starting, resuming, and stopping at once can leave `~/.claude.json`
truncated for a while, and every `claude` command fails until a later write completes
the file. See [13-platform-facts.md](13-platform-facts.md).

- A `claude` command that starts, resumes, or stops a session, and the listing that
  `agent_spawn` and `agent_resume` read first, is tried again when it fails with "JSON
  Parse error" or "`.claude.json` is corrupted": after 1, 2, 4, and 8 seconds, five
  tries over 15 seconds. That covers `agent_spawn`, queued spawns, `agent_resume`, the
  ledger's resume of a wake-up target, and the watchdog's resume of the Oracle. The
  mod's wake-up send is tried again on the same error, after 2, 4, and 8 seconds.
- When the last try fails and `~/.claude.json` still does not parse, the ledger pauses
  the run. The pause reason starts "~/.claude.json does not parse", gives the error,
  and names the newest file in `~/.claude/backups/` whose name starts `.claude.json`,
  that is not a `.corrupted` copy, and that parses, with the advice to copy it over
  `~/.claude.json` or wait for Claude Code to rewrite it. The failed call returns the
  same text. When the file parses again by then, the call fails without a pause.
- A queued spawn whose start failed this way stays queued.

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
- `run_start` returns `other_swarms`: the repo, run id, and server pid of every other
  lock whose server is alive. Admission reads the same machine; see "Memory" above.
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
