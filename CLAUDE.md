# sentinel-swarm

Read `plans/CLAUDE.md` first. It indexes the design documents and their review
status; `01-roles.md`, `02-rubric.md`, `03-ledger.md`, and `04-mcp-and-hooks.md`
are the source of truth for every role's responsibilities.

## Layout

- `.claude-plugin/` — `plugin.json`, `marketplace.json`.
- `.mcp.json` — the `swarm-ledger` MCP server entry.
- `skills/` — `swarm-protocol`, `run`, `plan`, `status`, `resume`, `setup`.
- `hooks/` — `hooks.json`, which carries no hooks, and a README with the per-role hook
  table. Each role's hooks live in its project agent file.
- `templates/` — `agents/<role>.md`, the four role templates that setup writes to a host
  repo's `.claude/agents/swarm-<role>.md`; `hook_shim.py`, which setup writes to
  `.sentinel-swarm/hook.py`; and the `.local.md` settings example. The plugin has no
  `agents/` folder, so no plugin agent can run as a subagent.
- `mcp/` — the swarm-ledger Python server. A separate project; see `mcp/README.md`.
  `python -m swarm_ledger.setup` prepares a host repo, and `python -m
  swarm_ledger.launch` starts the Oracle session.
- `knowledge/` — `code_graph.db`, the codebase-kg map of this repo. Committed.
- `scripts/smoke.sh` — the smoke test. It rebuilds `runs/hello/` (git-ignored) on each run
  and starts the Oracle through the installed copy's launcher: interactive by default,
  `--bg` for a background session, `--headless` for `claude -p`.
- `.githooks/` — the codebase-kg staleness hooks. Run `sh .githooks/install.sh` once per
  clone.
- `plans/` — design documents. Do not treat anything outside `plans/` as settled
  until the matching plan document says so.

## Conventions shared with the sibling plugins

- Python 3.10+, `fastmcp`, `uv`. The shipped server runs with `uv run --frozen
  --no-dev`.
- `hatchling` build backend, `pytest`, `pyright`, `ruff`.
- Hook commands are written as `python3 ... || python ...`, so they run on hosts
  where only one of the two names exists.
- LF line endings. `.gitattributes` normalizes this on checkout.
- `mcp/uv.lock` is committed.

## Rules

- The plugin ships no agents. The four roles are templates in `templates/agents/`;
  setup writes them into a host repo's `.claude/agents/` as `swarm-<role>.md`, where
  their `hooks` frontmatter applies. Every role runs as its own session; no role has
  the `Agent` tool.
- No role template sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.
- Every enforcement rule lives in a role template's hooks or in a swarm-ledger tool,
  not in agent prompt text. An agent's system prompt describes what it must not do; a
  hook or a ledger gate is what actually stops it. `hooks/hooks.json` carries no hooks,
  so no hook runs twice.
- Mark any design addition that Alex has not reviewed as **(proposed)** in the
  `plans/` documents.
- Before committing, run the three checks from `mcp/`: `uv run pytest`, `uv run
  pyright`, `uv run ruff check`.
- `mcp/tests/test_plugin_surface.py` guards the surface contract described here:
  the role templates' frontmatter and hooks, the empty `hooks.json`, and the plugin
  manifest fields.
