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
| Code graph | `graph_upsert`, the same arguments as `kg_upsert_node`, applied under one lock in the ledger process. Decided 2026-09-21; see check 5 | Coder |
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
2. Checks that the code graph is current for the file: the file has a node, the node's anchors resolve, and no symbol in the file is unmapped. Decided 2026-09-21; see check 7.
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
| A message goes to an agent that exists in the run | `message_post` refuses an unregistered name and lists the registered ones. Any agent may message any other. Added 2026-09-22 at Alex's request |
| A finished Lead or Manager is released so it stops and frees its slot | `phase_update(handed_up)` refuses while a Lead of the phase is live, so the Manager releases each Lead with `agent_release` after it accepts the module. `phase_update(approved)` releases the phase's Manager and every agent still live under it, and refuses while a deferral in the phase is open. `run_finish` releases anything left. Added 2026-09-23 at Alex's request |
| Only the owner's parent chain escalates an issue | `issue_escalate` refuses anyone outside the file owner's chain. It records the receiver in `issues.escalated_to`: the Manager for round 2, the Oracle for round 3. It also messages the receiver. Added 2026-09-22 at Alex's request |

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

Decision from Alex on 2026-09-22: each role keeps its fixed `tools` allowlist in the frontmatter. A denylist was considered so that agents would inherit a host project's plugins, skills, and language servers, and the probe below shows that it works. Alex chose the allowlist after the probe showed what a denylist also lets in: personal-account connectors, GitHub and Jira under any server name, and session tools that bypass the ledger. A host project's plugin tools are not available to the swarm.

Later decision from Alex on 2026-09-22, which extends the one above: the allowlist defaults to every plugin enabled at the host project's scope, and the user can remove entries or add others in the project. User-level plugins, user MCP servers, and claude.ai connectors are never included. The mechanism is open. Alex prefers per-agent settings that a project can override to a hook, if the platform offers one.

Probe on 2026-09-22, with a throwaway plugin on haiku, run in both a `--agent` session and as plugin subagents:

- A plugin agent's `disallowedTools` works in both places. With no `tools` key, the agent inherits every other tool in the session: other plugins' MCP tools (a11y, the drivers) and `Skill`.
- An MCP server name without a tool suffix, such as `mcp__plugin_github_github`, denies every tool of that server. The `__*` form works too.
- The shell has two names. Denying `Bash` leaves `PowerShell`, so both are denied.
- GitHub and Jira reach a session through five servers on this machine: `plugin_github_github`, the user server `github`, `plugin_atlassian_atlassian`, the claude.ai connector `claude_ai_Atlassian_Rovo`, and the user server `atlassian-attachments`. A denylist catches only the names it lists, so another user's GitHub or Jira server under a different name is not caught.
- A session also inherits the claude.ai connectors (Gmail, Google Calendar, Google Drive, Claude Docs) and session tools such as `EnterWorktree`, `Workflow`, `CronCreate`, and `RemoteTrigger`.
- No language server tool appeared, because no installed plugin provides one. That case is not verified.

Per-project agent configuration, researched on 2026-09-22 from `code.claude.com/docs/en/sub-agents.md`, `plugins.md`, and `plugins-reference.md`, then probed:

- No setting edits a plugin agent's frontmatter. A plugin's own `settings.json` supports only `agent` and `subagentStatusLine`. `userConfig` values substitute into an agent's body text, not its `tools` line.
- A `tools` line accepts a whole server: `mcp__<server>` or `mcp__<server>__*`.
- A project agent in `.claude/agents/` honors `hooks`, `mcpServers`, and `permissionMode`, which plugin agents ignore.
- The docs say a project agent with the same name takes precedence over a plugin agent. The probe showed that this does not apply to a namespaced plugin agent: a project `control` and the plugin's `probe:control` both exist, and each name spawns its own file. A project agent replaces the plugin's agent only when the swarm spawns the project agent's name.
- The project copy, on haiku, answered in the persona from the user's global `CLAUDE.md` instead of following its own one-line prompt. Subagents load the user's `CLAUDE.md`.
- A session loads its agents once, at startup. An agent file written during the session, even by a `SessionStart` hook, is not found: `Agent type 'late' not found`. A new agent file takes effect in the next session only.
- The Write tool cannot write into `.claude/`, even with `acceptEdits`. The files must be written by the ledger or a hook, in Python.

