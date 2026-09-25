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
3. A ledger tool `drive_request(focus)` that only the Oracle may call, and only while no phase is working. `focus` lists what to explore: the PRD's features, the last wave's changes, and earlier issues to recheck.
4. The Driver builds with `build_command`, launches the app, explores the focus list, and saves evidence under `.sentinel-swarm/evidence/<request_id>/`.
5. A ledger tool `drive_report(request_id, issues, evidence)` records each issue as a ledger issue with its evidence, and owes the Oracle a wake-up.
6. The Oracle plans a fix wave from the open Driver issues, as new phases, and requests another exploration when that wave ends.
7. The run finishes only after an exploration that records no new issue.
8. The run report lists each exploration, its issues, the wave that fixed each one, and links to the evidence.

## Decisions

- Decided by Alex on 2026-09-25: the Driver starts when a request arrives, and it shuts down when its exploration ends, so it never runs while a wave edits files.
- Decided by Alex on 2026-09-25: only the Oracle sends requests. The Driver explores, tests, and records issues; the Oracle starts a new wave of fixes from them; this repeats until everything works. Every other role does unit testing only, because the app cannot build while other edits are in progress.

## Questions to settle before building

Ask these one at a time.

### Lifecycle
1. ~~Who starts the Driver, and when?~~ Decided: when the Oracle requests it, between waves.
2. ~~Who stops it?~~ Decided: it stops when its exploration ends.

### Requests
3. ~~Which roles may send requests?~~ Decided: the Oracle only.
4. What does the Oracle's request contain: the whole PRD to explore, only the last wave's changes, or a focus list it writes?
5. ~~How should the queue work?~~ Settled: one requester, one exploration at a time.
6. What happens when the build fails, or an exploration runs too long? A failed build could itself become the first issue of the fix wave.
6a. When does the loop stop: when an exploration finds no new issue, or after a maximum number of fix waves, with the rest reported to the user?
6b. How does the Oracle turn issues into a fix wave: new phases grouped by module, or reopening the files that caused them?

### Build and devices
7. ~~Who runs the build?~~ Settled: the Driver, with `build_command`, since nothing is editing.
8. Who boots and shuts down the emulator, Simulator, or browser? Does the device stay up between requests?
9. Which platforms come first? Android only, then web, then iOS once there's a Mac?

### Reports and gates
10. What does a finding contain: a pass or fail per check, a severity, a screenshot reference?
11. Where does evidence live, how long is it kept, and is there a size limit?
12. ~~Does any gate require a Driver report?~~ Settled: `run_finish` requires a final exploration with no new issue, if the loop-stop rule in 6a says so.
13. ~~Does a failed check open an issue?~~ Settled: every finding is recorded as an issue for the fix wave.
14. Should Driver evidence count toward the accessibility dimension of the rubric?

### Setup
15. Are the driver plugins loaded only when the host has them installed, like a11y?
16. Which model does the Driver run on?
17. How should the watchdog treat a long driving session, which can look stuck?
18. What is the Driver's session name and color?
