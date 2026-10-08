# sentinel-swarm hooks

`hooks.json` carries no command hooks. It names one module, `register.ts`: the
plugin's mod. The mod is the only hook transport. Every role runs as its own Claude
Code session, and the mod runs every hook in the table below inside that session's own
process. The role files (`.claude/agents/swarm-<role>.md`) carry no `hooks`
frontmatter, and `python -m swarm_ledger.setup` removes any ledger command hook it
finds in one.

The mod acts only in a session whose `agent_type` is `swarm-<role>` and that has no
`agent_id`, so every other session in the host repo is left alone.
`register.test.ts` holds its tests; run them with `claude plugin test .`. See "The mod"
in `knowledge/prd/07-hooks-and-enforcement.md` for the full contract.

`claude --bg` starts a role session only in a trusted folder. The `setup` skill reports
whether the host repo is trusted and prints the one command to trust it.

The mod runs only when Claude Code loads the plugin. The launcher refuses to start the
Oracle unless `claude plugin list` shows sentinel-swarm enabled, and the ledger refuses
`run_start` and `brief_ack` from a session the mod never checked in for.

## How a hook reaches the ledger

The mod posts each hook's input to `POST /hook/<event>` itself, with the token and
the repo header, and falls back to running the shim when the post fails. It also
posts `owed`, `wake_sent`, `inbox_take`, `inbox_ack`, and `inbox_release`: it pays the
session's owed wake-ups with `$.session.send` and reads the inbox into a delivered
wake-up.

The fallback runs this command from the host repo root:

```
python3 .sentinel-swarm/hook.py hook <event> || python .sentinel-swarm/hook.py hook <event>
```

`.sentinel-swarm/hook.py` is the shim that setup copies from
`templates/hook_shim.py`. It uses the standard library only. The mod's fallback,
`hook.py mcp`, and `hook.py watch` use it.

The shim first posts the hook input to the repo's running ledger server, at
`POST /hook/<event>` on the port in `.sentinel-swarm/server.json`, and prints the
answer. The server runs the same handler as `python -m swarm_ledger.hooks <event>`, so
the answer is byte for byte what the subprocess would print, without starting two
interpreters.

When there is no `server.json`, the server does not accept the connection within
0.25 seconds, it does not answer within 10 seconds (40 for `stop` and `session_end`),
or it answers anything but 200 with the repo header, the shim falls back. It finds the
sentinel-swarm install for the repo in `~/.claude/plugins/installed_plugins.json`,
in this order: scope `local`, then `project` with a matching `projectPath`, then
`user`. It then runs `uv run --project <installPath>/mcp --frozen --no-dev python -m
swarm_ledger.hooks <event>` and passes stdin and stdout through.

Every `uv run` of the shim sets `UV_PROJECT_ENVIRONMENT` to
`<config>/plugins/data/sentinel-swarm-sentinel-swarm/venv-<first 12 hex of sha256(mcp/uv.lock)>`,
where `<config>` is `$CLAUDE_CONFIG_DIR` or `~/.claude`, so the venv lives outside the
versioned plugin cache. `mcp/ledger_venv.py` prints the same path for the skills.

The per-event behavior lives in `mcp/src/swarm_ledger/hooks/events.py`, as functions
`handle_<event>(ledger, data)` that tests call directly, in-process.

