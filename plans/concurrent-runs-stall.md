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
  - **More instances, same pattern (run B):**
    - 08:46: the watchdog did file `waiting_permission` for
      `coder-p3-stitched-diagnose-l1b` (directive 29). It went to the Oracle, which was itself
      waiting on the user from 09:02, so nobody acted on it until 20:58.
    - 21:12: `coder-p3-stitched-diagnose-l4b` showed `"waitingFor": "permission prompt"` on
      a `Write` to `.claude/scratch/stitched/test_emit_l4.py`, eight minutes after the run
      restarted.
    - Each prompt came from a write or command outside the role's allowed paths. So a run
      that writes scratch files will keep raising prompts that only agent view shows.
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
  - The write hook denies a role's write outside its claim, under `.claude/` included,
    before Claude Code can prompt for it. The deny message names the role's claimed test
    file as the place for a scratch helper. A deny returns to the agent at once, and a prompt
    in a background session waits for agent view. Both run B Coder prompts (08:46 and 21:12)
    were `Write` calls to `.claude/scratch/stitched/`.

### 4. The 600 s test-run cap cannot hold a large suite

- **Evidence:**
  - `testing.run_tests` has `timeout_s: int = 600` (`mcp/src/swarm_ledger/testing.py:184`).
  - Run B's full suite took about 25 minutes, and the Oracle's full-suite `tests_run`
    timed out with no record. Measured out of band on the run's branch before launch:
    4,524 tests in 2,318 s (38.6 min) with `-n 4`, while run A was live. The PRD's bench
    module adds 407 s on its own. Three failures in that run (`test_maproom_site_health`)
    came from load and passed alone (8 of 8 in 54 s). `phase_review` needs a passing full-suite run, so no run B
    phase could close.
  - Run A's serial suite took about 582 s, too close to the cap. Its operator wrapped
    `test_command` to run seven parallel shards, which took 209 to 271 s.
  - **A second limit sits in front of the cap.** The MCP tool call itself times out on
    the client before the run ends. Run B's Oracle: "My call timed out on the client
    side, and no record has appeared yet". Run A's Oracle paused at 17:05 UTC because
    "tests_run(scope=full) exceeds the tool timeout". So a longer ledger cap alone does
    not let a full run finish.
- **Fix:**
  - **`tests_run` returns at once.** It starts the run, answers with its
    `test_run_id`, and records the result when the run ends. The caller reads it with
    `test_run_get`, or gets a wake-up when it is recorded. No tool call waits out a
    suite.
  - `test_timeout_s` becomes a setting, with 600 as the default. It bounds the test
    process, not the tool call.
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
- **Measured on run B's ledger (2026-10-28 21:15 UTC):**
  - The stray claim is file 20, `knowledge/PRD/07-replay.md`, claimed and released at
    07:38 by `coder-p4-utterances-prd`, which then claimed the real path
    `07-path-tests-and-replay.md` (files 21 and 56).
  - The two unclaimed paths in issues 5 and 6 were claimed. Files 25
    (`maproom/js/views/screen.js`), 27 (`knowledge/PRD/09-maproom.md`) and 28
    (`maproom/css/app.css`) were claimed at 07:40 for queued Coders. They were released at
    08:02:07 to 08:02:21, within 15 s of the ledger parking their Lead "under memory
    pressure while its children work" (08:02:03 and 08:02:07). Their Coders started only
    at 08:17 to 08:24, so `who_owns` returned no owner and the write hook denied every edit.
    That parking releases its queued children's claims is a hypothesis from the
    timestamps; the code path is not confirmed.
- **Fix:**
  - A released claim on a missing path counts as closed.
  - A Manager may claim an unowned path into one of its modules, which closes the
    missing-claim case without the Oracle.
  - Parking or replacing a parent keeps the claims of its queued and live children. Confirm
    the release path first, from run B's ledger.
  - For a file with no `test_path`, `cr_complete` takes the file's latest submitted
    handoff after acceptance as evidence, so a handoff that waits on the change request does
    not wait forever. Run B's CRs 1 and 3 on a PRD file could not complete otherwise.
  - Reproduce the handoff deadlock from run B's ledger, and add the case to the tests.

### 5c. A full test run that can never count as clean

