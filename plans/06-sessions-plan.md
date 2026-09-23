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
| Crash | The subagent dies with its parent | A watchdog compares `claude agents --json` with the ledger and resumes a crashed session with `claude --resume <id> --bg` |
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
- A parent whose session has crashed or exited cannot receive a message. The watchdog restarts it with `claude --resume <session_id> --bg "<pointer>"`, which continues the same session and conversation. `--resume` on a session that is still running starts a copy, so it is never used on a live session. **(proposed)**

## Crashes and resume

- The watchdog, step 3 of the handoff list, reads `claude agents --json`: each session's `state`, `status`, and `waitingFor`. It compares them with the ledger.
- A session that the ledger says is live but that has exited, crashed, or stopped is resumed with `claude --resume <session_id> --bg "Re-read your brief and your inbox in the ledger."`. It keeps its own conversation, and the ledger supplies the truth.
- A session that is `waitingFor` a permission answer is reported to the user, not resumed.
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
- `agent_spawn` refuses to start a session when the live sessions reach `parallelism_cap`. The parent tries again after a release frees a slot.
- Default cap: 6 sessions, about 3 to 6 GB at the measured sizes. The user raises it in `.claude/sentinel-swarm.local.md`.
- Release stops the session, so a finished Lead or Coder frees its memory at once.

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

## Build order

1. The prototype checks.
2. The project agent files: the templates, the generator in `setup` and in the launchers, and the per-role hooks in frontmatter.
3. The ledger: `agent_spawn`, the session columns on `agents` with a small migration, the wake call, stop on release, and the cap.
4. The hooks: move the subagent logic to the session hooks, and remove `SubagentStart` and `SubagentStop`.
5. The four prompts and the six skills: `agent_spawn` instead of `Agent`, end the turn after a spawn and let the ledger wake you, and the `Agent` tool removed from every allowlist.
6. The smoke test in `--bg` mode: four rows in agent view, each with its role's color, plus the ledger checks from `04-mcp-and-hooks.md`.
7. A crash test: stop a Coder's session in the middle of its work, then confirm the watchdog or `resume` brings it back and the run finishes.

## Open questions

1. The default parallelism cap.
2. Where the model settings live: the `models` lists in `.claude/sentinel-swarm.local.md`, the `model` key in each agent file, or both.
