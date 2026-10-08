# Watchdog

The watchdog is a background job, not a role. It detects and reports. It never resumes,
replaces, or stops a Manager, Lead, or Coder. It resumes the Oracle when the Oracle's
session is not running, so that a report can reach the Oracle at all. For any other
agent's failure, it reports to the Oracle, and the Oracle acts.

## Detection

A thread in the ledger server (`watchdog.py`) runs every `interval_seconds`. It reads
`claude agents --json` and scans the active run. It skips a paused run, and a failed
listing skips one pass.

| Kind | Condition | Next step the report names |
|---|---|---|
| `crashed` | A Manager, Lead, Coder, or Driver is `working`, or `registered` for more than 2 minutes, and its session is missing or not running. An `idle` or `handed_up` agent whose session exited is normal. A Driver whose exploration is closed but that the ledger has not released yet is reported whatever its state, since a closed Driver stays live only until its last wake-up goes out. | `agent_resume(target_name=...)`. For a Driver whose exploration is closed: read its result in the ledger, then `agent_release` it |
| `stuck` | The session runs, the agent is `working`, and its last heartbeat is older than `stuck_minutes`. Does not apply to a Driver: see `driver_overdue` below | Message it, or have its parent replace it |
| `driver_overdue` | The Driver's session runs, it is `working`, and its open exploration's last check-in (or its start, with none yet) is older than 30 minutes plus a 5-minute grace | Message it, or have the Oracle stop it and start a fix for whatever blocked it. |
| `driver_unsent` | The Driver's exploration is closed, the ledger has not released it, its session runs but sits idle, and it has owed a live agent an unsent wake-up for more than 2 minutes. This is the Driver ignoring its Stop hook block twice: `stop_hook_active` lets the second stop through idle. Not also reported as `crashed`, which covers a dead session instead. | Read its result in the ledger, then `agent_release` it. |
| `waiting_permission` | A Manager's, Lead's, or Coder's session waits on a permission prompt. Not also reported as crashed or stuck | Tell the user to open that session and answer |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed. Does not apply to a Driver, which runs no `tests_run` | Ask its parent to `return_work` or `issue_escalate` |
| `context_high` | The latest request of the agent, the Oracle included, fills `context_pct` of its window | Have its parent replace it. For the Oracle: `run_pause`, then resume in a fresh session |
| `stalled` | No session of the run runs; or sessions run, but no member session is busy, no member is `working`, and nothing changed for 2 minutes. Not reported while a directive waits on the user | Wake the agent whose work is pending: the reviewer of a submitted handoff, an agent with unread messages, or the Lead of a file with an open issue. With no such agent: resume or spawn the owner of the pending work, or, with nothing pending, continue the plan or call `run_finish` |

- The context size is the input, cache read, and cache creation tokens of the last
  assistant response in the agent's transcript. The window is 200,000 tokens when the
  model names Haiku, else 1,000,000, unless the `context_window` setting names one.
- For the idle stall, "nothing changed" reads the latest agent event, message, and test
  run, and the last write of the Oracle's transcript. The Oracle's own session status
  does not count, because its armed Monitor keeps it `busy`.
- A stall is reported only when two passes in a row see it, since a run passes through
  "no session running" while one session exits and another resumes.
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

## Settings

Under `watchdog` in `.claude/sentinel-swarm.local.md`: `interval_seconds` (30),
`stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `context_window` (unset:
taken from the model), `idle_exit_minutes` (15).
