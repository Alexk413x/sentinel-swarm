# The ledger server

- One server runs per host repo, over HTTP on `127.0.0.1`, with MCP at `/mcp` and a
  health check at `/health`. Every session of the run connects to it. One process is
  required: `graph_upsert` protects the code graph with a lock inside it.
- `python -m swarm_ledger.serve [--repo <root>]` binds the port saved in
  `.sentinel-swarm/server.port`, or a free port when that one is taken, and writes
  `.sentinel-swarm/server.json` with `url`, `port`, `pid`, and `started_at`. A second
  start finds the first one answering, prints its URL, and exits 0. A server counts as
  answering when `/health` returns the name `swarm-ledger` and this repo's root. The
  saved port lets a resumed session reach the ledger at the URL it started with.
- `serve.ensure_server(repo_root)` starts the server detached when it does not answer,
  waits until it answers, and returns its URL. On Windows it starts with a hidden
  console (`CREATE_NO_WINDOW`).
- Errors go to `.sentinel-swarm/server.log`.
- The ledger server runs no other MCP server. Each plugin shares its own servers
  through its own relay. See "Plugin servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- Tool calls run one at a time under one lock, on one SQLite connection.
- The server loads the settings file once: the watchdog at start, and the ledger at the
  first tool call. `profile_set` changes the commands for the server process and the
  run's settings snapshot, not the file. The hooks read the file on every call, so the
  Coder's shell gate follows the file's commands.
- The watchdog runs on a thread inside the server.
- `GET /events?session=<session id>` holds one session's `swarm-events` event stream
  open: newline-delimited JSON, one event per line, and a `{"kind": "ping"}` line after
  15 seconds without an event. The request records `launched` on that session's agent
  row, and the stream registers the session until the connection closes. A request
  without `session` gets 400. See "Wake-up delivery" in
  [05-sessions.md](05-sessions.md). **(proposed)**
- Lifetime: the launcher starts the server before the Oracle. It exits after
  `run_finish`, and after `idle_exit_minutes` with no active run, or a paused run, and
  no session of the run running. A ledger tool call restarts the idle clock. A failed
  `claude agents --json` counts as no session running.
- The plugin declares no MCP server of its own, so an ordinary session in a host repo
  starts no ledger process and sees none of the ledger's tool names. An ordinary session
  steers a live run with `python -m swarm_ledger.directive [--repo <root>] [--source
  skill|outside_session|user_chat] [--sender <name>] [--reply-to <id>] "<text>"`, which
  records the directive in the repo's ledger the way `directive_submit` does and prints
  it as JSON. It exits 1 with the ledger's reason when no run is active.

## Records folder

`.sentinel-swarm/` at the root of the main checkout holds `ledger.db`, `versions/`,
`report.md` (the latest run's report), `report-<run_id>.md` for each run, `server.json`,
`server.port`, `server.log`, and the hook shim `hook.py`.
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
  `watchdog_findings`, `notifications` **(proposed)**); the Driver (`drive_requests`,
  `drive_findings`, `drive_stops` **(proposed)**). Read `schema.sql` for the columns.
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
| Run and plan | `run_start`, `run_status`, `run_pause`, `phase_resume`, `run_finish`, `profile_set`, `guidelines_set`, `guidelines_get`, `phase_add`, `phase_update`, `plan_unlocked`, `module_add` |
| Repo | `repo_check`, `repo_branch_create` |
| Briefs and agents | `brief_create`, `brief_get`, `brief_ack`, `agent_spawn`, `agent_resume`, `agent_release` |
| Ownership | `claim_file`, `release_file`, `who_owns` |
| Messages and directives | `message_post`, `message_inbox`, `directive_submit`, `directive_inbox`, `directive_resolve` |
| Tests and graph | `tests_run`, `test_run_get`, `graph_upsert` |
| Review | `score_record`, `handoff_submit`, `review_compare`, `approve`, `return_work`, `attempt_record`, `accept_incomplete`, `version_restore`, `module_review`, `phase_review` |
| Issues | `issue_open`, `issue_list`, `issue_close`, `idea_record`, `issue_escalate` |
| Agreements | `cr_open`, `cr_accept`, `cr_complete`, `cr_verify`, `cr_list`, `departure_record`, `departure_decide`, `shortfall_record`, `deferral_propose`, `agreement_decide`, `override_grant` |
| Reporting | `status_tree`, `report_build`, `analytics_query` (one read-only SELECT, Oracle only), `events`, `ledger_info` |
| Driver | `drive_request`, `drive_issue`, `drive_checkin`, `drive_done`, `drive_unavailable` **(proposed)** |

- `tests_run(scope, target)` is role-bound: `file` to the Coder (its own path or test
  path only), `module` to the Lead, `phase` to the Manager, `full` to the Oracle. It
  runs the profile's test command with `{target}` replaced, or removed for no target,
  with the ledger's own venv dropped from `PATH` and `VIRTUAL_ENV`, and a 600-second
  timeout. It parses pytest and Go output. An agent never reports a test result itself.
- `tests_run` stores the last 20,000 characters of output in the ledger, and returns a
  summary of at most 4,000: the last 500 characters of a pass, or the first traceback
  from the `FAILURES` section and the `short test summary info` section of a failure.
  Output in another format returns its last 4,000 characters. `output_chars` gives the
  stored length, and `test_run_get(test_run_id)` returns the stored run with its full
  output to any agent of the run.
- `run_status` and `status_tree` return a run summary (`run_id`, `state`, `outcome`,
  `branch`, `plugin_version`, `started_at`, and the decoded `repo_check`), not the run
  row. The PRD text comes back only with `include_prd=true`, and the settings snapshot
  never does. Their agent lists carry names, not session ids (`parent` is the parent's
  name), and leave out empty fields; a file's `owner` is the owner's name.
  `agent_spawn` leaves empty fields and the settings snapshot out of its result.
- `version_restore(version_id)` restores a saved version onto the Coder's own file.
- The server itself shows a desktop notification for each notification it records:
  a Driver event or an issue that ends round 3 below the floor. It runs from
  `notify.py`, in a background thread, when the settings' `notify` list includes `os`.
  A failure goes to the server log.
  See "Driver notifications" in [02-run-lifecycle.md](02-run-lifecycle.md).
  **(proposed)**
