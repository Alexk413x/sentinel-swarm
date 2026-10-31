# Changelog

All notable changes to the `sentinel-swarm` plugin.

## [0.6.0] — 2026-10-30 — answer permission prompts from the run panel

### Added

- The run panel answers a role session's permission prompt. When Claude Code would ask
  about a call in a role session, the mod's `tool.check` hook holds it, records a
  `permission_requests` row, and waits for the panel's **Allow** or **Deny**. After
  `permission_hold_seconds` (default 300; 0 turns holding off), or on any error, Claude
  Code shows its own prompt. Questions and plan approvals stay in agent view.
- `ledger.py permission --id <n> (--allow|--deny) [--note]` answers a held request.
- The watchdog reports a held request as `needs_user`, with its notification and
  reminders, and does not report the held session as stuck.

### Changed

- A ledger created before 0.6.0 has no `permission_requests` table. Delete
  `.sentinel-swarm/ledger.db` before the first run on 0.6.0.

## [0.5.2] — 2026-10-30 — notice a blocked session

### Fixed

- The watchdog and the run panel treat a session whose `claude agents --json` status or
  state is `blocked` as waiting on the user, as they treat `waiting`. A background
  session idle on a question to the user showed `state: blocked` and raised no
  notification.

## [0.5.1] — 2026-10-30 — notice every session that waits on you

### Fixed

- The watchdog reports a session as waiting on the user whenever `claude agents --json`
  shows its `status` or `state` as `waiting`, whatever its `waitingFor` text says. A
  prompt type the watchdog did not know raised no notification before.
- A session blocked on the user repeats its OS notification every `remind_minutes` until
  it stops waiting. It notified once before.

## [0.5.0] — 2026-10-30 — fixes for the stalls in the 0.4.0 runs

Fixes every issue that the two 0.4.0 runs of 2026-10-28 and 2026-10-29 found. See
`plans/swarm-run-issues.md` for the evidence and `knowledge/prd/16-open-items.md` for
what still needs a live run.

### Added

- The watchdog files a `needs_user` finding when a role session waits on a permission
  prompt or a question, with the request text from its transcript. It files a
  `run_stalled` finding when no agent works and no wake-up is owed. Both notify the user.
- `user_ask`, the Oracle's way to ask the user in a background run. The mod's `pre_ask`
  gate denies `AskUserQuestion` in a background session.
- A `permission_mode.<role>` setting. Role sessions inherit the user's permission mode
  by default, and setup reports each role's starting mode.
- `run_pause(clear_check=...)`: the watchdog resumes a paused run once its check passes.
  Every pause and `needs_user` directive notifies the user, with reminders every
  `watchdog.remind_minutes`.
- A detached test runner (`ledger.py testrun`). `tests_run` returns within 20 s with the
  result or a `running` state, and the caller gets a wake-up when the result is recorded.
  Settings `test_timeout_s`, `full_target`, `test_command_by_scope`, `allowed_skips` and
  `slow_tests`.
- `file_delete`, `file_accept_incomplete`, `run_close` and `ledger.py close`, product
  claims for generated files (`claim_file(product_of=)`), and a `commands` setting of
  named host commands the Coder's and Manager's shell gates allow.
- `brief_create(replaces=)`, and re-pointing of live children to any replacement parent.
- The `parked` agent state, release of parked Managers and Leads whose work is done, and
  release of a released session's process tree with a `stray_process` finding.
- Machine-wide spawn admission across every live swarm, and a `parallelism_cap` default
  from total memory.
- A "Waiting on you" section in the run panel, with the request under the agent's row
  and an answer field for the Oracle's questions.

### Changed

- The role templates carry no `permissionMode`.
- A Lead may update a graph node whose files are its module's or unclaimed. The Oracle
  holds `graph_upsert` for nodes no module owns, and a Coder may anchor its own file on
  any node. `graph_upsert` refuses a description over 240 characters.
- `release_file` refuses while the owning Coder is live.
- The run panel dims ended, parked and not-started rows, shows a file task by its file
  name, and shows the run's PRD on the Oracle's row until the run has an outcome.
- `scripts/smoke.sh` uses its own marketplace name on each run.

### Fixed

- Method anchors (`path#Class.method`) pass the handoff graph check.
- A binary file hands off without being decoded.
- A released claim on a missing path, and a change request on a file with no tests, no
  longer hold a module open.
- Spawns, resumes and wake-ups retry when `~/.claude.json` fails to parse, and the run
  pauses only if it still fails.

## [0.4.0] — 2026-10-27 — run panel

### Added

- A run panel: a side pane that shows each run as a tree of agents, with each agent's
  phase, module, or file as a task. A task's status rolls up from the work below it, and
  clicking a task shows its brief and latest review. The panel opens in any repo; with no
  active run it says how to start one and lists the repo's past runs. See
  `knowledge/prd/17-run-panel.md`.
- The `open_panel` and `swarm_status` function tools, which other plugins can call to open
  the panel and to learn whether a run is active in a repo.

### Changed

- The mod is `hooks/register.tsx`, with the panel's pure helpers in `hooks/panel.ts`.

## [0.3.1] — 2026-10-23 — release under the intended version number

### Changed

- The code is the same as 0.3.0. Install 0.3.1 instead of 0.3.0; see 0.3.0 below for what
  changed since 0.2.2.

## [0.3.0] — 2026-10-23 — cleanup of dead code, duplicates, and the ledger upgrade path

### Removed

- The ledger no longer upgrades a ledger from an earlier release. A ledger created before
  0.3.0 can lack columns and tables, and the ledger fails on them. Delete
  `.sentinel-swarm/ledger.db` in the host repo to start fresh.
- `directive_submit` no longer accepts the `user-chat` and `outside-session` spellings. Use
  `user_chat` and `outside_session`.
- The `tracking` setting, which nothing read, and the `agents` columns `phase_at_start`,
  `context_pct_peak`, `context_pct_at_end`, and `duration_ms`, which nothing wrote.
- The `agent_type` argument of `brief_ack`.

### Changed

- The ledger code is split into smaller modules (`report`, `messages`, `issues`, `host`,
  `queries`, `paths`), and duplicated helpers in the ledger, the hooks, the watchdog, the
  server, and the tests are merged. The tool catalog is unchanged except for `brief_ack`.
- `same_path` returns false for an empty path or one it cannot read, `parent_chain` stops
  on a parent cycle, and the report's duration no longer fails on a malformed timestamp.
- The finished plans are folded into the PRD, and CI runs on pushes to `main` and on pull
  requests only.

## [0.2.2] — 2026-10-14 — role sessions reach HTTP plugin servers by manifest path

### Fixed

- A role session connects to a plugin's HTTP MCP server directly when the plugin's
  `plugin.json` names its servers by path (`"mcpServers": "./mcp/claude.mcp.json"`), as
  codebase-kg, cartographer, a11y and the three driver plugins do. Before, only a root
  `.mcp.json` or an inline `mcpServers` object counted, so those roles fell back to the
  stdio shim. A path that leaves the plugin folder is ignored. The entry keeps its
  `headersHelper`, so Claude Code runs it in the role session and a driver server gets the
  session's owner id. A plugin installed in a folder whose path holds `"`, `$`, a backtick
  or a newline keeps the stdio shim, so no broken helper command is written.
- The `.sentinel-swarm/hook.py mcp` shim reads a plugin's servers from a manifest path or
  list too, so the stdio fallback works for those plugins.

## [0.2.1] — 2026-10-08 — tool catalog argument descriptions and Linux and macOS tests

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
