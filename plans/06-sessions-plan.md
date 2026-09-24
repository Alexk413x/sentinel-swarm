# sentinel-swarm: every role runs as a session

Status on 2026-09-23: draft plan for Alex to review. The decision in the first section is Alex's. Everything after it is **(proposed)** unless a line says otherwise. Background: `notes-agent-view-vs-subagents.md`, and the smoke runs in `04-mcp-and-hooks.md`.

## The decision

Decided by Alex on 2026-09-23:

- The Oracle, every Manager, every Lead, and every Coder runs as its own Claude Code session. The swarm no longer spawns subagents.
- Each session is a row in agent view, with its role's name and color. The user can open, watch, and type into any of them.
- A session that locks up or crashes is resumed on its own, without restarting the run. Nothing is lost: the session keeps its conversation, and the ledger keeps the run's plan, progress, and evidence.
- Hooks live in the agent definitions. Decided by Alex on 2026-09-23: the generated agent files in the host's `.claude/agents/` come first, and each role's hooks go in its own file's frontmatter. Users edit those files directly to control each role.

This replaces the earlier rule "Only the Oracle runs as a session. Manager, Lead, and Coder run as nested subagents."

## What changes, in one table

| Area | Today: subagents | Plan: sessions **(proposed)** |
|---|---|---|
| Starting a child | The parent calls the `Agent` tool | The parent calls a ledger tool, `agent_spawn(child_name)`, which runs `claude --bg --agent <the role's project agent> --name <child_name> ...` in the host repo |
| The spawn gate | Hook 2 checks role, model, and brief | `agent_spawn` checks role, model, brief, and the parallelism cap, and refuses. A ledger gate, not a hook |
| Identity | Hook input `agent_id` | The session's `session_id`, bound to the name by `brief_ack` |
| Finished child | A completion notice to the parent, which skips a parent that ended its turn | The child sends its parent a cross-session `SendMessage` with a one-line pointer, and hooks enforce it |
| Turn end | `SubagentStop` marks the agent idle | The `Stop` hook marks the session idle |
| Release | The ledger marks the row released | The ledger also stops the session with `claude stop <id>`, which frees its memory |
| Crash | The subagent dies with its parent | A watchdog compares `claude agents --json` with the ledger and reports a crashed session to the Oracle, which resumes it with `agent_resume` (`claude --resume <id> --bg`) |
| Nesting | Three layers, the Coder at the limit | No nesting. Every session is a top-level row |
| Permission prompts | A background subagent's prompt is denied | A session shows its prompt in its agent-view row |

## The project agent files

Decided by Alex on 2026-09-22 and 2026-09-23: the plugin's four agent files are templates. Each host project gets its own copies in `.claude/agents/`, and the user edits them to control each role. **(the details below are proposed)**

