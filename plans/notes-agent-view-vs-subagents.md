# Notes: agent view sessions versus subagents

Status: reference notes for the mechanics discussion. Read on 2026-09-20 from `code.claude.com/docs/en/sub-agents.md` and `agent-view.md`, through a summarizing fetch. Verify a detail against the page before the design depends on it. Agent view is in research preview, so its interface can change.

## What each one is

- **Subagent:** a worker inside one session. The parent spawns it with the Agent tool. It shows in the subagent panel below the prompt, as a tree.
- **Agent view session:** a full, independent Claude Code session that a supervisor process (`claude daemon`) keeps running without a terminal. It shows as a row in `claude agents`.

## Context: what a subagent gets

| Loads | Does not load |
|---|---|
| Its own system prompt: the agent file's body, plus environment details | The parent's conversation history |
| The task message that the parent writes | Files that the parent already read |
| Every CLAUDE.md level the main session loads, unless `omitClaudeMd: true` | Skills that the parent already invoked |
| A git status snapshot from the start of the parent session | The parent's output style |
| The full content of each skill in its `skills` field | The main session's auto memory |
| A roster of named agents it can message, when it has `SendMessage` | |

- Only the subagent's final report returns to the parent. The full transcript stays on disk at `~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`.
- A fork is the exception. It inherits the parent's whole conversation, tool pool, model, and output style.
- The subagent's context window is sized by its own model.

## Context: what an agent view session gets

- Nothing from any other session's conversation. It starts like a new `claude` session in that directory.
- It shares the directory's settings, plugins, MCP servers, credentials, and CLAUDE.md files.
- It does not share files in progress. Before it edits, it moves into its own git worktree under `.claude/worktrees/`. `worktree.bgIsolation: "none"` turns that off. Outside a git repo there is no isolation.
- Sessions talk to each other only through cross-session messaging, where it is enabled.

## Customization

| | Subagent | Agent view session |
|---|---|---|
| Color | `color` field: red, blue, green, yellow, purple, orange, pink, cyan | `/color` inside the session tints its name |
| Name | The parent passes `name` on the Agent call | `--name`, `Ctrl+R`, or an automatic label |
| Model | `model` field, or a per-call override | `--model`, the agent's `model` field through `--agent`, or `/model` in agent view |
| Effort | `effort` field | `--effort` |
| Tools | `tools`, `disallowedTools` | The agent's fields through `--agent` |
| Preloaded skills | `skills` field | The agent's fields through `--agent` |
| Turn limit | `maxTurns` | None documented |
| Isolation | `isolation: worktree` (optional) | A worktree by default |
| Cache lifetime | `experimental.cacheTtl`: `5m` or `1h` | Main-conversation lifetime |
| Permission mode | `permissionMode`, ignored for plugin agents | `--permission-mode`, or inherited on `/bg` |
| First prompt | The parent's task message | The dispatch prompt, or the agent's `initialPrompt` |
| Display | Tree in the subagent panel, `(+N)` descendant counts, `/tasks` | Rows grouped by state or directory, pins, filters (`a:<agent>`, `s:<state>`), peek panel, PR labels |

Plugin agents ignore `hooks`, `mcpServers`, and `permissionMode`.

## Interaction

| | Subagent | Agent view session |
|---|---|---|
| Permission prompts | Surface in the main session and name the subagent. A lasting grant applies to the whole session | Show in the row and the peek panel. The session waits until the user answers |
| User input | Open its row and type into its transcript, or the parent uses `SendMessage` | Attach, or reply from the peek panel |
| Notifications | A completion notice to the parent | `Notification` hook with `agent_needs_input` or `agent_completed`, terminal notification, footer count |
| Stop | `x` in `/tasks`, or `TaskStop` | `Ctrl+X`, `claude stop <id>` |
| Survives closing the terminal | No. It lives inside its session | Yes. The supervisor restarts a crashed session and stops an idle one after about an hour, unless pinned |
| Scripting | None outside the session | `claude --agent <name> --bg --name <n> --model <m> "<prompt>"`, `claude --resume <id> --bg "<follow-up>"`, `claude agents --json`, `claude logs <id>`, `claude stop <id>` |
| Nested work | Children show in the tree | A session's subagents do not get rows of their own |

## What this means for the swarm

- `claude agents --json` reports each session's `state`, `status`, and `waitingFor`. That is a ready-made data source for the watchdog at the session level.
- A subagent's brief must carry everything it needs. It sees none of the parent's conversation. This supports the "no state lives only in an agent" principle.
- Every subagent loads the user's global CLAUDE.md. `omitClaudeMd: true` drops it, but it drops the project's CLAUDE.md too.
- `experimental.cacheTtl` lets a role that waits a long time choose the 1-hour cache. See `notes-prompt-cache-costs.md`.
- Worktree isolation in agent view sessions separates parallel work, but it adds a merge step that some role must own.
- The ledger cannot live inside a worktree, or each session gets its own copy. It must live at one shared path.

## Added on 2026-09-23, from the docs and the smoke runs

Verified in the smoke runs:

- A background subagent cannot answer a permission prompt; the tool call is denied (`asyncAgent`). A background session shows its prompt in its agent-view row.
- A child's completion notice skips a parent that ended its turn and goes to the main session.
- Subagents run inside the parent's process. Each session is its own process: the Oracle's process held about 600 MB, and the memory reaper stopped background shells twice while runs were going.
- Agent view labels a session with its agent, and its color, only when the launcher resolves the agent. It does not see `--plugin-dir`; an installed plugin works. A folder with project-scope plugins must be trusted once before `claude --bg` runs there.
- Nesting: the prototype found three subagent layers with no spare, so the Coder sits at the limit.

From the docs, through a summarizing agent; verify before the design depends on them:

- `SubagentStart` and `SubagentStop` fire for Agent-tool subagents, not for `--agent` sessions. A session's hooks identify it by `session_id`.
- Background sessions survive sleep, stop on shutdown, and restart on the next interaction within 48 hours; `Ctrl+T` pins one (`agent-view.md`).
- Sessions talk through `SendMessage` across sessions (`cross-session-messaging.md`).
- Agent teams are a third option: teammates with their own context that message each other, with permission prompts in the lead session. "No nested teams: teammates cannot spawn their own teammates" (`agent-teams.md`), so a team cannot hold four levels.
- `subagentPromptCacheTtl` sets a subagent's cache lifetime; the default is five minutes (`agent-teams.md`).
