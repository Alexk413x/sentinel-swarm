# Watchdog

The watchdog is a background job, not a role. It detects and reports. It never replaces a
Manager, Lead, or Coder. Its only resumes and stops serve memory: it resumes a parked
agent that is owed a wake-up once memory allows, starts queued spawns, and stops an
idle parent under memory pressure; see "Memory" in [05-sessions.md](05-sessions.md). It
resumes the Oracle when the Oracle's session is not running, so that a report can reach
the Oracle at all, and it resumes a paused run whose clear-check passes. What only the
user can answer, it reports to the user as well, with an OS notification. For any
other agent's failure, it reports to the Oracle, and the
Oracle acts.

## Detection

A thread in the ledger server (`watchdog.py`) runs every `interval_seconds`. It reads
`claude agents --json` and scans the active run. It does not scan a paused run, and a
failed listing skips one pass. On a paused run it only tries the pause's clear-check
and sends reminders; see "Paused runs" and "Reminders" below.

Each pass first sweeps the test runs, paused run or not: a `running` row whose runner
process is gone becomes `abandoned`, and its caller is owed the wake-up. See "A test run
outlives the tool call" in [06-ledger-server.md](06-ledger-server.md).

Before the scan, each pass of an active run runs the memory steps in this order, each
on its own so a failure in one is logged and the next still runs: sample each session's
memory, park agents whose session is gone, resume parked agents that are owed a
wake-up, start queued spawns, and park idle parents. When a step changed anything, the
pass reads the listing again for the scan. See "Parked agents" and "Memory" in
[05-sessions.md](05-sessions.md).

| Kind | Condition | Next step the report names |
|---|---|---|
| `needs_user` | Any live agent's session, the Oracle's included, shows a `waitingFor` in `claude agents --json` that names a permission, input, a question, or an approval (`"permission prompt"`, `"input needed"`). Reported at once, with no second pass. The detail names the session, what it waits on, and the pending request from the end of its transcript: the tool and its file path, command, path, or URL, or for `AskUserQuestion` each question and its options, cut to 300 characters. A waiting Manager, Lead, Coder, or Driver is not also reported as crashed, stuck, or overdue. Notifies the user | Tell the user to open that session in agent view and answer it, and keep the rest of the run moving |
| `crashed` | A Manager, Lead, Coder, or Driver is `working`, or `registered` for more than 2 minutes, and its session is missing or not running. An `idle` or `handed_up` agent whose session exited is normal. A Driver whose exploration is closed but that the ledger has not released yet is reported whatever its state, since a closed Driver stays live only until its last wake-up goes out. | `agent_resume(target_name=...)`. For a Driver whose exploration is closed: read its result in the ledger, then `agent_release` it |
| `stuck` | The session runs, the agent is `working`, and its last heartbeat is older than `stuck_minutes`. Does not apply to a Driver: see `driver_overdue` below | Message it, or have its parent replace it |
| `driver_overdue` | The Driver's session runs, it is `working`, and its open exploration's last check-in (or its start, with none yet) is older than 30 minutes plus a 5-minute grace | Message it, or have the Oracle stop it and start a fix for whatever blocked it. |
| `driver_unsent` | The Driver's exploration is closed, the ledger has not released it, its session runs but sits idle, and it has owed a live agent an unsent wake-up for more than 2 minutes. This is the Driver ignoring its Stop hook block twice: `stop_hook_active` lets the second stop through idle. Not also reported as `crashed`, which covers a dead session instead. | Read its result in the ledger, then `agent_release` it. |
| `wait_landed` | A live Coder or Lead has an open `wait_for` row on a path whose latest claim is `approved` or `incomplete`. `approve` closes the waits in the same transaction, so this reports a race or a lost close | `agent_resume(target_name=...)`, or the `SendMessage` to its session when it runs |
| `wake_unsent` | A wake-up a live agent owes a live Manager, Lead, Coder, or Driver has been unsent for `unsent_wakeup_minutes`. One finding per target, from its oldest such wake-up. Not reported for a Driver's debt, which `driver_unsent` covers, or for a test result an agent owes itself | The wake call (`SendMessage` or `agent_resume`) for the target. For a parked target: the ledger resumes it once memory allows, or wake it now with that call |
| `spawn_queued` | A spawn `agent_spawn` queued for memory has waited `queued_spawn_minutes`. Reported on the parent, one finding per queued spawn | Free memory on the machine, lower `memory.headroom_mb`, or `run_pause` and tell the user the run waits on memory |
| `phase_unmanaged` | A phase is `unlocked` or `working`, not paused, has had a Manager, and has no live one: its Manager released itself or was released before the phase handed up. Reported on the Oracle, one finding for every such phase | Brief and spawn a new Manager for the phase, settle the file that blocks it with `file_accept_incomplete`, or `run_close` a run that cannot finish |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed. Does not apply to a Driver, which runs no `tests_run` | Ask its parent to `return_work` or `issue_escalate` |
| `context_high` | The latest request of the agent, the Oracle included, fills `context_pct` of its window | Have its parent replace it. For the Oracle: `run_pause`, then resume in a fresh session |
| `stalled` | No session of the run runs; or sessions run, but no member session is busy, no member is `working`, and nothing changed for 2 minutes. Not reported while a directive waits on the user, or while a spawn is queued for memory, which `spawn_queued` reports | Wake the agent whose work is pending: the reviewer of a submitted handoff, an agent with unread messages, or the Lead of a file with an open issue. With no such agent: resume or spawn the owner of the pending work, or, with nothing pending, continue the plan or call `run_finish` |
| `run_stalled` | Run-level, filed on the Oracle's row. No live agent is `working` with a running session that is not waiting on the user, no live agent owes another live agent an unsent wake-up, and the newest of the run's start, its latest agent event, message, and test run, and every live agent's transcript write is older than `stuck_minutes`. Reported while a directive waits on the user too, unlike `stalled`. Not reported while a spawn is queued for memory. The detail lists the sessions that wait on the user. Notifies the user | Read `status_tree()`, then wake the agent whose work is pending, or ask the user with `user_ask` when only the user can move the run |

