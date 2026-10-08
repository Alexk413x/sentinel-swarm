# Setup, launch, and settings

## Install

sentinel-swarm runs in Claude Code only: the CLI, the desktop app, or an IDE extension.
Every workflow starts `claude --bg` sessions from a local shell, the role gates are
frontmatter hooks in project agent files, and each host repo runs its own ledger server.
claude.ai chat ignores agents, hooks, and local servers, and Cowork runs shell commands
in a VM without the `claude` CLI, so neither surface can run a swarm. The plugin is not
uploaded to claude.ai or added to organization sync. A future launcher stays out of a
top-level `bin/` folder.

1. Install sentinel-swarm at project scope in the host repo. `--plugin-dir` is not
   enough, because the launcher does not resolve its agents. codebase-kg must be
   installed too. Install cartographer and a driver plugin (android-driver, ios-driver,
   or web-driver, from the accessibility-tools marketplace) as well for a Driver;
   without them the run finishes on unit tests alone.
2. Trust the host folder once: run `claude` in it and accept the trust prompt.
   The project-scope install is what loads the plugin's mod in each role session; no
   other step installs it. `claude --bg` refuses an untrusted folder, so the mod never
   runs in a role session there. The plugin, mod included, stays unloaded while a
   `dependencies` entry is not installed: the session's debug log says
   `dependency-unsatisfied`, and the role sessions then run no ledger hook at all
   under the `mod` transport.
3. Run setup, then the launcher.

## The ledger's venv

Every `uv run` of the plugin's own `mcp` project uses one venv outside the plugin cache:
`<config>/plugins/data/sentinel-swarm-sentinel-swarm/venv-<key>`, where `<config>` is
`$CLAUDE_CONFIG_DIR` or `~/.claude`, and `<key>` is the first 12 hex digits of the
SHA-256 of `mcp/uv.lock`. `uv` gets it as `UV_PROJECT_ENVIRONMENT`.

- `${CLAUDE_PLUGIN_ROOT}` changes on every update, so a venv inside the cache folder
  would be rebuilt, about 82 MB, for each version.
- Two versions with the same lock share one venv, and a version with a new lock gets a
  new venv, so an update never syncs a venv that a running session still uses.
- `mcp/ledger_venv.py`, standard library only, computes the path. The skills run it
  and pass its output as `UV_PROJECT_ENVIRONMENT`. The hook shim computes the same path
  itself, because it cannot import the file, and a test checks that the two agree.
  Neither reads `CLAUDE_PLUGIN_DATA`, which is not reliable inside an agent-file hook.
- The `swarm_ledger` package removes `UV_PROJECT_ENVIRONMENT` from its own environment
  at import when it names the package's own venv, so no child process inherits it. A
  child `uv run`, such as `graph_upsert`'s fallback or the host's test command, would
  otherwise sync its own project into the ledger's venv.
- `scripts/smoke.sh` uses the same venv for its `uv sync` and `uv run` calls.

## Setup

`python -m swarm_ledger.setup [--repo <root>] [--check-trust]`, or the
`/sentinel-swarm:setup` skill, which also creates the settings file, fills in the
commands from the detected stack, confirms codebase-kg, and builds the graph. Setup:

- Picks the hook transport and reports it first: `hook_transport` from the settings
  file, or else `mod` when `claude --version` is 2.1.294 or later and `command`
  otherwise, including when the version cannot be read. See "Where the hooks live" in
  [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).
- Under `mod`, writes each role file without the template's ledger command hooks,
  and removes them from an existing file, keeping any hook of the user's own. Under
  `command`, the rules below add them back.
