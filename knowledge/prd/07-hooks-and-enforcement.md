# Hooks and enforcement

## Where the hooks live

- Each role's hooks live in the frontmatter of its project agent file,
  `.claude/agents/swarm-<role>.md`. `hooks/hooks.json` carries no hooks, so no hook
  runs twice.
- Claude Code runs an agent file's frontmatter hooks only in a trusted folder.
- The hooks are the user's to edit, and a user can weaken a hook gate in their own
  project. The ledger tools' gates apply whatever the files say. A rerun of setup
  restores every ledger hook the template has.

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent` | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell` | all |
| `PreToolUse` | `Monitor` | `pre_monitor` | all |
| `PreToolUse` | `SendMessage` | `pre_send_message` | all |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | `SendMessage\|PushNotification\|Monitor\|Write\|Edit\|MultiEdit\|NotebookEdit` | `post_any` | all |
| `PostToolUse` | all, `async: true` | `post_activity` | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | Coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

## The shim

Every hook command is
`python3 .sentinel-swarm/hook.py hook <event> || python .sentinel-swarm/hook.py hook <event>`.
`.sentinel-swarm/hook.py` is a standard-library shim that setup copies from
`templates/hook_shim.py`. It has these commands:

- `hook <event>` posts the hook input to the running ledger server at
  `POST /hook/<event>`, on the port in `.sentinel-swarm/server.json`, with the repo
  root in the `X-Sentinel-Swarm-Repo` header, and prints the answer. See "The hook
  route" in [06-ledger-server.md](06-ledger-server.md). It falls back when there is
  no `server.json`, the connection is not accepted within 0.25 seconds, no answer comes
  within 10 seconds (40 for `stop` and `session_end`), or the answer is not a 200 that
  carries the repo header. The fallback finds the sentinel-swarm install for this repo in
  `~/.claude/plugins/installed_plugins.json` (scope `local`, then `project`, each with a
  matching `projectPath`, then `user`), and runs
  `uv run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.hooks <event>`
  with stdin and stdout passed through. A plugin upgrade changes the registry, not the
  agent files. Both paths print the same bytes.
- Every `uv run` of the shim sets `UV_PROJECT_ENVIRONMENT` to the ledger's venv in the
  plugin data folder, keyed by `mcp/uv.lock`. See "The ledger's venv" in
  [11-setup-and-settings.md](11-setup-and-settings.md).
- For `post_activity`, the shim adds `sentinel_swarm_fired_at`, the time the hook
  fired, to the hook input.
- `mcp <plugin_id> <server>` starts another plugin's MCP server the same way, from its
  `.mcp.json`, or its manifest's `mcpServers` when it has no `.mcp.json`, with
  `${CLAUDE_PLUGIN_ROOT}` and `${VAR:-default}` expanded in the command, the arguments,
  and the `env` values. When it cannot find the server or its command, it writes the
  reason to stderr and exits 1. A plugin that names its own relay there shares its
  server through that relay. See "Plugin servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- `watch` runs `python -m swarm_ledger.watch` and passes each line through with no
  timeout.
- `channel` runs `python -m swarm_ledger.bridge`, the `swarm-events` channel server,
  with stdin and stdout passed through. When the registry, the install, or `uv` fails,
  it writes the reason to stderr and exits 1, and the session gets no channel.
  **(proposed)**

When the server does not answer and then the registry, the install, `uv`, or the
ledger hook fails, or the two paths together run longer than 50 seconds, a gating event (`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`,
`pre_send_message`, `pre_ledger`) answers `deny` with the reason, and every event adds
a `systemMessage` that says to run `/sentinel-swarm:setup`. The shim exits 0.

## Rules every ledger hook follows

- A hook blocks by printing a JSON decision and exiting 0. It never uses exit code 2.
  **(proposed)**
- A broken ledger hook allows: `swarm_ledger.hooks` catches every exception, writes one
  line to stderr, and prints nothing.
- Apart from `pre_write`'s records-folder rule, `pre_ledger`, and `pre_monitor`'s
  `agent_type` check, a hook ignores a caller the registry does not know, so a non-swarm
  session in the same repo passes. An agent that has ended counts as unknown.
  **(proposed)**

## What each hook does

- `session_start`: for a swarm session, records the transcript path and sets an idle
  agent to working. For any other session, it reports an active or paused run; checks
  the plugin registry for codebase-kg, reports it when it is not installed, and, only
  once it is installed, reports a missing `knowledge/code_graph.db`; and adds
  `.sentinel-swarm/` to the git excludes.
