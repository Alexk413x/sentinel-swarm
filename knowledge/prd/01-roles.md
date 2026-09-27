# Principles and roles

## Principles for every role

1. **Exclusive ownership.** Every unit of work has exactly one owner: a run, a phase, a
   module, or a file. Nobody else changes it.
2. **Non-owners request, owners change.** An agent that needs a change in work it does
   not own files a change request. The owner makes the change. The requester verifies
   it.
3. **Planning prevents conflicts.** Each layer plans its breakdown so that two owners
   rarely need the same file. Change requests are the fallback.
4. **Every layer reviews.** No layer is skipped, even for a single hello-world file.
5. **Evidence, not claims.** A role reports work as complete only with recorded
   evidence: test runs, scores, and reviews. A later reader can confirm it from the
   records alone.
6. **Small scope, visible work.** A Coder holds one file. Each agent's context stays
   focused, and each agent's work is easy to see.
7. **No state lives only in an agent.** **(proposed)** Every plan, brief, review,
   score, and decision is a ledger record, so a replacement agent or a later run can
   continue from the records.
8. **Only the Coder writes project files.** Every other role works through the ledger.
9. **No telephone game.** Every plan and task is a ledger record. A spawn prompt
   carries only the child's name. A wake-up message only points at a record. An agent
   re-reads its brief from the ledger before each handoff and after each return,
   instead of trusting its memory.
10. **Enforcement lives in hooks and ledger gates, not in prompt text.** A prompt says
    what a role must not do. A hook or a ledger gate stops it. Enforcement is a hard
    block, and only the Oracle overrides a rule.

## Roles

### Hierarchy

| Role | Count | Owns | Started by | Reports to | Agent file | Color |
|---|---|---|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user, through the launcher | The user | `swarm-oracle` | cyan |
| Manager | 1 per phase | One phase | Oracle | Oracle | `swarm-manager` | green |
| Lead | 1 per module | One module | Manager | Manager | `swarm-lead` | purple |
| Coder | 1 per file | One file and its unit test file | Lead | Lead | `swarm-coder` | orange |
| Driver | 1 per exploration | One exploration: building, exploring, and its findings | Oracle | Oracle | `swarm-driver` | yellow |

- The minimum run is one of each of the first four roles. The Driver exists only when
  the host has cartographer and a driver plugin installed; `setup` writes
  `swarm-driver.md` only then. See "Driver" below.
- Each role starts only the role below it. The Oracle starts Managers and, through
  `drive_request`, Drivers; a Manager starts Leads; a Lead starts Coders. A Coder and a
  Driver start no ledger child.
- No role has the `Agent` or `Workflow` tool except the Driver, whose `Agent` tool runs
  only cartographer's own `map-driver` and `map-reviewer` subagents. The `pre_agent`
  hook denies an `Agent` call from every other swarm session, and denies any other
  `subagent_type` from a Driver session too.
  `mcp/tests/test_plugin_surface.py` checks the template surface for every role.
- A Lead reviews its Coders' work itself.
- No role template sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.

### Names

| Role | Name pattern **(proposed)** | Example |
|---|---|---|
| Oracle | `oracle` | `oracle` |
| Manager | `mgr-<phase>` | `mgr-p2-api` |
| Lead | `lead-<phase>-<module>` | `lead-p2-auth` |
| Coder | `coder-<phase>-<module>-<file>` | `coder-p2-auth-login` |
| Driver | `driver-e<exploration number>` | `driver-e2` |

- The Driver's pattern is decided; the **(proposed)** mark covers the other rows.
  `drive_request` sets it from the exploration's ordinal in the run.

- A name is unique among the live agents of a run. It is the address in the ledger, and
  the agent passes it as `caller` on every ledger call. **(proposed)**
- A session name is unique on the machine and is the address for `SendMessage`. A
  child's session name is `<repo slug>-r<run_id>-<MMDDHHMM>-<name>`, with the run's UTC
  start time, because a stale session keeps its old name and a rebuilt host restarts at
  run 1. **(proposed)** The Oracle's session name is
  `<repo slug>-oracle-<MMDD-HHMMSS>`, from its launch time.

### Models