- The context size is the input, cache read, and cache creation tokens of the last
  assistant response in the agent's transcript. The window is 200,000 tokens when the
  model names Haiku, else 1,000,000, unless the `context_window` setting names one.
- For the idle stall, "nothing changed" reads the latest agent event, message, and test
  run, and the last write of the Oracle's transcript. The Oracle's own session status
  does not count, because its armed Monitor keeps it `busy`.
- A stall is reported only when two passes in a row see it, since a run passes through
  "no session running" while one session exits and another resumes.
- A handoff that a newer handoff of the same file superseded is not pending work. The
  stall check, and the Oracle's Stop hook, count only `submitted` handoffs.
- A scoped pause (`run_pause(reason, phases=[...])`) takes its phase's unlocked state
  and submitted handoffs out of the "pending" count the stall check reads, so a run with
  only paused phases left to do reports no stall over them.

## Reporting

- `watchdog_findings` keeps one live row per run, agent, and kind. A finding the next
  pass does not see is cleared. One that comes back gets a new row and a new report.
- Each new finding becomes a directive with source and sender `watchdog`. Its body names
  the agent, its session, the kind, the detail, and the next step. The Oracle resolves
  each one; open directives block `run_finish`.
- A new `crashed` or `driver_overdue` report on a Driver session also
  records a user notification, keyed by its directive id: "Driver crashed" at level
  error, or "Driver overdue" at level warning. The server shows it as an OS
  notification when `notify` includes `os`, and the Oracle owes a `PushNotification`
  for it when `notify` includes `push`. See "Driver notifications" in
  [02-run-lifecycle.md](02-run-lifecycle.md). `driver_unsent` records no
  such notification: it is a delivery miss the Oracle can fix from the directive alone,
  not a Driver error.
- A new `needs_user` or `run_stalled` report records a user notification at level
  warning, keyed by its directive id: "The swarm needs you: <agent>, <detail>" or "The
  swarm run stalled: <detail>". It is shown and owed like a Driver notification, except
  that a `needs_user` report on the Oracle's own session owes no `PushNotification`,
  since that session is the one waiting.
- A Driver stop rule is a separate mechanism, not the watchdog: it files a directive
  with source `driver`, computed by `compute_loop_status` in `drive.py` from every
  exploration and finding of the run, not from `claude agents --json`. See
  "Explorations" in [02-run-lifecycle.md](02-run-lifecycle.md).

## How a report reaches the Oracle

