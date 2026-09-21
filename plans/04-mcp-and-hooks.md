# sentinel-swarm: MCP servers and hooks

Status on 2026-09-20: first draft for Alex to review. Alex decided the frame: one ledger MCP tool records everything, hooks call it before each lifecycle stage, enforcement is a hard block, and only the Oracle overrides. The tool names, hook list, and flow below are **(proposed)** unless a line says otherwise.

## The division of work

- **The ledger MCP server holds the state and the rules.** Every gate is a ledger call that refuses a transition when the evidence is missing.
- **Hooks make the ledger unavoidable.** A hook stops an agent from acting outside the ledger: writing a file it does not own, spawning the wrong child, or finishing without a handoff.
- **Agents hold no state.** An agent reads its brief from the ledger and writes its results to the ledger.

## MCP servers

| Server | Status | Purpose |
|---|---|---|
| `swarm-ledger` | Ships with the plugin. Local stdio server, Python, fastmcp, SQLite | State, gates, evidence, messages, analytics |
| `codebase-kg` | Required dependency (Alex) | Find existing code, impact analysis, per-file graph updates |
| `accessibility-tools` | Optional | The Accessibility dimension for UI files |
| `cartographer` | Optional, outside the run | End-to-end testing after a run |
| Atlassian, GitHub | Future | Tracker values for the tracking setting |

## swarm-ledger tools

| Group | Tools | Used by |
|---|---|---|
| Repo | `repo_check` (up to date with `main`, clean tree), `repo_branch_create` | Oracle |
| Run and plan | `run_start`, `run_status`, `profile_set`, `guidelines_get`, `phase_add`, `phase_update`, `plan_unlocked` | Oracle. All roles read |
| Briefs | `brief_create`, `brief_get`, `brief_ack` | A parent creates. A child reads and acknowledges |
| Ownership | `claim_file`, `release_file`, `who_owns` | A Lead claims for its Coders. All roles read |
| Change requests | `cr_open`, `cr_accept`, `cr_complete`, `cr_verify` | Any role opens. The owner completes. The requester verifies |
| Tests | `tests_run` with a scope of file, module, phase, or full | Coder, Lead, Manager, Oracle, each at its own scope |
| Handoff | `handoff_submit` | The child, at the end of its work |
| Review | `score_record`, `review_compare`, `approve`, `return_work`, `accept_incomplete` | The parent |
| Issues | `issue_open`, `attempt_record`, `idea_record`, `issue_escalate` | Parent and child |
| Agreements | `deferral_propose`, `departure_record`, `shortfall_record`, `agreement_decide` | Any role proposes. The responsible level decides |
| Overrides | `override_grant` | Oracle only |
| Versions | `version_save`, `version_restore` | The tooling on handoff. The Coder on a regression |
| Messages | `message_post`, `message_inbox` | All roles |
| Reporting | `status_tree`, `report_build`, `analytics_query` | Oracle, the `status` skill |

`handoff_submit` is the hard check. For a Coder it does four things itself and refuses the handoff when any of them fails:

1. Runs the file's tests and records the result. It fails on zero tests or skipped tests.
2. Checks that the code graph is current for the file.
3. Requires the Coder's own scores and its list of open issues and departures.
4. Saves a version of the file.

`tests_run` executes the project's test command from the project profile. An agent never reports a test result itself.

## Hooks

All hooks live in the plugin's `hooks/hooks.json`, because plugin agents ignore the `hooks` frontmatter field. Each hook reads the caller's `agent_id` and `agent_type` from its input. Commands use the siblings' `python3 ... || python ...` form.

| # | Event | Matcher | What it does | Blocks |
|---|---|---|---|---|
| 1 | `SessionStart` | all | Registers a session-runtime agent. Reports an unfinished run. Checks that codebase-kg is installed, that a graph exists, and that the records folder is excluded from git | No |
| 2 | `PreToolUse` | `Agent` | Spawn gate. The caller's role may create this child role. The model is on the child role's approved list. A brief exists for the child. The parallelism cap has room. It records the parent for the registry | Yes |
| 3 | `SubagentStart` | the four roles | Writes the registry row: id, role, parent, model, phase at start, start time | No |
| 4 | `PreToolUse` | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | Role gate: only a Coder writes. Ownership gate: the path is claimed by this agent, as its file or its test file. Protected paths: nobody hand-writes the records folder | Yes |
| 5 | `PreToolUse` | `Bash`, `PowerShell` | Shell gate for Coders: allows the commands in the project profile, such as test, build, lint, and read-only git. Denies the rest | Yes |
| 6 | `PreToolUse` | `mcp__.*swarm-ledger.*` | Stamps the caller's real identity onto the call. Checks that the role may use the tool. `override_grant` is for the Oracle only | Yes |
| 7 | `PostToolUse` | all | Heartbeat and current activity to the registry. After a Coder's edit, marks the file's test evidence and graph check as stale, so that a handoff needs fresh ones | No |
| 8 | `PostToolUse` | `Bash`, `PowerShell` | Compares the changed files with the caller's claims. A change outside the claim is recorded as a violation and reported to the Lead | No |
| 9 | `PreCompact` | all | Adds one to the agent's context overflow count | No |
| 10 | `SubagentStop` | the four roles | Closes the registry row: end time, tokens and context percentage from the transcript, phase at end. Flags an agent that stopped without a handoff, so that its parent hears about it | To verify |
| 11 | `Stop` | Oracle session | Keeps the Oracle going while the run has unlocked work or pending reviews, unless it waits on the user | Yes |
| 12 | `SessionEnd` | all | Closes the registry row of a session-runtime agent | No |