- `pre_agent`: denies `Agent` to every swarm session, unconditionally, except a Driver
  session calling cartographer's `map-driver` or `map-reviewer` subagent, which it
  allows under the bare name or the plugin form (`cartographer:map-driver`,
  `cartographer:map-reviewer`). Another plugin's agent of the same name is denied.
  No override rule covers it: the rule that no role runs an arbitrary subagent
  has no legitimate exception, so `pre_agent` never calls `override_consume`.
  **(proposed)**
- `pre_send_message`: denies a `SendMessage` whose `to` does not name a registered
  agent's `session_name` in the caller's own run, and lists the valid names in the
  reason. A caller the registry does not know, or one with no run yet, passes.
- `pre_write`: while a run is active or paused, denies anyone a write into the records
  folder, unconditionally; the records folder is the ledger's own state, so no
  override rule covers it either. **(proposed)** Denies a write by any role but the
  Coder, and a Coder's write outside its claimed path and test path, naming the owner;
  an override of rule `write` lets one of these two writes through.
- `pre_shell`: while a run is active or paused, denies the shell to every role but the
  Coder and the Driver. A Coder may run a command that starts with the profile's test,
  build, or lint command, or read-only git (`status`, `diff`, `log`, `show`,
  `ls-files`, `branch`). A Driver may run only a command that starts with the profile's
  `build_command`. A command with `;`, `&`, `|`, `<`, `>`, a backtick, a newline, or
  `$(` is denied. An override of rule `shell` lets one command through. **(proposed)**
- `pre_monitor`: allows exactly one `Monitor` call from a swarm session, the Oracle's
  watchdog call. It denies every other one and names the allowed call. A session not
  yet in the ledger counts as a swarm session when its `agent_type` is `swarm-<role>`.
  No override rule covers it.
- `pre_ledger`: stamps `agent_id`, and denies `override_grant` to anyone but the
  Oracle. The identity stamp itself takes no override: faking `agent_id` is what the
  stamp exists to prevent. **(proposed)** The stamp is the hook input's `agent_id`, or
  else its `session_id`. For the five tools that take no identity (`ledger_info`,
  `brief_get`, `who_owns`, `directive_submit`, `events`), it removes `agent_id`
  instead. It answers `allow`, so a ledger call never asks for permission.
  **(proposed)**
- `post_any`: runs synchronously after `SendMessage`, `PushNotification`, `Monitor`,
  and the write tools, because the Stop hook and `handoff_submit` read what it records.
  It writes the heartbeat and current activity, sets an idle agent to working,
  records the transcript path, records the watchdog arm time, clears owed wake-ups on a
  `SendMessage` and then releases a Driver whose exploration is closed and that owes
  nothing more, and after a Coder's write marks its file stale, so the handoff needs a
  newer self review. On the Oracle's `PushNotification` call, it marks one owed
  notification sent, whatever the call's result, even after the run finished.
  **(proposed)**
- `post_activity`: runs with `async: true` after every tool, so no tool call waits for
  it. It returns at once for the tools `post_any` covers, so the two never both run for
  one call. Otherwise it writes the heartbeat and current activity and records the
  transcript path. It sets an idle agent to working only when the agent's last move to
  `idle` came before `sentinel_swarm_fired_at`: an async hook can land after the Stop
  hook, and the tool call it reports came before the stop.
- `post_shell`: after a Coder's shell call, lists changed paths with `git status`.
  A change outside the Coder's claim, other claimed files of the run, the records
  folder, and `knowledge/` is recorded as a `violation` event and posted to its Lead.
- `pre_compact`: adds one to the agent's `context_overflow_count`.
- `stop`: records tokens and cost from the transcript, then applies the stop rules in
  "Sessions". For a wake-up the ledger pushed through a channel, it first waits until
  30 seconds after the push for the target's transcript to confirm it; see "Wake-up
  delivery" in [05-sessions.md](05-sessions.md). A Driver whose exploration is closed
  and that owes nothing more is released instead of set idle. After a run finishes, the
  Oracle's stop refreshes the report, then blocks while the Oracle still owes a
  `PushNotification` call.
  **(proposed)**
- `session_end`: records tokens and cost and the end reason. After a run finishes, the
  Oracle's `session_end` refreshes the report.

## Rule-to-enforcement map

