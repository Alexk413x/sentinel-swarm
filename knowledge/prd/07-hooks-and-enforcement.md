# Hooks and enforcement

## Where the hooks live

- The plugin's mod, `hooks/register.ts`, is the only transport for the ledger hooks.
  `hooks/hooks.json` names it under `modules`. The role files
  (`.claude/agents/swarm-<role>.md`) carry no `hooks` frontmatter. See "The mod" below.
- `hooks/hooks.json` carries no command hooks. The mod acts only in a swarm role
  session, so no hook runs twice. Setup removes any ledger command hook (`hook.py hook
  <event>`) it finds in a role file. See "Setup" in
  [11-setup-and-settings.md](11-setup-and-settings.md).
- The mod is plugin code. A user cannot edit it from the project; the user can only
  disable the plugin, which also stops its setup. The ledger tools' gates apply
  whatever the files say.
- A session in which the plugin does not load has no mod, so no hook gates it. Two
  guards stop that case. The launcher refuses to start the Oracle unless `claude plugin
  list` shows sentinel-swarm enabled. The ledger refuses `run_start` and `brief_ack`
  from a session the mod never checked in for. See "Launch" in
  [11-setup-and-settings.md](11-setup-and-settings.md) and "The mod check-in" below.

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent` | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell` | all |
| `PreToolUse` | `Monitor` | `pre_monitor` | all |
| `PreToolUse` | `SendMessage` | `pre_send_message` | all |
| `PreToolUse` | `Skill` | `pre_skill` | Driver |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | `SendMessage\|PushNotification\|Monitor\|Write\|Edit\|MultiEdit\|NotebookEdit` | `post_any` | all |
| `PostToolUse` | all, `async: true` | `post_activity` | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | Coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

## The shim

`.sentinel-swarm/hook.py` is a standard-library shim that setup copies from
`templates/hook_shim.py`. The mod runs it as its fallback, as
`python3 .sentinel-swarm/hook.py hook <event>` and then `python …`. The MCP launcher
runs `hook.py mcp`, and the watchdog listener runs `hook.py watch`. No role file runs
it as a command hook. It has these commands:

- `hook <event>` posts the hook input to the running ledger server at
  `POST /hook/<event>`, on the port in `.sentinel-swarm/server.json`, with the repo
  root in the `X-Sentinel-Swarm-Repo` header and the token from
  `.sentinel-swarm/http-token` as `Authorization: Bearer <token>`, and prints the
  answer. See "The hook route" in [06-ledger-server.md](06-ledger-server.md). It falls
  back when there is no `server.json`, no token that matches `[A-Za-z0-9_-]{32,128}`,
  the connection is not accepted within 0.25 seconds, no answer comes
  within 10 seconds (40 for `stop` and `session_end`), or the answer is not a 200 that
  carries the repo header. The fallback finds the sentinel-swarm install for this repo in
  `~/.claude/plugins/installed_plugins.json` (scope `local`, then `project`, each with a
  matching `projectPath`, then `user`), and runs
  `uv run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.hooks <event>`
  with stdin and stdout passed through. Both paths print the same bytes.
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

When the server does not answer and then the registry, the install, `uv`, or the
ledger hook fails, or the two paths together run longer than 50 seconds, a gating event (`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`,
`pre_send_message`, `pre_skill`, `pre_ledger`) answers `deny` with the reason, and every event adds
a `systemMessage` that says to run `/sentinel-swarm:setup`. The shim exits 0.

## The mod

`hooks/register.ts` runs the ledger hooks inside the session's own Claude Code
process. It posts the hook input as JSON to the `POST /hook/<event>` route, with the
token and the repo header, and maps the answer onto the function-hook result. A post
costs the server's own time plus a few milliseconds; the shim's process costs about
380 to 770 ms more (see [13-platform-facts.md](13-platform-facts.md)).

- **Which sessions.** At `classic.SessionStart` the mod takes every session whose
  `agent_type` is `swarm-<role>` on the main thread (no `agent_id`, so not a subagent).
  It keeps the session id, the transcript path, the folder, and the role in `$.state`,
  so a reload of the module keeps them. Every hook returns `next(e)` at once in any
  other session. The mod does not read the role file.