Decision from Alex on 2026-09-22: each host project gets its own agent files in `.claude/agents/`, generated from the plugin's templates. Each file's `tools` line holds the core set plus the plugins the project enables, and the user edits it. When a run starts and the files are missing, the run performs the setup and writes them, then tells the user plainly to type `/reload-plugins`. No tool, skill, or hook can issue that command; only the user or a script driving the session can. Verified on 2026-09-22 in an interactive session: after `/reload-plugins`, a new file in the project's `.claude/agents/` is listed as an agent type and spawns. In both probes, a haiku agent with a one-line prompt answered in the user's persona instead of following its prompt. The swarm's prompts are long and run on sonnet or larger, and the smoke test followed them, but a run should check that project agents follow their prompts.

## Watchdog

The Oracle starts a plain script at the start of a run. The script reads the registry's heartbeats and states. It wakes the Oracle only when it finds an agent that is stuck, spinning, or over the context threshold.

It reports through the directive channel. See "User directives".

### What was built on 2026-09-24

Every item below is **(proposed)** unless it says it is Alex's rule. Checked with unit tests only, not yet in a live run.

Where it runs:

- The detection script runs as a thread inside the repo's ledger server (`swarm_ledger/watchdog.py`), not as a process the Oracle starts. It starts and stops with the server, which starts before the Oracle and exits after `run_finish`. So it is started and stopped with the run, and nothing is left running after the run ends. The user's requirement is that nothing stays in the background after the run ends or is abandoned.
- It opens its own SQLite connection and writes with `BEGIN IMMEDIATE`, like any other ledger writer.
- Every `interval_seconds` it reads `claude agents --json` and scans the active run. It skips a paused run. A failed `claude agents --json` skips one pass. Errors go to `.sentinel-swarm/server.log`, and the loop keeps going.

What it detects, for the live Manager, Lead, and Coder sessions of the active run:

| Kind | Condition | Next step the report names |
|---|---|---|
| `crashed` | The ledger says `working`, or `registered` for more than 2 minutes, and the session is missing from `claude agents --json` or not running. An `idle` or `handed_up` agent whose session exited is normal | `agent_resume(target_name=...)` |
| `stuck` | The session runs, the ledger says `working`, and the last heartbeat (or the start) is older than `stuck_minutes` | Message it, or have its parent replace it. `agent_resume` refuses a running session |
| `waiting_permission` | The session waits on a permission prompt. Not reported as `stuck` too | Tell the user to open that session in agent view and answer the prompt |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed | Ask its parent to review: `return_work` or `issue_escalate` |
| `context_high` | The agent's latest request fills `context_pct` of its window: input, cache read, and cache creation tokens of the last assistant message in its transcript. The window is 1,000,000 when the model names `[1m]`, else 200,000. Checked for the Oracle too | Have its parent replace it with a fresh agent that continues from the ledger. For the Oracle: pause the run so the user resumes it in a fresh Oracle session |
| `stalled` | The run is active, no session of the run runs (the Oracle included), and two passes in a row see it | Resume the agent whose work is pending, named by submitted handoffs and unread messages |

- `stalled` counts pending work as live agents, submitted handoffs, unlocked phases, and open directives from any source but the watchdog. A run with none of these still reports `stalled`, with "the run is not finished", so an abandoned active run always reaches the Oracle or a pause.
- The transcript path comes from `SessionStart`, or from the first `PostToolUse` when `SessionStart` ran before `agent_spawn` wrote the row.

How it reports:

