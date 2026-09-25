# Watchdog

The watchdog is a background job, not a role. It detects and reports. It never resumes,
replaces, or stops a Manager, Lead, or Coder. It resumes the Oracle when the Oracle's
session is not running, so that a report can reach the Oracle at all. For any other
agent's failure, it reports to the Oracle, and the Oracle acts.

The mechanics below are **(proposed)**.

## Detection

A thread in the ledger server (`watchdog.py`) runs every `interval_seconds`. It reads
`claude agents --json` and scans the active run. It skips a paused run, and a failed
listing skips one pass.

| Kind | Condition | Next step the report names |
|---|---|---|
| `crashed` | A Manager, Lead, or Coder is `working`, or `registered` for more than 2 minutes, and its session is missing or not running. An `idle` or `handed_up` agent whose session exited is normal | `agent_resume(target_name=...)` |
| `stuck` | The session runs, the agent is `working`, and its last heartbeat is older than `stuck_minutes` | Message it, or have its parent replace it |
| `waiting_permission` | The session waits on a permission prompt. Not also reported as stuck | Tell the user to open that session and answer |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed | Ask its parent to `return_work` or `issue_escalate` |
| `context_high` | The latest request of the agent, the Oracle included, fills `context_pct` of its window | Have its parent replace it. For the Oracle: `run_pause`, then resume in a fresh session |
| `stalled` | No session of the run runs; or sessions run, but no member session is busy, no member is `working`, and nothing changed for 2 minutes | Wake the agent whose work is pending: the reviewer of a submitted handoff, an agent with unread messages, or the Lead of a file with an open issue |

- The context size is the input, cache read, and cache creation tokens of the last
  assistant response in the agent's transcript. The window is 200,000 tokens when the
  model names Haiku, else 1,000,000, unless the `context_window` setting names one.
- For the idle stall, "nothing changed" reads the latest agent event, message, and test
  run, and the last write of the Oracle's transcript. The Oracle's own session status
  does not count, because its armed Monitor keeps it `busy`.
- A stall is reported only when two passes in a row see it, since a run passes through
  "no session running" while one session exits and another resumes.

## Reporting

- `watchdog_findings` keeps one live row per run, agent, and kind. A finding the next
  pass does not see is cleared. One that comes back gets a new row and a new report.
- Each new finding becomes a directive with source and sender `watchdog`. Its body names
  the agent, its session, the kind, the detail, and the next step. The Oracle resolves
  each one; open directives block `run_finish`.

## How a report reaches the Oracle

- The listener is `python -m swarm_ledger.watch`, run as `hook.py watch`. Every 2
  seconds it prints one line per unnotified watchdog directive, marks it notified, and
  writes `runs.watch_heartbeat_at`. It exits when the run is not active. The newest
  listener owns the run, and an older one exits.
- The Oracle arms it right after `run_start`, and again whenever it expires, with
  exactly this call:
  `Monitor(command="python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch", description="sentinel-swarm watchdog", timeout_ms=1800000)`.
  Each printed line wakes the Oracle.
- The Oracle's Stop hook blocks while the run is active and the listener's heartbeat is
  missing or older than 60 seconds, or the armed Monitor expires within 30 seconds.
- `directive_inbox` marks the directives it returns as notified.
- When a watchdog directive stays unnotified for one interval and the Oracle's session
  is not running, the server runs
  `claude --resume <Oracle session> --bg "The watchdog reported N finding(s). Read directive_inbox."`,
  at most once every 5 minutes. After 3 attempts with no Oracle heartbeat since the
  first, it pauses the run with the reason "the watchdog could not wake the Oracle".

## Settings

Under `watchdog` in `.claude/sentinel-swarm.local.md`: `interval_seconds` (30),
`stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `context_window` (unset:
taken from the model), `idle_exit_minutes` (15).