- **Evidence (run B):**
  - `build_command` drops `{target}` for a full run, so the full command runs pytest from
    the repo root with no path. pytest applies `testpaths` only when it is invoked from the
    rootdir, so it walked the whole tree and imported `plans/jev/scripts/test_d.py`, a
    host script that raises `SystemExit` on import. Full run 101 recorded `INTERNALERROR`,
    exit code 3, 0 passed. Adding `-c mcp/pyproject.toml --rootdir mcp` did not change
    that; `--ignore=plans` did, collecting the same 4,751 tests as `mcp/tests`.
  - `test_run_passed` and `_CLEAN_TEST_RUN` count any skip as a failure (`skipped = 0`). The
    PRD's bench module skips when its corpus variable is unset, so its file run 203 (13
    passed, 1 skipped) could never pass, and no full run including it could either.
- **Fix:**
  - `setup` dry-runs the full command (`--collect-only` where the stack allows it) and
    reports a collection error before the run starts.
  - A full run takes a default target, a `full_target` setting, instead of an empty one.
  - Count a skip as clean when the test reports an environment skip the host declares in
    settings, or warn at `setup` that a skip blocks every handoff.

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

### 6b. A replacement parent cannot reach its live children

- **Evidence:** run A's report, shortfall 7.
  - The Oracle released a dead `mgr-p1-settle` and spawned a replacement.
  - `lead-p1-settle` stayed parented to the released agent's id, both in the ledger and
    in the `SendMessage` hook.
  - `status_tree` named the new Manager, but `message_post` and `SendMessage` from
    the new Manager and from the Oracle both refused the Lead.
  - `brief_create` refused a replacement Lead ("a live agent already holds the name"),
    and `agent_resume` refused because the Lead was running.
  - Nobody in the run could wake the Lead. An outside nudge got it moving again.
- **Today:** only the Oracle's own resume re-points children. `run_start` runs
  `UPDATE agents SET parent_agent_id = ? WHERE parent_agent_id = ?` and the same on
  `briefs` (`mcp/src/swarm_ledger/ledger.py:338-345`). A Manager or Lead replaced by
  `agent_spawn` gets no such step.
- **Fix:**
  - **The re-pointing step works for every role.** When a replacement takes a released
    agent's brief (same `child_name`, or a brief carrying `replaces=<agent_id>`), the
    ledger moves every live child and every brief from the old id to the new one, in
    the same transaction.
  - **The `SendMessage` hook reads the parent from the ledger** on each call, rather
    than from what it cached at spawn.
  - **A test** releases a Manager with a live Lead, spawns a replacement, and checks
    that `message_post`, `SendMessage` and `agent_resume` all reach the Lead.

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

### 9. The roles lack tools the work needs

Run A's report and ledger (issues 1 and 3, deferral 1, overrides 1 to 3, and the Oracle's
pause at 19:49 UTC) show five places where the work was done but no role could take the
next step.

- **No role can delete a file.**
  - **Evidence:** the plan told run A to delete a helper script and its test. The
    Coder's shell gate denies `Remove-Item` and `git rm`, and `Write` and `Edit` cannot
    delete (issue 1). The Oracle had the Coder write both files empty and deferred the
    delete to the coordinator (deferral 1). The emptied test file then had to pass review
    as a stub.
  - **Fix:** add a `file_delete` ledger call for a claimed file. The deletion is recorded
    as the file's version, so a handoff and a review cover it like an edit. Allow
    `git rm` of a claimed path in the Coder's shell gate.
- **A command outside test, build and lint needs a person.**
  - **Evidence:** a derived database (`driver_recipes.db`) went stale after a graph
    change, and its test failed the full suite. Rebuilding it is one command,
    `python scripts/recipes/build_recipes.py`. No role could run it, so the Oracle paused
    the run until a person ran it (19:49 to 20:20 UTC). Run A's operator already had to
    put the driver graph's export and build loop behind `build_command` (cause 7).
  - **Fix:** add a `commands` setting, a list of named host commands each with its
    working directory and interpreter. The Coder's and the Manager's shell gates allow
    them, and `setup` proposes the ones the repo's docs name. A failing test whose
    message names a rebuild command is reported with that command.