- The `setup` skill, or a run that finds the files missing, writes `swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and `swarm-coder.md` from the templates. The files are kept out of git through `.git/info/exclude` unless the user commits them.
- Project agent files honor every frontmatter key, including `hooks`, `mcpServers`, and `permissionMode`, which plugin agents ignore.
- What the user controls in each file: `model`, `color`, `tools`, `permissionMode`, `mcpServers`, and `hooks`, plus the prompt body.
- The default `tools` line is the core set plus each plugin the project enables at project scope. Decided 2026-09-22.
- A new session loads agent files when it starts. The Oracle's file must exist before the Oracle starts, so the launcher (`/sentinel-swarm:run`, or `scripts/smoke.sh`) writes the files first. Every child starts as a new session, so it always sees the current files.
- Updating after a plugin upgrade: `setup` replaces the prompt body from the new template and keeps the user's frontmatter. It reports any key the new template adds.
- The hooks in a role's file are the user's to edit. A user can weaken a hook-based gate in their own project. The ledger tools' gates, such as `handoff_submit`, `approve`, and `agent_spawn`, still apply whatever the files say.

## Starting a session

- `agent_spawn(child_name)` reads the child's brief for its role and model, checks the gates, and starts the session with the role's project agent file. It records the background id and the `session_id` on the child's `agents` row, so the ledger can wake, stop, and resume it.
- The prompt is one line: "You are `<name>`. Read your brief from the swarm ledger and follow it." The no-telephone-game rule is unchanged.
- The session runs in the host repo root, in the one shared working tree. `worktree.bgIsolation` is `"none"`; the `setup` skill writes it to the host's `.claude/settings.local.json`.
- The plugin is installed at project scope, which gives the sessions the ledger server and the skills. `--plugin-dir` is not enough: the launcher does not resolve its agents (verified 2026-09-23).
- The host folder is trusted once. `setup` tells the user to do this; a background session will not start in an untrusted folder with project plugins (verified 2026-09-23).
- No role keeps the `Agent` tool, so no role can start a subagent. A hook denies an `Agent` call from any swarm session, as a second line of defense.

## Waking a parent, and messages

Decided by Alex on 2026-09-23: the child wakes its parent with a cross-session `SendMessage`, and hooks enforce it.

- The ledger cannot send the message itself: `SendMessage` is a tool only a session can call, and the CLI has no command that sends to a session (checked 2026-09-23). An idle session wakes when a cross-session message arrives.
- The ledger tool that finishes a step returns the next step. For example, `handoff_submit` returns "Next: SendMessage(to=<the parent's session>, message=\"Handoff 1 for hello.py is waiting in the ledger.\")". **(proposed)**
- A `PostToolUse` hook on `SendMessage` records in the ledger who the child messaged. The child's `Stop` hook blocks the child from ending its turn until it has messaged its parent after the step. **(proposed)**
- The same rule covers every upward step: a Coder's handoff, a Lead's module report, a Manager's phase hand-up, and a returned file going down to a Coder. **(proposed)**
- A parent whose session has crashed or exited cannot receive a message. The child restarts it with `agent_resume`, which runs `claude --resume <session_id> --bg "<pointer>"` and continues the same session and conversation. `--resume` on a session that is still running starts a copy, so it is never used on a live session. **(proposed)**

## Crashes and resume

- The watchdog, step 3 of the handoff list, reads `claude agents --json`: each session's `state`, `status`, and `waitingFor`. It compares them with the ledger.
- A session that the ledger says is live but that has exited, crashed, or stopped is reported to the Oracle, which resumes it with `agent_resume`: `claude --resume <session_id> --bg "Re-read your brief and your inbox in the ledger."`. It keeps its own conversation, and the ledger supplies the truth. The watchdog itself never resumes a Manager, Lead, or Coder (Alex's rule, `01-roles.md` "Watchdog").
- A session that is `waitingFor` a permission answer is reported to the user through the Oracle, not resumed.
- The `resume` skill does the same for a whole run, one session at a time.

## Hooks

- Each role's hooks live in its project agent file's frontmatter. Decided by Alex on 2026-09-23.
- Plugin-wide `hooks/hooks.json` keeps only what is not tied to one role, if anything. A hook in both places would run twice, so each hook lives in exactly one place.
- Hook changes for sessions:
  - `SessionStart` registers or re-binds the session, and marks a resumed session working.
  - `Stop` marks the session idle, blocks a child that has not messaged its parent after a step, keeps the Coder's once-only "no handoff yet" block, and keeps the Oracle's stop gate.
  - `PostToolUse` on `SendMessage` records who the session messaged.
  - `SessionEnd` closes the row and records tokens from `transcript_path`.
  - `SubagentStart` and `SubagentStop` are removed. They do not fire for sessions.
  - `PreToolUse`, `PostToolUse`, and `PreCompact` are unchanged in what they check.
- Which events an agent file's `hooks` block supports when the agent runs as a session through `--agent` must be verified. See prototype check 4.

## Memory and the parallelism cap

- Each session uses about 530 to 980 MB of physical memory (measured 2026-09-23 across five sessions): 260 to 430 MB for the Claude Code process, and 250 to 610 MB for its helper processes. The helpers are the MCP servers of every plugin enabled for the user, about 14 Python processes, which each session starts for itself. Subagents shared their parent's process and its servers.
- A swarm session needs only the ledger and codebase-kg servers. Starting only those may cut a session to roughly 300 to 450 MB; see prototype check 8.
- Decided by Alex on 2026-09-23: no limit by default. `parallelism_cap` stays empty in the settings template, and `agent_spawn` starts every session the plan calls for.
- A user who sets `parallelism_cap` in `.claude/sentinel-swarm.local.md` gets a limit: `agent_spawn` refuses at the cap, and the parent tries again after a release frees a slot. **(proposed)**
- Release stops the session, so a finished Lead or Coder frees its memory at once.

## MCP servers per session

- Decided by Alex on 2026-09-23: a swarm session starts only the MCP servers its role needs, not every plugin's servers. Each session today starts its own copy of every enabled plugin's stdio servers, about 14 Python processes (measured 2026-09-23).
- A stdio server serves exactly one session. A server that listens over HTTP serves every session that connects to its URL. **(proposed)**
- The ledger runs as one shared HTTP server per host repo, started before the first session and stopped after the run. The port and the process id live in the records folder. **(proposed)** This is required, not only a saving: `graph_upsert` protects the code graph with a lock inside the ledger's process, and one ledger process per session would break that lock.

## Several swarms at once

Checked on 2026-09-23. Swarms in different repos keep separate ledgers, graphs, working trees, and claims. The gaps, each with a fix **(proposed)**:

| Gap | Fix |
|---|---|
| A second `run_start` in the same repo resumes the active run and releases the first Oracle, even while it is working | Decided by Alex on 2026-09-23: one swarm per repo. `run_start` refuses while another run's Oracle holds the repo. A swarm that works in several repos holds every one of them, so no other swarm starts in any of them until it finishes. Mechanism **(proposed)**: a lock record in each repo's records folder names the run and its Oracle's `session_id`; `run_start` takes the lock on every repo of the run or refuses; `run_finish` releases them; a lock whose Oracle session no longer exists in `claude agents --json` is stale and can be taken over. Runs span one repo today, so the multi-repo case applies once multi-repo runs exist |
| Every swarm names its roles the same way, and cross-session `SendMessage` can address a session by name | Sessions are addressed by `session_id`. A hook refuses a `SendMessage` to a session outside the caller's run |
| One ledger process per session breaks the `graph_upsert` lock | The shared ledger server above |
| Each run counts only its own sessions against a cap | No cap by default (Alex, 2026-09-23). When a user sets one, `agent_spawn` also counts the machine's other live swarm sessions, from `claude agents --json` **(proposed)** |
| Every project install shares one cached plugin copy per version, and `scripts/smoke.sh` reinstalls it | Smoke runs install under their own version, so a real run elsewhere keeps its files |

## Prototype checks before the build

Each one is a cheap haiku probe, like the checks on 2026-09-21.

1. The ledger's MCP server process can run `claude --bg` and read back the background id and the `session_id`.
2. A cross-session `SendMessage` from one background session wakes another, idle background session, and a hook sees the call and its target. Separately, `claude --resume <id> --bg "<message>"` restarts a stopped session under the same id.
3. The `Stop` and `SessionEnd` hook input in a background `--agent` session carries `session_id` and `transcript_path`.
4. A project-level agent file started with `--agent` in a background session runs its frontmatter hooks, and which events work there: `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SessionEnd`, `PreCompact`.
5. A frontmatter hook command can reach the ledger code: the plugin root must be known inside a project agent file, where `${CLAUDE_PLUGIN_ROOT}` may not be set.
6. A permission prompt in a background session appears in its agent-view row and waits for the user.
7. Two sessions write different files in one working tree at the same time without trouble.
8. A swarm session can start with only the MCP servers its role needs, not every user-level plugin's servers, and how much memory that saves.
9. The ledger as a FastMCP HTTP server: two sessions connect to one process, hooks still stamp identity, and the plugin's `.mcp.json` points at a URL that differs per repo.

## Prototype results on 2026-09-23

Each check ran as short haiku sessions in scratch folders. Results:

| # | Check | Result |
|---|---|---|
| 1 | An MCP server starts a background session | **Works.** A tool inside a FastMCP server ran `claude --bg`; `claude agents --json` returned the short id and the full `sessionId` |
| 2 | A cross-session message wakes an idle session; a hook sees it | **Works.** `SendMessage` to an idle background session returned `success`, and the session woke and did the task. `PreToolUse` and `PostToolUse` saw `tool_input.to` and the message. Tested by session name, not by `session_id` |
| 3 | Hook input in a background `--agent` session | **Works.** `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and `SessionEnd` fired, each with `session_id`, `transcript_path`, and `agent_type` set to the agent's name. `agent_id` is empty. `SessionEnd` fired on `claude stop` with `reason: other` |
| 4 | Frontmatter hooks in a project agent file | **Works in a trusted folder**, for the same six events, both headless and in the background. In an untrusted folder the frontmatter hooks did not run, while plugin hooks did |
| 5 | A frontmatter hook finds the plugin's code | **No.** `CLAUDE_PLUGIN_ROOT` inside an agent-file hook pointed at an unrelated plugin. The generator must write the plugin's install path into each hook command. `CLAUDE_PROJECT_DIR` is correct |
| 6 | A permission prompt in a background session | **Works.** The session showed `status: waiting`, `waitingFor: "permission prompt"`, and held |
| 7 | Two sessions write in one working tree | **Works** with `worktree.bgIsolation: "none"`. Without it, a background session made its own worktree and branch before writing |
| 8 | A session starts only the MCP servers it needs | **Works.** `--strict-mcp-config` with `--mcp-config` starts only the listed servers: 0 instead of 15. An idle session used 289 MB instead of 1,222 MB (306 MB plus 916 MB in 31 helper processes) |
| 9 | One ledger over HTTP for two sessions | **Works.** `swarm_ledger.server` ran with FastMCP's HTTP transport; two sessions with only that URL in `--mcp-config` shared one run. Not tested: the identity hook on the HTTP tools |

