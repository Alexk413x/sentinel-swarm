# Plan: fix every issue the 0.4.0 swarm runs found

## Goal

Two runs of 0.4.0 started at about the same time on one Windows machine, one per host repo,
on 2026-10-28. Both stopped making progress within three hours, and neither stall cleared
without a person. Run B resumed on 2026-10-29 and stopped four more times.

This plan records every issue the two runs found, with its evidence, what got the runs
moving again, and the fix in the plugin. It is the one plan for this work.

## The runs

- **Machine:** Windows 11, 31.7 GB RAM, Claude Code 2.1.312, sentinel-swarm 0.4.0.
- **Run A:** started 2026-10-28 07:01 UTC on a Python driver-plugin host. The PRD had three
  phases. The host's default test suite was 3,477 tests. Its peak was 19 agents: the
  Oracle, 3 Managers, 4 Leads and 11 Coders. Closed by hand at 20:35 UTC the same day.
- **Run B:** started 2026-10-28 07:20 UTC on a Python host with about 4,500 tests. The PRD
  had six phases. Its peak was 59 agents: the Oracle, 4 Managers, 10 Leads and 44
  Coders. It paused overnight and resumed on 2026-10-29 at 06:05 UTC.
- **Both were live together** for about six hours on 2026-10-28, with an IDE and other
  Claude Code sessions open.

## Timeline (UTC)

### 2026-10-28

| Time | Event |
|---|---|
| 07:01 | Run A starts. |
| 07:20 | Run B starts. |
| 07:4x | The machine has about 1.2 GB free. The daemon retires idle sessions "[low memory]", and Claude Code reaps background shells. |
| 07:49 | Run A's Oracle calls `run_pause`: `~/.claude.json` is corrupt ("JSON Parse error: Unexpected EOF"), so its wake-up to `mgr-p1-settle` cannot be delivered. |
| 08:02 | Run B's ledger parks a Lead under memory pressure. Within 15 s, three claims of its queued Coders are released (issue 8). |
| 08:25 to 08:55 | Three more corrupt copies of `~/.claude.json` are written to `~/.claude/backups/`. Run B's watchdog skips a pass on the parse error at 08:55. |
| 08:46 | A run B Coder stops at a permission prompt. |
| 09:02 | Run B's Oracle asks the user a question (`AskUserQuestion`) and waits. |
| 09:35 to 09:44 | The watchdog files `stuck` findings on run B agents with no activity for 41 to 83 minutes. |
| 10:07 | A run B Manager escalates a ledger deadlock to the Oracle, which is still waiting on the user. Run B's last action for 10 h 49 min. |
| 13:12 | The daemon retires run B's last idle Managers and Leads. Nothing moves until a person looks. |
| 14:35 | Another session sends run A's paused Oracle "continue". Run A moves again. |
| 17:05, 17:49 | Run A's Oracle pauses: the full-suite `tests_run` outlasts the tool call, and `run_finish` refuses while three phases are unapproved. |
| 19:49 to 20:20 | Run A pauses for a rebuild command no role can run. |
| 20:35 | Run A is closed by hand, with SQL on its ledger. |
| 20:56 | A person answers run B's 09:02 question in agent view. Run B moves again. |
| 21:12 | A run B Coder waits on a permission prompt to write a scratch file. |
| 21:15 | A coordinator session fixes run B's stuck ledger records by SQL (directive 48). |
| 21:38 | A run B Coder starts a scratch `pytest` that outlives the Coder (issue 13). |
| 23:21 | A directive from Alex sets run B's goal. The Oracle moves the handoff bench gate to a three-run corpus subset. |

### 2026-10-29 (run B)

| Time | Event |
|---|---|
| 06:05 | Run B resumes from an overnight pause Alex asked for. |
| 06:05 to 07:31 | The daemon retires four Managers. The ledger keeps showing them live (issue 12). |
| about 07:03 | A coordinator removes the p2→p1 phase dependency and ends twelve orphaned agents, both by SQL (directives 68 and 71). |
| about 07:20 | A coordinator stops the scratch `pytest` from 21:38 the night before. |
| 09:23 to 10:57 | The Oracle posts 11 directives (84 to 105) asking the coordinator for graph edits (issue 10). |
| 11:15, 11:16 | The Oracle asks the coordinator for the full run (directive 109), then pauses the run to keep the tree still. No notification fires. |
| before 12:08, about 12:45 | Claude Code kills two coordinator full runs with their shells (issue 6). |
| about 18:08 | Alex finds the stall by asking in another session. |
| 18:10 | A coordinator starts the full run as a separate Windows process. |

Neither run was failing. Each was waiting, and nothing outside agent view showed that.

## Issues

