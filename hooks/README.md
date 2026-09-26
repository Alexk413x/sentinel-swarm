# sentinel-swarm hooks

`hooks.json` carries no hooks. Every role runs as its own Claude Code session, and
each role's hooks live in the frontmatter of its project agent file,
`.claude/agents/swarm-<role>.md` in the host repo. `python -m swarm_ledger.setup`
writes those files from `templates/agents/<role>.md`. A hook in both places would run
twice, so each hook lives in exactly one place.

Claude Code runs an agent file's frontmatter hooks only in a trusted folder. The
`setup` skill reports whether the host repo is trusted and prints the one command to
trust it.

## How a hook reaches the ledger

Every hook command has this form, run from the host repo root:

```
python3 .sentinel-swarm/hook.py hook <event> || python .sentinel-swarm/hook.py hook <event>
```

`.sentinel-swarm/hook.py` is the shim that setup copies from
`templates/hook_shim.py`. It uses the standard library only. It finds the
sentinel-swarm install for the repo in `~/.claude/plugins/installed_plugins.json`,
in this order: scope `local`, then `project` with a matching `projectPath`, then
`user`. It then runs `uv run --project <installPath>/mcp --frozen --no-dev python -m
swarm_ledger.hooks <event>` and passes stdin and stdout through. A plugin upgrade
changes the registry, not the agent files.

The per-event behavior lives in `mcp/src/swarm_ledger/hooks/events.py`, as functions
`handle_<event>(ledger, data)` that tests call directly, in-process.

## Hooks per role

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent`, which denies | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell` | all |
| `PreToolUse` | `Monitor` | `pre_monitor` | all |
| `PreToolUse` | `SendMessage` | `pre_send_message` | all |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | all | `post_any` | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`, `pre_send_message`, and `pre_ledger`
are the gating events.

`pre_send_message` denies a `SendMessage` whose `to` is not the `session_name` of a
registered agent of the caller's own run, and names the valid session names in the
reason. A caller the registry does not know, or one with no run yet, passes.

`pre_monitor` allows exactly one `Monitor` call from a swarm session: the Oracle's
watchdog listener, with no `ws` input and this command, compared after whitespace is
normalized:

```
python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch
```

It denies every other `Monitor` call from a swarm session and names the allowed call
in the reason. A session that is not yet in the ledger counts as a swarm session when
its hook input carries `agent_type` `swarm-<role>`. A non-swarm caller passes.

## When the shim cannot run a hook

The registry is missing, the plugin is not installed for the repo, the install's
files are missing, `uv` is not on `PATH`, or the ledger hook fails or runs longer than
50 seconds. Then:

- A gating event answers `deny`, with the reason.
- Every event adds a `systemMessage` with the reason and tells the user to run
  `/sentinel-swarm:setup`.
- The shim exits 0.

## The two rules every ledger hook follows

- **A hook never exits non-zero and never blocks through exit code 2.** It blocks by
  printing a JSON decision (`permissionDecision: "deny"` for `PreToolUse`, `decision:
  "block"` for `Stop`) and exiting 0.
- **A broken ledger hook allows.** Any exception inside `swarm_ledger.hooks` is
  caught at the top level, written as one line to stderr, and the hook prints nothing
  and exits 0. The shim's deny covers only the cases above, where the ledger code
  never ran.

## The user owns the hooks

The hooks in a role's file are the user's to edit, and a user can weaken a hook-based
gate in their own project. The ledger tools' gates, such as `handoff_submit`,
`approve`, and `agent_spawn`, apply whatever the files say.

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