Also found:

- A background session refuses to start in an untrusted folder, and trust does not pass down from a trusted parent folder. Every host repo needs the user's one-time trust step. The exact rule is unclear: one untrusted folder with no project files was refused, while an earlier smoke run in an untrusted folder started.
- Hooks receive `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN`, the cross-session messaging channel. It is undocumented; a lead only.
- Check 9 reproduced the same-repo takeover: the second session's `run_start` released the first Oracle. The per-repo lock fixes it.

## Research results on 2026-09-23

Alex's direction first, then what the probes found, then the resulting design **(proposed)**.

| # | Question | Alex's direction | Found | Resulting design |
|---|---|---|---|---|
| 1 | Address a session by id | Session ids are unique; use them | `SendMessage` accepts only a session's name. A short id, a full `sessionId`, and an unknown id all returned "No agent named ... is reachable". Sending by name works. `ListAgents` shows a `[ref]` per row, which is not the `sessionId` and which the ledger cannot see | `agent_spawn` gives every session a name that is unique on the machine, such as `<repo>-r<run>-mgr-hello`, and checks `claude agents --json` before it starts the session. Messages go by that name. The ledger maps the name to the agent row |
| 2 | Trust | Claude Code asks for trust on its own; a refusal means the agents are in the wrong folder | A background session refuses to start in an untrusted folder; trust does not pass down from a trusted parent. Claude Code stores trust per folder path | `scripts/smoke.sh --bg` checks the test folder's trust first and, when it is missing, prints the one command to fix it and stops. The test folder's path never changes, so the user trusts it once. Interactive runs show Claude Code's own trust prompt |
| 3 | The HTTP ledger's limits | Find out whether repos share it and whether there is a limit | Two repos ran two servers on ports the OS picked, with no conflict. 100 and 400 concurrent clients across both servers had 0 errors (2.2 s and 9.7 s). Each repo's ledger held only its own records. Each server used about 126 MB | One ledger server per repo. It binds a free port and writes its URL to `.sentinel-swarm/server.json`. The launcher starts it before the Oracle; `agent_spawn` reads the URL for each child; `run_finish` stops it |
| 4 | Is `--strict-mcp-config` needed | Asked whether it is needed at all | Turning off unneeded servers for one session means tracking plugins, user MCP servers in `~/.claude.json`, and the claude.ai connectors, which change whenever the user adds one. Strict mode is one flag. It drops only MCP servers: the plugin's agents, skills, and hooks still loaded and fired. An agent file's `mcpServers` key did not load when that agent ran as the session, strict or not | Every swarm session starts with `--strict-mcp-config`. `agent_spawn` reads the role's agent file, takes its `mcpServers` list, adds the ledger's URL, and passes the result through `--mcp-config`. The agent file stays the one place the user edits |
| 5 | Hook commands find the plugin | Paths relative to the project root, with error handling | A hook command `python <relative path>/hook.py <event>` in an agent file ran from the project root. `~/.claude/plugins/installed_plugins.json` records each install's `installPath`, per project for project scope, and Claude Code updates it on install and upgrade. A shim that looks up that record ran the real ledger hooks from the installed copy. With the plugin missing, the shim blocked the gating hook with "sentinel-swarm cannot check this call: ... is not installed for ...; run /sentinel-swarm:setup" and reported the other events | `setup` writes a small shim to `.sentinel-swarm/hook.py`. Agent-file hook commands call it by a project-relative path. It finds the install from the registry, so upgrades need no change to the agent files. When the plugin or its files are missing, or the ledger hook fails, it blocks a gating hook with the reason and reports the rest |