| Role | Default model (agent file `model`) | Approved list (settings `models`) |
|---|---|---|
| Oracle | opus | opus, fable |
| Manager | opus | opus |
| Lead | sonnet | opus, sonnet |
| Coder | sonnet | sonnet, haiku |
| Driver | sonnet | sonnet, opus **(proposed)** |

- The settings file holds the approved models per role. `brief_create` refuses a model
  outside the child role's list.
- Each agent file's `model` key is the role's default. The launcher starts the Oracle on
  its file's model, or on the first entry of the Oracle's approved list when the file
  has no `model`. `agent_spawn` passes the brief's model with `--model`.
- The parent picks the child's model from the child role's list, by the complexity of
  the task, and the brief records the choice. Ideally, work reaches a Coder broken down
  far enough to run on the cheaper model.
- Escalation can raise the model: a fresh Coder in round 2 or 3 can run on the stronger
  model in its list. **(proposed)**
- `drive_request` picks the Driver's model itself, from the first entry of its approved
  list, since the Oracle's call carries no `model` argument. **(proposed)**
- The registry records the model of each agent. **(proposed)** For the Oracle's row,
  `run_start` records the model the launcher uses, by the same rule. A new Oracle
  session that resumes the run records it the same way.

### Oracle

- **Does:** reads the PRD and asks the user about it. Checks the repo and creates the
  run's branch. Sets the test, build, and lint commands and the high-level guidelines.
  Plans the work as a dependency graph of phases. Starts one Manager per unlocked phase.
  Audits the scores and reviews from the layers below and investigates the low ones.
  Runs the full test suite after each phase hands up. Reviews and approves each phase.
  Decides departures at its level. Resolves directives. Arms the watchdog listener.
  Grants overrides. Decides between a notification now and a line in the final report.
  Requests each Driver exploration and starts fixes from its findings. Writes the final
  report and finishes the run.
- **Owns:** PRD meaning, acceptance criteria, the guidelines, the phase plan, the final
  verdict, and all communication with the user.
- **Arbitrates:** disputes between Managers, including which Manager owns a shared
  file. **(proposed)**
- **On a low score or a failed full run:** returns the work to its Manager, opens a new
  phase, or accepts the result with a recorded reason. **(proposed)**
- **Escalates to the user:** only an issue that research and rework cannot solve.
- **Must not:** write a project file, start a subagent, score a file, direct a Lead or a
  Coder, or report a test result from its own reading.
- **Done when:** every phase is approved, the full suite passes, the evidence is on
  file, and `run_finish` accepts the run.
- **Tools:** Read, Grep, Glob, AskUserQuestion, ToolSearch, WebSearch, WebFetch,
  SendMessage, Monitor (the watchdog call only), PushNotification (the notifications
  the ledger records for the user) **(proposed)**, the ledger, and the codebase-kg read
  tools. No write tool and no shell.

### Manager

- **Does:** breaks its phase into modules (`module_add`). Starts one Lead per module.
  Agrees with other Managers on one owner for any shared file before a Lead claims it.
  Reviews each Lead's result: whether the Lead met its tasks, and the files whose self
  and Lead scores disagreed. Runs phase-scope tests on each module's target. Decides
  departures at its level. Records `module_review`, releases each accepted Lead, and
  hands the phase up to the Oracle.
- **Owns:** the phase, its module breakdown, the boundaries between its Leads, and test
  health across its modules.
- **Arbitrates:** disputes between its Leads, and the contracts between its modules.
  **(proposed)**
- **On a regression:** starts a new Lead to fix it.
- **Must not:** write a project file, score a file, or direct a Coder without going
  through the Coder's Lead. **(proposed)**
- **Done when:** the Oracle approves the phase.
- **Tools:** Read, Grep, Glob, ToolSearch, SendMessage, WebSearch, WebFetch, the
  ledger, and the codebase-kg read tools.

### Lead

- **Does:** breaks its module into per-file tasks. A task is a pair: the source file and
  its unit test file, owned by one Coder. Claims each file, briefs one Coder per file,
  and starts it. Scores each handoff blind, compares, and approves, returns, or accepts
  it as incomplete. Classifies each fix attempt. Decides departures first. Runs the
  module's tests when every file is settled, and reports to its Manager.
- **Owns:** the module, the file assignments inside it, the contracts between its files,
  and the approval of each Coder's work.
- **Arbitrates:** disputes between its Coders about contracts and about where a shared
  function belongs. **(proposed)**
