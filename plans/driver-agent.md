# Plan: the Driver role

Status: not built. The responsibilities below are Alex's, decided on 2026-09-24. Everything under "Proposed shape" is **(proposed)**. Settle the open questions before building. `knowledge/prd/` describes the system this role joins.

## What the Driver does

- Runs the app and tests changes on it with the driver plugins: android-driver, ios-driver, and web-driver.
- Takes requests from the Oracle only, between waves, when no Coder is editing. It builds the app, explores and tests it, and records every issue it finds, with evidence.
- The Oracle turns the issues into a new wave of fixes. Managers, Leads, and Coders work that wave with unit tests only.
- The loop repeats until the Driver finds nothing: wave, explore, fix wave, explore.
- It does not edit project files. It observes and reports.
- It is the only role that loads the driver plugins.

## What we know

### The driver plugins

- All three come from the `accessibility-tools` marketplace.
- android-driver and ios-driver each ship one MCP server (`android-driver-kg`, `ios-driver-kg`). It serves a knowledge graph that maps actions to commands, and driving tools: `driver_open`, `driver_launch`, `driver_run`, `driver_read`, `driver_state`, `driver_wait`, `driver_batch`, and `driver_close`, plus `driver_controls` and `driver_shell` on Android.
- web-driver drives a browser through the `webctl` CLI, built on Playwright. Its MCP server serves only the knowledge graph.
- Their skills cover capture: UI state, screenshots, screen sequences, video, logs (logcat, the iOS log stream), performance, settle-waits, and screen-reader speech checks. They also cover driving: `driver-iosctl`, `driver-webctl`, and the TalkBack and VoiceOver toggles.
- iOS needs macOS for the Simulator. Screen-reader speech checks need a real device.
- On 2026-09-25, the Android emulator (`qemu`) used about 3.8 GB of memory on Alex's machine.

### How the swarm runs today

- Every role runs as its own Claude Code session with `--strict-mcp-config`, so a session loads only the MCP servers its config lists. `agent_spawn` builds that config in `mcp/src/swarm_ledger/agentfiles.py`. The a11y servers join when the host has a11y installed, and the Driver's servers can follow the same pattern.
- Each role starts only the role below it: the Oracle starts Managers, a Manager starts Leads, and a Lead starts Coders. The Driver sits outside that chain.
- The ledger already has the pieces a request queue needs: messages, owed wake-ups enforced by the Stop hook, change requests that close only with evidence, test runs, and a records folder for evidence files.
- The settings file already has a `build_command`.
- The rubric has an accessibility dimension. The Driver's a11y evidence could feed it.

## Proposed shape **(proposed)**

1. A fifth role, `driver`, with its template in `templates/agents/driver.md`. It has no Write or Edit tool, and a shell limited to the driver CLIs and the build command.
2. One Driver per run. The ledger starts it when the Oracle's first `drive_request` arrives, and stops it when the exploration ends, before the next wave starts.
3. A ledger tool `drive_request(focus)` that only the Oracle may call, when the previous exploration's fixes have all finished. `focus` lists what to explore: the PRD's features, the last wave's changes, and earlier issues to recheck.
4. The Driver builds with `build_command`, launches the app, explores the focus list, and saves evidence under `.sentinel-swarm/evidence/<request_id>/`.
5. A ledger tool `drive_issue(request_id, issue, evidence)` records each issue as the Driver finds it and owes the Oracle a wake-up at once; `drive_done(request_id)` ends the exploration.
6. The Oracle starts fixes for each issue as it arrives, and requests the next exploration once the exploration and every fix have finished.
7. The run finishes after an exploration that records no issue, or once the loop stops on lack of progress and the user decides what to do with the issues left.
8. The run report lists each exploration, its issues, the wave that fixed each one, and links to the evidence.

## Decisions

- Decided by Alex on 2026-09-25: the Driver starts when a request arrives, and it shuts down when its exploration ends.
- Decided by Alex on 2026-09-25: fixes start while the Driver still explores. The Oracle sees each issue as the Driver records it and starts work on it at once. When a fix phase for that module is already running, a Coder joins it; otherwise the Oracle starts a new Manager, Lead, and Coder for that bug. When the exploration ends and every fix agent has finished, the Driver builds and retests.
  - **(proposed)** detail: the Oracle asks a running module for a new Coder through its Manager, which asks the Lead, so the spawn order holds.
  - **(proposed)** detail: the Driver tests the app it built at the start of the exploration, while Coders edit the source. It must test a built snapshot, never a dev server that reloads on edits.
  - **(proposed)** detail: a failed build is recorded at once as an issue with the build log, which ends that exploration; the fixes start from it, and the retest starts with a build.
- Decided by Alex on 2026-09-25: no time limit on an exploration. Every 30 minutes the Driver reports its progress to the Oracle and keeps working. The Oracle reviews the testing done and the steps taken so far, looking for problems that are not obvious bugs, and starts fixes for them. It also decides whether the Driver is stuck; if so, it stops the Driver and starts a fix for whatever blocked it.
  - **(proposed)** detail: a `drive_checkin(request_id, covered, steps, notes)` ledger tool records the check-in and owes the Oracle a wake-up; the watchdog does not report a Driver as stuck while its check-ins arrive on time.
