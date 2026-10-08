# Plan: smoke-test the a11y relay

## Goal

Run the smoke test against a host that has an a11y release with its own shared-server
relay installed. Confirm that every swarm role reaches the shared a11y servers through
the relay. The version number does not matter; the relay does. Any a11y version whose
`.mcp.json` runs `${CLAUDE_PLUGIN_ROOT}/bin/<server>-shim` qualifies, from 0.8.2 on.

This closes "Plugin relays in a live run" in `knowledge/prd/16-open-items.md`.

## Background

- The ledger runs no shared MCP servers. It stopped on 2026-09-26.
- A role reaches a plugin server through the stdio entry
  `python .sentinel-swarm/hook.py mcp <plugin_id> <server>`. The shim runs the command
  that the plugin's own `.mcp.json` names. `agentfiles.optional_servers` adds
  `a11y-tools` and `a11y-kg` to every role's `--mcp-config` when the host has
  `a11y@accessibility-tools` installed, and adds `mcp__a11y-tools` and `mcp__a11y-kg` to
  its `--allowedTools`.
- Until a relay version is installed, each session on an a11y host starts its own a11y
  servers through `uv run`.

### Where the relay stands (2026-09-27)

- a11y 0.8.2 exists only on branches of `accessibility-tools`: `fix/explore-acceptance`,
  local and on `origin`, and the local `fix/AP-21-venv-lock`. `origin/main`, which the
  GitHub marketplace serves, is still 0.7.1.
- a11y 0.8.1, on `feat/AP-21-shared-server`, already names the shims. It starts the
  shared server through `uv run`, which 0.8.2 replaced with the venv's Python: on
  Windows, a sync fails while a live server holds the console script. Test 0.8.2 or
  later.
- The installed a11y on this machine is 0.7.1, at project scope in `News-iOS`. Its
  `.mcp.json` runs `uv run`.

### How the relay works

Read from `plugins/a11y/` in `accessibility-tools` 0.8.2: `bin/`,
`mcp/src/a11y_mcp/shim.py`, `mcp/src/a11y_mcp/daemon.py`, and the identical
`mcp-kg/src/a11y_kg/` pair.

- `.mcp.json` runs `bin/a11y-tools-shim` and `bin/a11y-kg-shim <root>/mcp-kg/data/graph.json`.
  On Windows, the shim picks the `.cmd` twin, which finds Python through `py` and runs
  `shim.py`.
- The relay names its server from its package folder: `a11y-mcp` for `a11y-tools`, and
  `a11y-kg`. Every file name, log line, and process below uses these names.
- A build is the plugin version plus a digest of the server's files and arguments, for
  example `0.8.2+<12 hex>`. Each build gets one shared server per machine.
- State lives in `%LOCALAPPDATA%\a11y\` on Windows (`~/Library/Caches/a11y/` on macOS),
  or in `A11Y_CACHE_DIR` when set:
  - `<server>-<build>.json`: `version`, `port`, `pid`, `token`, and `venv`.
  - `<server>-<build>.lock`: held while one relay starts the server.
  - `<server>-<build>.log`: the shared server's log. It records
    `serving <server> <build> on 127.0.0.1:<port>` and one
    `connection <id> from <cwd>` line per relay connection.
- The shared server runs as `<root>\mcp\.venv\Scripts\python.exe -c "from
  a11y_mcp.server import main; main()" --serve`, and the same for `a11y_kg`. It exits
  after `A11Y_IDLE_TIMEOUT` seconds with no connection (default 600), and removes its
  state file.
- Each relay first syncs the plugin's venv, unless a live server uses it. Then it
  connects within `A11Y_SHARED_TIMEOUT` seconds (default 10), and starts the shared
  server when none answers.
- Private fallback: when the relay cannot reach a shared server in time, it runs the
  same command without `--serve` as its own child and logs
  `<server>: shared server unavailable; running a private one` to stderr.
  `A11Y_SHARED=0` goes straight to the private server.
- After a dropped connection, the relay reconnects and replays the session's setup. If
  no shared server returns, it switches to a private server.

## The problem

`scripts/smoke.sh` rebuilds `runs/hello/host` on each run and installs only codebase-kg
there. No smoke host ever has a11y, so no role gets the a11y servers.

## Changes (proposed)