- Table `watchdog_findings` keeps one live row per run, agent, and kind. A finding that the next pass does not see is cleared. One that comes back later gets a new row and a new report.
- Each new finding becomes a directive with source `watchdog` and sender `watchdog`, inserted directly by the server. Its body names the agent, its session, the kind, the detail, and the next step. Open directives already block `run_finish`, so the Oracle resolves each one.
- The watchdog never resumes, replaces, or stops a Manager, Lead, or Coder. Alex's rule.

How a report reaches the Oracle:

- The listener: `python -m swarm_ledger.watch`, run through the shim as `hook.py watch`. Every 2 seconds it prints one line per unnotified watchdog directive, marks it notified, and writes `runs.watch_heartbeat_at`. It exits when no run is active, or the run is paused or finished.
- The Oracle arms it with `Monitor(command="python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch", description="sentinel-swarm watchdog", timeout_ms=1800000)`. Each printed line wakes the Oracle (verified 2026-09-24). A monitor expires after 30 minutes, so the Oracle re-arms it.
- `pre_monitor` allows that one call from the Oracle and denies every other `Monitor` call from a swarm session. This closes the shell that `Monitor` gave the Oracle.
- The Oracle's `Stop` hook blocks while the run is active and the listener's heartbeat is missing or older than 60 seconds, or the armed monitor expires within 30 seconds. `PostToolUse` on the Oracle's watch call records the arm time as a heartbeat and the expiry from `timeout_ms`, so the listener's start-up time does not trip the rule.
- `directive_inbox` marks the directives it returns as notified, so the listener does not repeat them.
- Waking a stopped Oracle: when a watchdog directive stays unnotified for one interval and the Oracle's session is not running, the server runs `claude --resume <Oracle session> --bg "The watchdog reported N finding(s). Read directive_inbox."`, at most once every 5 minutes. Each attempt is an `agent_events` row. After 3 attempts with no Oracle heartbeat since the first, it pauses the run with the reason "the watchdog could not wake the Oracle". This exists so that a report can reach the Oracle at all.

When the server exits on its own:

- After `idle_exit_minutes` with no active run, or with a paused run, and in both cases no session of the run running. A ledger tool call restarts the idle clock, so an Oracle that is still talking to the user before `run_start` keeps its server. A failed `claude agents --json` counts as no session running here, so a broken `claude` cannot keep the server alive.
- The server keeps its port in `.sentinel-swarm/server.port` and binds it again on restart, so a session resumed after an idle exit still reaches the ledger at the URL it started with. When the port is taken, it binds a free one, and sessions started before then lose the ledger.

