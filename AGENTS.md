# sentinel-swarm

Read `knowledge/prd/README.md` first. The `knowledge/prd/` folder is the source of
truth for how sentinel-swarm works, one document per subject: roles, the run lifecycle,
scoring, agreements, sessions, the ledger server, hooks, the watchdog, MCP servers, the
report, setup, and testing. The code graph maps every document, so codebase-kg's
`kg_search` finds the one for a subject. `plans/` holds only plans for work not built yet.

## Layout

- `.claude-plugin/` — `plugin.json`. The repo ships no marketplace: the plugin installs
  as `sentinel-swarm@alexk413x` from `Alexk413x/marketplace`, which reads the `release`
  branch.
- `skills/` — `swarm-protocol`, `run`, `status`, `resume`, `setup`.
- `hooks/` — `hooks.json`, which carries no command hooks and names one module,
  `register.ts`: the plugin's mod, which runs every role's ledger hooks in its swarm
  sessions. `register.test.ts` holds its tests (`claude plugin test .`). The README
  has the per-role hook table.
- `assets/` — `icon.png`, the plugin icon that OS notifications show.
- `templates/` — `agents/<role>.md`, the five role templates that setup writes to a host
  repo's `.claude/agents/swarm-<role>.md`; `hook_shim.py`, which setup writes to
  `.sentinel-swarm/hook.py`; and the `.local.md` settings example. The plugin has no
  `agents/` folder, so no plugin agent can run as a subagent.
- `mcp/` — the swarm-ledger Python server. A separate project; see `mcp/README.md`.
  The plugin declares no MCP server: each host repo runs its own HTTP ledger server.
  `mcp/launch/ledger.py <module>` runs a ledger module on the base interpreter with
  `-I -S`: `setup` prepares a host repo, `launch` starts the Oracle session, and
  `directive` steers a live run from an ordinary session.
- `knowledge/` — `code_graph.db`, the codebase-kg map of this repo, and `prd/`, the
  source-of-truth documents with their index in `prd/README.md`. Both committed.
- `scripts/smoke.sh` — the smoke test. It rebuilds `runs/hello/` (git-ignored) on each run,
  installs `codebase-kg@alexk413x` and the working tree's plugin at project scope there,
  and starts the Oracle through the installed copy's launcher: interactive by default,
  `--bg` for a background session, `--headless` for `claude -p`. `--prd <name>` uses
  `scripts/prds/<name>.md` as the prompt: `modules` (three waves: two independent phases,
  a subclass that depends on one, then a command that uses all three) or `textstats`
  (two dependent phases, about 10 files; about $12).
- `scripts/bench.sh` — the benchmark: K headless smoke trials of one PRD, graded by
  acceptance tests in `scripts/bench/` (`acceptance/<name>/` for the regression and dev
  PRDs, `heldout/<name>/` for the held-out set, never used while tuning), with results
  in `runs/bench/`. `scripts/evals.sh` runs the `evals/` skill-trigger suite with
  `claude plugin eval`.
- `.githooks/` — the codebase-kg staleness hooks. Run `sh .githooks/install.sh` once per
  clone.
- `plans/` — plans for work not built yet, such as `a11y-relay-smoke-test.md`.
- `.github/workflows/` — `ci.yml`, the backup checks, and `release.yml`, which tags
  `sentinel-swarm--v<version>` and force-pushes the `release` branch: `main` without
  `.claude`, `AGENTS.md`, `.gitattributes`, `.githooks`, `.github`, `.gitignore`, `evals`,
  `knowledge`, `plans`, `scripts`, `hooks/register.test.ts`, and `mcp/tests`.
- `LICENSE` — the no-resale licence: Apache 2.0 terms plus the no-resale and
  prohibited-use conditions. `CHANGELOG.md` takes one `## [<version>] — <date> — <title>`
  entry per release; the release workflow reads the title from it.

## Conventions shared with the sibling plugins

- The ledger imports only the standard library and runs on Python 3.9 or newer, on the
  base interpreter with `-I -S`, through `mcp/launch/ledger.py`. No venv ships. The dev
  project needs Python 3.10+ and `uv`; `fastmcp` and PyYAML are dev dependencies.
- `hatchling` build backend, `pytest`, `pyright`, `ruff`.
- Hook commands are written as `python3 ... || python ...`, as in cartographer and
  codebase-kg. a11y writes `python ... || python3 ...`. Either order runs on a host where
  only one of the two names exists.
- LF line endings. `.gitattributes` normalizes this on checkout.
- `mcp/uv.lock` is committed.

## Rules

- `AGENTS.md` at the repo root is this file's copy for Codex. Keep the two identical:
  when you change one, copy it over the other.
- The plugin ships no agents. The roles are templates in `templates/agents/`;
  setup writes them into a host repo's `.claude/agents/` as `swarm-<role>.md`. They
  carry no hooks; the plugin's mod runs every role's hooks. Every role runs as its own session. No role has
  the `Agent` tool except the Driver, which `pre_agent` limits to cartographer's
  `map-driver` and `map-reviewer`; setup writes the Driver only when the host has
  cartographer and a driver plugin installed.
- No role template sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.
- Every enforcement rule lives in the plugin's mod or in a swarm-ledger tool, not in
  agent prompt text. An agent's system prompt describes what it must not do; a mod hook
  or a ledger gate is what actually stops it. The mod is the only hook transport, so no
  hook runs twice.
- The PRD is the maintained specification, and the code must match it. No item waits
  for review. When you change behavior, update the matching `knowledge/prd/` document in
  the same change. When the code and a document disagree, fix the code to match the
  document, unless the document is wrong. Mark an item the code does not implement yet
  **(needs implementation)**, and list it in `16-open-items.md`; remove the mark when
  the code ships. Record each decision Alex makes as a plain rule in the document for
  its subject, and add a dated line to `14-key-decisions.md`. When you add or rename a
  document, update the table in `knowledge/prd/README.md` and refresh the code graph.
- Before committing, run the four checks from `mcp/`: `uv run pytest`, `uv run
  pyright`, `uv run ruff check`, `uv run ruff format --check`. Then run `claude plugin
  test .` and `claude plugin validate --strict .claude-plugin/plugin.json` from the repo
  root. Local results are the gate; CI is a backup.
- `mcp/tests/test_plugin_surface.py` guards the surface contract described here:
  the role templates' frontmatter, `hooks.json` naming only the mod, and the plugin
  manifest fields, and that the ledger imports only the standard library.