- The `Monitor` listener is the Oracle's channel for events no role session sends: the
  watchdog's findings, and the `PushNotification` calls the Oracle owes the user. Every
  event a role session causes wakes the Oracle through that session's own wake-up
  instead. The Driver's results are such events: `drive_issue`, `drive_checkin`,
  `drive_done`, and the Driver's own `drive_unavailable` each owe the Oracle a wake-up.
  See "Wake-ups owed" in [05-sessions.md](05-sessions.md).
- The listener is `mcp/launch/ledger.py watch`, run as `hook.py watch`. Every 2
  seconds it prints one line per unnotified watchdog directive, marks it notified, and
  writes `runs.watch_heartbeat_at`. It also prints, once, each `PushNotification` call
  the Oracle owes the user. It exits when the run is not active. The newest
  listener owns the run, and an older one exits.
- Each line the listener prints ends with the run's elapsed time, `elapsed <n>s` or
  `elapsed <n>s / <budget>s`, since every line goes to the Oracle. So does the resume
  message below. See "Time signal" in [05-sessions.md](05-sessions.md).
- The Oracle arms it right after `run_start`, and again whenever it expires, with
  exactly this call:
  `Monitor(command="python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch", description="sentinel-swarm watchdog", timeout_ms=1800000)`.
  Each printed line wakes the Oracle.
- The Oracle's Stop hook blocks while the run is active and the listener's heartbeat is
  missing or older than 60 seconds, or the armed Monitor expires within 30 seconds.
- `directive_inbox` marks the directives it returns as notified.
- When a watchdog directive stays unnotified for one interval and the Oracle's session
  runs but is not `busy` (its armed Monitor keeps it `busy`), the listener is down or the
  Oracle waits on the user, and a resume would start a second copy. The watchdog then
  notifies the user once per oldest such directive (`oracle_unreachable:<directive_id>`,
  level warning, OS notification only): "The swarm's Oracle session <name> waits on
  <what>, and N watchdog finding(s) wait for it. Open it in agent view." When it does
  not wait on the user, "waits on <what>" reads "sits idle with no watchdog listener".
- When a watchdog directive stays unnotified for one interval and the Oracle's session
  is not running, the server runs
  `claude --resume <Oracle session> --bg "The watchdog reported N finding(s). Read directive_inbox."`
  with the Oracle's session name and launch options,
  at most once every 5 minutes. After 3 attempts with no Oracle heartbeat since the
  first, it pauses the run with the reason "the watchdog could not wake the Oracle".
- When the Oracle's `agents` row has no `session_name`, the watchdog skips `claude
  --resume`, because a resume without `--name` comes back as a session named after the
  message. It records the agent event "the watchdog cannot wake the Oracle: no session
  is recorded for '<agent_id>'". That event counts as a wake attempt, so the run pauses
  after the usual 3 attempts.

## Paused runs

- A pause the watchdog makes itself ("the watchdog could not wake the Oracle") goes
  through the same `pauses.record_pause` as `run_pause`, so it notifies the user too.
- On each pass of a paused run whose pause names a clear-check, the watchdog runs the
  check and resumes the run when it passes. See "Pause and resume" in
  [02-run-lifecycle.md](02-run-lifecycle.md). A check that raises is logged and counts
  as not passed.
- A paused run with a clear-check keeps the ledger server from its idle exit, so the
  check keeps running.

## Reminders

Every `remind_minutes`, while the run is paused or an open directive waits on the user
(`needs_user`), the watchdog shows the OS notification again: the pause message, and
"<n> question(s) wait on you: <first question>". The first reminder comes one interval
after the pause or after the watchdog first sees the question, since `run_pause`,
`user_ask`, and `directive_resolve(needs_user)` notify at once. `runs.reminded_at`
records the last one, and clears when nothing waits. Reminders show only the OS
notification and owe no `PushNotification`. With `notify` lacking `os`, or
`remind_minutes: 0`, there are none.

## Settings

Under `watchdog` in `.claude/sentinel-swarm.local.md`: `interval_seconds` (30),
`stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `context_window` (unset:
taken from the model), `idle_exit_minutes` (15), `unsent_wakeup_minutes` (5; 0 turns
`wake_unsent` off), `queued_spawn_minutes` (10; 0 turns `spawn_queued` off),
`remind_minutes` (30; 0 shows each notification once). `stuck_minutes` also sets
`run_stalled`'s window. The memory steps read `memory.headroom_mb` and
`memory.park_idle_parents`; see [11-setup-and-settings.md](11-setup-and-settings.md).