| # | Issue | Run | Status in 0.5.0 |
|---|---|---|---|
| 1 | A session waiting on a person looks idle to the swarm | A, B | [x] Built and tested; live check open |
| 2 | Role files override the user's permission mode | A, B | [x] Built and tested |
| 3 | A paused run tells nobody and does not resume by itself | A, B | [x] Built and tested; live check open |
| 4 | Two runs on one machine exceed its memory | A, B | [x] Built and tested; live check open |
| 5 | Concurrent sessions corrupt `~/.claude.json` | A, B | [x] Retry and pause built and tested; upstream report open |
| 6 | A full test run cannot finish inside the ledger | A, B | [x] Built and tested; live check open |
| 7 | A full test run that can never count as clean | B | [x] Built and tested |
| 8 | Claims and change requests that can never close | B | [x] Built and tested |
| 9 | A binary file can never hand off | A | [x] Built and tested |
| 10 | The graph gate blocks handoffs | A, B | [x] Built and tested |
| 11 | A replacement parent cannot reach its children | A, B | [x] Built and tested |
| 12 | The ledger shows closed sessions as live | B | [x] Built and tested |
| 13 | A released agent's processes keep running | B | [x] Built and tested; live check open |
| 14 | The roles lack tools the work needs | A, B | [x] Built and tested; live check open |
| 15 | Setup friction | A, B | [x] Built and tested |
| 16 | The run panel hides the rows that matter | A, B | [x] Part 1 built and tested; part 2 open |

### What shipped, and what stays open

Every issue has a fix in 0.5.0, covered by unit tests. The checks on the merged branch:
`uv run pytest` 1,462 passed and 6 skipped, `pyright` 0 errors, `ruff check` and
`ruff format --check` clean, `claude plugin test .` 46 passed, and
`claude plugin validate --strict` passed.

What differs from the fixes below:

- **Issue 6:** `tests_run` waits up to 20 s before it returns, so a short file run comes
  back with its result. A detached runner process, `ledger.py testrun`, owns the run, so
  the run outlives the tool call, the worker and the server.
- **Issue 7:** per-scope commands are a `test_command_by_scope` mapping, and
  `test_command` stays a string. A skip is clean when its pytest skip line contains an
  `allowed_skips` text.
- **Issue 8:** run B's released claims were not released by parking. The Lead released
  them itself after its Manager told it to cancel queued Coders. `release_file` now
  refuses while the owner is live, and `agent_release` cancels a queued child.
- **Issue 10:** the method-anchor refusal was the ledger's own check, which searched for
  `Engine.start` as one word. It now matches segment by segment, as codebase-kg does, so
  the interim "accept as incomplete" rule was not needed.
- **Issue 11:** the `SendMessage` hook already read the parent from the ledger. The fault
  was the ledger row, which re-pointing fixes.
- **Issue 1:** the Oracle asks with the new `user_ask` tool. The mod's `pre_ask` gate
  denies `AskUserQuestion` in a background session.

These items are open in `knowledge/prd/16-open-items.md`:

- Live checks of waiting sessions, the write deny before the prompt, `pre_ask`, a
  running Oracle with no listener, clear-check resume, asynchronous test runs,
  process-tree release, machine-wide admission, config retry, and closing a run.
- The upstream report of the `~/.claude.json` corruption (issue 5).
- Answering a permission prompt in the run panel (issue 16, part 2). A hook that holds
  the call keeps Claude Code from showing the prompt, so agent view and the watchdog
  would see the session as busy, not waiting.

Delete this plan once those open items are confirmed in a live run.

### 1. A session waiting on a person looks idle to the swarm