- **Gates.** A `tool.call` hook per gating event (`pre_agent`, `pre_write`,
  `pre_shell`, `pre_monitor`, `pre_send_message`, `pre_skill` for the Driver,
  `pre_ledger`) posts the classic `PreToolUse` input, with `agent_id` set to the
  loop's id for a subagent's call. A `deny` answer becomes `{ deny }`; an
  `updatedInput` answer rewrites the call's arguments, which is how `pre_ledger`
  stamps `agent_id`. The mod's own `$.session.send` runs as a `SendMessage` call
  through these hooks, addressed to the target's `uds:` pipe and not to a session
  name, so `pre_send_message` would refuse it. The `SendMessage` gate passes a call
  whose `message` is a wake-up text the mod is sending at that moment: the ledger
  ran the recipient check when it listed that wake-up.
- **Fail closed.** When the post fails (no `server.json`, no valid token, no 200 with
  the repo header, or no answer within 10 s), the mod runs the shim itself, `python3`
  then `python .sentinel-swarm/hook.py hook <event>`, which tries the server and then
  `uv run`. When neither answers, a gate denies with "sentinel-swarm cannot check
  this call: <reason>; run /sentinel-swarm:setup". Each gate also carries a `.catch`
  that denies when the hook throws or outruns its budget.
- **Other events.** `classic.SessionStart` adds the answer's `additionalContext`;
  `classic.Stop` maps `decision: block` to `block`; `classic.PostToolUse` posts
  `post_any` and waits for it after the tools `post_any` covers, posts `post_activity`
  without waiting after every other tool, with `sentinel_swarm_fired_at`, and posts
  `post_shell` for a Coder's shell; `classic.PreCompact` and `classic.SessionEnd`
  post theirs. A failed post here allows, as a failed shim does. Every payload
  carries `sentinel_swarm_transport: "mod"`.
- **Owed wake-ups and the inbox.** The mod posts `owed` and `wake_sent` to pay the
  session's wake-ups, and `inbox_take`, `inbox_ack`, and `inbox_release` to read the
  inbox inside a wake-up. See "Mod wake-up delivery" and "Messages" in
  [05-sessions.md](05-sessions.md).
- **The mod check-in.** The mod's `SessionStart` post carries
  `sentinel_swarm_transport: "mod"`, and the ledger records a `mod_sessions` row for the
  session. A session with no row has no mod, so no hook gates it. The ledger refuses
  `run_start` and `brief_ack` from such a session; see "What each hook does" below
  and `identity.require_mod_session`.
- **Tests.** `hooks/register.test.ts` runs under `claude plugin test .`.

## Rules every ledger hook follows

- A hook blocks by printing a JSON decision and exiting 0. It never uses exit code 2.
- A broken ledger hook allows: `swarm_ledger.hooks` catches every exception, writes one
  line to stderr, and prints nothing.
- Apart from `pre_write`'s records-folder rule, `pre_ledger`, and `pre_monitor`'s
  `agent_type` check, a hook ignores a caller the registry does not know, so a non-swarm
  session in the same repo passes. An agent that has ended counts as unknown.

## What each hook does

- `session_start`: records a `mod_sessions` row for the session when the input
  carries `sentinel_swarm_transport: "mod"`, before it makes any other call. The ledger
  refuses `run_start` and `brief_ack` from a session with no row; see "The mod
  check-in" above. This hook's own `brief_ack` for a child passes, because the row
  exists by then. For a swarm session, it records the transcript path and sets an idle
  agent to working. For a Manager, Lead, Coder, or Driver, it also makes the start
  calls: `brief_ack` when the agent is still `registered`, then `ledger_info`,
  `brief_get`, `guidelines_get`, and, for a Manager or Lead, `run_status`, returned as
  `additionalContext` under 9,500 characters. A refused `brief_ack` comes back as the
  context. When the brief is in the context, it records `briefs.last_read_by_child_at`.
  See "Start calls" in [05-sessions.md](05-sessions.md). For the Oracle it returns
  nothing. For any other session, it reports an active or paused run; checks
  the plugin registry for codebase-kg, reports it when it is not installed, and, only
  once it is installed, reports a missing `knowledge/code_graph.db`; and adds
  `.sentinel-swarm/` to the git excludes.
