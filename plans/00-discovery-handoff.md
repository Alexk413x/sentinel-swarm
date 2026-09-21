# sentinel-swarm: discovery handoff

Status on 2026-09-20: Phase 1 (discovery) is done. Phase 2 (component plan) is proposed, not confirmed. Phase 3 (role responsibilities and the open decisions below) is the next conversation. No plugin files exist yet.

This work follows the `/plugin-dev:create-plugin` workflow. Resume it at Phase 3.

## Purpose

sentinel-swarm takes a PRD of any size and delivers built, tested, reviewed code through an enforced TDD loop.

- It scales from a single hello-world file with tests and review, to adding features in a large existing project, to building a large project from scratch.
- Four roles work in parallel and in a pipeline loop: Oracle, Manager, Lead, Coder.
- Each agent has a role, communicates with the others, and takes ownership of its section of work so that agents do not interfere with each other.
- Users start it with slash commands. It also triggers when the conversation mentions it.
- It produces working, tested, built apps, with multiple outputs at each stage of the process.
- It needs local MCP tooling and hooks, plus external tooling to track progress and codebase work.
- It ships as a private marketplace plugin for Alex's own use. It must work on Windows and macOS, install into other repos, and adapt to each host project.
- It reuses the sibling tooling in `C:\Users\Alexk\Projects\Plugins`.
- The project's own development tooling (tests, type checking, lint, CI, git hooks) must be set up too.

## Platform constraint

Corrected on 2026-09-20 against `code.claude.com/docs/en/sub-agents.md` and `agent-view.md`. The earlier claim that subagents cannot spawn subagents is wrong.

- A subagent can spawn its own subagents, up to three layers below the main conversation by default. `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` sets the limit. A value of `1` turns nesting off. At the limit, Claude Code withholds the `Agent` tool.
- A subagent definition needs `Agent` in its `tools` list to spawn. The `Agent(type, ...)` allowlist works only for a main-thread agent run with `claude --agent`. Inside a subagent definition, the type list is ignored.
- Plugin agents ignore the `hooks`, `mcpServers`, and `permissionMode` frontmatter fields. Enforcement must come from the plugin's own `hooks/hooks.json`, not from per-agent hooks.
- A subagent with `SendMessage` can resume another agent. The resumed agent reports back to the subagent that resumed it.
- Agent teams: teammates message each other directly, but cannot spawn teammates.
- Agent view background sessions (`claude agents`, `claude --bg`, `/bg`, `/fork`) are full sessions. Each moves into its own git worktree under `.claude/worktrees/` before it edits. They can use cross-session messaging where it is enabled. The docs describe no tool for starting one from inside a session; `claude --bg "<prompt>"` from a shell command is the only route seen. A background session that needs a permission decision waits for the user.

The shared ledger still holds tasks, file-ownership claims, messages, and test verdicts: a local MCP server backed by SQLite. Nesting changes who drives the loop, not the need for a shared record. Who drives the loop is now an open decision.

## Proposed components (not yet confirmed)

| Component | Count | Purpose |
|---|---|---|
| Agents | 4 | `oracle`, `manager`, `lead`, `coder` |
| Skills | 6 | `swarm-protocol` (auto-triggers on mention), `run` (PRD to finished work), `plan` (decompose only), `status`, `resume`, `setup` (detect the host repo's stack, test and build commands) |
| Hooks | 4 | Ownership gate (Write/Edit on an unclaimed file), TDD gate (no implementation edit before a recorded failing test; dropped by Alex on 2026-09-20, see `01-roles.md` "Testing"), handoff verifier (tests exist and passed), SessionStart (report an unfinished run) |
| MCP | 1 local | `swarm-ledger`: claim, release, post and read messages, record verdicts, query progress |
| Settings | 1 | `.claude/sentinel-swarm.local.md`: test command, build command, parallelism, enforcement mode |
| Integrations | optional | codebase-kg, cartographer, accessibility-tools, Jira, GitHub |

Draft role definitions, which Alex has not accepted and wants to discuss:

- **Oracle:** owns PRD meaning and acceptance criteria.
- **Manager:** decomposes the PRD, schedules work, assigns ownership.
- **Lead:** owns a workstream, writes failing tests first, reviews the Coder's output.
- **Coder:** makes tests pass inside its claimed files only.

## Open decisions

Alex declined the multiple-choice form of these questions and wants to talk them through, starting with what each agent does and is responsible for.

1. **Roles.** What each of the four roles does, owns, and hands to the next. Whether the Lead reviews its own workstream or a separate Reviewer role exists.
2. **Enforcement.** Hard block, block with a logged override, or advisory only. The sibling plugins use "advisory, never blocking, one-command override". Alex said "enforced layers".
3. **Tracking.** Local ledger only, or also mirror tasks to Jira, GitHub issues, or both.
4. **Sibling dependencies.** Optional (used when installed) or required. codebase-kg is the candidate for a required dependency.
5. **Git.** Initialize a repo here and create a private GitHub remote, as the siblings have.

## Sibling tooling survey

All siblings share a stack: Python 3.10+, fastmcp, `uv run --project ${CLAUDE_PLUGIN_ROOT}/... --frozen --no-dev`, hatchling, pytest, pyright, hook commands written as `python3 ... || python ...`, LF-normalizing `.gitattributes`, and a per-project `.claude-plugin/marketplace.json`. cartographer declares `optionalDependencies` and `allowCrossMarketplaceDependenciesOn`.

- **codebase-kg** (plugin, v0.7.0): committed SQLite code graph. Query tools `kg_search`, `kg_node`, `kg_neighborhood`, `kg_find_by_kind`, `kg_find_by_path`, `kg_find_by_link`, `kg_find_by_reference`, `kg_parity_gaps`, `kg_stats`, `kg_validate`. Write tools `kg_upsert_node`, `kg_delete_node`, `kg_add_link`, `kg_remove_link`, `kg_add_reference`, `kg_remove_reference`. Use for impact analysis and ownership boundaries before decomposition. Its `kg_search_gate` PreToolUse hook is a pattern for forcing a role to consult the graph first.
- **cartographer** (plugin, v0.11.1): end-to-end testing from a knowledge graph of screens and routes. `map_frontier` and `map_capability` show what is untested. `map_cases`, `map_run_cases`, `map_sweep` run the deterministic suite. `map_find_by_code` maps a diff to affected screens. Use as the acceptance layer.
- **accessibility-tools** (marketplace of 4 plugins): 24 deterministic `*_check` tools, WCAG lookups, the `a11y:reviewer` agent, and Android, iOS, and web drivers. Use as a quality gate and for device evidence.
- **atlassian-attachments-mcp** (standalone MCP server, not a plugin, not a git repo): `upload_attachment`, `delete_attachment`, `list_attachments`, `download_attachment`. Use to pull PRD attachments from Jira and post run artifacts back.

Conventions from `accessibility-tools/CLAUDE.md` and `cartographer/CLAUDE.md` that apply here:

- Each layer answers one question. The set loses its value when one layer starts answering another's.
- Cross-layer references are foreign keys, not copied strings.
- Tool-owned artifacts (graphs, baselines, runs) are never hand-edited. Hooks deny hand-writes.
- `plans/CLAUDE.md` is the work index. Read it before starting.
- Device work runs through subagents, never the main session.

## Folder note

The misspelled folder `C:\Users\Alexk\Projects\Plugins\sentinal-swarm` is empty. Windows locks it while a VS Code window is open in it. Delete it after that window closes.
