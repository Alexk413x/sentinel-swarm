# Sessions

## Starting a child

`agent_spawn(caller, child_name)` refuses unless the caller is the parent named in the
child's unacknowledged brief. It also refuses a Manager before a `repo_check`, a child
whose phase is paused, a child that is already registered, a session name
a running session holds. A start past a parallelism cap, or one that would take free
memory below `memory.headroom_mb`, is queued, not refused; see "Parallelism" and
"Memory" below. It then runs, in the repo root:

```
claude "You are <name>. Read your brief from the swarm ledger and follow it." --bg \
  --name <session name> --agent swarm-<role> --model <brief model> \
  [--permission-mode <mode>] --strict-mcp-config --mcp-config <config> \
  --allowedTools <file tools> --settings '{"worktree":{"bgIsolation":"none"}}'
```

- `--permission-mode` comes from the role's `permission_mode` setting, never from the
  role file. A role with no setting starts in the user's own
  `permissions.defaultMode`: the launch passes no flag, and Claude Code reads the
  local project, project, and user settings. See "Permission mode"
  in [11-setup-and-settings.md](11-setup-and-settings.md). The same rule applies to
  `agent_resume`, the watchdog's Oracle resume, and the launcher.
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
- The claude binary is `SENTINEL_SWARM_CLAUDE` when set, else `claude`. On Windows, a
  `.cmd` or `.bat` launcher (the npm shim) runs through cmd.exe, where agent-written text
  could expand `%VAR%` or run a command. The ledger runs the shim's own
  `node_modules/@anthropic-ai/claude-code/bin/claude.exe` when it sits beside the shim;
  otherwise it rewrites the spawn prompt and the resume message, the only agent-written
  arguments, so the message still arrives: a percent sign becomes "percent", a double
  quote a single quote, `&` "and", `|` "/", `<` and `>` brackets, `!` a full stop, and
  `^` and line breaks go. The inline JSON of `--mcp-config` and `--settings` goes to a
  file under `.sentinel-swarm/launch/`, named by its digest, and the flag names the
  file. Any argument that still holds one of those characters refuses the launch and
  says to set `SENTINEL_SWARM_CLAUDE` to `claude.exe`.
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
- A wake-up is owed only to a live agent with a session name. The one wake-up an
  agent owes itself is a test result; see "Test results" below.
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
one. The recipient's mod takes a wake-up that stays unsent for 60 seconds (see
"Recipient-side delivery" below). The ledger pushes nothing into a session.

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
  minus the role's estimate is below `memory.headroom_mb`, the target stays parked,
  "waiting for memory to resume it for a wake-up", and the watchdog resumes it later.
  Whether other sessions of the run still run does not matter: waiting is the rule
  for every resume, as for a queued spawn. A running entry under the target's
  session name with another id re-keys the agent, and the wake-up goes to the new id.
  When the listing fails, nothing is resumed and every target is answered as before.
- The Oracle is never resumed this way: an interactive Oracle is the user's terminal,
  and a background Oracle follows the watchdog's Oracle resume (see "How a report
  reaches the Oracle" in [08-watchdog.md](08-watchdog.md)).
- The mod sends each entry with `$.session.send({ to: { sessionId }, text })`, all
  targets in parallel. Every role is addressed by session id, the interactive Oracle
  included, never by name.
- A send that gives no answer within 15 seconds is not counted as sent and is not sent
  again while it is still in flight: a send runs through every plugin's `SendMessage`
  hooks, so a slow hook can hold it and it still lands. When it lands, the mod posts
  `wake_sent` for it then.
- A send that answers "no live session" is tried again after 2, 4, and 8 more seconds:
  a session registers 10 to 14 s after it starts. Any other refusal ends the attempt.
- The mod posts `wake_sent` with the ids whose send reported `isDelivered`. The ledger
  marks only the caller's own rows sent and releases a closed Driver, as `post_any`
  does. A wake-up whose send failed stays owed. `isDelivered` means the message is
  queued for the target, not read, as for a `SendMessage` call.
