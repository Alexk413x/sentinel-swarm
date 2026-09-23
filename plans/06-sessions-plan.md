# sentinel-swarm: every role runs as a session

Status on 2026-09-23: draft plan for Alex to review. The decision in the first section is Alex's. Everything after it is **(proposed)** unless a line says otherwise. Background: `notes-agent-view-vs-subagents.md`, and the smoke runs in `04-mcp-and-hooks.md`.

## The decision

Decided by Alex on 2026-09-23:

- The Oracle, every Manager, every Lead, and every Coder runs as its own Claude Code session. The swarm no longer spawns subagents.
- Each session is a row in agent view, with its role's name and color. The user can open, watch, and type into any of them.
- A session that locks up or crashes is resumed on its own, without restarting the run. Nothing is lost: the session keeps its conversation, and the ledger keeps the run's plan, progress, and evidence.
- Hooks should live in the agent definitions where the platform allows it. See "Hooks" below.

This replaces the earlier rule "Only the Oracle runs as a session. Manager, Lead, and Coder run as nested subagents."

## What changes, in one table

| Area | Today: subagents | Plan: sessions **(proposed)** |
|---|---|---|
| Starting a child | The parent calls the `Agent` tool | The parent calls a ledger tool, `agent_spawn(child_name)`, which runs `claude --bg --agent sentinel-swarm:<role> --name <child_name> ...` in the host repo |
| The spawn gate | Hook 2 checks role, model, and brief | `agent_spawn` checks role, model, brief, and the parallelism cap, and refuses. A ledger gate, not a hook |
| Identity | Hook input `agent_id` | The session's `session_id`, bound to the name by `brief_ack` |
| Finished child | A completion notice to the parent, which skips a parent that ended its turn | The ledger wakes the parent: `claude --resume <session_id> --bg "<one-line pointer>"` |
| Turn end | `SubagentStop` marks the agent idle | The `Stop` hook marks the session idle |
| Release | The ledger marks the row released | The ledger also stops the session with `claude stop <id>`, which frees its memory |
| Crash | The subagent dies with its parent | A watchdog compares `claude agents --json` with the ledger and resumes a crashed session with `claude --resume <id> --bg` |
| Nesting | Three layers, the Coder at the limit | No nesting. Every session is a top-level row |
| Permission prompts | A background subagent's prompt is denied | A session shows its prompt in its agent-view row |

## Starting a session

- `agent_spawn(child_name)` reads the child's brief for its role and model, checks the gates, and starts the session. It records the background id and the `session_id` on the child's `agents` row, so the ledger can wake, stop, and resume it.
- The prompt is one line: "You are `<name>`. Read your brief from the swarm ledger and follow it." The no-telephone-game rule is unchanged.
- The session runs in the host repo root, in the one shared working tree. `worktree.bgIsolation` is `"none"`; the `setup` skill writes it to the host's `.claude/settings.local.json`.
- The plugin is installed at project scope, so the launcher resolves `sentinel-swarm:<role>` and agent view shows the role's color. `--plugin-dir` does not work for this (verified 2026-09-23).
- The host folder is trusted once. `setup` tells the user to do this; a background session will not start in an untrusted folder with project plugins (verified 2026-09-23).
- No role keeps the `Agent` tool, so no role can start a subagent. Hook 2 denies an `Agent` call from any swarm session, as a second line of defense.

## Waking a parent, and messages

- The ledger knows when a session's turn ends (the `Stop` hook) and when work arrives for it: a handoff to review, a module or phase handed up, a message, a returned file.
- When work arrives for an idle session, the ledger resumes it with `claude --resume <session_id> --bg "<pointer to the ledger record>"`.
- A session that is working reads its inbox at the next turn boundary, as today.
- The Oracle's Stop-hook wake list stays as a backstop.

## Crashes and resume

- The watchdog, step 3 of the handoff list, reads `claude agents --json`: each session's `state`, `status`, and `waitingFor`. It compares them with the ledger.
- A session that the ledger says is live but that has exited, crashed, or stopped is resumed with `claude --resume <session_id> --bg "Re-read your brief and your inbox in the ledger."`. It keeps its own conversation, and the ledger supplies the truth.
- A session that is `waitingFor` a permission answer is reported to the user, not resumed.
- The `resume` skill does the same for a whole run, one session at a time.

## Hooks

- Plugin agents ignore the `hooks`, `mcpServers`, and `permissionMode` frontmatter keys (this repo's CLAUDE.md, and the docs). Only project-level agent files in the host's `.claude/agents/` honor them.
- Alex decided on 2026-09-22 that each host project gets generated agent files in `.claude/agents/`. Those files can carry per-role hooks in their frontmatter.
- Until the generated files exist, the hooks stay in `hooks/hooks.json`. Every hook looks up its caller's role in the ledger by `session_id`, so the same hooks already apply per role.
- Hook changes for sessions:
  - `SessionStart` registers or re-binds the session, and marks a resumed session working.
  - `Stop` marks the session idle, wakes the parent through the ledger when needed, keeps the Coder's once-only "no handoff yet" block, and keeps the Oracle's stop gate.
  - `SessionEnd` closes the row and records tokens from `transcript_path`.
  - `SubagentStart` and `SubagentStop` are removed. They do not fire for sessions.
  - `PreToolUse`, `PostToolUse`, and `PreCompact` are unchanged.

## Memory and the parallelism cap

- Each session is its own process, about 600 MB (measured 2026-09-22). Subagents shared their parent's process.
- `agent_spawn` refuses to start a session when the live sessions reach `parallelism_cap`. The parent tries again after a release frees a slot.
- Default cap: 6 sessions, about 3.6 GB. The user raises it in `.claude/sentinel-swarm.local.md`.
- Release stops the session, so a finished Lead or Coder frees its memory at once.

## Prototype checks before the build

Each one is a cheap haiku probe, like the checks on 2026-09-21.

1. The ledger's MCP server process can run `claude --bg` and read back the background id and the `session_id`.
2. `claude --resume <id> --bg "<message>"` delivers the message to an idle session and keeps the same id.
3. The `Stop` and `SessionEnd` hook input in a background `--agent` session carries `session_id` and `transcript_path`.
4. A project-level agent file started with `--agent` runs its frontmatter hooks.
5. A permission prompt in a background session appears in its agent-view row and waits for the user.
6. Two sessions write different files in one working tree at the same time without trouble.

## Build order

1. The prototype checks.
2. The ledger: `agent_spawn`, the session columns on `agents` with a small migration, the wake call, stop on release, and the cap.
3. The hooks: move the subagent logic to the session hooks, and remove `SubagentStart` and `SubagentStop`.
4. The four prompts and the six skills: `agent_spawn` instead of `Agent`, end the turn after a spawn and let the ledger wake you, and the `Agent` tool removed from every allowlist.
5. The smoke test in `--bg` mode: four rows in agent view, each with its role's color, plus the ledger checks from `04-mcp-and-hooks.md`.
6. A crash test: stop a Coder's session in the middle of its work, then confirm the watchdog or `resume` brings it back and the run finishes.

## Open questions

1. Hooks: keep them in `hooks/hooks.json` for now, or build the generated project agent files first so the hooks can live in each role's definition.
2. Waking: the ledger resumes idle sessions through the CLI, or sessions message each other with cross-session `SendMessage`.
3. The default parallelism cap.
