# Testing this repo

## The checks

Run all four from `mcp/` before each commit:

```
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```

Then, from the repo root, validate the marketplace and the plugin. Both must pass with
`--strict`:

```
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json
```

The local run is the gate. CI runs the same checks as a backup. The dev instructions
live in `.claude/CLAUDE.md`, not at the plugin root, because the validator warns about a
root `CLAUDE.md`, which a plugin install does not load.

`mcp/tests/test_plugin_surface.py` guards the surface contract: the role templates'
frontmatter and hooks, the empty `hooks.json`, the manifest fields, the skills'
frontmatter, and one version across `plugin.json`, `mcp/pyproject.toml`, and the
package.

This repo maps itself in `knowledge/code_graph.db`. Run `sh .githooks/install.sh` once
per clone. After a change to a mapped file, refresh the graph with
`/codebase-kg:refresh`. The `pre-commit` hook reports drift in the staged change and
never blocks. The `pre-push` hook reports drift, and blocks a push when mapped files
the push does not touch are stale. When the codebase-kg tools are missing in a session,
run `/reload-plugins`.

## The smoke test

`bash scripts/smoke.sh [--headless | --bg | --results] ["<prompt>" | --prd <name>]`

- It empties `runs/hello/` (git-ignored), builds a host repo in `runs/hello/host/` with
  a README, a `pyproject.toml`, a settings file with the pytest test command, a
  one-node code graph, and `git init -b main`. It copies the plugin to a fresh temp
  folder, rewrites that copy's version to a dev version such as
  `0.0.1-dev.<epoch seconds>` **(proposed)**, installs it at project scope under that
  version, runs setup, commits, and starts the Oracle through the installed copy's launcher. Each run
  also removes any older `*-dev.*` copy from the plugin cache, skipping one a live
  session still has open, so a run never replaces a cached copy another session holds.
- No flag opens an interactive Oracle. `--bg` checks trust first, then starts a
  background session; `bash scripts/smoke.sh --results` prints the results and then the
  automated checklist below, with a non-zero exit if a check fails. `--headless` checks trust
  too, then runs `claude -p` and writes `runs/hello/transcript.jsonl` and `runs/hello/stderr.txt`.
- The default prompt asks for `hello.py`, which writes `Hello, world!` to
  `hello_world.txt`. After the run, `smoke.sh` runs `hello.py` or `hello_world.py` and
  prints the file, because the shell gate lets the swarm run only test commands.
- `KG_PLUGIN_DIR` picks a codebase-kg version other than the newest in the cache.
  `CLAUDE_BIN` picks the claude binary for the plugin install, and for the launcher and
  the role sessions unless `SENTINEL_SWARM_CLAUDE` is set.
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

`bash scripts/smoke.sh --results` runs `python -m swarm_ledger.checklist` against the
latest run in the ledger and prints `PASS`, `WARN`, or `FAIL` for each check below, then
exits non-zero if any check fails. **(proposed)** It expects the run to have ended in a
clean, success-like outcome, the shape a default hello-world run should reach; a
`--prd` run that legitimately defers or leaves a file incomplete does not fit this
checklist, and needs the manual judgment below instead. A missing ledger, or a ledger
with no run, prints one `FAIL`.

- The run is `finished` with an outcome that starts with `success`, `succeeded`, or
  `complete`, in any case.
- Every phase is `approved`.
- Every file is `approved`, ignoring a `superseded` row.
- Every agent is `released`.
- The run's last full-scope test run has `exit_code = 0`.
- No directive is `open`.
- No `watchdog_findings` row is live (`cleared_at IS NULL`); a finding reported and
  cleared during the run is a `WARN`, not a `FAIL`, because the run recovered from it.
- `report.md` exists next to the ledger.
- No session the run recorded is still running in `claude agents --json`, and the
  ledger server at `.sentinel-swarm/server.json` no longer answers. Both are skipped
  with a `WARN` when the claude binary is not on `PATH`. A session list that
  `claude agents --json` cannot produce fails the session check.

Judge any other run from `runs/hello/host/.sentinel-swarm/ledger.db`, not from the
transcript. Check that:

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