- The sender's mod sends a wake-up after its own ledger tool calls and at `Stop`, and
  does not retry a failed or missed send while the sender sits idle. A wake-up can stay
  unsent for that reason; the next section covers it.
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
  the mod's delivery path. **(future feature)**
- The Coder's Stop hook also blocks once, per Coder, when the Coder stops while working
  with no handoff, and names `handoff_submit`, a `message_post` to its Lead, or
  `wait_for`. It does not block a Coder with an open file wait or a test run still
  `running`.

### Test results

- When a test runner records a run (`done`, `stale`, or `abandoned`), it owes the
  agent that called `tests_run` a wake-up from that agent itself, reason `test_run`,
  whose pointer reads "Test run <id> (<scope> <target>) is recorded, <outcome>. Read it
  with test_run_get(test_run_id=<id>)." See "A test run outlives the tool call" in
  [06-ledger-server.md](06-ledger-server.md).
- `tests_run` marks the wake-up sent when it returns the result itself, within its
  20-second wait.
- `owed`, the member Stop hook's owed calls, and the watchdog's `wake_unsent` leave a
  self-owed wake-up out: no `SendMessage` to oneself is ever named.
- The Stop hook delivers one: when a self-owed wake-up is unsent and the stop does not
  follow the hook's own block (`stop_hook_active`), it marks it sent and blocks with
  its pointer, so the agent reads the result in the same turn. This runs for every role,
  before the member and Oracle checks.
- The mod delivers one to an idle session. While the agent has a test run in flight,
  and once after each of its ledger tool calls, the mod posts `self_owed` every 15
  seconds. The ledger marks the self-owed wake-ups sent and answers their pointers as
  one text, with the time signal, and the count of the agent's runs still `running`.
  The mod submits the text with `$.prompt.submit`, which starts a turn once the session
  is idle and never interrupts one. It stops polling when the answer shows no run in
  flight and no `poll: true`; see "Recipient-side delivery".
- A session the daemon retired is parked like any other, and `resume_parked` resumes
  it with the pointer of the wake-up owed to it.

### Recipient-side delivery

- The `self_owed` poll also takes any unsent wake-up addressed to the polling session,
  whoever owes it, once it has stayed unsent for 60 seconds. It marks the wake-up sent in
  the same transaction and answers its pointer with the others. The sender's mod
  normally delivers first, so a duplicate is rare. A Driver's final wake-up taken this
  way releases the closed Driver, as the sender's `wake_sent` does.
- The answer carries `poll: true` while the session is a live agent of an active or
  paused run. The mod keeps polling every 15 seconds while a test run is in flight or
  `poll` is true, so an idle session that never ran a test still takes a wake-up that
  arrives later. It stops when the answer carries neither, and a ledger tool call
  starts it again. The timer starts at session start.
- A resumed run owes its Oracle a wake-up from itself, reason `run_resumed`: `user_resume`
  (the `resume` command) and the watchdog's clear-check resume record it. Its pointer
  reads "The run resumed. Arm the watchdog Monitor again with <the Monitor call>, then
  read directive_inbox and act on what it holds." The directive the resume files reaches
  the Oracle only through its Monitor listener, which the pause ended, so this wake-up
  is what starts the Oracle again.

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
  when the run stalls. The text of a `SendMessage` is not checked. The hook reads the
  caller's row, and so its parent, from the ledger on every call; nothing about the
  relation is cached at spawn, so a re-pointed child reaches its new parent at once.
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

- It blocks while the Oracle owes the user a `PushNotification` call for a recorded
  notification, and names each call. A type switched off, or limited to `os`, owes none. This debt blocks even while the run is paused, while
  children work, while a directive waits on the user, and after `run_finish`, because
  the call ends at once. See "Notifications" in
  [02-run-lifecycle.md](02-run-lifecycle.md).
- Otherwise it never blocks while the run is paused.
- It does not block for pending work while any Manager, Lead, or Coder is working:
  waiting on running children is a valid stop.
- A child that is still `registered`, was spawned no more than 2 minutes ago
  (`REGISTER_GRACE`), and whose session is running counts as working, because it has
  not yet acknowledged its brief. The Oracle then stops instead of waking a child that
  is starting. A child that never starts is reported by the watchdog.