## Rules and what enforces each

| Rule | Enforced by |
|---|---|
| Only the Coder writes project files | Hook 4. Oracle, Manager, and Lead also have no write tools and no shell in their agent files |
| A Coder writes only its own file and its tests | Hook 4 for the edit tools. Hook 5 and hook 8 for the shell |
| A role spawns only its own child role | Hook 2. The platform ignores the `Agent(type)` allowlist inside a subagent definition |
| A model comes from the approved list | Hook 2 |
| No agent starts without a brief | Hook 2 and `brief_ack` |
| A handoff needs passing tests and a current graph | `handoff_submit` |
| No approval without a handoff and two sets of scores | `approve` refuses |
| The Lead scores before it sees the Coder's scores | `score_record` hides the other set until both exist |
| No agent fakes its identity to the ledger | Hook 6 |
| A rule gives way only to the Oracle | `override_grant`, checked by hooks 2, 4, 5, and 6 |
| Nobody edits the records by hand | Hook 4 |
| Look in the graph before writing | codebase-kg's own search gate hook |

## One file, start to finish

1. The Lead calls `brief_create` and `claim_file` for the file, then spawns a Coder. Hook 2 checks the role, the model, and the brief. Hook 3 registers the Coder.
2. The Coder calls `brief_get` and `brief_ack`. It queries the code graph for existing code.
3. The Coder writes its test file and its source file. Hook 4 allows both paths and denies any other. Hook 7 records each step.
4. The Coder runs its tests through `tests_run` while it works, and reviews its own work until it is satisfied.
5. The Coder updates the code graph for its file, then calls `handoff_submit`. The ledger runs the tests, checks the graph, takes the scores, and saves a version.
6. The Coder returns its report. Hook 10 closes this working period in the registry.
7. The Lead calls `score_record` without seeing the Coder's scores, then `review_compare`.
8. The Lead calls `approve`, `return_work`, or `accept_incomplete`. On a return, it resumes the Coder with `SendMessage`, and the loop repeats from step 3. `attempt_record` classifies the result as improved, plateau, or regression.
9. On approval the ledger releases the claim. The Coder is done.

## Role tool access

| Role | Tools |
|---|---|
| Oracle | Ledger, code graph read tools, Read, Grep, Glob, Agent, AskUserQuestion, web research, Monitor for the watchdog |
| Manager | Ledger, code graph read tools, Read, Grep, Glob, Agent, SendMessage, web research |
| Lead | Ledger, code graph read tools, Read, Grep, Glob, Agent, SendMessage, web research |
| Coder | Ledger, code graph read and write tools, Read, Grep, Glob, Write, Edit, the gated shell, web research, accessibility tools when installed. No Agent tool |

## Watchdog

The Oracle starts a plain script at the start of a run. The script reads the registry's heartbeats and states. It wakes the Oracle only when it finds an agent that is stuck, spinning, or over the context threshold.

## To verify with a prototype

1. Whether a `PreToolUse` hook can rewrite a tool's input, which hook 6 uses to stamp the identity. The fallback is that the hook denies a call whose identity argument does not match.
2. Whether `SubagentStop` can block.
3. Whether `PreCompact` fires inside a subagent.
4. How much time hook 7 adds to every tool call. It must stay small.
5. Whether concurrent writers are safe for the ledger's SQLite file and for the code graph file.

## Decisions from Alex on 2026-09-20

1. **No shell above the Coder.** Oracle, Manager, and Lead use the MCP servers and the plugin's specific tooling only. They cannot create or write files directly or in free form. The ledger's database tracks their responses.
2. **Coder shell and file locks.** A hook checks a Coder's shell use. The ledger's claims are also the file lock: `who_owns` tells any agent which agent holds a file, so it knows whom to ask when it needs to coordinate. The `status_tree` view shows every lock with its owner.
3. **The Oracle keeps working.** Hook 11 holds the Oracle until every item has a passing score or has reached diminishing returns, on all fronts, or until the run waits on the user.