Settings, in `.claude/sentinel-swarm.local.md` under `watchdog`: `interval_seconds` (30), `stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `idle_exit_minutes` (15).

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

## Prototype results on 2026-09-21

Run on Windows 11 from a main session. Each result names its check number from the list above.

| Check | Result | Consequence |
|---|---|---|
| 6. Nesting depth | Three subagent layers ran below the session. The third layer had no `Agent` tool, so a fourth layer is not possible by default | Oracle as the session, then Manager, Lead, and Coder, fits exactly. There is no spare layer. The relay-session option in `01-roles.md` "Models" would need the depth setting raised |
| 6. Resume with `SendMessage` | A parent resumed a finished child by its agent id, and the child kept its context: it repeated a number from its first turn | Step 8 of "One file, start to finish" works |
| 6. Trust in a resume message | A resumed child refused a request that its original prompt had not told it to expect. It treated the message as unverified. A child whose prompt announced the follow-up answered it | Each role's prompt states that its parent resumes it by message. The message only points at the ledger record, such as a `return_work` entry, and the child reads the detail there **(proposed)** |
| 5. SQLite writers | 12 processes, 300 writes each. WAL mode with a 5 second busy timeout and `BEGIN IMMEDIATE`: 3600 of 3600 writes, no errors, about 2900 writes a second. The default journal with the same timeout: 47 errors, about 60 writes a second. No timeout: most writes fail in both modes | The ledger opens every connection with WAL, a busy timeout, and `BEGIN IMMEDIATE` for writes **(proposed)** |
| 4 and 10. Hook cost | A Python hook that does nothing costs about 300 ms for each call, measured through Git Bash. `python3` exists on this machine, so the `python3 ... \|\| python ...` form added nothing here. The Store stub case is not measured | 300 ms on every tool call is too much for hook 7 as a plain Python process. Options: limit hook 7 to the tools that matter, or send the heartbeat to a process that is already running. Measure again through the real hook runner before deciding **(proposed)** |
| 1. `PreToolUse` rewrites an MCP tool's input | The probe returned `permissionDecision: allow` with an `updatedInput` that changed the `kg_search` query from `probe-original` to `oracle`. The server's reply echoed the query `oracle` and returned the Oracle nodes | `updatedInput` works on a plugin MCP tool. Hook 6 can stamp a value into a ledger call. The name check from the decision above still applies, because the hook knows the `agent_id`, not the name |
| 2. `SubagentStop` can block | The probe answered a subagent's first stop with `{"decision": "block", "reason": ...}` and exit 0. The subagent followed the reason, and the requested word was in its final report | Hook 12 can hold a subagent until its handoff is recorded. The block goes through JSON output, in line with the rule below |
| 2. `SubagentStop` fires more than once | One agent produced three `SubagentStop` records with the same `agent_id`. A parent also gets a `SubagentStop` while its background child still runs, then a new `SubagentStart` with the same `agent_id` when the child's report resumes it | `SubagentStop` does not mean that the agent ended. A stop hook must give the same answer on every call, and the registry must not record an end time on the first stop. "Waiting on running children" is a real state for subagents too **(proposed)** |
| 3. Hook input at each layer | `PreToolUse` and `PostToolUse` carried `agent_id` and `agent_type` at all three layers. `session_id` and `transcript_path` are the main session's at every layer. Only `SubagentStop` carries `agent_transcript_path`. No field names the parent. The layer 3 agent produced no `SubagentStart` record, although its tool calls and stops were logged | Hook 6 can tell agents apart by `agent_id` at any depth. The ledger must record the parent itself, from the brief, because hook input does not give it. Do not rely on `SubagentStart` alone to register an agent **(proposed)** |
| 3. Agents that are not the swarm's | `SubagentStop` records arrived with an empty `agent_type` and an `agent_id` that this session did not launch. Records from a second session in the same repo landed in the same log | Hooks must ignore an `agent_id` that the registry does not know, and must key on `session_id` as well **(proposed)** |
| 3. `PreCompact` in a subagent | Not determined. No `PreCompact` record appeared, and no subagent in the chain came near its context limit | The check needs a subagent that fills its context. Run it during the first long swarm test |
| 4. Hook cost through the real runner | With the probe on, the gap from the `PreToolUse` record to the `PostToolUse` record for `Read` had a median of 331 ms and a minimum of 194 ms, over 6 calls. The script body took under 1 ms. A bare `python3` start measured about 175 ms in Git Bash. A session with the probe off was not measured | The cost is the interpreter start, about 200 to 300 ms for each hook, and a tool call with a `PreToolUse` and a `PostToolUse` hook pays it twice. This confirms the result above |
| 7. `kg_upsert_node` and the file hash | It does not re-baseline. After an upsert that changed a node's description, `kg_validate` still listed the node's file under `changed_since_built`. An upsert that changes nothing writes nothing. An export followed by a plain `codebase-kg-build` did not clear the file either, because the build keeps the old baseline for a node that the JSON does not change. Only `codebase-kg-build --rebaseline` cleared it, and that flag re-hashes every anchored file | As designed, the graph-current check in `handoff_submit` never passes after a Coder's upsert. Decision from Alex on 2026-09-21: the hash is a review signal, not a gate. A stale hash says that the file changed and its node needs a re-read, which is the Lead's review. `handoff_submit` checks anchors and coverage only: the file has a node, the node's anchors resolve to source, and no symbol in the file is unmapped. The codebase-kg git hooks catch any stale file before a commit. A per-node re-hash in codebase-kg is a possible later addition, if one file's hash can be regenerated alone |
| 5. Code graph writers | Unsafe. `kg_upsert_node` copies the graph file, edits the copy, and replaces the original, with no lock. 4 concurrent writers: 4 reported success, 1 change landed. 8 writers: 8 reported, 1 landed. 12 writers: 11 reported, 2 landed, and one failed with a Windows permission error during the replace. A writer's change is lost silently when another writer's copy was taken before its replace | Coders must not call `kg_upsert_node` at the same time. Decision from Alex on 2026-09-21: one ledger tool, `graph_upsert`, takes the same arguments as `kg_upsert_node` and applies the write under a lock inside the ledger process. The Coder makes one call, and no lock is handed out. Fallback, if that does not work: Coders take a ledger lock, upsert, and release. The ledger imports `codebase_kg` for the write, or shells out to its CLI **(mechanism proposed)** |
| 8. Parallel test runs in one working tree | Reliable for the case that matters. Five per-file `pytest` runs at a time, three rounds, while a sixth module flipped between a syntax error and valid code every 50 ms: 15 of 15 runs passed. A run on the half-written module's own test failed as expected. Run with `-p no:cacheprovider` | `tests_run` runs only the file's own test file, so a sibling Coder's half-written file does not break it. The exception is a test that imports the half-written module, and that shows up as the sibling's failure at its own handoff. Pass `-p no:cacheprovider` to avoid `.pytest_cache` contention **(proposed)** |
| 9. Hook 8 attribution | `PreToolUse` and `PostToolUse` for `Edit` and `Write` carry `agent_id`, `tool_name`, `tool_input.file_path`, and on `PostToolUse` also `tool_response` and `tool_use_id`. The subagent records in check 3 show that `agent_id` is present at every layer | Hook 8 attributes the write to the calling agent from the same hook input, with no time correlation. A `Bash` write cannot be attributed by path, and the Coder role has no unrestricted shell |

Done: every prototype check except the `PreCompact` half of check 3 and the `--agent` checks in `03-ledger.md`. The `PreCompact` half of check 3 is open.

A note on codebase-kg in this repo: `.claude/settings.json` enables it, but a new session did not load its MCP server until `/reload-plugins` ran. `claude plugin install --scope project` fails on Windows while other sessions run a server from the same cache folder.

A rule for the `python3 ... || python ...` form: a hook blocks through its JSON output and exits 0. A hook that exits 2 makes the shell run the second command too, and that second run gets an empty stdin because the first run already read it. **(proposed)**

### Probe hooks for the remaining checks

Done on 2026-09-21, and the probe is deleted. The results are in the table above. Deleting `.claude/settings.local.json` removed the hooks from the running session without a restart.

The probe was local and git-excluded: `.claude/settings.local.json` registered `.claude/probe/probe.py` on every hook event, and the script appended each hook input to `.claude/probe/log.jsonl`. The steps were:

1. Check 1: call `kg_search` with the query `probe-original`. The probe rewrites it to `oracle`. Results about the Oracle mean that `updatedInput` works on an MCP tool.
2. Check 2: create the file `.claude/probe/block_subagent_stop_once`, then run a subagent. The probe blocks its first stop and asks for the word PINEAPPLE. The word in the subagent's final report means that `SubagentStop` can block.
3. Checks 3 and the registry columns: run a three-layer nested subagent chain, then read the log for `agent_id`, `agent_type`, and `agent_transcript_path` at each layer, and for any `PreCompact` record.
4. Check 4: compare tool-call timing in the session with the probe on and off.
5. Delete `.claude/settings.local.json` and `.claude/probe/` when the checks are done.


## No telephone game

Decision from Alex on 2026-09-21: every plan and task is recorded in the ledger so that each agent references the record and does not forget. Information never passes from agent to agent in paraphrase.

- A brief's `body` holds the full task: the goal, the scope, what the tests must prove, the contracts, and the guidelines that apply. The spawn prompt carries only the child's name and "read your brief".
- The plan lives in `phases`, `modules`, `files`, and `guidelines`. A return lives in `return_work`, `issues`, and `messages`. A resume message points at the ledger record and carries no detail.
- Nothing is forgotten: an agent re-reads its brief from the ledger before each handoff and after each return, and reads its inbox at every turn boundary, instead of trusting its own memory.
- The smoke test checks that every brief body is complete and that no spawn prompt carries task detail. **(check proposed)**

## Build order and the smoke test

Decision from Alex on 2026-09-21:

- Every piece is tested and working on its own first: the prototype checks, the ledger tools, and the hooks.
- The smoke test comes after that. It is one run with the prompt "Create a hello world file."
- The smoke test passes when all of the agents were created and all of the logs and recorded information are there: the tests, the scores, and the rest.

What the smoke test checks in the ledger: **(proposed list)**

- The registry has one Oracle, one Manager, one Lead, and one Coder, each with its name, parent, model, start time, end time, and token counts.
- A brief exists for each child, and each child acknowledged it.
- The Coder claimed the file and its test file. No other role wrote a project file, and no hook recorded a violation.
- `tests_run` recorded a passing run for the file, with no empty or skipped tests.
- The Coder's scores and the Lead's blind scores both exist for every applicable dimension, and `review_compare` ran.
- The file, the module, and the phase each have a recorded approval, and the claim is released.
- The code graph is current for the file.
- `agent_events` holds every lifecycle transition in order, and `report_build` produces the final report.

### Smoke test results on 2026-09-22

The run: a scratch host repo with a README, a `pyproject.toml`, a one-node code graph, and `.claude/sentinel-swarm.local.md` with `test_command: python -m pytest -q -p no:cacheprovider {target}`. The Oracle ran as a headless session:

```
echo "Create a hello world file." | claude -p --agent sentinel-swarm:oracle \
  --plugin-dir <sentinel-swarm> --plugin-dir <codebase-kg cache folder> \
  --permission-mode acceptEdits \
  --allowedTools "mcp__plugin_sentinel-swarm_swarm-ledger,mcp__plugin_codebase-kg_codebase-kg,Agent,Read,Grep,Glob,Write,Edit,MultiEdit,SendMessage,ToolSearch,Bash(python -m pytest:*),Bash(git status:*),Bash(git diff:*),Bash(git log:*)" \
  --output-format stream-json --verbose