- `pre_agent`: denies `Agent` to every swarm session, unconditionally, except a Driver
  session calling cartographer's `map-driver` or `map-reviewer` subagent, which it
  allows under the bare name or the plugin form (`cartographer:map-driver`,
  `cartographer:map-reviewer`). Another plugin's agent of the same name is denied.
  No override rule covers it: the rule that no role runs an arbitrary subagent
  has no legitimate exception, so `pre_agent` never calls `override_consume`.
- `pre_send_message`: denies a `SendMessage` whose `to` does not name a registered
  agent's `session_name` in the caller's own run, and lists the valid names in the
  reason. Within the run, it allows the caller's parent, children, and siblings, a
  session the caller owes an unsent wake-up, and, for the Oracle, any live agent that
  is not working; it denies any other target and lists the allowed ones. See
  "Messages" in [05-sessions.md](05-sessions.md). A caller the registry does not know,
  or one with no run yet, passes.
- `pre_skill`: in the Driver's agent file only. For a Driver with an open exploration,
  it records `drive_requests.map_test_at` when the skill in `tool_input.skill` is
  `map-test` or `cartographer:map-test`, and denies `map-explore` or
  `cartographer:map-explore` while `map_test_at` is empty, naming `map-test`. Any other
  skill, and any other caller, passes. No override rule covers it. Claude Code fires
  `PreToolUse` with the `Skill` matcher when the model calls the tool, not when a user
  types `/skill`; the hooks reference says so, and the field name comes from the
  `Skill` tool's input schema. It has not run live.
- `pre_write`: while a run is active or paused, denies anyone a write into the records
  folder, unconditionally; the records folder is the ledger's own state, so no
  override rule covers it either. Denies a write by any role but the
  Coder, and a Coder's write outside its claimed path and test path, naming the owner;
  an override of rule `write` lets one of these two writes through.
- `pre_shell`: while a run is active or paused, denies the shell to every role but the
  Coder and the Driver. A Coder may run a command that starts with the profile's test,
  build, or lint command, or read-only git (`status`, `diff`, `log`, `show`,
  `ls-files`, `branch`). A Driver may run only a command that starts with the profile's
  `build_command`. A command with `;`, `&`, `|`, `<`, `>`, a backtick, a newline, or
  `$(` is denied. An override of rule `shell` lets one command through.
- `pre_monitor`: allows exactly one `Monitor` call from a swarm session, the Oracle's
  watchdog call. It denies every other one and names the allowed call. A session not
  yet in the ledger counts as a swarm session when its `agent_type` is `swarm-<role>`.
  No override rule covers it.
- `pre_ledger`: stamps `agent_id`, and denies `override_grant` to anyone but the
  Oracle. It denies a swarm session still in state `registered` every ledger tool but
  `brief_ack` and the five tools that take no identity, and names `brief_ack`. This is
  the fast path: the ledger server refuses the same calls from any client; see "Tools"
  in [06-ledger-server.md](06-ledger-server.md). On a `brief_get` whose `child_name` is the calling session's own agent name,
  it records `briefs.last_read_by_child_at`, which `handoff_submit` reads. The identity stamp itself takes no override: faking `agent_id` is what the
  stamp exists to prevent. The stamp is the hook input's `agent_id`, or
  else its `session_id`. For the five tools that take no identity (`ledger_info`,
  `brief_get`, `who_owns`, `directive_submit`, `events`), it removes `agent_id`
  instead. It answers `allow`, so a ledger call never asks for permission.
  No tool schema lists `agent_id`: the ledger server's middleware takes
  the stamped value out of the arguments. See "Tools" in
  [06-ledger-server.md](06-ledger-server.md).
- `post_any`: runs synchronously after `SendMessage`, `PushNotification`, `Monitor`,
  and the write tools, because the Stop hook and `handoff_submit` read what it records.
  It writes the heartbeat and current activity, sets an idle agent to working,
  records the transcript path, records the watchdog arm time, clears owed wake-ups on a
  `SendMessage` and then releases a Driver whose exploration is closed and that owes
  nothing more, and after a Coder's write marks its file stale, so the handoff needs a
  newer self review. On the Oracle's `PushNotification` call, it marks one owed
  notification sent, whatever the call's result, even after the run finished.