- **Must not:** write a project file, or read a Coder's scores before it records its
  own.
- **Done when:** its Manager accepts the module with `module_review` and releases it.
- **Tools:** the same as the Manager.

### Coder

- **Does:** searches the code graph for existing code before it writes. Writes the test
  file first when it can, then the source file. Test-first is the expected practice,
  not a gate. Runs its tests through `tests_run` until they pass. Updates the code graph
  for its file with `graph_upsert`. Scores its own work, fixes it, and scores again
  until it is satisfied; this loop has no cap and is not an escalation attempt. Hands
  off. Works with its Lead and other Coders on contracts, through the ledger.
- **Owns:** one file and its unit test file. "File" means any project file the run
  touches: new or existing, code or configuration. **(proposed)**
- **Tests cover:** the happy path, the known possible edge cases such as API errors,
  and error handling that catches the specific error types plus a catch-all.
- **Must not:** edit a file it does not own. It files a change request instead.
- **Done when:** its Lead approves the file or accepts it as incomplete.
- **Tools:** Read, Grep, Glob, Write, Edit, Bash and PowerShell (gated), ToolSearch,
  SendMessage, WebSearch, WebFetch, the ledger, and the codebase-kg read tools. It
  changes the graph only through `graph_upsert`.

### Driver

- **Exists only when:** the host has `cartographer@cartographer` installed and at least
  one of `android-driver@accessibility-tools`, `ios-driver@accessibility-tools`, or
  `web-driver@accessibility-tools`. `setup` writes `swarm-driver.md` only then;
  `drive_request` refuses clearly when either is missing. When the plugins are missing
  or fail to load, the run skips every exploration step, and the report needs no
  special entry for it.
- **Started by:** the Oracle only. Only the Oracle sends requests, with
  `drive_request`. The Driver starts when the request arrives, and it shuts down when
  its exploration ends. Each session is named `driver-e<exploration number>` after the
  session-name prefix above, for example `myapp-r1-09251430-driver-e2`.
- **Does:** builds the app with `build_command`, boots the device, replays recorded
  routes with cartographer's `map-test`, then explores toward the Oracle's focus list
  with `map-explore` through cartographer's own `map-driver` and `map-reviewer`
  subagents. Records each finding with `drive_issue` as it is found, checks in with
  `drive_checkin` every 30 minutes, shuts the device down, and ends the exploration
  with `drive_done`, which releases and stops its own session. When a failed build or a
  device that will not boot stops it, it records a finding and passes `blocked` to
  `drive_done`, which notifies the user. **(proposed)**
- **Tests the app:** the Driver explores, tests, and records findings. Every other role
  does unit testing only, because the app cannot build while other edits are in
  progress.
- **Devices:** it boots the emulator, Simulator, or browser at the start of each
  exploration and shuts it down when the exploration ends, so no device holds memory
  between explorations.
- **Platforms:** Android first, then web, then iOS once a Mac is available.
- **Works through cartographer:** it does not drive the device itself. cartographer's
  `map-explore` and `map-test` do, and cartographer records each run and its evidence in
  `knowledge/cartographer/runs/<run-id>/`. The Driver turns cartographer's findings into
  ledger findings for the Oracle.
- **Subagents:** it is the one role allowed subagents, and only cartographer's own:
  `map-driver` and `map-reviewer`. The Oracle does not run them, so its context stays
  small.
- **Owns:** one exploration: the build, the device session, and every finding it
  records, with evidence in cartographer's run folder.
- **Must not:** write or edit a project file. It has no Write or Edit tool. It must not
  fix anything; it observes and reports, and the Oracle turns its findings into a fix
  wave.
- **Done when:** it calls `drive_done`.
- **Tools:** Read, Grep, Glob, ToolSearch, SendMessage, Bash and PowerShell (gated to
  `build_command`), Agent (gated to cartographer's `map-driver` and `map-reviewer`
  subagents only), Skill **(proposed)**, the ledger, and the codebase-kg read tools. No
  Write or Edit. cartographer's MCP server and the installed driver plugins' servers
  join its session only, the same way the a11y servers join every role's: see
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).

See "Explorations" in [02-run-lifecycle.md](02-run-lifecycle.md) for the request queue,
the finding shape, the stop rules, and the loop between exploration and fix waves.