| Rule | Enforced by |
|---|---|
| Only the Coder writes project files | `pre_write`; no write tool in the other agent files, and no shell except the Driver's, which `pre_shell` limits to the build command |
| A Coder writes only its own file and test file | `pre_write` for edit tools; `pre_shell` and `post_shell` for the shell |
| Nobody edits the records by hand | `pre_write` |
| A role starts only its own child role | `brief_create` and `agent_spawn`; `pre_agent` denies `Agent` except a Driver's cartographer subagents |
| The Oracle starts a new exploration only after every fix has finished | `drive_request` refuses while an exploration is open or a Manager, Lead, or Coder is live |
| `run_finish` needs a clean exploration or a resolved stop rule | `drive_request`, `drive_issue`, `drive_done`, and the `run_finish` gate in `drive.py` |
| A Driver that fails to load is a blocking issue | `drive_unavailable` files a `driver` directive; `drive_request` refuses while it is open, and `run_finish` refuses as for any open directive. Resolved `declined`, `drive_request` refuses for the rest of the run and the `run_finish` Driver gate passes |
| A stuck Driver's exploration can be closed | `agent_release` of the Driver abandons its open exploration, which never counts as clean |
| The Oracle is woken directly when the Driver ends an exploration or cannot load | `drive_done` and the Driver's `drive_unavailable` owe the Oracle a wake-up and do not release the Driver; the Driver's `stop` blocks until it is sent; `post_any`, `agent_resume`, or the Driver's `stop` releases the Driver once it is sent; a Driver that ignores the block twice goes idle without sending, which the watchdog reports as `driver_unsent` **(proposed)** |
| Fixes always name their finding ids | `brief_create` refuses an Oracle brief without `finding_ids` while the run has an open finding, and an unknown finding id from any role; `brief_get`, `status_tree`, and the report show the ids and titles |
| A stopped bug gets no more fixes, and its evidence reaches the user | `brief_create` and `agent_spawn` refuse a brief whose `finding_ids` name it; `drive_done` puts the evidence in the directive and the notification |
| A pattern of bugs pauses fixes in that area | `brief_create` and `agent_spawn` refuse a finding in the area while the pattern's directive is open |
| After 3 waves that fix nothing, the Oracle reports what is left | `drive_done` records the directive and the notification that list what is left; the report shows both |
| Every dimension is scored on every review | `score_record` refuses a set that leaves a dimension out |
| A model comes from the approved list | `brief_create` |
| No agent starts without a brief | `agent_spawn` and `brief_ack` |
| No agent fakes its identity | `pre_ledger` stamps `agent_id`; every tool matches `caller` to it |
| A handoff needs passing tests and a current graph | `handoff_submit` |
| No approval without a handoff and two sets of scores | `approve` |
| The Lead scores before it sees the Coder's scores | `score_record` refuses a Lead review after `review_compare` |
| A rule gives way only to the Oracle | `override_grant`, consumed by `pre_write` and `pre_shell` |
| The Oracle never grants itself a write or shell override | `override_grant` refuses a `target_agent_name` that names the Oracle |
| Look in the graph before writing | codebase-kg's own search gate hook |
| A message goes to an agent of the run | `message_post`; `pre_send_message` for `SendMessage` itself |
| Only the owner's chain escalates an issue | `issue_escalate` |
| A finished Lead or Manager is released | `phase_update(handed_up)` refuses a live Lead; `phase_update(approved)` releases the phase; `run_finish` releases the rest |
| A non-owner requests, the owner changes | `cr_open` routing and the write gate |
| A change request closes with evidence | `cr_complete` and `cr_verify`; gates on `handoff_submit`, `approve`, `phase_update(approved)`, `run_finish` |
| A departure is signed off or reworked | `departure_decide`; gates on `approve`, `return_work`, `accept_incomplete`, `module_review`, `phase_review`, `run_finish` |
| A deferral is decided by a high enough role in its scope | `agreement_decide`; gates on `phase_update(approved)` and `run_finish` |
| A directive outcome is one of the four values | `directive_resolve` |
| The user is notified when the Driver finishes or hits an error | `drive_done`, `drive_unavailable`, and the watchdog record the notification and show the OS notification; the Oracle's `stop` blocks until a `PushNotification` call, which `post_any` records |
| An issue that ends round 3 below the floor notifies the user now | `attempt_record` records the notification and shows the OS notification; the Oracle's `stop` blocks until the `PushNotification` call |
| The Oracle checks the repo before a Manager starts | `agent_spawn` |
| A phase hands up only after every module review | `phase_update(handed_up)` |
| A phase is approved only after the Oracle reviews it | `phase_update(approved)` |
| Code the graph maps has a test file by the end | `phase_review(accepted)` |
| A child wakes its parent after each step | The Stop hook, from the owed wake-ups; a pushed wake-up counts only once the target's transcript confirms it |
| The Oracle keeps working while the run has work, unless a directive waits on the user | The Oracle's Stop hook |
| The Oracle's watchdog listener stays armed | The Oracle's Stop hook and `pre_monitor` |
