# Watchdog

The watchdog is a background job, not a role. It detects and reports. It never replaces a
Manager, Lead, or Coder. Its only resumes and stops serve memory: it resumes a parked
agent that is owed a wake-up once memory allows, starts queued spawns, stops a Coder idle on
a blocker while a spawn waits, stops an idle parent under memory pressure, and releases a
parked parent whose work is done; see
"Memory" and "Parked agents" in [05-sessions.md](05-sessions.md). It
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
memory and record each running session's pid, park agents whose session is gone,
resume parked agents that are owed a wake-up, start queued spawns, park blocked Coders,
park idle parents, and release finished parked parents. When a step changed anything, the pass reads the
listing again for the scan. See "Parked agents" and "Memory" in
[05-sessions.md](05-sessions.md).

| Kind | Condition | Next step the report names |
|---|---|---|
| `needs_user` | Any live agent's session, the Oracle's included, shows `status` or `state` `waiting` or `blocked` in `claude agents --json`, or a `waitingFor` that names a permission, input, a question, or an approval (`"permission prompt"`, `"input needed"`), **and** a person can act on it: its transcript holds a tool call with no result (a permission prompt or an `AskUserQuestion`; the watchdog reads only `assistant` and `user` records and matches results to calls by id, so a replayed `api-request-blob` never makes an answered call pending), or its mod holds a call for the run panel. Claude Code also shows `waiting` or `blocked` for a session that ended its turn on a work blocker, and for an idle Oracle, so a status alone is never a user wait: such a session is a work blocker (`blocked` below), or for the Oracle no finding. A `blocked` session with no `waitingFor` is reported as waiting on "the user (blocked)". Reported at once, with no second pass. The detail names the session, what it waits on, and the pending request from the end of its transcript: the tool and its file path, command, path, or URL, or for `AskUserQuestion` each question and its options, cut to 300 characters. A session whose mod holds a call for the run panel (an open `permission_requests` row that has not expired) waits on the user too, though `claude agents --json` shows it busy: the watchdog reads it as `waiting` on "a permission request held for the run panel", and its detail names the tool and input summary and says to answer the request in the run panel. See "Answering a permission prompt in the panel" in [17-run-panel.md](17-run-panel.md). A Manager, Lead, Coder, or Driver that waits on the user, a held one included, is not also reported as crashed, stuck, or overdue. Neither a user wait nor a work blocker counts as working for `run_stalled`. Notifies the user | Tell the user to open that session in agent view and answer it, or, for a held request, to allow or deny it in the run panel before the hold ends, and keep the rest of the run moving |
| `crashed` | A Manager, Lead, Coder, or Driver is `working`, or `registered` for more than 2 minutes, and its session is missing or not running. For a `parked` agent, the state it had before it was parked counts. An `idle` or `handed_up` agent whose session exited is normal. A Driver whose exploration is closed but that the ledger has not released yet is reported whatever its state, since a closed Driver stays live only until its last wake-up goes out. | `agent_resume(target_name=...)`. The detail counts the failed resumes (`resume_failed` and `resume_vanished` events since its last `resume_stable`) and quotes the last one: the exit code and stderr of `claude --resume --bg`, or the time after the resume at which a session that had registered was gone. When 3 resumes have failed and its last heartbeat is older than `stuck_minutes`, or at once when no conversation is left to resume (no session id of the agent has a transcript, or `claude --resume` answered "No conversation found"), the finding says its conversation cannot be resumed and names `agent_release` for its parent, then a replacement brief under the same name, from its brief; the watchdog does not release it itself, because a working agent's unfinished work stays in its conversation and the ledger. For a Driver whose exploration is closed: read its result in the ledger, then `agent_release` it |
| `stuck` | The session runs, the agent is `working`, and its last heartbeat is older than `stuck_minutes`. Not reported for a Manager or Lead that waits on work: a live child `registered` or `working`, a spawn of its own queued for memory, or an open `wait_for`; a child in `handed_up` waits on the parent's review, so it does not count. `stuck` and `blocked` are one idle episode: a change from one to the other updates the same finding and files no second directive. Does not apply to a Driver: see `driver_overdue` below | Message it, or have its parent replace it |
| `blocked` | A Manager, Lead, or Coder whose session shows `waiting` or `blocked` with no pending request and no held call (a work blocker, not a user wait), is `working`, and has a last heartbeat older than `stuck_minutes`. Reported in place of `stuck`, and like it, not for a parent that waits on work. Does not notify the user. A blocked Driver keeps its `driver_overdue` clock instead | Wake its parent to unblock it: answer its question, `return_work` with new direction, or release it. For a Manager, whose parent is the Oracle: ask it what blocks it, then unblock or release it |
| `superseded` | A live Coder's file is `superseded`: its Lead re-claimed the path under a new file id. Reported in place of every other session finding for that Coder, so a replaced Coder raises no `needs_user`, `blocked`, `stuck`, or `crashed` | Ask its Lead to release it with `agent_release`; the later claim owns the file, and the session holds memory |
| `driver_overdue` | The Driver's session runs, it is `working`, and its open exploration's last check-in (or its start, with none yet) is older than 30 minutes plus a 5-minute grace | Message it, or have the Oracle stop it and start a fix for whatever blocked it. |
| `driver_unsent` | The Driver's exploration is closed, the ledger has not released it, its session runs but sits idle, and it has owed a live agent an unsent wake-up for more than 2 minutes. This is the Driver ignoring its Stop hook block twice: `stop_hook_active` lets the second stop through idle. Not also reported as `crashed`, which covers a dead session instead. | Read its result in the ledger, then `agent_release` it. |
| `wait_landed` | A live Coder or Lead has an open `wait_for` row on a path whose latest claim is `approved` or `incomplete`. `approve` closes the waits in the same transaction, so this reports a race or a lost close | `agent_resume(target_name=...)`, or the `SendMessage` to its session when it runs |
| `wake_unsent` | A wake-up a live agent owes a live Manager, Lead, Coder, or Driver has been unsent for `unsent_wakeup_minutes`. One finding per target, from its oldest such wake-up. Not reported for a target parked waiting for memory, which the ledger resumes itself. Not reported for a Driver's debt, which `driver_unsent` covers, or for a test result an agent owes itself | The wake call (`SendMessage` or `agent_resume`) for the target. For a target the Oracle's `SendMessage` gate refuses (a working agent that is not the Oracle's child, unless the Oracle owes the wake-up): the call that wakes its nearest ancestor the gate allows, which then messages the target. For a target parked for another reason: the ledger resumes it once memory allows, or wake it now with that call |
| `spawn_queued` | A spawn `agent_spawn` queued for memory has waited `queued_spawn_minutes`. Reported on the parent, one finding per queued spawn | Free memory on the machine, lower `memory.headroom_mb`, or `run_pause` and tell the user the run waits on memory |
| `phase_unmanaged` | A phase is `unlocked` or `working`, not paused, has had a Manager, and has no live one: its Manager released itself or was released before the phase handed up. Reported on the Oracle, one finding for every such phase | Brief and spawn a new Manager for the phase, settle the file that blocks it with `file_accept_incomplete`, or `run_close` a run that cannot finish |
| `directive_unread` | Run-level, filed on the Oracle's row. A directive from `user_chat`, `outside_session`, or `skill` has been open for `unsent_wakeup_minutes` with `seen_at` unset: the Oracle has not called `directive_inbox` since it arrived. One finding for every such directive. The watchdog directive it files reaches a busy Oracle through its Monitor line and wakes an Oracle whose session is not running, as for any finding. It clears when the Oracle reads the inbox. Does not notify the user | `directive_inbox()` now, then apply each directive, or acknowledge one the Oracle cannot apply at once with `directive_ack(directive_id, note)` |
| `stray_process` | A process recorded at a release, or started under one, still runs a minute after the release: the session's own process, or one its tool shells started. Each listed process is verified (see below). One finding per released agent, listing each process by name and pid. See "Liveness, resume, and release" in [05-sessions.md](05-sessions.md) | Tell the user to stop them: `taskkill /F /PID <pid> /PID <pid>` on Windows, `kill <pid> <pid>` elsewhere. The command names each verified pid and never takes a process tree (`/T`) |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed. Does not apply to a Driver, which runs no `tests_run` | Ask its parent to `return_work` or `issue_escalate` |
| `context_high` | The latest request of the agent, the Oracle included, fills `context_pct` of its window | Have its parent replace it. For the Oracle: `run_pause`, then resume in a fresh session |
| `stalled` | No session of the run runs; or sessions run, but no member session is busy, no member is `working`, and nothing changed for 2 minutes. Not reported while a directive waits on the user, or while a spawn is queued for memory, which `spawn_queued` reports | Wake the agent whose work is pending: the reviewer of a submitted handoff, an agent with unread messages, or the Lead of a file with an open issue. With no such agent: resume or spawn the owner of the pending work, or, with nothing pending, continue the plan or call `run_finish` |
| `run_stalled` | Run-level, filed on the Oracle's row. No live agent is `working` with a running session that is not waiting on the user, no live agent owes another live agent an unsent wake-up, and the newest of the run's start, its latest agent event, message, and test run, and every live agent's transcript write is older than `stuck_minutes`. Reported while a directive waits on the user too, unlike `stalled`. Not reported while a spawn is queued for memory. The detail lists the sessions that wait on the user. Notifies the user | Read `status_tree()`, then wake the agent whose work is pending, or ask the user with `user_ask` when only the user can move the run |

- The context size is the input, cache read, and cache creation tokens of the last
  assistant response in the agent's transcript. The window is 200,000 tokens when the
  model names Haiku, else 1,000,000, unless the `context_window` setting names one.
  `watchdog.context_windows` maps a lower-case part of a model name to a window and may
  hold a `default` for any other model; the longest matching family wins, and an unlisted
  model keeps the built-in window above.
- For the idle stall, "nothing changed" reads the latest agent event, message, and test
  run, and the last write of the Oracle's transcript. The Oracle's own session status
  does not count, because its armed Monitor keeps it `busy`.
- A `stray_process` finding names only verified processes. A recorded process counts only
  while a process with its pid still has exactly its recorded start time; a recorded or
  current start time that is unknown (Windows cannot read it for service processes) fails
  the check. A process under it counts only when it runs, its parent is a verified process
  that runs, and it started no later than the child. A child of a dead process never counts,
  because its parent pid may belong to a reused pid. System and service processes
  (`svchost`, `System`, `services`, `wininit`, `csrss`, `lsass`, `init`, `systemd`,
  `launchd`, and similar) never count. The watchdog takes a second process snapshot just
  before it raises the finding and drops each process that no longer matches.
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
- `crashed` is the exception: it is identified by the agent's name, not its session id,
  because every resume can re-key the agent. One agent that keeps dying keeps one finding,
  updated in place. A `crashed` finding that cleared less than 30 minutes ago is reopened
  when the same agent name crashes again, and its detail updates with the count of failed
  resumes and the last failure. It files a new directive only when the directive it
  filed is no longer open.
- The new findings of one pass share one directive with source and sender `watchdog`. A
  pass with one such finding files the single-finding directive: its body names the agent,
  its session, the kind, the detail, and the next step. A pass with several files a
  digest: the first line reads "Watchdog pass: N findings", and one line follows for each
  finding with its kind, agent, session, detail, and next step. Every finding row of the
  digest carries the digest's directive id, so the Oracle reads and resolves one
  directive. Nothing closes a directive when one of its findings clears; the Oracle
  resolves it once it has handled every line. These kinds never join a digest, because
  their user notification is keyed by their own directive id: `needs_user`,
  `run_stalled`, `driver_overdue`, and `crashed` on a Driver. Each of those files its own
  directive. Open directives block `run_finish`.
- A new `crashed` or `driver_overdue` report on a Driver session also
  records a user notification, keyed by its directive id: "Driver crashed" at level
  error, or "Driver overdue" at level warning. The server shows it as an OS
  notification when the `driver` type's channels include `os`, and the Oracle owes a
  `PushNotification` for it when they include `push`. See "Notifications" in
  [02-run-lifecycle.md](02-run-lifecycle.md). `driver_unsent` records no
  such notification: it is a delivery miss the Oracle can fix from the directive alone,
  not a Driver error.
- A new `needs_user` or `run_stalled` report records a user notification at level
  warning, keyed by its directive id: "The swarm needs you: <agent>, <detail>" or "The
  swarm run stalled: <detail>". It is shown and owed like a Driver notification, with
  the channels of its own type (`needs_user` or `run_stalled`), except that a
  `needs_user` report on the Oracle's own session owes no `PushNotification`, since that
  session is the one waiting.
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
  listener owns the run, and an older one exits. It writes UTF-8 whatever the
  platform's encoding, so a directive with a character outside the Windows ANSI code
  page still prints; setup's report does the same.
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
  Oracle waits on the user, and a resume would start a second copy. The watchdog first
  owes the Oracle a wake-up from itself (reason `watchdog_directive`): "The watchdog
  reported N finding(s). Arm the watchdog Monitor again with <the Monitor call>, then read
  directive_inbox and act on what it holds." The Oracle's own mod delivers it from its
  `self_owed` poll, within 15 seconds. It files one such wake-up per batch of directives:
  none is filed while the latest is unsent or newer than the oldest waiting directive.
  The watchdog notifies the user only when the directive is still unnotified one more
  interval after that wake-up was filed. It notifies once per oldest such directive
  (`oracle_unreachable:<directive_id>`, level warning, OS notification only, type
  `oracle_unreachable`): "The swarm's Oracle session <name> waits on
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
  through the same `pauses.record_pause` as `run_pause`, so it notifies the user too,
  under the `run_paused` type. The resume after a passed clear-check notifies under
  `run_resumed`.
- On each pass of a paused run whose pause names a clear-check, the watchdog runs the
  check and resumes the run when it passes. See "Pause and resume" in
  [02-run-lifecycle.md](02-run-lifecycle.md). A check that raises is logged and counts
  as not passed.
- A paused run with a clear-check keeps the ledger server from its idle exit, so the
  check keeps running.
- A pass whose `claude agents --json` listing failed does not know whether a session
  runs, so it never counts as "no session runs": it resets the idle timer.

## Reminders

Every `remind_minutes`, while the run is paused, an open directive waits on the user
(`needs_user`), or a session waits on the user (an open `needs_user` finding), the
watchdog shows the OS notification again: the pause message, "<n> question(s) wait on
you: <first question>", and "<n> session(s) wait on you: <agent>, <detail>". A session
blocked on a person is a blocker, so it is reminded until it clears, not reported once.
A session held for the run panel counts, through its `needs_user` finding, until the
request is answered or its hold ends.
The first reminder comes one interval after the pause or after the watchdog first sees
the question or the waiting session, since `run_pause`, `user_ask`,
`directive_resolve(needs_user)`, and a new `needs_user` finding notify at once. `runs.reminded_at`
records the last one, and clears when nothing waits. Reminders show only the OS
notification and owe no `PushNotification`. With the `pause_reminder` type off or
lacking `os`, or `remind_minutes: 0`, there are none.

## Settings

Under `watchdog` in `.claude/sentinel-swarm.local.md`: `interval_seconds` (30),
`stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `context_window` (unset:
taken from the model), `context_windows` (unset: per family, see above), `idle_exit_minutes` (15; also how long a finished parent stays
parked before the watchdog releases it), `unsent_wakeup_minutes` (5; 0 turns
`wake_unsent` off), `queued_spawn_minutes` (10; 0 turns `spawn_queued` off),
`remind_minutes` (30; 0 shows each notification once). `stuck_minutes` also sets
`run_stalled`'s window. The memory steps read `memory.headroom_mb` and
`memory.park_idle_parents`, and `memory.park_blocked_minutes`; see [11-setup-and-settings.md](11-setup-and-settings.md).