1. **(proposed)** `smoke.sh --a11y [<source>]`. After it installs sentinel-swarm and
   before `swarm setup`, it:
   - adds the `accessibility-tools` marketplace at project scope from `<source>`, and
     installs `a11y@accessibility-tools` at project scope in the host;
   - runs `uv sync --frozen --no-dev --inexact` in the installed copy's `mcp/` and
     `mcp-kg/`, so the first relay does not spend its 10-second budget on a sync and
     fall back to a private server;
   - fails when the installed `.mcp.json` does not name `bin/a11y-tools-shim`.

   `<source>` defaults to `Alexk413x/accessibility-tools`. Until 0.8.2 reaches `main`,
   pass the local checkout on `fix/explore-acceptance`. `marketplace add` with the name
   `accessibility-tools` replaces that marketplace's source for the whole machine, as it
   already does for `sentinel-swarm`. After the test, restore it with
   `claude plugin marketplace add Alexk413x/accessibility-tools`.
2. **(proposed)** `scripts/prds/a11y.md`, a small PRD that makes roles call a11y tools.
   The current smoke PRDs have no UI file, so no role has a reason to call one. The new
   PRD asks for `hello.py`, as the default prompt does, plus `hello.html`, a static page
   that shows the same text with fixed text and background colors. It requires:
   - the colors pass WCAG 2.2 AA contrast, checked with `a11y-tools`'
     `contrast_check`;
   - the page cites the criterion by number, looked up with `a11y-kg`'s
     `kg_criterion`.

   The Lead's accessibility review applies to `hello.html`, whose criterion
   `accessibility_tools_check` also points at these tools.

## Steps

1. Install a relay version as above: `bash scripts/smoke.sh --bg --a11y <source> --prd a11y`.
2. Before the launch, check that `optional_servers` returns the two a11y entries:
   ```
   uv run --project <installed mcp> python -c "from pathlib import Path; from swarm_ledger.agentfiles import optional_servers; print(optional_servers(Path('runs/hello/host')))"
   ```
3. While the run is live, check the processes. In PowerShell:
   ```
   Get-CimInstance Win32_Process | ? CommandLine -match 'a11y_(mcp|kg)|a11y-(tools|kg)-shim' | select ProcessId, ParentProcessId, CommandLine
   ```
   Check again after each new role starts, and after a resume.
4. After the run, run `bash scripts/smoke.sh --results`, then read the a11y state and log
   files, and each role's transcript. The ledger's `agents.transcript_path` names each
   transcript.

## Pass or fail

Each check passes only if it holds for every role session the run started.

- Each role session's `--mcp-config` lists `a11y-tools` and `a11y-kg` as
  `python .sentinel-swarm/hook.py mcp a11y@accessibility-tools <server>` stdio entries,
  and its `--allowedTools` lists `mcp__a11y-tools` and `mcp__a11y-kg`. Read this from
  the session process's command line. If a `--bg` session process does not carry its
  flags, check that each role session has two `hook.py mcp a11y@accessibility-tools`
  children instead.
- One shared server per a11y server runs on the machine while the run is live: one
  `--serve` process tree for `a11y_mcp` and one for `a11y_kg`, each for the installed
  build. On Windows, the venv's `python.exe` can show as a redirector with one Python
  child; count trees, not processes. `%LOCALAPPDATA%\a11y\` holds one state file per
  server for that build, and its `pid` is the live server.
- Each role session's relays connect to that server. Each live session has one
  `shim.py` process per a11y server under its `hook.py mcp` process. Each server's log
  has one `connection <id> from <host path>` line per session start and per resume.
- No private fallback starts while the shared server answers. No `a11y_mcp.server` or
  `a11y_kg.server` process runs without `--serve`.
- An a11y tool call from a role succeeds. At least one transcript shows a
  `mcp__a11y-tools__contrast_check` call and a `mcp__a11y-kg__kg_criterion` call, each
  with a result that is not an error.
- `smoke.sh --results` passes, as for any smoke run.
- After the run, no relay remains, and each shared server exits within
  `A11Y_IDLE_TIMEOUT` of its last connection and removes its state file.

## Record afterward

- Add a dated fact to `knowledge/prd/13-platform-facts.md`, in its format: whether role
  sessions reach the shared a11y servers through the shim's stdio entry, the process
  count per session, and whether any private fallback started.
- Close "Plugin relays in a live run" in `knowledge/prd/16-open-items.md`, or narrow it
  to what failed.
- If `--a11y` and `a11y.md` are built, describe them in
  `knowledge/prd/12-testing.md`.
- Delete this plan.

## Cost and time

- The quick smoke test takes about 6 minutes and costs about $1.50. The complete test
  takes about 23 minutes and costs about $6. See `knowledge/prd/12-testing.md`.
- `a11y.md` adds one file, so expect a little more than the quick test. Record the
  measured cost from the report.
- The a11y install and the two venv syncs add a few minutes to the first run.
- The a11y plugin's own hooks and skills also load in every role session. Note any
  hook output that affects the run.
