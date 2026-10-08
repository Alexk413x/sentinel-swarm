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
frontmatter (which carries no hooks), the empty `hooks.json`, the manifest fields, the skills'
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
  one-node code graph, and `git init -b main`. It builds the graph from the host's
  project-scope install of `codebase-kg@alexk413x`. Before it installs sentinel-swarm, it
  runs `claude plugin marketplace add --scope project Alexk413x/marketplace` and
  `claude plugin install codebase-kg@alexk413x --scope project -y` in the host, because
  Claude Code refuses to load sentinel-swarm in a project where that dependency is not
  installed. It reads the install path from `installed_plugins.json` and runs
  `uv run --no-project --quiet <install>/mcp/launch/kg_cli.py build <graph.json> -o
  knowledge/code_graph.db`. It copies the plugin (`.claude-plugin`, `assets`, `skills`,
  `hooks`, `templates`, `types`, `mcp`) to a fresh temp folder, rewrites that copy's version to a dev version such as
  `<plugin version>-dev.<epoch seconds>`, for example `0.1.0-dev.1790650000`, installs it at project scope under that
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
- `CLAUDE_BIN` picks the claude binary for the plugin install, and for the launcher and
  the role sessions unless `SENTINEL_SWARM_CLAUDE` is set.
- Two variables let `scripts/bench.sh` reuse the smoke test. `SMOKE_PRD_FILE` names a
  prompt file, used when the command line gives no prompt and no `--prd`.
  `SMOKE_SETTINGS` names a YAML or JSON mapping that `scripts/host_settings.py` merges
  into the host's `.claude/sentinel-swarm.local.md` after setup and before the first
  commit. When the mapping sets `models.oracle`, it also sets the `model` key of the
  host's `.claude/agents/swarm-oracle.md` to the first entry, because the launcher
  starts the Oracle on that file's model before the approved list.
- The run tears down its plugin records when it ends. It runs `claude plugin uninstall
  sentinel-swarm@sentinel-swarm --scope project --keep-data` from the host folder, then
  `claude plugin uninstall codebase-kg@alexk413x --scope project --keep-data` from the
  host folder, then `claude plugin marketplace remove sentinel-swarm`. Teardown never
  removes the `alexk413x` marketplace. Teardown runs at the end of the
  interactive and `--headless` modes and in `--results` mode, never in `--bg` mode,
  which returns while the run goes on. Each run also tears down an earlier run's records
  before it installs, since Windows empties the temp folder the marketplace record
  points at. After teardown, `~/.claude/plugins/known_marketplaces.json` has no
  `sentinel-swarm` entry that points at the temp folder. Teardown never deletes a
  session.
  - `--keep-data`, on teardown's uninstall and on the uninstall before each install,
    keeps the plugin data folder, which holds the ledger venv that every smoke host
    and live session shares.
  - Teardown removes the marketplace only when its record is a smoke copy (a
    `directory` source named `sentinel-swarm-plugin-*`) and no install uses it any
    more. `claude plugin marketplace remove` uninstalls every plugin from the
    marketplace in every project and deletes their data, so while another host still
    has the plugin installed, teardown leaves the marketplace and prints that host.
  - Teardown is skipped while the ledger's latest run has no `ended_at`, and the
    script prints the `--results` command instead: the hook shim finds the plugin through its install
    record, so an uninstall would break the run's live sessions.
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

## The benchmark

`bash scripts/bench.sh --prd <name|heldout/name> [--trials K] [--model-pin ROLE=MODEL]...
[--effort ROLE=LEVEL]... [--settings FILE] [--label NAME] [--dry-run]`

- It runs K trials in a row through `scripts/smoke.sh --headless`, grades each one, and
  writes to `runs/bench/<timestamp>[-<label>]/`: `variant.json`, `override.json`, one
  `trial-<n>/` folder per trial (`smoke.log`, `transcript.jsonl`, `stderr.txt`,
  `report.md`, `checklist.json`, and the acceptance copy and its results),
  `trials.jsonl` with one JSON line per trial, and `summary.json`.
- The PRDs come in three sets:
  - Dev PRDs, `scripts/prds/modules.md` and `scripts/prds/textstats.md`, are the ones
    to use while tuning prompts.
  - The regression suite is the default hello prompt and `modules`. Their acceptance
    tests, with a set for `textstats` too, live in
    `scripts/bench/acceptance/<name>/`.
  - The held-out set lives in `scripts/bench/heldout/<name>/`, each with `prd.md` and
    `acceptance/test_*.py`: `shapes`, `units`, and `tally`. Never read or run a
    held-out PRD while tuning prompts, and never move one into `scripts/prds/`.
- Grading judges the outcome, not the path. A trial passes when its run's state is
  `finished` and every acceptance test passes. The grader, `scripts/bench/bench.py
  grade`, runs only after the run ends. It copies the acceptance tests into the trial
  folder, outside the host repo, adds `scripts/bench/acceptance_conftest.py` as its
  `conftest.py` and an empty `pytest.ini`, and runs `python -m pytest` there with
  `BENCH_HOST` set to the host. The swarm never sees the tests. The conftest puts the
  host on `sys.path` and gives each test a `run_python` fixture, which runs a script or
  module from the host in a fresh temporary folder, so the tests also check what the
  program writes when it runs.
- A trial whose ledger run started before the trial fails with "no run started in this
  trial", so a smoke run that stops early never grades the last run's host.