- It does not block for pending work while a directive waits on the user, with outcome
  `needs_user`, or while a test run of the Oracle's is still `running`: its wake-up
  brings the Oracle back.
- Otherwise it blocks while the run has unlocked phases, submitted handoffs, live file
  claims, handed-up phases without an accepted review, or live agents none of which is
  working. Phases under a scoped pause do not count. The reason names
  each idle agent to wake and the call to wake it, and suggests `run_pause` when only
  the user can unblock the run.
- It also blocks while the run is active and the watchdog listener is not armed, even
  while children work. See "Watchdog" in [08-watchdog.md](08-watchdog.md).
- It does not block a stop that follows its own block (`stop_hook_active`).

## Liveness, resume, and release

- A session is running when its status and state in `claude agents --json` are not
  `stopped`, `exited`, `crashed`, `failed`, `killed`, `dead`, or `completed`, and, when
  the entry carries a `pid`, that pid names a process that exists and was created no
  later than a minute after the entry's `startedAt` (a later process holds a reused
  pid). An entry with a pid is judged by its process alone: a killed session's entry can
  keep its last `status`. An entry with no pid is running when its `status` is `busy`,
  `idle`, or `waiting`. State `done` counts as running. An entry with neither, such as
  one a reboot left with only `state: "blocked"`, is not running, so `run_start` and
  `agent_resume` do not refuse on it. The ledger reads a pid's start time at most once
  every 5 seconds, since on macOS and Linux each read runs `ps`. On Windows on
  2026-11-14, every live session's process started 2 to 35 seconds before its
  `startedAt`.
- For an agent the ledger also judges by the process it recorded: when the entry has no
  pid and the agent row holds a `session_pid`, the session runs only while that process
  exists and its creation time matches `session_pid_created`. `agent_resume`, the
  watchdog's parking and `crashed` check, and the wake-up resume use this rule
  (`sessions.agent_running`). A resume clears the recorded pid, and the next memory
  sample records the new process's.
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
- `agent_resume` follows the memory rule of every resume. When free memory minus the
  role's estimate is below `memory.headroom_mb`, it starts nothing and returns
  `deferred: true`, `reachable: false`, the reason, and a `next` that says the ledger
  retries. It parks the target ("waiting for memory to resume it for a wake-up") and
  records a wake-up the target owes itself, reason `resume_deferred`, whose pointer
  is "Re-read your brief and your inbox in the ledger." The watchdog's resume of parked
  agents then resumes it once memory frees, and the caller's Stop hook has nothing to
  name for it.
- A resume starts from the newest session id of the agent whose transcript file exists,
  never from a session id that has none. A resumed session that dies before it writes
  a transcript still gets the agent re-keyed to its id, because the hooks key by session
  id, so `claude --resume <that id>` fails with "No conversation found". The agent
  keeps `resume_from_id`, the last id with a transcript, and every resume uses it. An
  agent with no recorded `transcript_path` resumes from its own id. When no id with a
  transcript is left, the ledger starts nothing and says the agent has no conversation
  to resume. The watchdog then reports `crashed` with the step "release it and brief a
  replacement from its brief"; see [08-watchdog.md](08-watchdog.md).
- Release sets the row `released` and stops the session with `claude stop <bg_id>`,
  which frees its memory. It also stops the background entry of each earlier session id of
  a re-keyed agent that `claude agents --json` still lists (`agents.prior_session_ids`; see
  "Re-keying a resumed session"). The Oracle's session is never stopped this way.
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
- A process counts as a child only when both start times are known and it started after
  its parent, because a parent pid can name a dead process whose pid was reused. A
  process is terminated only while its start time is known and still matches the one
  recorded, and never a system or service process (`svchost`, `services`, `lsass`,
  `systemd`, `launchd`, and the like). Windows cannot read a service process's start
  time, so an unknown start time never counts as a match.
- The release records the session's process and every process it found in
  `released_processes`, and logs how many it stopped in the release's agent event. The
  watchdog reports any of them still running, and any process started under them, as
  `stray_process`, but only after it verifies each process by its pid and exact start
  time and by its parent chain back to a recorded process; see
  [08-watchdog.md](08-watchdog.md).