```

Both `--plugin-dir` flags are needed: sentinel-swarm declares codebase-kg as a dependency, and the plugin does not load when the dependency is absent from the session.

Decision from Alex on 2026-09-22: the smoke test runs inside this repo, in the git-ignored folder `runs/hello/`. `scripts/smoke.sh` deletes the folder, rebuilds the host repo in `runs/hello/host/`, and runs the command above. The transcript goes to `runs/hello/transcript.jsonl`, outside the host repo. The run loads this repo's `CLAUDE.md` as a parent file. Alex accepted that, because a real host repo loads its own `CLAUDE.md` in the same way. `runs/hello/` was seeded with run 4's results.

Runs 5 to 7 on 2026-09-22 failed: every Coder `Write` was denied with `asyncAgent: Permission prompts are not available in this context`. Cause, isolated with haiku probes: Claude Code asks before any write inside a loaded plugin's own folder, and a background subagent cannot answer a prompt, so the write is denied. The host repo sat in `runs/hello/host/`, inside the folder that `--plugin-dir` loaded. The Claude Code version (2.1.278 and 2.1.280), the user's `defaultMode: auto`, nesting depth, and the swarm's hooks were each ruled out. Fix: `scripts/smoke.sh` loads a copy of the plugin from a temp folder, which also matches an installed plugin. The swarm could not pause cleanly while blocked: the Stop hook holds the Oracle, and `run_finish` refuses an unapproved phase. That gap is open.

Decisions from Alex on 2026-09-22, after run 9:
- The smoke test must leave a `hello_world.txt` produced by running the code. The default prompt is now "Create hello.py. When it runs, it writes the text Hello, world! to hello_world.txt in the current folder." After the swarm finishes, `scripts/smoke.sh` runs `python hello.py` and prints the file, because the swarm's shell gate allows only test commands.
- The failing duplicate `swarm-ledger` server in the smoke run comes from this repo's own `.mcp.json`, which a real host repo does not have. It stays.
- Next: a clean pause for a blocked run, then the Oracle's token counts in the report.
- Runs are interactive by default, decided by Alex on 2026-09-23. `scripts/smoke.sh` opens the Oracle as a normal session in `runs/hello/host`, with the prompt as its first message, so the user watches the Manager, Lead, and Coder in the session's agent tree and can open any of them. `--headless` keeps the old `claude -p` run with its transcript in `runs/hello/transcript.jsonl`, for automated checks.
- A blocked run pauses. An Oracle-only `run_pause(reason)` sets the run to `paused` and records the reason. The Stop hook lets the Oracle stop while the run is paused, and the Oracle tells the user once what to fix. `/sentinel-swarm:resume` or the user's next message sets the run back to `active` and continues from the ledger. Decided by Alex on 2026-09-23.
- Run 10 (2026-09-23) wrote `hello_world.txt` correctly, but the session exited with the handoff unreviewed. A probe showed why: when a parent has ended its turn, its child's completion notice goes to the main session, not to the parent. The Lead had ended its turn after spawning the Coder, so nothing woke it to review. Decided by Alex on 2026-09-23: the ledger names who to wake. `SubagentStop` marks an agent `idle` when its turn ends. When work waits on an idle agent, the Oracle's Stop hook blocks with the exact agent to resume through `SendMessage`.

| Run | Result | What it found |
|---|---|---|
| 1 | Stalled after `run_start` | Hook 6 stamped `agent_id` onto `ledger_info`, which has no such parameter. FastMCP runs tool calls on worker threads, and the SQLite connection refused cross-thread use |
| 2 | Stalled at the Coders | The Coders invented rating names and looped on a raw `KeyError`. `graph_upsert` failed on an edge to a missing node, and the ledger's `VIRTUAL_ENV` leaked into the codebase-kg subprocess. The Lead claimed the test file as a second file and created two Coders. The Manager polled `message_inbox` on every turn |
| 3 | **Passed** in 5 minutes 46 seconds, 16 Oracle turns, about $3.61 | See the checklist below |
| 4 | **Passed** in 5 minutes 16 seconds, 20 Oracle turns, about $3.28, on the code that fixes run 3's findings | Same checklist, every row clean: the full run without a target passed, the module is `approved`, the Oracle row closed at `run_finish`, and the report says `Outcome: success`. The Coder wrote `greeting()` and `main()` with two tests; the Lead's first score call omitted a dimension from `applicable`, the ledger refused it with the reason, and the retry passed |
| 5 to 7 | Failed: no file written | Every Coder `Write` was denied. The host repo sat inside the loaded plugin's folder. See the note below the command |
| 8 | Stopped after 3 Oracle turns, about $0.59 | The plugin copy had no venv yet, so the ledger server was still pending when the Oracle checked. `scripts/smoke.sh` now runs `uv sync` on the copy first |
| 9 | **Passed** in 9 minutes, about $3.97, on 2026-09-22 | Run finished with `success`; phase, module, and file approved; self and Lead reviews with 21 ratings each; handoff compared and approved; two versions. The ledger's test runner resolved `python` to its own venv, which has no pytest. The Coder escalated the issue, `issue_escalate` routed it to the Manager, and the Oracle switched the test command to `uvx pytest`. Fixed: `run_tests` drops the ledger's venv from `PATH` and `VIRTUAL_ENV` |
| 10 | Stopped with the handoff unreviewed, about $2.17, on 2026-09-23 | `hello_world.txt` was correct, but the idle Lead was never woken. Fixed by idle tracking and the Stop hook's wake list |
| 11 | Stopped after 1 Oracle turn, about $0.51 | The ledger server was still connecting, and the Oracle had no `ToolSearch` to wait for it. Fixed: `ToolSearch` is in the Oracle's tools and is its first start step; a probe showed it waits for a pending server |
| 12 | **Passed** in 4.5 minutes, about $2.40, on 2026-09-23 | Run finished with `success`; phase, module, and file approved; three passing tests, including a subprocess run of the script; self and Lead reviews; `hello_world.txt` holds `Hello, world!`. The Stop hook's wake list worked: the Oracle resumed the idle Lead for the handoff and the idle Manager for the module report. The report now carries the Oracle's tokens and a run total |
| 13 | **Passed** in 3.6 minutes, about $2.33, on 2026-09-23 | Every agent released in order: the Coder on `approve`, the Lead by its Manager's `agent_release`, the Manager on phase approval, and the Oracle on `run_finish`. No agent was left live. The report's Oracle tokens match the ledger's final count |
| 14 | **Passed** on 2026-09-23 with every role as its own session | See `06-sessions-plan.md` "Build status": four sessions, four wake-ups, `success` in 6 minutes |
| 15 | **Passed** on 2026-09-23: the crash test | The Coder's session was stopped mid-file; the Oracle resumed it with `agent_resume`, and the run finished with `success` |

Every finding has a fix and a test. The fixes: hook 6 skips the tools without `agent_id`; the connection opens with `check_same_thread=False` and every tool call runs under one lock; `score_record` refuses a malformed rating with the full key list, and the tool description carries that list; `graph_upsert` checks edge targets first and drops `VIRTUAL_ENV`; the Lead prompt says one Coder owns the source and test pair; the parent prompts say to end the turn after a spawn instead of polling.

Run 3 against the checklist, read from the ledger rows:

| Check | Result |
|---|---|
| Registry | `oracle` (fable), `mgr-hello` (opus), `lead-hello-hello` (opus), `coder-hello-hello-hello` (sonnet), each with its parent, start and end time, tokens, and tool count. The Oracle row had no end time or tokens, because the session's own transcript is not read; `run_finish` now closes it |
| Briefs | Three briefs, 1313 to 2885 characters each, all acknowledged |
| Claim | `hello.py` with `test_hello.py`, owned by the Coder, released on approval. No violation event |
| Tests | Six `test_runs` rows: file twice, module, phase, full twice. All passed except one full run with no target, where `{target}` stayed in the command; the runner now drops the placeholder |
| Scores | A self review and a lead review, 27 ratings each over the seven applicable dimensions, accessibility and error handling marked not applicable. `review_compare` ran |
| Approvals | The handoff is `approved` with two saved versions. The phase is `approved`. The module stayed `planned`, so `approve` now marks a module approved when its last file is |
| Graph | The Coder's `hello_module` node anchors on `hello.py#main` and `test_hello.py#test_main_prints_hello_world` |
| Events | 12 lifecycle events in order, from `run_start` to the two `agent_release` calls |
| Report | `report.md` was written before `run_finish`, so it said `Outcome: active`; `run_finish` now rebuilds it |

Observed but not fixed: the Oracle's final message used the persona from the user's global `CLAUDE.md`. The prompts tell each role to ignore persona instructions in the host repo's `CLAUDE.md`, and the global file is not the host repo's file. Decision from Alex on 2026-09-22: this is expected. A persona from the user's own settings may appear in messages to the user. No persona text reached the ledger rows or `report.md` in run 4. The Stop hook held the Oracle in the session across six intermediate stops while its children ran, which is hook 11 working as designed. The gates that fired in the run: the shell gate denied a Coder's `cd` command in run 2, and `tests_run(scope="file")` refused the Lead in run 3.

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