- `--model-pin` and `--effort` pin a variant, because an alias such as `sonnet` moves to
  a new model and baselines must stay comparable. `--model-pin coder=<model id>` makes
  that model the only approved Coder model, `all=` sets every role, and a pinned Oracle
  model also goes into the host's Oracle agent file. `--effort` writes the role's
  `effort` key. `--settings` merges any other keys. The variant records the pins, the
  settings, `claude --version`, the commit, and whether the tree was dirty.
- `summary.json` holds the trial count, passes, `pass_rate`, `pass^k` (true only when
  all K trials pass), and the mean and median of `cost_usd` and wall seconds.
- `--dry-run` prints the PRD, the acceptance folder, the smoke command, and the variant,
  and writes nothing.
- Trials build the same host as the smoke test, `runs/hello/host`, so that folder must
  be trusted, as for `--headless`. From PowerShell, call Git Bash explicitly, as for the
  smoke test.
- A hello trial costs about $1.50 and takes 7 to 10 minutes.

## The skill-trigger evals

`evals/` holds a `claude plugin eval` suite that checks skill triggering only. The
`status`, `setup`, and `swarm-protocol` skills each have a prompt that must load them
and one that must not. `run` and `resume` carry `disable-model-invocation: true`, and
their cases pass only when the model never calls them. A phase-breakdown prompt must
load no sentinel-swarm skill but `swarm-protocol`. Every case grants only `Read`,
`Glob`, `Grep`, and `Skill`, so it needs no shell sandbox, which native Windows lacks.
The plugin declares no MCP server, so the suite needs no mocks.

Run it with `bash scripts/evals.sh [claude plugin eval options]`, such as
`--runs 1 --ablation none` for a cheap pass, which costs about $1.25 and takes about 5
minutes. The script runs the suite against a copy of the plugin, changed in two ways:

- The copy has no `mcp/.venv`. uv hard-links a venv's files from its cache, and
  `claude plugin eval` refuses a plugin that holds a hard-linked file.
- The copy's `plugin.json` has no `dependencies`. Each eval run starts from an empty
  configuration without codebase-kg, and a plugin with a missing dependency loads no
  skill, so every positive case would fail.

Results go to `evals/results/` (git-ignored).

## After a run

`bash scripts/smoke.sh --results` runs `python -m swarm_ledger.checklist` against the
latest run in the ledger and prints `PASS`, `WARN`, or `FAIL` for each check below, then
exits non-zero if any check fails. It expects the run to have ended in a
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

`python -m swarm_ledger.checklist --json` prints the same checks and the latest run's
metrics as one JSON document, with the same exit code; without `--json` the output is
the text above. The document has `schema` (1), `repo`, `passed` (no check failed),
`checks` (each with `name`, `status` of `pass`, `warn`, or `fail`, and `detail`), and
`metrics`, which is `null` when there is no ledger or no run. `mcp/src/swarm_ledger/
metrics.py` builds `metrics` from the ledger and from each agent's transcript, read from
`agents.transcript_path` or else `<config>/projects/<munged host path>/<agent_id>.jsonl`,
where every character of the absolute host path that is not a letter or digit becomes
`-`. The field shapes:

- `run`: `run_id`, `state`, `outcome`, `started_at`, `ended_at`, and `wall_seconds`
  from start to end.
- `cost_usd`: `total` and `by_role`, from `agents.cost_usd` or the estimate from token
  totals. A cost the ledger cannot price makes its role's value and the total `null`.
- `agents`: the count per role.
- `tool_calls`, `tool_calls_by_tool`, and `toolsearch_calls`: `tool_use` blocks in the
  transcripts, outside sidechains.
- `tool_latency_ms_median`: per tool, the median time from a `tool_use` to its
  `tool_result`, always listing `Read` and `ToolSearch`; `null` for a tool with no
  result.
- `hook_ms`: `total`, `swarm` (commands that run `.sentinel-swarm/hook.py`), and `runs`,
  summed from each hook attachment's `durationMs` and each stop summary's hooks. A Stop
  hook's own attachment is skipped, since the summary already counts it.
- `lead_returns` (rows in `attempts`), `escalation_rounds` (each issue's rounds past
  the first, summed), and `max_issue_round`.
- `watchdog_findings`: `total`, `live`, and `by_kind`.
- `context_overflow_count`: summed across agents.
- `test_runs`: `total`, `failed` (a recorded non-zero exit code), and `by_scope`.
- `transcripts`: how many were `read`, and the agents whose transcript is `missing`.

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
- Teardown ran: `claude plugin list` shows no project-scope `sentinel-swarm` or
  `codebase-kg@alexk413x` install for the host, and `~/.claude/plugins/known_marketplaces.json` has no `sentinel-swarm`
  entry that points at the temp folder. A `teardown:` line on stderr names what stayed.
- No codebase-kg `--serve` process from the run is left. One keeps running from
  the `codebase-kg@alexk413x` install folder in `~/.claude/plugins/cache/` after the run, and on
  Windows it makes the next run's install fail with `EPERM` when that install replaces
  the same version. The harness does not stop processes. Find it in PowerShell with
  `Get-CimInstance Win32_Process | ? CommandLine -match 'plugins\\cache\\.*codebase-kg.*--serve'`,
  and stop it yourself once no other session uses that shared server.