- **Evidence:**
  - `claude agents --json` showed run B's Oracle as `"status": "waiting", "waitingFor":
    "input needed"`, on an `AskUserQuestion` from 09:02. Its transcript has no entry
    between 09:02 and 20:56.
  - A run B Coder showed `"waitingFor": "permission prompt"` from 08:46.
  - The ledger has no record of either. The watchdog filed `stuck` findings on other
    agents, and a Manager's escalation (10:07) went to the waiting Oracle.
  - The watchdog did file `waiting_permission` for `coder-p3-stitched-diagnose-l1b` at
    08:46 (directive 29). It went to the Oracle, which was itself waiting on the user
    from 09:02, so nobody acted on it until 20:58.
  - At 21:12, `coder-p3-stitched-diagnose-l4b` showed `"waitingFor": "permission prompt"`
    on a `Write` to `.claude/scratch/stitched/test_emit_l4.py`, eight minutes after the
    run restarted.
  - Each prompt came from a write or command outside the role's allowed paths. A run that
    writes scratch files keeps raising prompts that only agent view shows.
  - The stall lasted 10 h 49 min, from the last action at 10:07:47 to 20:56:52. In that
    time the watchdog's only action was at 13:12, when the daemon retired six idle
    sessions. It filed no finding, because every agent was `idle` waiting on a wake-up,
    and `_wake_oracle` skips while the Oracle's session counts as running. Its first
    `stuck` findings after the stall came at 20:57, about 650 minutes late.
- **Effect:** every escalation path ends at the Oracle, and the Oracle was waiting on a
  dialog nobody saw.
- **Fix:**
  - The watchdog reads `waitingFor` from `claude agents --json`. A session waiting on
    input or a permission files a `needs_user` finding at once, with the session name
    and the question text.
  - The watchdog files a run-level finding when no agent is working and no wake-up is
    owed for `stuck_minutes`.
  - `notify` (OS toast and `PushNotification`) fires on both findings, not only on
    Driver results and round-3 issues.
  - `status` lists waiting sessions first.
  - In a background run, the Oracle posts a question as a `needs_user` directive and
    keeps the rest of the run moving, instead of blocking on `AskUserQuestion`.
  - The write hook denies a role's write outside its claim, under `.claude/` included,
    before Claude Code can prompt for it. The deny message names the role's claimed test
    file as the place for a scratch helper. A deny returns to the agent at once, and a
    prompt in a background session waits for agent view. Both run B Coder prompts (08:46
    and 21:12) were `Write` calls to `.claude/scratch/stitched/`.

### 2. Role files override the user's permission mode

Alex asked on 2026-10-28 that every role session start in `bypassPermissions`, so that a
trivial prompt does not stop a run.

- **Evidence:**
  - The role templates set `permissionMode: default` for the Oracle, Manager, Lead and
    Driver, and `acceptEdits` for the Coder (`templates/agents/*.md`, line 6).
  - `agentfiles.py:351-353` passes that value as `--permission-mode` on every spawn and
    resume. The flag overrides the user's `permissions.defaultMode`, which on this machine
    is `bypassPermissions` (`~/.claude/settings.json`).
  - Run B's Coders stopped at permission prompts at 08:46 and 21:12 UTC (issue 1), and
    nothing in the run could answer them.
- **Effect:** a role session asks about any tool call its role's rules do not allow,
  even when the user runs every other session without prompts. In a background run nobody
  sees the prompt, so the session waits until a person opens its row.
- **Workaround on both hosts:** the four role files in `.claude/agents/` were edited to
  `permissionMode: bypassPermissions`. `setup` keeps a user's frontmatter values, so the
  edit survives a re-run. It takes effect on the next spawn or resume.
- **Fix:**
  - Add a `permission_mode` setting in `sentinel-swarm.local.md`, per role, like
    `effort`. Empty means inherit: the launcher passes no `--permission-mode`, and the
    session takes the user's `defaultMode`.
  - Make inherit the default for every role. Keep `acceptEdits` as the Coder's floor
    when the inherited mode is `default`, because a `--bg` session in `default` stalls
    for good on a file write (PRD 13).
  - `setup` reports the mode each role will start in.
  - The swarm's limits hold in any mode: the write, shell and `SendMessage` gates are
    `PreToolUse` hooks, and hooks run in `bypassPermissions` too. Add a test that checks
    a Coder in `bypassPermissions` is still refused a write outside its claim and a
    command outside its shell gate.
  - When policy disables bypass (`permissions.disableBypassPermissionsMode`), `setup`
    says so and the role falls back to `acceptEdits`.

### 3. A paused run tells nobody and does not resume by itself

- **A pause fires no notification.**
  - **Evidence:** run B's directive 109 was a `needs_user` directive, and the 11:16 pause
    on 2026-10-29 named what it waited for. No OS notification or `PushNotification`
    fired. Alex found the stall at about 18:08 by asking in another session.
  - **Fix:** a `needs_user` directive and every `run_pause` fire `notify` with the
    reason, and fire it again on the watchdog's timer while the run stays paused.
- **A paused run does not resume when its cause clears.**
  - **Evidence:** run A paused on the corrupt `~/.claude.json` at 07:49 UTC. The file
    parsed again by about 14:30, but the run stayed paused until another session sent
    "continue" at 14:35.
  - **Fix:** a pause records the check that would clear it (for example "the config
    file parses"). The watchdog tries that check on its timer and resumes the run when
    it passes, with a note in the run's events.

### 4. Two runs on one machine exceed its memory

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
  - `parallelism_cap` counts other swarms' sessions; say so in `setup`'s output, and set
    a default from total RAM.
  - `status` shows queued spawns and memory-retired agents in its first lines.

### 5. Concurrent sessions corrupt `~/.claude.json`

- **Evidence:**
  - Run A's Oracle paused on "JSON Parse error: Unexpected EOF".
  - `~/.claude/backups/` holds `.claude.json.corrupted.*` files from 08:25, 08:44 and
    08:55 UTC, 210 to 212 KB each, about the size of a whole file.
  - The ledger server's log shows the same parse error twice.
  - Run B's watchdog skipped a pass at 08:55 on "`~/.claude.json` is corrupted: JSON
    Parse error: Unexpected EOF".
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
    names the newest backup that parses, and the pause clears itself (issue 3).
  - Lower the spawn rate under memory pressure (issue 4), which also lowers the
    number of writers.
  - Report the corruption upstream, with these timestamps.

### 6. A full test run cannot finish inside the ledger

- **Evidence:**
  - `testing.run_tests` has `timeout_s: int = 600` (`mcp/src/swarm_ledger/testing.py:184`).
  - Run B's full suite took about 25 minutes, and the Oracle's full-suite `tests_run`
    timed out with no record. `phase_review` needs a passing full-suite run, so no run B
    phase could close.
  - Measured out of band on run B's branch before launch: 4,524 tests in 2,318 s
    (38.6 min) with `-n 4`, while run A was live. The PRD's bench module adds 407 s on its
    own. Three failures in that run (`test_maproom_site_health`) came from load and
    passed alone (8 of 8 in 54 s).
  - Run A's serial suite took about 582 s, too close to the cap. Its operator wrapped
    `test_command` to run seven parallel shards, which took 209 to 271 s.
  - **A second limit sits in front of the cap.** The MCP tool call itself times out on
    the client before the run ends. Run B's Oracle: "My call timed out on the client
    side, and no record has appeared yet". Run A's Oracle paused at 17:05 UTC because
    "tests_run(scope=full) exceeds the tool timeout". A longer ledger cap alone does not
    let a full run finish.
  - **A run outside the ledger dies with its shell.** On 2026-10-29 the Oracle asked the
    coordinator for the full run at 11:15 (directive 109) and paused at 11:16. The first
    coordinator ran `full_run.py` in a background shell, and Claude Code stopped it before
    12:08 "because the system was critically low on memory". A second coordinator ran it
    in a foreground shell. Claude Code moved it to the background after 10 minutes and
    stopped it there at about 12:45. A run launched through WMI's
    `Win32_Process.Create` also died without a record. Run B sat paused with every phase
    handed up for about seven hours, until a coordinator started the run as a separate
    Windows process at 18:10.
- **Fix:**
  - **`tests_run` returns at once.** The ledger server starts the run as its own child
    process, answers with its `test_run_id`, and records the result when the run ends.
    The caller reads it with `test_run_get`, or gets a wake-up when it is recorded. No
    tool call waits out a suite, and no run depends on a Claude Code shell staying alive.
  - `test_timeout_s` becomes a setting, with 600 as the default. It bounds the test
    process, not the tool call.
  - `setup` times one full run and warns when it exceeds half the cap.
  - The docs name sharding (`pytest -n`) as the way to fit a large suite.
- **Workaround in use until the fix lands.**
  - The Oracle posts a `needs_user` directive (run B's 109 and 119), and the coordinator
    runs the full suite outside the ledger.
  - It runs `full_run.py`, a host script that builds the command with the plugin's own
    `testing.build_command`, runs it with no time cap, and inserts the `scope='full'` row
    under the Oracle with the plugin's count parser and tree fingerprint. It writes the
    row only when the fingerprint is unchanged at the end.
  - It has to run as a detached Windows process (`Start-Process`), for the reasons above.
  - Runs 426 and 464 were recorded this way: 26 to 32 minutes at `-n 8`, with the browser
    tests failing under load.

### 7. A full test run that can never count as clean

- **A full run collects from the repo root.**
  - **Evidence (run B):** `build_command` drops `{target}` for a full run, so the full
    command runs pytest from the repo root with no path. pytest applies `testpaths` only
    when it is invoked from the rootdir, so it walked the whole tree and imported
    `plans/jev/scripts/test_d.py`, a host script that raises `SystemExit` on import. Full
    run 101 recorded `INTERNALERROR`, exit code 3, 0 passed. Adding
    `-c mcp/pyproject.toml --rootdir mcp` did not change that; `--ignore=plans` did,
    collecting the same 4,751 tests as `mcp/tests`.
  - **Fix:** `setup` dry-runs the full command (`--collect-only` where the stack allows
    it) and reports a collection error before the run starts. A full run takes a default
    target, a `full_target` setting, instead of an empty one.
- **Any skip counts as a failure.**
  - **Evidence:** `test_run_passed` and `_CLEAN_TEST_RUN` require `skipped = 0`. Run B's
    bench module skips when its corpus variable is unset, so its file run 203 (13 passed,
    1 skipped) could never pass, and no full run including it could either. A host suite
    with designed skips needs a wrapper.
  - **Fix:** count a skip as clean when the test reports an environment skip the host
    declares in settings, or warn at `setup` that a skip blocks every handoff.
- **Environment in `test_command` applies to every test run.**
  - **Evidence:** run B's `test_command` set `CARTOGRAPHER_REPLAY_CORPUS` so the corpus
    bench would run at each handoff. Every full run then re-judged 16 recorded replays.
    A full run on 16 cores (`-n 8`) took 6 h 29 min and never recorded, so no phase
    could pass `phase_review`. The coordinator then removed a phase dependency in the
    ledger so the next phase could start.
  - **Fix:** add per-scope commands (`test_command.full`, `test_command.file`), and a
    `slow_tests` setting naming tests that run only when a brief asks for them.
    `phase_review` names the scope it needs.

### 8. Claims and change requests that can never close

- **Evidence:**
  - Run B's Oracle reports that a released claim on a path that does not exist can never
    reach `approved` or `superseded`, so its module cannot close. Measured on run B's
    ledger at 21:15 UTC on 2026-10-28: the stray claim is file 20,
    `knowledge/PRD/07-replay.md`, claimed and released at 07:38 by
    `coder-p4-utterances-prd`, which then claimed the real path
    `07-path-tests-and-replay.md` (files 21 and 56).
  - Two issues (5 and 6) ask for files that no module claimed. The write hook denies the
    edit, and no role can add a claim. The ledger shows both paths were claimed: files 25
    (`maproom/js/views/screen.js`), 27 (`knowledge/PRD/09-maproom.md`) and 28
    (`maproom/css/app.css`) were claimed at 07:40 for queued Coders. They were released at
    08:02:07 to 08:02:21, within 15 s of the ledger parking their Lead "under memory
    pressure while its children work" (08:02:03 and 08:02:07). Their Coders started only
    at 08:17 to 08:24, so `who_owns` returned no owner and the write hook denied every
    edit. That parking releases its queued children's claims is a hypothesis from the
    timestamps; the code path is not confirmed.
  - At 21:34, CRs 1 and 3 asked for rows in `knowledge/PRD/06-baseline-and-promotion.md`
    (file 10). `approve(39)` refused with "change request(s) [1, 3] on this file are not
    verified". `cr_complete` refused with "no passing test run or approved handoff for …
    since acceptance". A `.md` file has no `test_path`, and handoff 39 waits on the CRs,
    so neither condition can ever hold.
  - A Manager reports a deadlock on a reviewed handoff (score 80, no disagreement, issue
    closed) that needs an Oracle override. This comes from the agent's message, not from
    a reproduction.
- **Fix:**
  - A released claim on a missing path counts as closed.
  - A Manager may claim an unowned path into one of its modules, which closes the
    missing-claim case without the Oracle.
  - Parking or replacing a parent keeps the claims of its queued and live children.
    Confirm the release path first, from run B's ledger.
  - For a file with no `test_path`, `cr_complete` takes the file's latest submitted
    handoff after acceptance as evidence, so a handoff that waits on the change request
    does not wait forever.
  - Reproduce the handoff deadlock from run B's ledger, and add the case to the tests.

### 9. A binary file can never hand off

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

### 10. The graph gate blocks handoffs

- **Method anchors are refused.**
  - **Evidence:** run A's open issue says "handoff graph check cannot resolve method
    anchors that references require". `graph_upsert` refuses with "anchor symbol
    Engine.<method> not found" for every method anchor of one class, including methods
    that exist. The file could not hand up, and the Lead and Manager spent rounds on it.
  - **Fix:**
    - Reproduce the case against codebase-kg's anchor resolution, for a method anchor
      `path#Class.method`, and fix whichever side is wrong.
    - Until then, a graph-check refusal on an anchor the source shows exists is accepted
      as incomplete, with the reason, instead of blocking the handoff.
- **The own-module rule blocks a node that spans modules.**
  - **Evidence:** a run A Lead could not `graph_upsert` a node whose anchors were a file
    in its module and a file in no module (override 1). The Oracle has no `graph_upsert`,
    so the run needed an override.
  - **Fix:** a Lead may update a node when every anchor it changes is in its module or
    unclaimed. The Oracle gets `graph_upsert` for nodes that no module owns.
- **Each file on a shared node costs a coordinator round trip.**
  - **Evidence:**
    - Between 09:23 and 10:57 on 2026-10-29, run B's Oracle posted 11 directives (84 to
      105) asking the coordinator for graph edits that blocked p2 handoffs. The tests
      behind them passed.
    - Each edit was one of two kinds: add a `path#Symbol` anchor for every public
      top-level `def` and `class`, which `graph_current_for` requires; or re-record one
      file's baseline after a one-line edit, which `stale_files` requires.
    - Every node involved also anchored files outside the Coder's claim. For example,
      `walk_facts` anchors p2's `walkfacts.py` and p4's `test_walkfact_utterance.py`, so
      `graph_upsert` refused the Coder and the Lead. Each round trip took 5 to 25
      minutes.
    - Three of the Oracle's new descriptions were over codebase-kg's 240-character cap,
      which the coordinator learned only from the refusals.
  - **Fix:**
    - A Coder may add `#Symbol` anchors and re-record the baseline for its own claimed
      file on any node, because both belong to one file.
    - The gate's refusal lists the exact anchors to add.
    - The role guidance states the 240-character description cap.

### 11. A replacement parent cannot reach its children

- **A replacement cannot reach the live children of the agent it replaced.**
  - **Evidence:** run A's report, shortfall 7.
    - The Oracle released a dead `mgr-p1-settle` and spawned a replacement.
    - `lead-p1-settle` stayed parented to the released agent's id, both in the ledger
      and in the `SendMessage` hook.
    - `status_tree` named the new Manager, but `message_post` and `SendMessage` from
      the new Manager and from the Oracle both refused the Lead.
    - `brief_create` refused a replacement Lead ("a live agent already holds the name"),
      and `agent_resume` refused because the Lead was running.
    - Nobody in the run could wake the Lead. An outside nudge got it moving again.
  - **Today:** only the Oracle's own resume re-points children. `run_start` runs
    `UPDATE agents SET parent_agent_id = ? WHERE parent_agent_id = ?` and the same on
    `briefs` (`mcp/src/swarm_ledger/ledger.py:338-345`). A Manager or Lead replaced by
    `agent_spawn` gets no such step.
- **An agent whose parent is released cannot be released by anyone.**
  - **Evidence:** run B's directive 71. Only a direct parent may release an agent.
    Twelve agents (a p3 Lead and eleven Coders) were left live under released parents.
    No role could release them, so `brief_create` refused a fresh
    `lead-p3-stitched-surface` because "a live agent already holds the name". The
    coordinator ended them by SQL and stopped their sessions.
- **Fix:**
  - **The re-pointing step works for every role.** When a replacement takes a released
    agent's brief (same `child_name`, or a brief carrying `replaces=<agent_id>`), the
    ledger moves every live child and every brief from the old id to the new one, in
    the same transaction. An orphan then moves under the replacement parent.
  - **The Oracle may release any agent whose parent is released.**
  - **The `SendMessage` hook reads the parent from the ledger** on each call, rather
    than from what it cached at spawn.
  - **A test** releases a Manager with a live Lead, spawns a replacement, and checks
    that `message_post`, `SendMessage` and `agent_resume` all reach the Lead.

### 12. The ledger shows closed sessions as live

- **Evidence:**
  - After the daemon retired run B's sessions for memory on 2026-10-28, the ledger kept
    4 Coders `working` and 3 `handed_up` whose sessions no longer existed. The panel
    showed a column of busy Coders under parked Leads.
  - At 18:09 on 2026-10-29 the ledger listed five Managers and the Oracle as live, and
    the run panel showed all six. `claude agents` showed a process for only the Oracle
    and `mgr-p2-arrival`.
  - The daemon had retired the other four Managers between 06:05 and 07:31. For each,
    the ledger set `parked_at` and `park_reason` ("the daemon retired its session …"),
    but `state` stayed `idle` and `ended_at` stayed empty.
  - The panel and `status_tree` read `state` and `ended_at`, so they showed the four as
    live for more than eleven hours. Their phases were handed up, with no work left for
    them, and nothing released them.
- **Effect:** a person reading the panel cannot tell a running session from a closed
  one, and asks for agents to be stood down that are already gone.
- **Fix:**
  - Retiring a session sets `state` to `parked` in the same write as `parked_at`, and a
    resume sets it back.
  - The watchdog reconciles each agent against `claude agents --json`. An agent whose
    session is gone becomes `parked`, with the reason.
  - The panel and `status_tree` show `parked` as closed and dim it (issue 16).
  - The watchdog releases a parked Manager or Lead whose phase or module is handed up
    or approved after `idle_exit_minutes`, and notes it in the run's events. The Oracle
    briefs a fresh one if a review returns work.

### 13. A released agent's processes keep running

- **Evidence:**
  - A `pytest` of the scratch test `.claude/scratch/stitched/test_emit_l4.py` ran from
    21:38 UTC on 2026-10-28 until a coordinator stopped it at about 07:20 on 2026-10-29,
    after the Coder that started it had been released (directive 55).
  - It used about 50 CPU minutes and wrote 56,5xx files under
    `.claude/scratch/stitched/b4/`.
  - Test run 229 failed on a file it held "busy".
- **Fix:** `agent_release` stops the whole process tree of the agent's session. The
  watchdog reports any process whose ancestor is a released agent's session.

### 14. The roles lack tools the work needs

Run A's report and ledger (issues 1 and 3, deferral 1, overrides 1 to 3, and the Oracle's
pause at 19:49 UTC) show places where the work was done but no role could take the next
step.

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
    put the driver graph's export and build loop behind `build_command` (issue 15).
  - **Fix:** add a `commands` setting, a list of named host commands each with its
    working directory and interpreter. The Coder's and the Manager's shell gates allow
    them, and `setup` proposes the ones the repo's docs name. A failing test whose
    message names a rebuild command is reported with that command.
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

### 15. Setup friction

- **`test_command` runs through `cmd.exe` with `shell=True`.** A forward-slash relative
  path fails there ("'plugins' is not recognized"). `setup` tests the command once,
  through the same runner (with issue 7's collection dry run).
- **A Coder's shell gate allows only test, build and lint commands.** A host whose data
  files go through an export, edit and build loop had to put that loop behind
  `build_command`. The docs name this, and issue 14's `commands` setting replaces it.
- **The smoke test installs and removes a dev marketplace.** Two hosts running
  `scripts/smoke.sh` at the same time would collide. Give each smoke run its own
  marketplace name.
- **`setup` cannot run `/reload-plugins`.** A fresh install needs a new session before
  the tools load. `setup` says so.

### 16. The run panel hides the rows that matter

Alex asked for these on 2026-10-28, after watching both runs in the panel.

- **The Oracle's row shows "No brief recorded." for the whole run.**
  - **Today:** `panel.py:192` fills the row's brief from `run["outcome"]`, which stays
    empty until the run ends. A user reads that as an Oracle with no instructions.
  - **Fix:** show the run's PRD (`runs.prd`) until there is an outcome.
- **A Coder's file path pushes the row off the screen.**
  - **Today:** a file task's name is its full repo path
    (`mcp/src/swarm_ledger/panel.py:173`: `item("file_id", …, f["path"], …)`).
    `taskButton` renders it in a box with `flexShrink={0}` (`hooks/register.tsx:304-306`),
    so on a narrow panel the path takes the row and the agent's name or status is cut
    off.
  - **Fix:** a file task shows its file name only. The full path goes in the task's
    detail lines. When even the file name does not fit, it truncates from the left with
    a leading `…` (Ink `wrap="truncate-start"`), so the end of the name stays visible.
    The task box shrinks before the agent's name does.
- **Rows that are done look the same as rows that are working.**
  - **Today:** a row is dim only when `dimEnded` is on and the agent has an `ended_at`
    (`register.tsx:362`).
  - **Fix:** always dim a row whose agent is released, parked, gone, or finished with
    its task approved (issue 12). Rows that are working keep full colour.
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
      session, and every `needs_user` directive or pause (issues 1 and 3).
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
      instead of calling `AskUserQuestion` (issue 1), and the panel answers it with a
      directive reply.
    - First check how long Claude Code lets a `PreToolUse` hook wait, and what
      happens at that limit. A request still open at the limit falls back to
      Claude Code's own prompt.
- **The legend** (`register.tsx:456-458`) gains dimmed for "ended or not started" and
  the warning glyph for "waiting on you".

## What got the runs moving again

### Run A

- **2026-10-28 14:35 UTC:**
  - The coordinator confirmed `~/.claude.json` parsed (211,217 bytes).
  - It sent the paused Oracle "continue" by `SendMessage`, from another Claude Code
    session on the machine.
  - The message also capped the run at three live sessions below the Oracle, and told
    the Oracle to accept the graph-check refusal (issue 10) as incomplete.
  - Within two minutes the run's state was `active`. The Oracle resumed `lead-p1-settle`
    (`agent_resume`, rekeyed to a new session) and released the idle `mgr-p1-settle` to
    free memory.
  - So a paused Oracle can be woken without agent view, by any session that can message
    it.
- **2026-10-28 20:20 UTC:**
  - The Oracle paused for a command no role could run (issue 14).
  - The coordinator ran it, rebuilding `driver_recipes.db`, and messaged "continue".
  - The run resumed in under a minute.
- **Closed 2026-10-28 20:35 UTC:**
  - The coordinator told the Oracle to release every agent and pause.
  - It committed the work by hand after the full suite passed.
  - It stopped the last two sessions with `claude stop <id>`, and closed the ledger
    with SQL (issue 14).

### Run B

- **2026-10-28 20:56 UTC:**
  - A person answered the Oracle's 09:02 `AskUserQuestion` in agent view ("I fix the
    plugin, continue").
  - Within 48 s the Oracle re-armed its watch and messaged each Manager. The ledger then
    resumed the parked parents and delivered the wake-ups they were owed.
- **2026-10-28 21:15 UTC, by a coordinator session:**
  - It backed up the ledger, then fixed by SQL what no role can: file 20 set to
    `superseded`, and files 25, 27 and 28 set back to `claimed` (issue 8).
  - It ran the bench through the plugin's own `run_tests` with
    `set "CARTOGRAPHER_REPLAY_CORPUS=…" && … pytest … --ignore=plans {target}`: 27 passed,
    2 failed, 0 skipped, in 407 s (issue 7). It asked the Oracle to `profile_set` that
    command.
  - It wrote the host-side full-run recorder, `full_run.py` (issue 6). The Oracle asks
    for it with a `needs_user` directive.
  - It sent all of this as directive 48 and woke the Oracle by `SendMessage`.
  - At 21:12 a run B Coder was waiting on a permission prompt to write a scratch test
    file (issue 1). It needed a person in agent view.
- **Measured, 2026-10-28 21:31 to 21:36 UTC:**
  - The Oracle read directive 48 at 21:31:03 and applied all of it: `profile_set` with
    the new command, issues 5 and 6 closed, and the full-run protocol adopted.
  - p4 `utterances` handed up at 21:33:39, its first phase move since 07:26, once file 20
    no longer held its module open.
  - `coder-p3-stitched-surface-prd09` restarted at 21:35:13 on its restored claim.
  - The Oracle raised one more issue 8 record as directive 49: the change requests on a
    file with no tests. The coordinator checked that the working tree held both requested
    rows, then set CRs 1 and 3 to `completed` by SQL at 21:34, with handoff 39 as the
    evidence. The Oracle routed `cr_verify` to both requesters by 21:35.
- **2026-10-28 23:21 UTC:** a directive from Alex set the goal, and the Oracle moved the
  handoff bench gate to a three-run corpus subset.
- **2026-10-29:**
  - It resumed at 06:05 from an overnight pause Alex asked for.
  - At about 07:03, a coordinator removed the p2→p1 phase dependency and ended twelve
    orphaned agents, both by SQL (directives 68 and 71; issues 7 and 11).
  - From 07:20, a coordinator stopped the stray `pytest` (issue 13) and deleted the
    scratch files and claim-check noise the Oracle listed.
  - From 09:27 to 10:58, it made the p2 graph edits with codebase-kg's
    `kg_upsert_node`, through the shared server (issue 10). It backed up the graph
    before each edit.
  - At 18:10, it started the full run as a separate Windows process (issue 6). Runs 426
    and 464 were recorded this way.

## Closing a run

Run A built and reviewed everything its PRD asked for, but the ledger could not close it.
The Oracle paused twice, at 17:05 and 17:49 UTC: `run_finish` refuses while three phases
are unapproved. Run B's phases hit the same wall. These fixes clear it:

| Blocker | Issue | Fix |
|---|---|---|
| `phase_review` needs a recorded full test run, and `tests_run(scope=full)` outlasts the tool call and any Claude Code shell. | 6 | `tests_run` returns at once, the ledger server runs the suite as its own child, and `test_timeout_s` is a setting. |
| A full run collects outside the test tree, and any skip fails it. | 7 | A `full_target` setting, a collection dry run at `setup`, and declared environment skips. |
| `module_review` refuses a file that never handed off: the binary `android_driver_graph.db`, and `engine.py` and `events.py`, whose graph check refuses `Class.method` anchors. | 9 and 10 | `handoff_submit` hashes binary files and resolves `Class.method` anchors. A phase whose only blocker is a deferred file can hand up. |
| A replacement Manager cannot reach the Lead its predecessor started. | 11 | Re-point live children and briefs to every replacement parent, and route `SendMessage` from the ledger. |
| A file verified another way cannot be settled, and a run cannot be closed by its owner. | 14 | `file_accept_incomplete` and `run_close`. |

**Acceptance:** replay run A's ledger (saved on the host as
`.claude/scratch/swarm-a/ledger-run-a-2026-10-28.db`, before its coordinator integrates the
work) against the fixed plugin. `phase_review` and `run_finish` must then succeed with no
override.

## Order of work

1. **See a stall in minutes, not hours.** Issue 1 (the watchdog reading `waitingFor`, the
   run-level finding, and the write hook's deny), issue 2 (role sessions inherit the
   user's permission mode, which removes most of the prompts issue 1 would report),
   issue 3 (notify on every pause and `needs_user` directive, and resume when the cause
   clears), and issue 16's "Waiting on you" section.
2. **Let a run close.** Issues 6, 7, 9 and 11, issue 10's method anchors, and issue 14's
   `file_accept_incomplete` and `run_close`. Without them no run with a large suite, a
   binary file or a replaced parent can finish. Accept them on run A's saved ledger.
3. **Let the roles finish their own work.** The rest of issue 14 (deleting files, named
   host commands), the rest of issue 10 (cross-module and per-file graph edits),
   issue 12 (closed sessions shown as closed) and issue 13 (process-tree release).
4. Issue 4, admission against machine-wide memory.
5. Issue 5, retrying after a config parse error.
6. Issue 8, the claims and change requests that cannot close, from run B's ledger.
7. Issue 15's setup friction, and the rest of issue 16's panel changes.

When a fix ships, update the matching `knowledge/prd/` document in the same change. Add
each issue that stays open to `knowledge/prd/16-open-items.md`, and delete this plan once
every issue has a fix or an open item.
