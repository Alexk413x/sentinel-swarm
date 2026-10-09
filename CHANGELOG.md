# Changelog

All notable changes to the `sentinel-swarm` plugin.

## [0.2.1] — 2026-10-08

### Fixed

- The ledger's tool catalog lists an optional argument's description at the argument's
  top level, the same on every Python version. `module_add`, `brief_create`, `claim_file`,
  `deferral_propose` and `agreement_decide` showed it inside a nested `anyOf`.
- The test suite passes on Linux and macOS: the tests expect `python3` as the MCP
  command and POSIX quoting for a test target there, and supply a stand-in `notify-send`
  on Linux.

## [0.2.0] — 2026-10-08 — public release

### Changed

- The plugin is public and installs from the `alexk413x` marketplace:
  `claude plugin install sentinel-swarm@alexk413x`. That marketplace installs from the
  `release` branch, which `.github/workflows/release.yml` builds from `main` without
  development files. The self-hosted `.claude-plugin/marketplace.json` is removed.
- The licence changes from proprietary to the no-resale licence: Apache 2.0 terms plus the
  no-resale and prohibited-use conditions.
- The plugin's mod (`hooks/register.ts`) is the only hook transport. It runs every role's
  ledger hooks inside the role's own session, in about 6 ms per hook, and falls back to the
  `.sentinel-swarm/hook.py` shim when the ledger server does not answer. The role files carry
  no command hooks, setup strips any it finds, and the launcher refuses to start unless
  `claude plugin list` shows sentinel-swarm enabled. Requires Claude Code 2.1.294 or later.
- The mod pays owed wake-ups with `$.session.send`, by session id, and puts the recipient's
  unread messages into the wake-up, so the recipient spends no turn on `message_inbox`.
- The ledger server is a standard-library HTTP front with an elastic pool of worker
  processes (`max_workers`, default 8). It serves `/mcp`, `/hook` and `/health` from a tool
  catalog checked against `server.py`. Idle memory drops from about 80 MB to 25 MB.
- The ledger imports only the standard library and runs on Python 3.9 or newer. The skills,
  the shim and the server start it with `<python> -I -S mcp/launch/ledger.py <module>` on the
  base interpreter, so it needs no venv, no `uv` and no install step. A small parser reads
  the YAML frontmatter of the settings file and the role files in place of PyYAML.
  `ledger.py serve --detach` starts the server in the background.
- Each repo's ledger keeps a bearer token in `.sentinel-swarm/http-token`. The server answers
  403 to a remote caller, a wrong `Host`, a foreign `Origin`, or a missing or wrong token.
- Hooks and ledger gates enforce the rules that were prompt text only: dependency order and
  contracts, brief scope, message peers, dispute arbitration, agent names, deferral levels,
  phase unlock order, the Driver's build command and `map-test` order, graph gaps, and the
  brief re-read before a handoff.
- The swarm-events channel, the `/events` route and `wake_transport` are removed. The ledger
  refuses a registered session every tool but `brief_ack` and the identity-free tools.
- Role sessions connect to a plugin's HTTP MCP server directly, and drop the codebase-kg
  tools that moved to its CLI. `graph_upsert` runs codebase-kg on the base interpreter.
- A Lead's `graph_upsert` takes only nodes that span two or more of its module's files.
- The PRD in `knowledge/prd/` is the maintained specification. Items the code does not
  implement yet are marked **(needs implementation)** and listed in `16-open-items.md`.
- codebase-kg installs from the `alexk413x` marketplace as `codebase-kg@alexk413x`.

## [0.1.0] — 2026-09-28 — tooling for Claude Code 2.1.284

### Changed

- Hooks answer through the running ledger server (`POST /hook/<event>`) and fall back to a
  subprocess, fail-closed. Activity bookkeeping runs as an async hook.
- The plugin declares no MCP server. A directive command steers a live run from an ordinary
  session.
- Each role's `tools` list names only its own ledger tools, and `score_record` carries its
  rubric keys in the schema. Read-only tools carry `readOnlyHint`.
- `run_status` and `status_tree` leave out the PRD and the settings snapshot. `tests_run`
  returns a capped summary, `test_run_get` the full output, and a passing run on an unchanged
  tree is reused.
- Every role runs at `medium` effort by default, and a brief can raise a child's effort. The
  Coder's prompt cache TTL defaults to 5 minutes.
- The Oracle and Managers see the run's elapsed time, and an optional time budget, on their
  wake-ups.
- The Driver uses cartographer's `map-driver` and `map-reviewer`.
- Setup denies the roles as subagents. `run` and `resume` are user-invoked only.
- A benchmark (`scripts/bench.sh`) with held-out PRDs and hidden acceptance tests, run
  metrics from `checklist --json`, and a skill-trigger eval suite (`evals/`).
- Requires codebase-kg `>=0.8.2`. The plugin runs in Claude Code only.