- `post_activity`: runs with `async: true` after every tool, so no tool call waits for
  it. It returns at once for the tools `post_any` covers, so the two never both run for
  one call. Otherwise it writes the heartbeat and current activity and records the
  transcript path. It sets an idle agent to working only when the agent's last move to
  `idle` came before `sentinel_swarm_fired_at`: an async hook can land after the Stop
  hook, and the tool call it reports came before the stop. After a `Grep` or `Glob` by a
  swarm session, it writes a `graph_gaps` row: the run, the agent, the tool, the
  pattern, the path, and the first 20 result paths from `tool_response`. codebase-kg's
  own gate decides whether the search may run; this only records it.
- `post_shell`: after a Coder's shell call, lists changed paths with `git status`.
  A change outside the Coder's claim, other claimed files of the run, the records
  folder, and `knowledge/` is recorded as a `violation` event and posted to its Lead.
- `pre_compact`: adds one to the agent's `context_overflow_count`.
- `stop`: records tokens and cost from the transcript, then applies the stop rules in
  [05-sessions.md](05-sessions.md). For a Manager, Lead, Coder, or Driver it blocks
  while the caller owes a wake-up or has unread messages in its run. The block names
  each owed call first and `message_inbox` last. It does not block a stop that follows
  its own block (`stop_hook_active`). It names the owed calls at once, with no wait; see
  "Wake-up delivery" in [05-sessions.md](05-sessions.md). A Driver whose exploration is closed
  and that owes nothing more is released instead of set idle. After a run finishes, the
  Oracle's stop refreshes the report, then blocks while the Oracle still owes a
  `PushNotification` call.
