# Plan: fix what stalled two concurrent runs

## Goal

Two runs of 0.4.0 started at about the same time on one Windows machine, one per host repo.
Both stopped making progress within three hours, and neither stall cleared without a person.
This plan records each cause with its evidence, what got the runs moving again, and the fix
for each cause in the plugin.

## The runs

- **Machine:** Windows 11, 31.7 GB RAM, Claude Code 2.1.312, sentinel-swarm 0.4.0.
- **Run A:** started 2026-10-28 07:01 UTC on a Python driver-plugin host. The PRD had three
  phases. The host's default test suite was 3,477 tests. Its peak was 19 agents: the
  Oracle, 3 Managers, 4 Leads and 11 Coders.
- **Run B:** started 2026-10-28 07:20 UTC on a Python host with about 4,500 tests. The PRD
  had six phases. Its peak was 59 agents: the Oracle, 4 Managers, 10 Leads and 44
  Coders.
- **Both were live together** for about six hours, with an IDE and other Claude Code
  sessions open.

## Timeline (UTC)

| Time | Event |
|---|---|
| 07:01 | Run A starts. |
| 07:20 | Run B starts. |
| 07:4x | The machine has about 1.2 GB free. The daemon retires idle sessions "[low memory]", and Claude Code reaps background shells. |
| 07:49 | Run A's Oracle calls `run_pause`: `~/.claude.json` is corrupt ("JSON Parse error: Unexpected EOF"), so its wake-up to `mgr-p1-settle` cannot be delivered. |
| 08:25 to 08:55 | Three more corrupt copies of `~/.claude.json` are written to `~/.claude/backups/`. |
| 08:46 | A run B Coder stops at a permission prompt. |
| 09:02 | Run B's Oracle asks the user a question (`AskUserQuestion`) and waits. |
| 09:35 to 09:44 | The watchdog files `stuck` findings on run B agents with no activity for 41 to 83 minutes. |
| 10:07 | A run B Manager escalates a ledger deadlock to the Oracle, which is still waiting on the user. |
| 13:12 | The daemon retires run B's last idle Managers and Leads. Nothing moves until a person looks. |

Neither run was failing. Each was waiting, and nothing outside agent view showed that.

## Causes

### 1. Two runs on one machine exceed its memory

- **Evidence:** `claude.exe` processes held about 14.6 GB, and the IDE about 4.6 GB. A
  role session costs about 600 MB (two processes, about 150 MB and about 450 MB). The
  ledger's `spawn_queue` held 10 rows in run A and 41 in run B. `agent_events` show many
  "the daemon retired its session: … [low memory]" rows. Claude Code reaped two
  coordinator watchers "because the system was critically low on memory".
- **Effect:** `memory.headroom_mb` (1,500) is a per-run floor. Each run admits spawns while
  free memory is above it, but neither run sees the other's sessions. Retired parents
  stop answering their children until something resumes them.
- **Fix:**
  - `run_start` reads every live swarm on the machine. The multi-repo lock already lists
    them. Admit a spawn only when the machine-wide estimate stays above the floor.
  - `parallelism_cap` counts other swarms' sessions today; say so in `setup`'s output,
    and set a default from total RAM.
  - `status` shows queued spawns and memory-retired agents in its first lines.

### 2. Concurrent sessions corrupt `~/.claude.json`

- **Evidence:**
  - Run A's Oracle paused on "JSON Parse error: Unexpected EOF".
  - `~/.claude/backups/` holds `.claude.json.corrupted.*` files from 08:25, 08:44 and
    08:55 UTC, 210 to 212 KB each, about the size of a whole file.
  - The ledger server's log shows the same parse error twice.
  - The file parsed again by about 14:30 UTC, with nobody having restored it by hand.
- **Effect:** session start, stop, `--bg` and messaging fail while the file is truncated.
  A wake-up that fails during that window is never retried, so the run waits for a
  person.