- **The graph's own-module rule blocks a node that spans modules.**
  - **Evidence:** a Lead could not `graph_upsert` a node whose anchors were a file in
    its module and a file in no module (override 1). The Oracle has no `graph_upsert`,
    so the run needed an override.
  - **Fix:** a Lead may update a node when every anchor it changes is in its module or
    unclaimed. The Oracle gets `graph_upsert` for nodes that no module owns.
- **The Oracle cannot mark a file "accepted incomplete" directly.**
  - **Evidence:** the Oracle told a Manager, "I have no call that sets a file state. The
    ledger's 'incomplete' state comes from escalation: an issue that ends its last
    round below the floor". A file that was verified another way (the binary `.db`,
    covered by its approved export and round-trip tests) could only be deferred. A
    deferral did not let its module pass `module_review`.
  - **Fix:** add `file_accept_incomplete(file_id, reason, evidence)` for the Oracle. It
    records the reason and the test runs that cover the file, and `module_review`
    counts the file as settled.
- **No path lets the run's owner close a run the ledger cannot close.**
  - **Evidence:** after the work was committed by hand, the coordinator closed run A by
    editing `ledger.db` with SQL. It set three files to `incomplete`, the phases and
    modules to `approved`, every agent to released, and the run to `finished`, with a
    backup first. That is the only way today.
  - **Fix:** add `run_close(outcome, files_incomplete=[...])`, callable by the Oracle or
    from the launcher (`ledger.py close`). It records the outcome and each file's reason,
    releases every agent, stops their sessions, and lets the server exit.
