# The ledger server

- One server runs per host repo, over HTTP on `127.0.0.1`, with MCP at `/mcp` and a
  health check at `/health`. Every session of the run connects to it. One process is
  required: `graph_upsert` protects the code graph with a lock inside it.
- `python -m swarm_ledger.serve [--repo <root>]` binds the port saved in
  `.sentinel-swarm/server.port`, or a free port when that one is taken, and writes
  `.sentinel-swarm/server.json` with `url`, `port`, `pid`, `started_at`, and `servers`. A second
  start finds the first one answering, prints its URL, and exits 0. The saved port
  lets a resumed session reach the ledger at the URL it started with.
- `serve.ensure_server(repo_root)` starts the server detached when it does not answer,
  waits until it answers, and returns its URL. On Windows it starts with a hidden
  console (`CREATE_NO_WINDOW`).
- Errors go to `.sentinel-swarm/server.log`.
- Shared MCP servers **(proposed)**. At start, a background thread in the server starts
  one shared HTTP instance of codebase-kg, and of a11y-tools and a11y-kg when the host
  has a11y. See "Shared HTTP servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
  - Each server gets the port saved for it in `.sentinel-swarm/shared-ports.json`, or a
    free port when that one is taken. The saved port lets a resumed session reach the
    server at the URL it started with.
  - `server.json` holds `servers: null` while they start. When every server answers,
    fails, or passes the 60-second limit, `servers` maps each server that answers to
    its URL. A server that fails is logged to `server.log` and left out, and sessions
    use its stdio entry.
  - `ensure_server` waits up to 65 seconds for `servers` to be set, so the Oracle's
    session gets the shared URLs. A record with no `servers` key does not wait.
  - The server starts each shared server with the ledger's pid. On Windows it starts
    hidden (`CREATE_NO_WINDOW`), in a job object with `KILL_ON_JOB_CLOSE`. On POSIX it
    starts in its own session and process group.
  - A supervisor thread restarts a shared server that dies, on the same port. After 3
    restarts in 5 minutes, it gives up: it removes the server from `servers` in
    `server.json` and files a watchdog directive for the Oracle. See "Restarts" in
    [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
  - Every exit path stops every shared server's whole process tree. `run_finish`, the
    idle exit, and a signal that uvicorn handles call `stop_all`. It signals the
    supervisor to stop before it stops the servers. On Windows it terminates each
    job. On POSIX it sends `SIGTERM` to the process group, waits 2
    seconds, and sends `SIGKILL`. When the ledger dies without cleanup, Windows closes
    its job handles and kills each job. On POSIX, `mcp-entry` watches the ledger's pid
    and exits within about 1 second of its death.
- Tool calls run one at a time under one lock, on one SQLite connection.
- The server loads the settings file once, at start. `profile_set` changes the commands
  for the server process and the run's settings snapshot, not the file. The hooks read
  the file on every call, so the Coder's shell gate follows the file's commands.
- The watchdog runs on a thread inside the server.
- Lifetime: the launcher starts the server before the Oracle. It exits after
  `run_finish`, and after `idle_exit_minutes` with no active run, or a paused run, and
  no session of the run running. A ledger tool call restarts the idle clock. A failed
  `claude agents --json` counts as no session running. The shared MCP servers stop
  with it. **(proposed)**
- The plugin's `.mcp.json` also declares a stdio `swarm-ledger` entry. Swarm sessions
  do not use it, because they start with `--strict-mcp-config`.

## Records folder

`.sentinel-swarm/` at the root of the main checkout holds `ledger.db`, `versions/`,
`report.md`, `server.json`, `server.port`, `shared-ports.json`, `server.log`, and the
hook shim `hook.py`.
A worktree's `.git` file resolves to the main checkout, so every worktree shares one
ledger. The folder is excluded through `.git/info/exclude`, never the host's
`.gitignore`. **(proposed)** One ledger holds every run in the repo. The swarm writes
the project's code in the host repo itself. Tracking is `local` only.

## Database

- Every connection opens with WAL, `synchronous=NORMAL`, foreign keys on, and a 5-second
  busy timeout. Every write runs inside `BEGIN IMMEDIATE`. **(proposed)**
- `migrate` applies `schema.sql` at schema version 1 and adds later columns and tables
  idempotently.
- Tables, grouped: the plan (`runs`, `phases`, `phase_deps`, `modules`, `files`,
  `guidelines`); agents (`agents`, `agent_events`, `briefs`, `wakeups`); review
  (`handoffs`, `reviews`, `scores`, `test_runs`, `versions`, `attempts`, `issues`,
  `ideas`); agreements (`change_requests`, `departures`, `departure_decisions`,
  `deferrals`, `overrides`); communication (`messages`, `directives`,
  `watchdog_findings`). Read `schema.sql` for the columns.
- Ledger enum values, such as states, outcomes, and decisions, use snake_case.
- `agent_events` is append-only, and nothing deletes rows when a run finishes.
  **(proposed)**
- Each run records the plugin version and a settings snapshot. **(proposed)**

## Tools

`mcp/src/swarm_ledger/server.py` registers every tool as a thin wrapper over a `Ledger`
method. A refused call raises a tool error with the reason. A gate never returns a
partial success.

| Group | Tools |
|---|---|
| Run and plan | `run_start`, `run_status`, `run_pause`, `run_finish`, `profile_set`, `guidelines_set`, `guidelines_get`, `phase_add`, `phase_update`, `plan_unlocked`, `module_add` |
| Repo | `repo_check`, `repo_branch_create` |
| Briefs and agents | `brief_create`, `brief_get`, `brief_ack`, `agent_spawn`, `agent_resume`, `agent_release` |
| Ownership | `claim_file`, `release_file`, `who_owns` |
| Messages and directives | `message_post`, `message_inbox`, `directive_submit`, `directive_inbox`, `directive_resolve` |
| Tests and graph | `tests_run`, `graph_upsert` |
| Review | `score_record`, `handoff_submit`, `review_compare`, `approve`, `return_work`, `attempt_record`, `accept_incomplete`, `version_restore`, `module_review`, `phase_review` |
| Issues | `issue_open`, `issue_list`, `issue_close`, `idea_record`, `issue_escalate` |
| Agreements | `cr_open`, `cr_accept`, `cr_complete`, `cr_verify`, `cr_list`, `departure_record`, `departure_decide`, `shortfall_record`, `deferral_propose`, `agreement_decide`, `override_grant` |
| Reporting | `status_tree`, `report_build`, `analytics_query` (one read-only SELECT, Oracle only), `events`, `ledger_info` |

- `tests_run(scope, target)` is role-bound: `file` to the Coder (its own path or test
  path only), `module` to the Lead, `phase` to the Manager, `full` to the Oracle. It
  runs the profile's test command with `{target}` replaced, or removed for no target,
  with the ledger's own venv dropped from `PATH` and `VIRTUAL_ENV`, and a 600-second
  timeout. It parses pytest and Go output. An agent never reports a test result itself.
- `version_restore(version_id)` restores a saved version onto the Coder's own file.
