# sentinel-swarm

Read `plans/CLAUDE.md` first. It indexes the design documents and their review
status; `01-roles.md`, `02-rubric.md`, `03-ledger.md`, and `04-mcp-and-hooks.md`
are the source of truth for every role's responsibilities.

## Layout

- `.claude-plugin/` — `plugin.json`, `marketplace.json`.
- `.mcp.json` — the `swarm-ledger` MCP server entry.
- `agents/` — `oracle.md`, `manager.md`, `lead.md`, `coder.md`.
- `skills/` — `swarm-protocol`, `run`, `plan`, `status`, `resume`, `setup`.
- `hooks/` — `hooks.json` and the planned hook table.
- `templates/` — the `.local.md` settings example for a host repo.
- `mcp/` — the swarm-ledger Python server. A separate project; see `mcp/README.md`.
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

- Plugin agents must not set the `hooks`, `mcpServers`, or `permissionMode`
  frontmatter keys. Claude Code ignores these keys for plugin agents; setting them
  suggests behavior the plugin does not actually have.
- No agent sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.
- Every enforcement rule lives in `hooks/hooks.json` or in a swarm-ledger tool, not
  in agent prompt text. An agent's system prompt describes what it must not do; a
  hook or a ledger gate is what actually stops it.
- Mark any design addition that Alex has not reviewed as **(proposed)** in the
  `plans/` documents.
- Before committing, run the three checks from `mcp/`: `uv run pytest`, `uv run
  pyright`, `uv run ruff check`.
- `mcp/tests/test_plugin_surface.py` guards the surface contract described here:
  agent frontmatter shape, the forbidden keys, and the plugin manifest fields.
