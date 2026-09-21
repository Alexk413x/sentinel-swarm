# sentinel-swarm: session handoff

Status on 2026-09-21: written by session `sentinel-swarm-a8` for the next session. Read `plans/CLAUDE.md` first, then this file.

## Where the work stands

- The design is drafted in `01-roles.md`, `02-rubric.md`, `03-ledger.md`, and `04-mcp-and-hooks.md`. Alex settled every open question in them. Items marked **(proposed)** are Claude's additions, and Alex has not reviewed them one by one.
- The repo is on `main`, pushed to the private remote `github.com/Alexk413x/sentinel-swarm`. CI passes on Windows, macOS, and Ubuntu, with Python 3.10 and 3.12.
- The plugin skeleton exists: the manifest, four role agents, six skeleton skills, an empty `hooks/hooks.json`, a settings template, and the `swarm-ledger` Python project with one tool, `ledger_info`.
- `mcp/tests/test_plugin_surface.py` guards the agent frontmatter, colors, models, and tool grants. Run `uv run pytest`, `uv run pyright`, and `uv run ruff check` from `mcp/` before each commit.
- Nothing runs yet. No ledger tables, no hooks, and no skill logic exist.

## Next steps, in order

1. **Code graph for this repo.** `.claude/settings.json` enables codebase-kg. It loads at session start, so a session that starts after 2026-09-21 has its tools. Run `/codebase-kg:build`, then `/codebase-kg:setup`. The setup command installs `.githooks/`, so this repo has no hand-made git hooks.
2. **Prototype checks.** Run them as small tests inside the skeleton, because they decide several mechanics. The lists are under "To verify with a prototype" in `03-ledger.md` and `04-mcp-and-hooks.md`:
   - Whether a `PreToolUse` hook can rewrite a tool's input, to stamp an agent's identity onto a ledger call.
   - Whether `SubagentStop` can block.
   - Whether `PreCompact` fires inside a subagent, and how a compaction shows in a transcript.
   - The per-message `usage` fields in a subagent transcript, and whether nested subagents get their own transcripts.
   - Whether a session started with `--agent` reports `agent_type` to every hook, and whether the agent's `model` field applies.
   - How much time a `PostToolUse` hook on every tool call adds.
   - Whether concurrent writers are safe for the ledger's SQLite file and for the code graph file.
3. **Candidates to evaluate**, from "Plugin features found on 2026-09-21" in `04-mcp-and-hooks.md`: `monitors/monitors.json` for the watchdog, `subagentStatusLine` for marking agents, and the plugin `agent` setting for running the session as the Oracle.
4. **Build the ledger**, then the hooks, then the skills. Follow `/plugin-dev:create-plugin` from Phase 4.

## How Alex works

- Alex writes loosely and expects you to interpret intent. Restate your reading before you build on it.
- Alex declined multiple-choice prompts. Ask one question at a time, with a concrete example, two readings in a table, and a recommendation.
- A draft to react to works better than an abstract question.
- Alex wants responsibilities settled before mechanics.
- Record each decision in the plans as soon as Alex makes it. Mark your own additions **(proposed)**.
- Delegate multi-file work to Sonnet subagents with precise briefs, then run the checks yourself. A subagent's report is not proof.
- Commit with neutral messages. Pushing to the private remote is approved for this repo.

## Decisions worth knowing before you read the plans

- Four roles: Oracle (cyan), Manager (green), Lead (purple), Coder (orange). One Manager per phase, one Lead per module, one Coder per file. A hello-world run still creates one of each.
- Only the Coder writes project files. Oracle, Manager, and Lead have no shell and no write tools. They work through MCP tools only.
- The first version works in one working tree, on the checked-out branch. The swarm never commits or pushes. Only the Oracle runs as a session. Manager, Lead, and Coder run as nested subagents.
- The ledger MCP server holds the state and the gates. Hooks make the ledger unavoidable. Enforcement is a hard block, and only the Oracle overrides.
- The hard check at handoff: the ledger runs the file's tests itself, and the code graph must be current for the file.
- Scoring: each criterion rated 1 to 10, each dimension computed out of 100, no combined score and no weights. Target 90, floor 70.
- Escalation and diminishing returns are one mechanism: 3 rounds, and a round ends after 3 attempts that did not improve the scores.
- codebase-kg is required. Every agent searches the graph first, then falls back to Grep or Glob.
- Tracking is local only. The records folder is git-ignored through `.git/info/exclude`.
- No role has a time limit or a turn limit. The watchdog reports stuck or spinning agents to the Oracle.

## Housekeeping

- The misspelled folder `C:\Users\Alexk\Projects\Plugins\sentinal-swarm` is empty. Delete it once no VS Code window holds it open. Ask Alex first.
- `plans/notes-prompt-cache-costs.md` holds cost notes that Alex wants raised when swarm testing starts.