- `owed`, `wake_sent`, `inbox_take`, `inbox_ack`, `inbox_release`: posted by the mod
  only. See "Mod wake-up delivery" and "Messages" in
  [05-sessions.md](05-sessions.md). Each ignores a caller the registry does not know.
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
| The Oracle is woken directly when the Driver ends an exploration or cannot load | `drive_done` and the Driver's `drive_unavailable` owe the Oracle a wake-up and do not release the Driver; the Driver's `stop` blocks until it is sent; `post_any`, `agent_resume`, or the Driver's `stop` releases the Driver once it is sent; a Driver that ignores the block twice goes idle without sending, which the watchdog reports as `driver_unsent` |
| Fixes always name their finding ids | `brief_create` refuses an Oracle brief without `finding_ids` while the run has an open finding, and an unknown finding id from any role; `brief_get`, `status_tree`, and the report show the ids and titles |
| A fix Coder works on a fresh claim | `brief_create` refuses a Coder brief whose findings were recorded after the file's claim; `brief_get` returns each finding's steps, results, and evidence |
| A Driver starts only through `drive_request` | `brief_create` refuses `child_role="driver"` |
| A node that spans files is written through the Lead | `graph_upsert` refuses a Coder's anchor outside its own file and a Lead's anchor outside its module's files |
| A stopped bug gets no more fixes, and its evidence reaches the user | `brief_create` and `agent_spawn` refuse a brief whose `finding_ids` name it; `drive_done` puts the evidence in the directive and the notification |
| A pattern of bugs pauses fixes in that area | `brief_create` and `agent_spawn` refuse a finding in the area while the pattern's directive is open |
| After 3 waves that fix nothing, the Oracle reports what is left | `drive_done` records the directive and the notification that list what is left; the report shows both |
| Every dimension is scored on every review | `score_record` refuses a set that leaves a dimension out |
| A model comes from the approved list | `brief_create` |
| No agent starts without a brief | `agent_spawn` and `brief_ack`, which `session_start` calls |
| No session runs without the mod's hooks | The launcher runs `claude plugin list` and refuses to start the Oracle unless sentinel-swarm shows enabled (`sessions.plugin_load_problem`); `pool.run_tool` calls `identity.require_mod_session`, which refuses `run_start` and `brief_ack` from a session with no `mod_sessions` row |
| A child does no ledger work before it binds | `server._call` refuses a `registered` session every tool but `brief_ack` and the tools that take no identity, for any client; `pre_ledger` denies the same calls first |
| No agent fakes its identity | `pre_ledger` stamps `agent_id`; the server's middleware takes it out of the arguments; every tool matches `caller` to it |
| Only the run's own sessions reach the ledger | `serve.Guard` answers 403 to a remote caller, a wrong `Host`, a foreign `Origin`, and a missing or wrong token on every path but `/health`; `session_options` sends the token only to a checked `http://127.0.0.1:<port>/mcp` URL |
| A role calls only its own ledger tools | The role file's `tools` allowlist, which names each `mcp__swarm-ledger__<tool>` in `identity.ROLE_TOOLS`; `server._call` refuses a live agent's call outside its set |
| A handoff needs passing tests and a current graph | `handoff_submit` |
| No approval without a handoff and two sets of scores | `approve` |
| The Lead scores before it sees the Coder's scores | `score_record` refuses a Lead review after `review_compare` |
| A rule gives way only to the Oracle | `override_grant`, consumed by `pre_write` and `pre_shell` |
| An override holds for one run only | `override_consume` matches the caller's run as well as the rule, the agent name, and the target |
| The Oracle never grants itself a write or shell override | `override_grant` refuses a `target_agent_name` that names the Oracle |
| Look in the graph before writing | codebase-kg's own search gate hook |
| A message goes to an agent of the run | `message_post`; `pre_send_message` for `SendMessage` itself |
| A Manager directs a Coder only through its Lead, and the Oracle a Lead or Coder only through its Manager | `message_post` allows only the caller's parent, children, and siblings; `pre_send_message` allows those, owed wake-ups, and the Oracle's waiting agents |
| Helpers and contracts come before the files and modules that use them | `claim_file` and `module_add` record `depends_on`; `brief_create` refuses a dependent's brief until each dependency's latest brief has a contract; `brief_get` returns the contracts |
| A new Coder for a running module comes through its Manager and Lead | `brief_create` scope checks: a Manager briefs a Lead only for a module of its phase, a Lead a Coder only for a file its module claimed for that name |
| Names follow the patterns | `phase_add` prefixes the ordinal; `module_add` refuses a non-slug; `brief_create` and `claim_file` refuse a name off the pattern |
| A phase unlocks only when its dependencies are approved | `phase_update(unlocked)`; `brief_create` and `agent_spawn` refuse a Manager of a planned phase |
| Work is accepted as incomplete only for a reported reason, after a blind review | `accept_incomplete` refuses an empty reason, a handoff without `review_compare`, and work with no open issue |
| A deferral is decided at its responsible level, and a PRD change by the user | `deferral_propose(kind)`; `agreement_decide` checks the kind's level and, for `prd`, a later `user_chat` directive |
| A dispute is decided by the closest shared ancestor of its parties | `deferral_propose(parties)` records the arbiter and owes it a wake-up; `agreement_decide` accepts only the arbiter |
| The Driver tests a build, not a server that reloads | `profile_set` refuses a build command that serves or watches; `pre_shell` limits the Driver to `build_command` |
| The Driver runs `map-test` before `map-explore` | `pre_skill` |
| A role's fallback search is recorded | `post_activity` writes a `graph_gaps` row for each `Grep` and `Glob`; the report lists them |
| A Coder re-reads its brief after a return | `pre_ledger` records the read; `handoff_submit` refuses without one since the last return |
| An agent reads only its own run's mail, in bounded batches | `message_inbox` filters by `run_id` and name and caps one call at 40,000 characters; `message_post` refuses a body over 32,000 |
| A Manager, Lead, Coder, or Driver reads its mail before it stops | The member `stop` blocks once while the caller has unread messages in its run |
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
| A child wakes its parent after each step | The Stop hook, from the owed wake-ups |
| The Oracle keeps working while the run has work, unless a directive waits on the user | The Oracle's Stop hook |
| The Oracle's watchdog listener stays armed | The Oracle's Stop hook and `pre_monitor` |
