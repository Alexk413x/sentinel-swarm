# sentinel-swarm

Read `knowledge/prd/README.md` first. The `knowledge/prd/` folder is the source of
truth for how sentinel-swarm works, one document per subject: roles, the run lifecycle,
scoring, agreements, sessions, the ledger server, hooks, the watchdog, MCP servers, the
report, setup, and testing. The code graph maps every document, so codebase-kg's
`kg_search` finds the one for a subject. `plans/` holds only plans for work not built yet.

## Layout

- `.claude-plugin/` — `plugin.json`, `marketplace.json`.
- `skills/` — `swarm-protocol`, `run`, `status`, `resume`, `setup`.
- `hooks/` — `hooks.json`, which carries no hooks, and a README with the per-role hook
  table. Each role's hooks live in its project agent file.
- `assets/` — `icon.png`, the plugin icon that OS notifications show.
- `templates/` — `agents/<role>.md`, the four role templates that setup writes to a host
  repo's `.claude/agents/swarm-<role>.md`; `hook_shim.py`, which setup writes to
  `.sentinel-swarm/hook.py`; and the `.local.md` settings example. The plugin has no
  `agents/` folder, so no plugin agent can run as a subagent.
- `mcp/` — the swarm-ledger Python server. A separate project; see `mcp/README.md`.
  The plugin declares no MCP server: each host repo runs its own HTTP ledger server,
  and `python -m swarm_ledger.directive` steers a live run from an ordinary session.
  `python -m swarm_ledger.setup` prepares a host repo, and `python -m
  swarm_ledger.launch` starts the Oracle session.
- `knowledge/` — `code_graph.db`, the codebase-kg map of this repo, and `prd/`, the
  source-of-truth documents with their index in `prd/README.md`. Both committed.
- `scripts/smoke.sh` — the smoke test. It rebuilds `runs/hello/` (git-ignored) on each run
  and starts the Oracle through the installed copy's launcher: interactive by default,
  `--bg` for a background session, `--headless` for `claude -p`. `--prd <name>` uses
  `scripts/prds/<name>.md` as the prompt: `modules` (three waves: two independent phases,
  a subclass that depends on one, then a command that uses all three) or `textstats`
  (two dependent phases, about 10 files; about $12).
- `.githooks/` — the codebase-kg staleness hooks. Run `sh .githooks/install.sh` once per
  clone.
- `plans/` — plans for work not built yet, such as `a11y-relay-smoke-test.md`.

## Conventions shared with the sibling plugins

- Python 3.10+, `fastmcp`, `uv`. The shipped server runs with `uv run --frozen
  --no-dev`.
- `hatchling` build backend, `pytest`, `pyright`, `ruff`.
- Hook commands are written as `python3 ... || python ...`, so they run on hosts
  where only one of the two names exists.
- LF line endings. `.gitattributes` normalizes this on checkout.
- `mcp/uv.lock` is committed.

## Rules

- The plugin ships no agents. The roles are templates in `templates/agents/`;
  setup writes them into a host repo's `.claude/agents/` as `swarm-<role>.md`, where
  their `hooks` frontmatter applies. Every role runs as its own session. No role has
  the `Agent` tool except the Driver, which `pre_agent` limits to cartographer's
  `map-driver` and `map-reviewer`; setup writes the Driver only when the host has
  cartographer and a driver plugin installed.
- No role template sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.
- Every enforcement rule lives in a role template's hooks or in a swarm-ledger tool,
  not in agent prompt text. An agent's system prompt describes what it must not do; a
  hook or a ledger gate is what actually stops it. `hooks/hooks.json` carries no hooks,
  so no hook runs twice.
- Mark any design addition that Alex has not reviewed as **(proposed)** in the
  matching `knowledge/prd/` document, and list it in `15-proposed.md`. Record each
  decision Alex makes as a plain rule in the document for its subject, and add a dated
  line to `14-key-decisions.md`. When you add or rename a document, update the table in
  `knowledge/prd/README.md` and refresh the code graph.
- Before committing, run the four checks from `mcp/`: `uv run pytest`, `uv run
  pyright`, `uv run ruff check`, `uv run ruff format --check`. Then run `claude plugin
  validate --strict .` and `claude plugin validate --strict .claude-plugin/plugin.json`
  from the repo root. Local results are the gate; CI is a backup.
- `mcp/tests/test_plugin_surface.py` guards the surface contract described here:
  the role templates' frontmatter and hooks, the empty `hooks.json`, and the plugin
  manifest fields.
