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
| Directives | `directive_submit`, `directive_inbox`, `directive_resolve` | Any caller submits. The Oracle reads and resolves |
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

It reports through the directive channel. See "User directives".

## User directives

From Alex on 2026-09-21:

- The ledger has a tool that sends a user directive to the Oracle. A directive steers the run: the plan, a future phase, the guidelines, or anything else the Oracle owns.
- A directive can come from any input: the user types it in the Oracle's chat, or an outside agent or session sends it.
- A directive does not interrupt the agents. Their work continues, and the Oracle applies the directive through the normal path.
- The same mechanism carries the user's reply to an escalation.

Proposed mechanics: **(proposed)**

- Tools: `directive_submit` (any caller), `directive_inbox` and `directive_resolve` (Oracle only).
- A directive records its text, its source (`user-chat`, `outside-session`, or `skill`), the sender's name, and an optional `reply_to` that points at a notification or an escalation.
- The Oracle is the only reader. It never forwards a directive as-is. It turns it into a plan change, a guideline change, a new brief, or a change request, so the lower layers see it through the cycle they already follow.
- The Oracle resolves each directive with an outcome: applied, scheduled for a later phase, declined with a reason, or needs a question to the user. The final report lists every directive and its outcome.
- The Oracle reads its inbox at safe points: between reviews, at a join point, and when the watchdog wakes it for a new directive. Work that is already approved is not reopened unless the directive says so.
- A reply with `reply_to` closes the matching notification and unblocks the work that waited on it.

Decision from Alex on 2026-09-21: every directive carries full authority, whatever its source. The Oracle acts on it without a confirmation step. The reason: the same channel also carries what the watchdog reports, so the Oracle can correct what the watchdog sees when it pokes an idle Oracle.

- The watchdog sends its reports through `directive_submit`, with the source `watchdog`. **(tool wiring proposed)**
- The source and the sender's name are still recorded, so the final report shows where each directive came from. **(proposed)**

## To verify with a prototype

1. Whether a `PreToolUse` hook can rewrite a tool's input, which hook 6 uses to stamp the identity. The fallback is that the hook denies a call whose identity argument does not match.
   - Decision from Alex on 2026-09-21: the identity is the agent's name, not its `agent_id`. The name is set when the agent starts, it does not change, and it is the address that other agents message. Every agent knows its own name, so the fallback works: the agent passes its name, and hook 6 checks it against the registry.
   - To verify: how hook 6 links the `agent_id` in its input to the name. Hook input carries `agent_id` and `agent_type`, not the name. Candidate: the first `brief_ack` binds the name to the `agent_id`. The ledger refuses the bind when the name has no pending brief under that parent, or when another live agent holds it. **(proposed)**
2. Whether `SubagentStop` can block.
3. Whether `PreCompact` fires inside a subagent.
4. How much time hook 7 adds to every tool call. It must stay small.
5. Whether concurrent writers are safe for the ledger's SQLite file and for the code graph file.
6. Whether three nested subagent layers work, and whether a Lead can resume its Coder with `SendMessage`. The hierarchy and step 8 of "One file, start to finish" depend on it. **(proposed)**
7. Whether `kg_upsert_node` re-baselines the file's hash. If it does not, the graph-current check in `handoff_submit` never passes after a Coder's update. **(proposed)**
8. Whether `tests_run` for one file stays reliable while other Coders have half-written files in the same working tree. **(proposed)**
9. How hook 8 attributes a changed file to one agent while other Coders edit at the same time. **(proposed)**
10. The time that the `python3 ... || python ...` form adds on Windows, where `python3` can resolve to the Store stub. Measure it as part of check 4. **(proposed)**

Design point for hook 11: "waiting on running children" is a valid stop state, next to "waiting on the user". Without it, the Oracle spins while its Managers work. **(proposed)**

## Plugin features found on 2026-09-21

Read from `code.claude.com/docs/en/plugins-reference.md`, through a summarizing fetch. Each is a candidate, not a decision. **(proposed)**

- **`dependencies` in `plugin.json`.** A required dependency. Claude Code fails the install or the enable when it is missing. The manifest declares codebase-kg this way.
- **`monitors/monitors.json`.** A plugin can ship background monitors. This is a candidate home for the watchdog, in place of a script that the Oracle starts.
- **`subagentStatusLine` in the plugin's root `settings.json`.** A status line format for the plugin's agents, such as `[${agent.name}] ${status}`. This is a candidate for marking each agent clearly with its role and name.
- **`agent` in the plugin's root `settings.json`.** A default agent configuration that applies when the plugin is enabled. This is a candidate for running the session as the Oracle.
- **`userConfig` in `plugin.json`.** Typed settings that Claude Code prompts for and stores in user settings. Project settings files cannot set them. The per-project `.claude/sentinel-swarm.local.md` file stays the home for per-repo values, such as the test command.

## Decisions from Alex on 2026-09-20

1. **No shell above the Coder.** Oracle, Manager, and Lead use the MCP servers and the plugin's specific tooling only. They cannot create or write files directly or in free form. The ledger's database tracks their responses.
2. **Coder shell and file locks.** A hook checks a Coder's shell use. The ledger's claims are also the file lock: `who_owns` tells any agent which agent holds a file, so it knows whom to ask when it needs to coordinate. The `status_tree` view shows every lock with its owner.
3. **The Oracle keeps working.** Hook 11 holds the Oracle until every item has a passing score or has reached diminishing returns, on all fronts, or until the run waits on the user.