- `agent_release(target_agent_id)` releases a child of the caller. It takes the agent's
  id, or the name or session name of a live agent of the caller's run, so a parent can
  release a parked or dead child it knows only by name. The Oracle may also
  release an agent whose parent has ended, such as a Lead left live under a released
  Manager. A child name with no agent and no queued spawn voids the caller's
  unacknowledged brief for it. The other releases are automatic. Every release hands the released agent's
  `open` and `accepted` change
  requests to its nearest live ancestor; see "Change requests" in
  [04-agreements.md](04-agreements.md).
- A Driver whose exploration is closed is released once it owes no unsent wake-up: after
  its `SendMessage` to the Oracle, after its own `agent_resume` of the Oracle, or at its
  Stop hook when it owes nothing. `drive_request` and `run_finish` release one that is
  still live.

### A replacement takes over its predecessor's children

A parent can end while its children stay live: the daemon retires its session, or its
parent releases it. Its replacement takes over the children.

- `brief_ack` moves every live child and every brief of each ended agent of the run with
  the same name to the new agent, in the bind's write transaction. A Manager's and a
  Lead's names are fixed by their phase and module, so a replacement in the same scope
  carries the same name.
- `brief_create` refuses a name that a live agent holds. Before it refuses, it releases a
  holder of the caller's run whose session is not running (judged as in "Liveness,
  resume, and release"), when the caller is the holder's parent, or the Oracle and the
  holder's parent has ended. The release's agent event says "its session is not running,
  and a replacement is briefed under the name". It leaves a holder whose resume is in
  flight, and a holder whose session runs. When `claude agents --json` fails, it
  releases nothing and refuses.
- `brief_create(replaces=<agent_id>)` names an ended agent of the child's role whose
  children the child takes over under another name. It refuses an agent that is still
  live, one of another role, or one outside the run.
- Each moved child gets an agent event that names the old and new parent. An ended
  child keeps its old parent.
- The Oracle's own resume in `run_start` moves its children the same way. It also stops
  the replaced Oracle's background entry, and the entries of its earlier session ids, when
  the listing still holds them, whether or not they read as running. A listing entry
  without a background id, an interactive session, is left alone.

### Re-keying a resumed session

Every part of the ledger keys an agent by its current session id: the hooks stamp it as
`agent_id`, and liveness, the mod's sends, `mod_sessions`, and the transcript all use
it. A resumed background session can come back under a new session id (see
[13-platform-facts.md](13-platform-facts.md)), so the ledger re-keys the agent rather
than keep an alias.

- One claim serializes every resumer of an agent: the watchdog's resume of parked agents,
  the ledger's wake-up resume, another agent's `agent_resume`, and the watchdog's Oracle
  resume. The claim is `agents.resume_started_at`, set by one conditional write
  (`rekey.claim_resume`) that succeeds only when it is empty or older than 120 seconds.
  A resumer that loses the claim starts nothing: "already being resumed". A resumer that
  wins it reads the agent and `claude agents --json` again before it starts anything, because
  the checks it made before the claim can be stale: when the session runs, or runs
  under another session id, it starts nothing and re-keys or refuses. It releases the claim
  when it starts nothing or the start fails. A resumer that finds the agent re-keyed or
  ended between its candidate query and its claim skips it.
- Before the resume, the ledger sets `agents.resume_started_at`. After it, it records
  the background id the CLI printed as `bg_id`, then polls `claude agents --json` for
  up to 15 seconds for the entry whose `id` is that background id, or else a running
  entry with the session name.