- **Likely cause** (not verified in Claude Code's source): many sessions starting,
  resuming and stopping at once, each writing the shared config file.
- **Fix:**
  - Retry a failed `agent_spawn`, `agent_resume` or wake-up after a parse error, with a
    bounded backoff, instead of pausing at once.
  - Pause only when the file still fails to parse after the retries. The pause reason
    names the newest backup that parses.
  - Lower the spawn rate under memory pressure (cause 1), which also lowers the
    number of writers.
  - Report the corruption upstream, with these timestamps.

### 3. A session waiting on a person looks idle to the swarm

- **Evidence:**
  - `claude agents --json` showed run B's Oracle as `"status": "waiting", "waitingFor":
    "input needed"`, on an `AskUserQuestion` from 09:02. Its transcript ends there.
  - A run B Coder showed `"waitingFor": "permission prompt"` from 08:46.
  - The ledger has no record of either. The watchdog filed `stuck` findings on other
    agents, and a Manager's escalation (10:07) went to the waiting Oracle.
- **Effect:** run B stalled for over four hours. Every escalation path ends at the Oracle,
  and the Oracle was waiting on a dialog nobody saw.
- **Fix:**
  - The watchdog reads `waitingFor` from `claude agents --json`. A session waiting on
    input or a permission files a `needs_user` finding at once, with the session name
    and the question text.
  - `notify` (OS toast and `PushNotification`) fires on that finding, not only on Driver
    results and round-3 issues.
  - `status` lists waiting sessions first.
  - In a background run, the Oracle posts a question as a `needs_user` directive and
    keeps the rest of the run moving, instead of blocking on `AskUserQuestion`.

### 4. The 600 s test-run cap cannot hold a large suite

- **Evidence:**
  - `testing.run_tests` has `timeout_s: int = 600` (`mcp/src/swarm_ledger/testing.py:184`).
  - Run B's full suite took about 25 minutes, and the Oracle's full-suite `tests_run`
    timed out with no record. `phase_review` needs a passing full-suite run, so no run B
    phase could close.
  - Run A's serial suite took about 582 s, too close to the cap. Its operator wrapped
    `test_command` to run seven parallel shards, which took 209 to 271 s.
- **Fix:**
  - `test_timeout_s` becomes a setting, with 600 as the default.
  - `setup` times one full run and warns when it exceeds half the cap.
  - The docs name sharding (`pytest -n`) as the way to fit a large suite.

### 5. Ledger records that can never close

- **Evidence:**
  - Run B's Oracle reports that a released claim on a path that does not exist can never
    reach `approved` or `superseded`, so its module cannot close.
  - A Manager reports a deadlock on a reviewed handoff (score 80, no disagreement, issue
    closed) that needs an Oracle override.
  - Two issues ask for files that no module claimed. The write hook denies the edit, and
    no role can add a claim.

  These come from the agents' messages and issues, not from a reproduction.
- **Fix:**
  - A released claim on a missing path counts as closed.
  - A Manager may claim an unowned path into one of its modules, which closes the
    missing-claim case without the Oracle.
  - Reproduce the handoff deadlock from run B's ledger, and add the case to the tests.

### 5b. A binary file can never hand off, and its phase cannot close

- **Evidence:**
  - `handoff_submit` reads a claimed SQLite file (a driver plugin's committed
    `*_driver_graph.db`) as UTF-8 text and fails with `UnicodeDecodeError`. Run A's
    issue "Binary graph .db cannot pass handoff_submit" reached round 3.
  - The Oracle had no call that sets a file state, so it settled the file by plan
    deferral.
  - The phase's Manager then reported that `phase_update(2, handed_up)` still refuses
    ("module(s) [1] have no accepted manager review newer…"), and the Manager released
    itself. That left the phase `unlocked` with no live Manager and no reply.
- **Fix:**
  - `handoff_submit` hashes a binary file and records its version without decoding it.
  - A file whose content is generated (a build output) is claimed as a product of its
    source file, and its source's handoff carries it.
  - A phase whose only blocker is a deferred file can hand up.
  - The watchdog flags a phase that is `unlocked` with no live Manager.

### 6. The handoff graph check refuses method anchors

- **Evidence:** run A's open issue says "handoff graph check cannot resolve method anchors
  that references require". `graph_upsert` refuses with "anchor symbol Engine.<method> not
  found" for every method anchor of one class, including methods that exist.
- **Effect:** the file could not hand up, and the Lead and Manager spent rounds on it.
- **Fix:**
  - Reproduce the case against codebase-kg's anchor resolution, for a method anchor
    `path#Class.method`.
  - Fix whichever side is wrong.
  - Until then, a graph-check refusal on an anchor the source shows exists is accepted as
    incomplete, with the reason, instead of blocking the handoff.

### 7. Smaller setup friction

- **`test_command` runs through `cmd.exe` with `shell=True`.** A forward-slash relative
  path fails there ("'plugins' is not recognized"). `setup` should test the command once,
  through the same runner.
- **The ledger fails a test run that has any skip.** A host suite with designed skips
  needs a wrapper. Make "skips fail" a setting, or count skips separately.
- **A Coder's shell gate allows only test, build and lint commands.** A host whose data
  files go through an export, edit and build loop had to put that loop behind
  `build_command`. The docs should name this.
- **The smoke test installs and removes a dev marketplace.** Two hosts running
  `scripts/smoke.sh` at the same time would collide. Give each smoke run its own
  marketplace name.
- **The run panel shows "No brief recorded." on the Oracle's row for the whole run.**
  `panel.py:192` fills the row's brief from `run["outcome"]`, which stays empty until
  the run ends. A user reads that as an Oracle with no instructions. Show the run's
  PRD (`runs.prd`) until there is an outcome.
- **`setup` cannot run `/reload-plugins`.** A fresh install needs a new session before
  the tools load. `setup` should say so.

### 8. The run panel hides the rows that matter

Alex asked for these on 2026-10-28, after watching both runs in the panel.

- **A Coder's file path pushes the row off the screen.**
  - **Today:** a file task's name is its full repo path (`mcp/src/swarm_ledger/panel.py:173`: `item("file_id", …,
    f["path"], …)`). `taskButton` renders it in a box with `flexShrink={0}`
    (`hooks/register.tsx:304-306`), so on a narrow panel the path takes the row and
    the agent's name or status is cut off.
  - **Fix:** a file task shows its file name only. The full path goes in the task's
    detail lines. When even the file name does not fit, it truncates from the left with
    a leading `…` (Ink `wrap="truncate-start"`), so the end of the name stays visible.
    The task box shrinks before the agent's name does.
- **Rows that are done look the same as rows that are working.**
  - **Today:** a row is dim only when `dimEnded` is on and the agent has an `ended_at`
    (`register.tsx:362`).
  - **Fix:** always dim a row whose agent is released, gone, or finished with its task
    approved. Rows that are working keep full colour.
- **Rows waiting to start look active.**
  - **Fix:** dim these too:
    - a Manager whose phase is still `planned`;
    - a child with a brief but no session yet (queued in `spawn_queue`);
    - a parked agent waiting for memory.
- **Rows waiting on a person look idle.** Alex wants the request drawn at the top of the
  panel and under the agent's row, so it can be answered from the panel.
  - **Today:** these were checked against Claude Code 2.1.312's mod API
    (`plugin-authoring` types).
    - **Waiting sessions:** the mod can run `claude agents --json` through
      `$.process`. That output carries `"status": "waiting"` with `"waitingFor":
      "permission prompt"` or `"input needed"` for each background session.
    - **The request text:** the mod can read it from the session's transcript through
      `$.fs`. The ledger's `agents.transcript_path` points to it, and the pending
      tool use, or the `AskUserQuestion` input, is the last entry.
    - **A red border:** `Box` takes `borderStyle` and `borderColor`.
    - **Answering:** no mod API answers another session's prompt.
      - `$.agent.list()` lists only this session's own loops.
      - `$.ui.focus` moves focus only among the plugin's own elements.
      - The `claude agents` CLI has no command that sends input.
  - **Fix, part 1, show it:**
    - A "Waiting on you" section at the top of the panel lists every waiting role
      session.
    - Under that agent's row, a red-bordered box (`borderColor="error"`) shows the
      tool and its input, or the question and its options, and says where to answer:
      that session's row in agent view.
  - **Fix, part 2, answer it in the panel:** stop role sessions raising Claude Code
    prompts at all, and route each one through the ledger instead.
    - The role's `PreToolUse` hook (the swarm's `hook.py` shim) posts a
      `permission_request` row for a tool the role's rules would ask about, and waits.
    - The panel's box draws Allow and Deny buttons, and a press writes the answer.
    - The hook returns `permissionDecision` (`allow` or `deny`), which the mod types
      list as a hook output.
    - Questions work the same way: the Oracle posts a `needs_user` directive
      instead of calling `AskUserQuestion` (cause 3), and the panel answers it with a
      directive reply.
    - First check how long Claude Code lets a `PreToolUse` hook wait, and what
      happens at that limit. A request still open at the limit falls back to
      Claude Code's own prompt.
- **The legend** (`register.tsx:456-458`) gains dimmed for "ended or not started" and
  the warning glyph for "waiting on you".

## What got the runs moving again

- **Run A, 2026-10-28 14:35 UTC:**
  - The coordinator confirmed `~/.claude.json` parsed (211,217 bytes).
  - It sent the paused Oracle "continue" by `SendMessage`, from another Claude Code
    session on the machine.
  - The message also capped the run at three live sessions below the Oracle, and told
    the Oracle to accept the graph-check refusal (cause 6) as incomplete.
  - Within two minutes the run's state was `active`. The Oracle resumed `lead-p1-settle`
    (`agent_resume`, rekeyed to a new session) and released the idle `mgr-p1-settle` to
    free memory.
  - So a paused Oracle can be woken without agent view, by any session that can message
    it.
- **Run B:** the Oracle's question and the Coder's permission prompt need a person in
  agent view. Recorded once they are answered.

## Order of work

1. Cause 3, the watchdog reading `waitingFor`, together with cause 8's "waiting on a person" row. It turns an unseen multi-hour stall into a
   notification in minutes.
2. Cause 4, a configurable test timeout.
3. Cause 1, admission against machine-wide memory.
4. Cause 2, retrying after a config parse error.
5. Causes 5 and 6, from reproductions in the two ledgers.
6. The setup friction in cause 7, and the rest of cause 8's panel changes.

Add each finding that stays open to `knowledge/prd/16-open-items.md`, and delete this plan
once every cause has a fix or an open item.
