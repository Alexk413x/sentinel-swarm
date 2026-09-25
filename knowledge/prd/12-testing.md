# Testing this repo

## The checks

Run all four from `mcp/` before each commit:

```
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```

`mcp/tests/test_plugin_surface.py` guards the surface contract: the role templates'
frontmatter and hooks, the empty `hooks.json`, and the manifest fields.

This repo maps itself in `knowledge/code_graph.db`. Run `sh .githooks/install.sh` once
per clone. After a change to a mapped file, refresh the graph with
`/codebase-kg:refresh`; the `pre-push` hook reports drift. When the codebase-kg tools
are missing in a session, run `/reload-plugins`.

## The smoke test

`bash scripts/smoke.sh [--headless | --bg | --results] ["<prompt>" | --prd <name>]`

- It empties `runs/hello/` (git-ignored), builds a host repo in `runs/hello/host/` with
  a README, a `pyproject.toml`, a settings file with the pytest test command, a
  one-node code graph, and `git init -b main`. It copies the plugin to a fresh temp
  folder, installs it at project scope, runs setup, commits, and starts the Oracle
  through the installed copy's launcher.
- No flag opens an interactive Oracle. `--bg` checks trust first, then starts a
  background session; `bash scripts/smoke.sh --results` prints the results when it
  finishes. `--headless` runs `claude -p` and writes `runs/hello/transcript.jsonl`.
- The default prompt asks for `hello.py`, which writes `Hello, world!` to
  `hello_world.txt`. After the run, `smoke.sh` runs `hello.py` or `hello_world.py` and
  prints the file, because the shell gate lets the swarm run only test commands.
- `KG_PLUGIN_DIR` picks a codebase-kg version other than the newest in the cache.
  `CLAUDE_BIN` or `SENTINEL_SWARM_CLAUDE` picks the claude binary.
- From PowerShell, call Git Bash explicitly:
  `& "C:\Program Files\Git\bin\bash.exe" scripts/smoke.sh`. Plain `bash` can resolve
  to WSL.
- A headless run needs an explicit `--allowedTools` list. Do not use
  `--dangerously-skip-permissions`.

| Test | Command | Time | Cost |
|---|---|---|---|
| Quick | `bash scripts/smoke.sh --bg` | About 6 minutes | About $1.50 |
| Complete | `bash scripts/smoke.sh --bg --prd modules` | About 23 minutes | About $6 |

- `--prd modules` runs `scripts/prds/modules.md` in three waves: `hello` and `world` in
  parallel, then `name` (a `World` subclass), then the command that uses all three.
- `--prd textstats` runs `scripts/prds/textstats.md`, a larger two-module PRD.

## After a run

Judge a run from `runs/hello/host/.sentinel-swarm/ledger.db`, not from the transcript.
Check that:

- The run is `finished` with outcome `success`, and every phase is `approved`.
- Every file is `approved`, `incomplete`, or `superseded`, and no claim is live.
- Every agent row has an `ended_at`, a model, and token counts, and every row but the
  Oracle's has a parent.
- Each file has a self review and a Lead review, a compared and approved handoff, and a
  passing file-scope test run. Each phase has a passing full-scope run by the Oracle
  after its hand-up.
- Every module has an accepted `module_review` and every phase an accepted
  `phase_review`.
- `watchdog_findings` and the directives match what happened, and every directive is
  resolved.
- `report.md` exists and matches the ledger.

Then confirm that nothing is left running:

- No `swarm_ledger` process remains. In PowerShell:
  `Get-CimInstance Win32_Process | ? CommandLine -match swarm_ledger`.
- `.sentinel-swarm/server.json` is gone.
- The run's sessions are stopped in `claude agents`. Do not `claude rm` them: Alex
  reviews them in agent view.