- The resume's agent event records the exit code and the stderr of `claude --resume
  --bg`. A failed command logs a `resume_failed` event with the CLI's own message, which
  holds its exit code and stderr. `agents.last_resume_at` and `last_resume_detail`
  record the time, the exit code and stderr, and whether and when the session
  registered. The watchdog compares them with the listing on each pass: a session gone
  less than 3 minutes after the resume logs a `resume_vanished` event, "its resumed
  session was gone <n>s after the resume at <time>: <detail>", and a session that
  stays up 3 minutes logs `resume_stable`. When `daemon.log` holds a `bg retire` line for the
  resumed session, the `resume_vanished` reason ends with "; the daemon retired its session:
  <reason>".
- When the entry's session id differs, one write transaction replaces the old id with
  the new one in every column that stores an agent id: `rekey.agent_id_columns` reads
  the schema for columns named `*agent_id`, columns that reference `agents`, and
  `mod_sessions.session_id`. It also sets `bg_id` to the new id's first 8 characters,
  points `transcript_path` at `<new id>.jsonl` in the same folder, clears
  `resume_started_at`, and logs a `rekeyed` agent event. The same id needs no re-key.
- A re-key appends the old session id to `agents.prior_session_ids`, a JSON list of every
  session id the agent held before, oldest first. Right after the re-key, the ledger stops
  the background entry of each earlier id that `claude agents --json` still lists, running
  or not, with `claude stop`. This runs after `agent_resume`, the wake-up resume, the
  watchdog's Oracle resume, and the hook path that matches a resumed session. `claude rm`
  is never used: a stopped session stays visible in agent view.
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
- The watchdog's Oracle resume re-keys the Oracle the same way, and claims it first: it
  sets `resume_started_at` under the same 120-second window before `claude --resume`, skips
  the wake when a resume is already in flight, and clears the claim when the resume fails.

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
  stops at the first one that does not fit under the headroom, and says why in that agent's
  park reason. A candidate whose resume another actor holds or already finished, or that
  has no conversation left, is skipped.
- A wake-up owed to an agent whose park is claimed (`parked_at` set) while its session still
  runs is not sent: the session is about to be stopped, so the wake-up stays owed and the
  resume of the parked agent carries it.
- A resume fits when free memory, less the memory reserved for sessions still starting,
  minus the role's estimate stays at or above `memory.headroom_mb`. Unknown free memory
  fits. No other condition lets a resume through, and a run whose every session is
  parked waits for memory like any other: the `stalled` finding reports it. The check runs
  in `resume_agent`, under the resume claim, so the watchdog's resume, the wake-up
  resume, and `agent_resume` all follow it. The watchdog's Oracle resume is exempt: it
  wakes the Oracle whatever the free memory, so a report always reaches it.
- `crashed` still reports a `working` or late `registered` agent whose session is gone,
  and the Oracle resumes it with `agent_resume`. A failed resume is logged as a
  `resume_failed` agent event, and the agent stays parked.
- A parked agent is judged not running by its process, not by a stale listing entry, so
  a dead session is parked and resumed like a retired one. The watchdog never releases a
  parked agent that holds unfinished work: after 3 failed resumes (`resume_failed` or
  `resume_vanished` events since its last `resume_stable`) and a quiet heartbeat, or at
  once when no conversation is left to resume, `crashed` tells its parent to release it
  and brief a replacement. See [08-watchdog.md](08-watchdog.md).

### Orphaned sessions

Each watchdog pass of an active run looks for listed background entries that no live agent
owns, as the last memory step. An entry is orphaned when its session id is the id of a
released or ended agent of the run, or sits in some agent's `prior_session_ids` and is not
any live agent's current session id or background id. A stopped entry, an interactive
session, and an entry of another run never count.

- An entry the watchdog sees orphaned on two passes in a row is stopped with `claude stop`,
  whether or not it reads as running. It logs an `orphan_stopped` agent event on the owner
  and stops nothing else of that agent.
- An entry seen on one pass only starts the count again. A stop that fails keeps the entry
  for the next pass.
- It covers a release or a re-key whose own stop failed, and a session left from before a
  reboot. It never uses `claude rm`.

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
| `file_accept_incomplete` | The file's Coder |
| Two watchdog passes in a row that list a background entry of a released agent, or an earlier session id of a live one | That entry's session |
| `run_close` | Every agent still live. Called by the Oracle, the server then stops the Oracle's background session and exits as after `run_finish`; from `ledger.py close`, it stops a background Oracle session at once, and the server exits after `idle_exit_minutes` |
| About 3 seconds after `run_finish` | The server waits up to 5 minutes for the Oracle's last turn to end, stops the Oracle's background session, and exits. An interactive Oracle is the user's terminal and keeps running |
| `idle_exit_minutes` with no active run, or a paused run, no session of the run running, and no ledger tool call | The ledger server |
| A run that is not `active`, or a newer listener | The watchdog listener |

## Parallelism

- `agent_spawn` queues the spawn at `parallelism_cap`, counting the run's live agents, the
  Oracle included, plus other swarms' live sessions on the machine: entries from
  `claude agents --json` that are running, whose name matches the swarm session-name
  shape (`<slug>-r<run_id>-<name>`, from `looks_like_swarm_session`), and that are not
  one of this run's own session names. It returns the queued answer of "Memory" below,
  with the cap as the reason. Each watchdog pass starts the queued spawn once a release
  frees a slot, so the parent does not call `agent_spawn` again.
- Left empty, `parallelism_cap` is set from total memory: half of it, at 600 MB a
  session, and at least 4 (27 on a 32 GB machine). Unknown total memory leaves no cap,
  and `0` turns the cap off. Setup prints the value and says that it counts other
  swarms' sessions.
- `role_parallelism_cap.<role>` caps that role's own live agents in the run, on top of
  `parallelism_cap`. A spawn past it is queued the same way.
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
  that names the reason and says not to call `agent_spawn` for it again. A spawn past a
  parallelism cap is queued the same way, with the cap as its reason. A second `agent_spawn` of a queued
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
  they fit. It skips one whose phase is paused, whose fix a Driver pattern stop pauses,
  or that a parallelism cap still holds back, and drops one whose brief was acknowledged, whose parent ended, whose start failed, or
  that is a Coder whose file has no live claim for it, with a ledger message to the
  parent that says why. The ledger tells the two apart by the refusal's type
  (`SpawnDeferred`), not by its text, which names the child. A pass rewrites the reason of
  a spawn it leaves queued to what holds it back then, a cap or the memory shortfall. A
  start that fails on an unreadable `~/.claude.json` stays
  queued; see "An unreadable ~/.claude.json" below. A parent cancels a queued spawn with
  `agent_release(<child name>)`, which keeps the brief and the claim; `release_file` on
  the queued Coder's path drops its spawn too. A second `agent_release(<child name>)`, with no
  queued spawn left, voids the unacknowledged brief and keeps the claim. While a spawn is queued, the
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
- **Park blocked Coders.** While a spawn waits in the queue, and free memory is below
  the shortfall above, each watchdog pass stops Coders that sit idle on a blocker, before
  it parks idle parents, and the parents then cover only what is left of the shortfall.
  `memory.park_blocked_minutes` (10; `0` turns it off) sets how long a Coder is quiet.
  The ledger stops one only when all of these hold: its session runs and has ended its
  turn (a `working` Coder's session reports `waiting` or `blocked`; an `idle` Coder's
  session is not `busy`); its last heartbeat, or its start, is older than the setting;
  its session has no pending tool request in its transcript and no held permission
  request; it owes and is owed no unsent wake-up, has no unread message, and has no
  test run in state `running`, checked in the same transaction that sets `parked_at`.
  It takes the most idle first and stops only as many as the shortfall needs, by the
  role's estimate. The park sets `state_before_park` to `idle`, so `crashed` does not
  report it, and its reason reads "the ledger stopped it: idle on a blocker with no
  activity for over <n> minutes while a spawn waits for memory". A failed stop clears
  the flag and restores the state. Its Lead's answer (`message_post`, `return_work`, or
  a `SendMessage` to it) owes it a wake-up, and the resume path above brings it back.

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
  `~/.claude.json` or wait for Claude Code to rewrite it. The pause names the
  `config_parses` clear-check and notifies the user like any `run_pause`, so the
  watchdog resumes the run by itself once the file parses; see "Clear-checks" in
  [02-run-lifecycle.md](02-run-lifecycle.md). The failed call returns the same text and
  says the run resumes by itself. When the file parses again by then, the call fails
  without a pause.
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