- **A paused run does not resume when its cause clears.**
  - **Evidence:** run A paused on the corrupt `~/.claude.json` at 07:49 UTC. The file
    parsed again by about 14:30, but the run stayed paused until another session sent
    "continue" at 14:35.
  - **Fix:** a pause records the check that would clear it (for example "the config
    file parses"). The watchdog tries that check on its timer and resumes the run when
    it passes, with a note in the run's events.
- **Ended sessions still show as working.**
  - **Evidence:** after the daemon retired run B's sessions for memory, the ledger kept
    4 Coders `working` and 3 `handed_up` whose sessions no longer existed. The panel
    showed a column of busy Coders under parked Leads.
  - **Fix:** the watchdog reconciles each agent against `claude agents --json`. An agent
    whose session is gone becomes `parked`, with the reason. Cause 8 dims it.

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
- **Run A, 2026-10-28 20:20 UTC:**
  - The Oracle paused for a command no role could run (cause 9).
  - The coordinator ran it, rebuilding `driver_recipes.db`, and messaged "continue".
  - The run resumed in under a minute.
- **Run A, closed 20:35 UTC:**
  - The coordinator told the Oracle to release every agent and pause.
  - It committed the work by hand after the full suite passed.
  - It stopped the last two sessions with `claude stop <id>`, and closed the ledger
    with SQL (cause 9).
- **Run B, 2026-10-28 20:56 UTC:**
  - A person answered the Oracle's 09:02 `AskUserQuestion` in agent view ("I fix the plugin,
    continue"). The Oracle's transcript has no entry between 09:02 and 20:56.
  - Within 48 s the Oracle re-armed its watch and messaged each Manager. The ledger then
    resumed the parked parents and delivered the wake-ups they were owed.
  - The stall lasted 10 h 49 min, from the last action at 10:07:47 to 20:56:52. In that
    time the watchdog's only action was at 13:12, when the daemon retired six idle
    sessions. It filed no finding, because every agent was `idle` waiting on a wake-up, and
    `_wake_oracle` skips while the Oracle's session counts as running. Its first `stuck`
    findings after the stall came at 20:57, about 650 minutes late. Cause 3's fix needs a
    run-level finding too: no agent working and no wake-up owed for `stuck_minutes`.
- **Run B, 2026-10-28 21:15 UTC, by the coordinator session:**
  - It backed up the ledger, then fixed by SQL what no role can: file 20 set to
    `superseded`, and files 25, 27 and 28 set back to `claimed` (cause 5).
  - It ran the bench through the plugin's own `run_tests` with
    `set "CARTOGRAPHER_REPLAY_CORPUS=…" && … pytest … --ignore=plans {target}`: 27 passed,
    2 failed, 0 skipped, in 407 s (cause 5c). It asked the Oracle to `profile_set` that
    command.
  - For cause 4, it wrote a host-side recorder. It runs the run's full `test_command` with
    no time cap and inserts the result as the Oracle's `scope='full'` row, with the
    plugin's command, counts and fingerprint, only if the tree did not change. The Oracle
    asks for it with a `needs_user` directive.
  - It sent all of this as directive 48 and woke the Oracle by `SendMessage`.
  - At 21:12 a run B Coder was waiting on a permission prompt to write a scratch test file
    (cause 3 again). It needs a person in agent view.
  - **Measured, 21:31 to 21:36 UTC:**
    - The Oracle read directive 48 at 21:31:03 and applied all of it: `profile_set` with
      the new command, issues 5 and 6 closed, and the full-run protocol adopted.
    - p4 `utterances` handed up at 21:33:39, its first phase move since 07:26, once file 20
      no longer held its module open.
    - `coder-p3-stitched-surface-prd09` restarted at 21:35:13 on its restored claim.
    - The Oracle raised one more cause 5 record as directive 49 (below). The coordinator
      cleared it at 21:34, and the Oracle routed `cr_verify` to both requesters by 21:35.
  - **21:34 UTC, a change request on a file with no tests (cause 5):** CRs 1 and 3 asked for
    rows in `knowledge/PRD/06-baseline-and-promotion.md` (file 10). `approve(39)` refused
    with "change request(s) [1, 3] on this file are not verified". `cr_complete` refused
    with "no passing test run or approved handoff for … since acceptance". A `.md` file has
    no `test_path`, and handoff 39 waits on the CRs, so neither condition can ever hold.
    The coordinator checked that the working tree held both requested rows, then set both
    CRs to `completed` by SQL, with handoff 39 as the evidence.
  - Not yet measured: the first recorded full run. It waits for the Oracle's "full run
    needed" directive, and the tree has to stay still while it runs.

## Closing a run

Run A built and reviewed everything its PRD asked for, but the ledger could not close it.
The Oracle paused twice, at 17:05 and 17:49 UTC: `run_finish` refuses while three phases
are unapproved. Three fixes clear that:

| Blocker | Cause | Fix |
|---|---|---|
| `phase_review` needs a recorded full test run, and `tests_run(scope=full)` outlasts the tool call. | 4 | `tests_run` returns at once and records the result later, with `test_timeout_s` as a setting. |
| `module_review` refuses a file that never handed off: the binary `android_driver_graph.db`, and `engine.py` and `events.py`, whose graph check refuses `Class.method` anchors. | 5b and 6 | `handoff_submit` hashes binary files and resolves `Class.method` anchors. A phase whose only blocker is a deferred file can hand up. |
| A replacement Manager cannot reach the Lead its predecessor started. | 6b | Re-point live children and briefs to every replacement parent, and route `SendMessage` from the ledger. |

**Acceptance:** replay run A's ledger (saved on the host as `.claude/scratch/swarm-a/ledger-run-a-2026-10-28.db`, before its
coordinator integrates the work) against the fixed plugin. `phase_review` and `run_finish`
must then succeed with no override.

## Order of work

1. Cause 3, the watchdog reading `waitingFor`, together with cause 8's "waiting on a
   person" row. It turns an unseen multi-hour stall into a notification in minutes.
2. "Closing a run": causes 4, 5b, 5c, 6 and 6b, plus cause 9's `file_accept_incomplete` and
   `run_close`. Without them no run with a large suite, a binary file or a replaced parent
   can finish. Accept them on run A's saved ledger.
3. The rest of cause 9: deleting files, named host commands, cross-module graph updates,
   resuming after a pause clears, and reconciling ended sessions.
4. Cause 1, admission against machine-wide memory.
5. Cause 2, retrying after a config parse error. Run B's watchdog hit it too: at 08:55 it
   skipped a pass on "`~/.claude.json` is corrupted: JSON Parse error: Unexpected EOF".
6. Cause 5, the remaining records that cannot close, from run B's ledger.
7. The setup friction in cause 7, and the rest of cause 8's panel changes.

Add each finding that stays open to `knowledge/prd/16-open-items.md`, and delete this plan
once every cause has a fix or an open item.