- Writes `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and
  `swarm-coder.md` from `templates/agents/<role>.md`. For an existing file it keeps the
  user's frontmatter, adds any key the template has that the file lacks, adds every
  ledger hook the template has that the file lacks, and replaces the prompt body.
  When the file's one-line `tools` value still lists the whole server,
  `mcp__swarm-ledger`, setup puts the template's `mcp__swarm-ledger__<tool>` names in
  its place and keeps every other tool the file lists. A `tools` value that already
  names ledger tools one by one, or a block-form list, stays as it is.
  A hook entry that is exactly what an earlier template shipped, such as the
  unmatched synchronous `post_any` entry, is replaced with the template's entry; an
  edited entry is kept.
  A new file's MCP server `command` is `python` on Windows, and `python3` elsewhere
  when `python3` is on `PATH`.
- Writes `swarm-driver.md` the same way, but only when the host has
  `cartographer@cartographer` installed and at least one of `android-driver`,
  `ios-driver`, or `web-driver` from the `accessibility-tools` marketplace. Otherwise it
  reports the file skipped and leaves it alone.
- Adds no MCP server beyond the template's. See "MCP servers per role" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- Writes the shim to `.sentinel-swarm/hook.py`, overwriting local edits.
- Merges `{"worktree": {"bgIsolation": "none"}}` into `.claude/settings.local.json`,
  and adds `Agent(swarm-oracle)`, `Agent(swarm-manager)`, `Agent(swarm-lead)`,
  `Agent(swarm-coder)`, and `Agent(swarm-driver)` to its `permissions.deny` list, so
  an ordinary session in the host repo cannot run a role as a subagent. An exact-name
  `Agent(<name>)` rule does not block `claude --agent <name>`, so role launches still
  work. A `permissions.deny` that is not a list leaves the file unchanged.
- Sets `post_edit_nudge: false` in `.claude/codebase-kg.local.md`, adding the key to
  an existing file's frontmatter, or leaves the file alone when it already sets the
  key. See "The code graph" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- Adds `.sentinel-swarm/`, `.claude/agents/swarm-*.md`, and
  `.claude/codebase-kg.local.md` to `.git/info/exclude`.
- Reports whether the repo is trusted, from `~/.claude.json` (or
  `$CLAUDE_CONFIG_DIR/.claude.json`), and prints the command that trusts it.
  `--check-trust` only checks, and exits 1 with the command when the repo is not
  trusted.

The role files take effect in the next session. A session loads agent files once, at
startup.

## Launch

`python -m swarm_ledger.launch [--repo <root>] [--bg | --headless [--transcript <file>]] "<prompt>"`,
or the `/sentinel-swarm:run` skill.

- It runs setup when the shim or one of the four core role files is missing. A missing
  `swarm-driver.md` alone does not run setup.
- `--bg` and `--headless` refuse an untrusted repo and print the trust command. A
  `claude -p` session does not count as accepting the trust prompt, so a headless
  Oracle in an untrusted folder would run without its role file's hooks.
- It starts the ledger server, or reuses one that answers, then the Oracle with its
  agent file's model and the same flags as `agent_spawn`, including the ledger token
  header. It stops with the reason when the URL in `server.json` or the token fails
  `session_options`' checks; see [06-ledger-server.md](06-ledger-server.md). If the file has no `model`, it uses the first entry of the
  Oracle's approved list.
- Interactive by default. `--bg` starts a background session. `--headless` runs
  `claude -p` with a stream-json transcript and the prompt on stdin. Nobody answers
  questions in a headless run, so the Oracle records each assumption in the guidelines.
- It removes the variables a parent Claude Code session sets for its own children,
  such as `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ID`, and `CLAUDECODE`,
  from the Oracle's environment, and `agent_spawn` and `agent_resume` do the same for
  every child (`sessions.PARENT_SESSION_VARS`). A session started with them never
  registers, so `claude agents` and a send by session id miss it.
- No launch loads a channel of its own. The development-channel flag appears only when
  `CLAUDE_DEV_CHANNELS` names the user's own channels; see "Starting a child" in
  [05-sessions.md](05-sessions.md).

## Skills

| Skill | Purpose |
|---|---|
| `swarm-protocol` | Shared vocabulary, names, tool order, and rubric keys; loads when the swarm is mentioned |
| `run` | Starts a run with the launcher. User-invoked only |
| `status` | Reads `ledger.db` read-only and prints the run's state |
| `resume` | Continues a paused or crashed run, one session at a time. User-invoked only |
| `setup` | Prepares a host repo |

`run` and `resume` set `disable-model-invocation: true`, because each starts paid
background sessions. A user starts them with `/sentinel-swarm:run` and
`/sentinel-swarm:resume`; the model never starts them from a casual request, and their
descriptions stay out of every session's context.

## Settings file

`.claude/sentinel-swarm.local.md` in the host repo, YAML frontmatter. The template is
`templates/sentinel-swarm.local.md.example`. The file is not committed.

| Key | Default | Meaning |
|---|---|---|
| `tracking` | `local` | The only value |
| `runtime.<role>` | `session` | Recorded on each agent row; every role runs as a session |
| `models.<role>` | See "Models" in [01-roles.md](01-roles.md) | Approved models per role |
| `rubric.*` | See "Thresholds" in [03-scoring-and-review.md](03-scoring-and-review.md) | `target`, `floor`, `criterion_floor`, `disagreement_gap`, `plateau`, `regression_tolerance` |
| `escalation.rounds` | 3 | Escalation rounds |
| `escalation.attempts_per_round` | 3 | Non-improving attempts per round |
| `watchdog.*` | See "Watchdog" in [08-watchdog.md](08-watchdog.md) | Watchdog timing, thresholds, and server idle exit |
| `test_command` | empty | Must contain `{target}`, for example `python -m pytest -q -p no:cacheprovider {target}` |
| `build_command`, `lint_command` | empty | Optional; the Coder's shell gate allows them, and the Driver's allows `build_command` only |
| `parallelism_cap` | empty | No limit when empty; also counts other swarms' live sessions on the machine |
| `time_budget_minutes` | empty | Advisory minutes for the run. Each wake-up and watchdog line for the Oracle or a Manager ends with `elapsed <n>s / <budget>s`, or `elapsed <n>s` when empty. A value that is not a positive whole number reads as empty. See "Time signal" in [05-sessions.md](05-sessions.md) |
| `base_branch` | empty | `repo_check` tries `main`, then `master` |
| `effort.<role>` | `medium` for every role | `--effort <level>` for that role's sessions. A settings file with no `effort` key gets `medium` for every role; a role left empty runs at its model's default. A brief's own `effort` overrides it |
| `prompt_cache_ttl.<role>` | `5m` for the Coder, empty for the others | `promptCacheTtl` (`"5m"` or `"1h"`) for that role's sessions |
| `role_parallelism_cap.<role>` | empty | Cap on that role's own live sessions in the run |
| `hook_transport` | empty | `mod` or `command`. Empty lets setup pick `mod` on Claude Code 2.1.294 or later and `command` otherwise. Any other value reads as empty. See "Setup" above |
| `notify` | `[os, push]` | How the user hears that the Driver finished or hit an error, or that an issue ended round 3 below the floor. `os`: the ledger server shows a desktop notification. `push`: the Oracle owes a `PushNotification` call. `[]` or `""` turns both off; a single value reads as a one-item list; unknown values are dropped; an unset key, or `notify:` with no value, keeps both. See "Driver notifications" in [02-run-lifecycle.md](02-run-lifecycle.md) |
| `max_workers` | 8 | Worker processes for the ledger server's tool calls, and as many again for its hooks. `0` runs every call in the server process, one at a time. A negative value reads as `0`; a value that is not a whole number reads as 8. The server reads it at start. See [06-ledger-server.md](06-ledger-server.md) |

`-p no:cacheprovider` keeps parallel pytest runs from contending on `.pytest_cache`.

## Plugin features

Evaluated against the plugin manifest and settings reference:

- **`userConfig`.** Built. `plugin.json` declares one option, `test_command`, prompted
  when the user enables the plugin at project scope. The `setup` skill uses
  `${user_config.test_command}` verbatim when the user set it, instead of detecting the
  stack. The prompt is a one-time install step, not something that runs in a session.
- **`experimental.monitors` (`monitors/monitors.json`).** Not built. A monitor starts
  at session start (`when: "always"`, the default) or the first time a named skill
  runs, and keeps running as a background process for the rest of that session,
  in every session that has the plugin enabled, not only a session running a swarm
  role. The watchdog listener is already scoped correctly: the Oracle arms it itself,
  per run, with `Monitor(...)`. A plugin-level monitor would instead start the listener
  in a host repo's ordinary sessions that never run a swarm.
- **`subagentStatusLine`.** Not built. It labels Claude Code's own Agent-tool subagent
  mechanism. Every role is its own Claude Code session; only the Driver carries the
  `Agent` tool, and only for cartographer's own `map-driver` and `map-reviewer`
  subagents, which belong to cartographer's own status line, not sentinel-swarm's. The
  setting would have nothing else to attach to.
- **`agent` in the plugin's `settings.json`.** Not built. It replaces a session's main
  thread with one of the plugin's own agents, for every session that has the plugin
  enabled. sentinel-swarm ships no plugin-level agents to name (the five roles are
  templates setup writes into the host repo), and forcing a role's persona onto a
  user's ordinary session in that repo would be exactly the kind of non-swarm-session
  effect the plugin must avoid.
