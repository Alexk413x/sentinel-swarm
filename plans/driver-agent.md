# Plan: the Driver role

Status: not built. The responsibilities below are Alex's, decided on 2026-09-24. Everything under "Proposed shape" is **(proposed)**. Settle the open questions before building. `SPEC.md` describes the system this role joins.

## What the Driver does

- Runs the app and tests changes on it with the driver plugins: android-driver, ios-driver, and web-driver.
- Takes requests from any role. A request can ask for a build and a refresh, then names the checks to run.
- Keeps a queue of requested work, explores each item, and reports back to the agent that asked, with its findings and the evidence.
- The requester then fixes or tweaks its work and asks again, or records its work as complete.
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
2. One Driver per run, started by the Oracle when the PRD touches a UI, and released at `run_finish`.
3. A ledger tool `drive_request(checks, build=True)` from any role. It adds a row to a `drive_requests` queue and owes the Driver a wake-up.
4. The Driver takes requests in order. For each one, it builds if asked, refreshes the app, runs the checks, and saves evidence under `.sentinel-swarm/evidence/<request_id>/`.
5. A ledger tool `drive_report(request_id, findings, evidence)` records the result and owes the requester a wake-up. The requester reads it with `message_inbox` or a `drive_result` tool.
6. The run report lists each request, its findings, and links to its evidence.

## Questions to settle before building

Ask these one at a time.

### Lifecycle
1. Who starts the Driver, and when? One per run, one per platform, or one per phase?
2. Who stops it? Should it stop when its queue is empty and restart on the next request, to save memory?

### Requests
3. Which roles may send requests? Can a Coder ask before its handoff, to check its own work?
4. What does a request contain: free text, a fixed list of check types, or both?
5. How should the queue work? Options: first in, first out, priorities, and merging identical requests. The device can serve one request at a time.
6. What happens when a request times out, or the build fails?

### Build and devices
7. Who runs the build: the Driver, using `build_command`, or the requester?
8. Who boots and shuts down the emulator, Simulator, or browser? Does the device stay up between requests?
9. Which platforms come first? Android only, then web, then iOS once there's a Mac?

### Reports and gates
10. What does a finding contain: a pass or fail per check, a severity, a screenshot reference?
11. Where does evidence live, how long is it kept, and is there a size limit?
12. Does any gate require a Driver report? For example, `approve` for a UI file, or `phase_review`.
13. Does a failed check open an issue automatically, or only a report the requester acts on?
14. Should Driver evidence count toward the accessibility dimension of the rubric?

### Setup
15. Are the driver plugins loaded only when the host has them installed, like a11y?
16. Which model does the Driver run on?
17. How should the watchdog treat a long driving session, which can look stuck?
18. What is the Driver's session name and color?