## Build spec **(proposed)**

The contract both build packages follow. Names here are exact.

Files in a host repo:

- `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, `swarm-coder.md`: the role files, written by setup from the plugin's `templates/agents/<role>.md`. The plugin's own `agents/` folder is removed, so no plugin agent can run as a subagent.
- `.sentinel-swarm/hook.py`: the shim, written by setup from the plugin's `templates/hook_shim.py`. Three commands:
  - `hook.py hook <event>` finds the sentinel-swarm install for this repo in `~/.claude/plugins/installed_plugins.json` (scope `local`, then `project` with a matching `projectPath`, then `user`) and runs `uv run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.hooks <event>`, passing stdin and stdout through.
  - `hook.py mcp <plugin_id> <server>` finds that plugin's install the same way, reads the server's entry from its `.mcp.json`, expands `${CLAUDE_PLUGIN_ROOT}` and `${VAR:-default}`, and runs it with stdio passed through.
  - `hook.py watch` finds the install the same way and runs `uv run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.watch`, passing each stdout line through with no timeout. The Oracle runs it as its watchdog `Monitor`.
  - Errors: when the plugin, its files, or the ledger hook fail, a gating event (`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`, `pre_ledger`) answers `deny` with the reason, and every event adds a `systemMessage`. Exit code 0.
- `.sentinel-swarm/server.json`: `{"url", "port", "pid", "started_at"}` for the repo's ledger server.
- `.sentinel-swarm/server.port`: the port the ledger server last bound. It survives the server's exit, so a restarted server comes back on the same port and a resumed session's MCP URL still works.
- `.claude/settings.local.json`: setup merges in `{"worktree": {"bgIsolation": "none"}}`.

A role file's frontmatter:

- `name` (`swarm-<role>`), `description`, `model`, `color`, `tools`, `permissionMode`, `mcpServers`, `hooks`, then the prompt body.
- Claude Code applies `tools`, `model`, `color`, and `hooks` from a project agent file to a session. It does not apply `mcpServers`; the launcher and `agent_spawn` read `mcpServers` and `permissionMode` from the file and pass them as `--mcp-config` and `--permission-mode`.
- MCP tool names: the ledger is the server `swarm-ledger`, so its tools are `mcp__swarm-ledger__<tool>`. codebase-kg is the server `codebase-kg`, so its tools are `mcp__codebase-kg__<tool>`.
- Hook commands: `python3 .sentinel-swarm/hook.py hook <event> || python .sentinel-swarm/hook.py hook <event>`.
- Hooks per role:

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent`, which denies | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell` | all |
| `PreToolUse` | `Monitor` | `pre_monitor`, which allows only the Oracle's watchdog call | all |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | all | `post_any` | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

- The plugin's `hooks/hooks.json` carries no hooks, so no hook runs twice.

The ledger:

- `python -m swarm_ledger.serve [--repo <root>]` binds the port in `server.port` on 127.0.0.1, or a free port when that one is taken, writes `server.json`, and serves FastMCP over HTTP at `/mcp`. A second start finds the first one alive and exits 0. The server exits a few seconds after `run_finish`, and after `idle_exit_minutes` with no active run, or a paused run, and no session of the run running. It runs the watchdog on a thread. See `04-mcp-and-hooks.md` "Watchdog".
- `swarm_ledger.serve.ensure_server(repo_root) -> str` starts the server detached when it is not answering, waits until it answers, and returns its URL.
- `swarm_ledger.agentfiles.read_agent_file(repo_root, role) -> dict` returns a role file's frontmatter and body.
- `agent_spawn(caller, child_name, agent_id)`: the caller must be the parent role of the child's unacknowledged brief. It names the session `<repo slug>-r<run_id>-<child_name>`, refuses a name that a live session already uses, and applies `parallelism_cap` when the user set one. It runs, in the repo root: `claude "<prompt>" --bg --name <session name> --agent swarm-<role> --model <brief model> --permission-mode <file value> --strict-mcp-config --mcp-config <swarm-ledger URL plus the file's mcpServers> --allowedTools <the file's tools> --settings '{"worktree":{"bgIsolation":"none"}}'`, with the prompt before the options. It records the row: `agent_id` = the `sessionId`, `session_name`, `bg_id`, state `registered`.
- `agent_resume(caller, target_name, agent_id)` runs `claude --resume <sessionId> --bg "<pointer>"` for a run's session that is not running, and refuses one that is.
- Owed wake-ups: `message_post`, `handoff_submit`, `return_work`, and `phase_update(handed_up)` record that the caller owes the recipient a wake-up, and return `next`: the exact `SendMessage(to="<recipient session name>", message="<one-line pointer>")`, or `agent_resume(...)` when the recipient is not running. `post_any` clears the debt when it sees `SendMessage` to that name. `stop` blocks a Manager, Lead, or Coder that still owes one.
- Release stops the session with `claude stop <bg_id>`, except the Oracle's. `run_finish` releases everything left, then shuts the server down.
- `run_start` refuses while another run's Oracle session is still live in `claude agents --json`; a dead Oracle's run is resumed.
- The claude binary is `SENTINEL_SWARM_CLAUDE` when set, else `claude`.

The launcher and setup:

- `python -m swarm_ledger.setup [--repo <root>]` writes or refreshes the role files, keeping the user's frontmatter and replacing the body. It writes the shim and merges `settings.local.json`. It adds `.sentinel-swarm/` and `.claude/agents/swarm-*.md` to `.git/info/exclude`, adds each project-scope plugin's MCP servers to the role files through the shim, and reports whether the repo is trusted.
- `python -m swarm_ledger.launch [--repo <root>] [--bg | --headless] "<prompt>"` runs setup when a role file is missing, starts the server, and starts the Oracle with the same flags as `agent_spawn`. The session name is `<repo slug>-oracle`. It is interactive by default.

## Build status on 2026-09-23

- Built as `35905d7`: build steps 2 to 5, in two delegated packages checked in the main session. 352 tests pass, and pyright and ruff are clean.
- Checked without a live session: in `runs/hello/host`, setup wrote the four role files, the shim, `settings.local.json`, and the git excludes. The shim ran a ledger hook, and it started codebase-kg's server, which answered the MCP handshake. The launcher refused the untrusted folder with the trust command.
- Build step 6 passed on 2026-09-23: the live background smoke run. Four sessions, `host-oracle`, `host-r1-mgr-hello`, `host-r1-lead-hello-hello`, and `host-r1-coder-hello-hello-hello`, each started by its parent's `agent_spawn`. Four owed wake-ups, all sent within 3 to 19 seconds. Five passing test runs, both reviews, approvals, `success` in 6 minutes. Each release stopped its session, and the ledger server exited after `run_finish`.
- Build step 7 passed on 2026-09-23: the crash test. The Coder's session was stopped after it wrote `hello.py` and before its handoff. Told of it, the Oracle called `agent_resume`; the Coder resumed under its own name, finished, and handed off. The Lead's, Manager's, and Oracle's sessions had exited while they waited, and each was woken by `agent_resume` through the `next` field. The run finished with `success`.
- Found in the live runs:
  - `claude --resume <id> --bg` restarts a session "with its saved options": the same name, agent, permission mode, MCP config, and tools.
  - A background session that has ended its turn can exit while it waits. `agent_resume` brings it back, so the wake-up path covers it.
  - The Oracle's fixed name `<repo>-oracle` collided with a stale Remote Control session of an earlier run, which then received a message meant for the live Oracle. Fixed: the Oracle's name carries its launch time.
  - Once, the ledger server exited about a minute after it started, with no error in its log, and the Oracle could not connect. It did not happen again in two later runs. Open.
  - An open session in the host repo holds the plugin's files, so reinstalling the plugin fails until it closes. `scripts/smoke.sh` now copies the plugin into a new temp folder each run.
  - Nothing notices a dead session on its own. The watchdog is still needed; in the crash test, the report to the Oracle came from outside the run.
- Built on 2026-09-24: the watchdog, in the ledger server process, with the Oracle's listener and the `pre_monitor` gate. It detects and reports through directives; the design is in `04-mcp-and-hooks.md` "Watchdog". Checked with unit tests only, not yet in a live run.
- Small follow-ups: the trust message prints twice, once from setup and once from the launcher. The first start of codebase-kg's server builds its Python environment, so setup should build it ahead of time.

## Build order

1. The prototype checks.
2. The project agent files: the templates, the generator in `setup` and in the launchers, and the per-role hooks in frontmatter.
3. The ledger: `agent_spawn`, the session columns on `agents` with a small migration, the wake call, stop on release, and the cap.
4. The hooks: move the subagent logic to the session hooks, and remove `SubagentStart` and `SubagentStop`.
5. The four prompts and the six skills: `agent_spawn` instead of `Agent`, end the turn after a spawn and let the ledger wake you, and the `Agent` tool removed from every allowlist.
6. The smoke test in `--bg` mode: four rows in agent view, each with its role's color, plus the ledger checks from `04-mcp-and-hooks.md`.
7. A crash test: stop a Coder's session in the middle of its work, then confirm the watchdog or `resume` brings it back and the run finishes.

## Open questions

1. Where the model settings live: the `models` lists in `.claude/sentinel-swarm.local.md`, the `model` key in each agent file, or both.