- Decided by Alex on 2026-09-25: the loop stops on lack of progress, not a fixed count.
  - A bug that is still there after 3 fix attempts in a row stops getting fixes and goes to the user with its evidence. A bug gets at most 5 fix attempts in all.
  - The loop stops when 3 fix waves in a row fix nothing, and the Oracle reports what is left to the user.
  - The loop also watches for patterns of bugs, and on one the Oracle pauses fixes in that area and reports it.
  - **(proposed)** detail: each Driver issue carries a fingerprint (the check, the location, and the symptom), so an exploration can mark every earlier issue fixed, still there, or new. The patterns are: a regression (a fixed bug returns); fixes causing bugs (new issues in the files the last fix wave touched, at least as many as it fixed); and ping-pong (two bugs that take turns coming back).
- Decided by Alex on 2026-09-25: every wave starts from a new plan and new agents, including a fix wave planned from a Driver exploration. A review fix inside a wave, such as a return or a pushback, resumes the existing agents, as today. **(proposed)** detail: the Oracle groups the issues by module, one fix phase per module, and each Coder's brief carries the issue and its evidence and re-claims the file.
- Decided by Alex on 2026-09-25: each request carries a focus list the Oracle writes. The first exploration covers every PRD feature. Each later one covers the features the last wave touched, every open issue to recheck, and a quick smoke pass over everything else. The final clean exploration is a full pass.
- Decided by Alex on 2026-09-25: the Driver boots the emulator, Simulator, or browser at the start of each exploration and shuts it down when the exploration ends, so no device holds memory between explorations.
- Decided by Alex on 2026-09-25: Android first, then web, then iOS once a Mac is available.
- Decided by Alex on 2026-09-25: every finding has a fixed shape, filled as fully as possible:
  - a fingerprint (the check, the location, and what it saw), used to spot repeats and patterns;
  - a title;
  - the steps to reproduce;
  - the expected and actual result;
  - a severity: blocker, major, or minor;
  - the area: the screen, and the module or file when the Driver can tell;
  - the evidence: screenshots before and after, the UI tree, and log excerpts.
- Decided by Alex on 2026-09-25: the Driver works through cartographer instead of driving the device itself. cartographer already explores toward a goal (`map-explore`), records each run with its evidence in `knowledge/cartographer/runs/<run-id>/`, replays recorded routes with no AI (`map-test`), diffs against functional, visual, and performance baselines, and diagnoses failures (`map-heal`). The Driver turns cartographer's findings into ledger issues for the Oracle.
- Decided by Alex on 2026-09-25: the Driver is the one role allowed subagents, and only cartographer's own: `map-driver` and `map-reviewer`. Every other role still has no Agent tool. The Oracle does not run them, so its context stays small over a long run.
  - **(proposed)** detail: the Oracle's focus list becomes `map-explore`'s goal; a recheck after a fix wave replays the recorded routes with `map-test` first, then explores only what changed.
  - **(proposed)** detail: cartographer's regression signals replace most of the fingerprint design above; the Driver keeps the fingerprint only to match an issue across explorations.
- Decided by Alex on 2026-09-25: only the Oracle sends requests. The Driver explores, tests, and records issues; the Oracle starts a new wave of fixes from them; this repeats until everything works. Every other role does unit testing only, because the app cannot build while other edits are in progress.

## Questions to settle before building

Ask these one at a time.

### Lifecycle
1. ~~Who starts the Driver, and when?~~ Decided: when the Oracle requests it, between waves.
2. ~~Who stops it?~~ Decided: it stops when its exploration ends.

### Requests
3. ~~Which roles may send requests?~~ Decided: the Oracle only.
4. ~~What does the Oracle's request contain?~~ Decided: a focus list the Oracle writes.
5. ~~How should the queue work?~~ Settled: one requester, one exploration at a time.
6. ~~What happens when the build fails, or an exploration runs long?~~ Decided: a failed build is an issue, fixed at once. No time limit: the Driver checks in every 30 minutes, and the Oracle reviews and decides whether to stop it.
6a. ~~When does the loop stop?~~ Decided: on lack of progress. See Decisions: 3 attempts in a row per bug, 5 in all, 3 waves in a row with no fix, and bug patterns.
6b. ~~How does the Oracle turn issues into a fix wave?~~ Decided: new agents for a fix wave; resumed agents for a review fix.

### Build and devices
7. ~~Who runs the build?~~ Settled: the Driver, with `build_command`, since nothing is editing.
8. ~~Who boots and shuts down the device?~~ Decided: the Driver boots it at the start of each exploration and shuts it down when the exploration ends.
9. ~~Which platform comes first?~~ Decided: Android, then web, then iOS on a Mac.

### Reports and gates
10. ~~What does a finding contain?~~ Decided: a fixed shape, filled as fully as the Driver can.
11. ~~Where does evidence live?~~ Settled: in cartographer's run folders, `knowledge/cartographer/runs/<run-id>/`, with its committed baselines.
12. ~~Does any gate require a Driver report?~~ Settled: `run_finish` requires a final exploration with no issue, or the user's decision once the loop stops.
13. ~~Does a failed check open an issue?~~ Settled: every finding is recorded as an issue for the fix wave.
14. ~~Should Driver evidence count toward the accessibility dimension?~~ Settled: cartographer already runs the a11y checks on every capture; its a11y findings become issues like any other.

### Setup
15. Are the driver plugins loaded only when the host has them installed, like a11y?
16. Which model does the Driver run on?
17. ~~How should the watchdog treat a long driving session?~~ Settled: the 30-minute check-ins show it is alive, and the Oracle judges whether it is stuck.
18. What is the Driver's session name and color?