## Hooks per role

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent`, which denies, except a Driver's `map-driver` and `map-reviewer` calls | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell`, build-command only for a Driver | all |
| `PreToolUse` | `Monitor` | `pre_monitor` | all |
| `PreToolUse` | `SendMessage` | `pre_send_message` | all |
| `PreToolUse` | `Skill` | `pre_skill`, which denies `map-explore` before `map-test` | driver |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | `SendMessage\|PushNotification\|Monitor\|Write\|Edit\|MultiEdit\|NotebookEdit` | `post_any` | all |
| `PostToolUse` | all, `async: true` | `post_activity`, which skips the tools `post_any` covers | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`, `pre_send_message`, `pre_skill`, and
`pre_ledger` are the gating events.

`post_any` stays synchronous after the tools whose records the Stop hook and
`handoff_submit` read: owed wake-ups, notifications, the watchdog arm time, and stale
files. `post_activity` runs in the background after every other tool and records only
the heartbeat, the current activity, and the transcript path, sets an idle agent to
working, and records each `Grep` and `Glob` as a graph gap. The mod and the shim stamp the input with the time it fired, so a `post_activity` that
lands after the agent's Stop hook does not set the agent working again.

The Driver is the one role whose `swarm-driver.md` carries `Agent` in its `tools` and
gets a shell: `pre_agent` allows only its calls to cartographer's `map-driver` and
`map-reviewer` subagents and denies every other `subagent_type`, and `pre_shell` allows
only a command that starts with the profile's `build_command`. Every other role's file
still has no `Agent` tool and no shell, so `pre_agent` and `pre_shell` deny them
unconditionally, as before.

`pre_send_message` denies a `SendMessage` whose `to` is not the `session_name` of a
registered agent of the caller's own run, and names the valid session names in the
reason. Within the run, it allows the caller's parent, children, and siblings, any
session the caller owes an unsent wake-up, and, for the Oracle, any live agent that is
not working. A caller the registry does not know, or one with no run yet, passes.

`pre_skill` runs for the Driver only. While the Driver has an open exploration, it
records a `map-test` or `cartographer:map-test` call and denies `map-explore` or
`cartographer:map-explore` until one is recorded. Every other skill passes.

`pre_monitor` allows exactly one `Monitor` call from a swarm session: the Oracle's
watchdog listener, with no `ws` input and this command, compared after whitespace is
normalized:

```
python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch
```

It denies every other `Monitor` call from a swarm session and names the allowed call
in the reason. A session that is not yet in the ledger counts as a swarm session when
its hook input carries `agent_type` `swarm-<role>`. A non-swarm caller passes.

## When the fallback cannot run a hook

The mod's post failed, the server did not answer the shim, and then the registry is missing, the plugin is not
installed for the repo, the install's files are missing, `uv` is not on `PATH`, or the
ledger hook fails. Or the two paths together run longer than 50 seconds. Then:

- A gating event answers `deny`, with the reason.
- Every event adds a `systemMessage` with the reason and tells the user to run
  `/sentinel-swarm:setup`.
- The shim exits 0. The mod denies a gating event itself when the shim answers nothing.

## The two rules every ledger hook follows

- **A hook never exits non-zero and never blocks through exit code 2.** It blocks by
  printing a JSON decision (`permissionDecision: "deny"` for `PreToolUse`, `decision:
  "block"` for `Stop`) and exiting 0.
- **A broken ledger hook allows.** Any exception inside `swarm_ledger.hooks` is
  caught at the top level, written as one line to stderr, and the hook prints nothing
  and exits 0. The shim's deny covers only the cases above, where the ledger code
  never ran.

## Who owns the hooks

The role files carry no ledger hooks. The mod is plugin code, so a user cannot edit
a gate from the project; the user can only disable the plugin, which also stops its
setup. The ledger tools' gates, such as `handoff_submit`, `approve`, and
`agent_spawn`, apply whatever the files say.

## MCP servers through the same shim

`hook.py mcp <plugin_id> <server>` starts another plugin's MCP server for a swarm
session. It finds the plugin's install the same way, reads the server's entry from
the install's `.mcp.json`, expands `${CLAUDE_PLUGIN_ROOT}` and `${VAR:-default}`, and
runs it with stdio passed through. The role files use it for codebase-kg.

## The watchdog listener through the same shim

`hook.py watch` finds the sentinel-swarm install the same way as `hook` and runs `uv
run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.watch`. It
passes each stdout line through as it arrives and sets no timeout. The Oracle runs it
as a `Monitor` command, so each line wakes the Oracle. When the install cannot be
found, it prints one line with the reason and exits 1.
