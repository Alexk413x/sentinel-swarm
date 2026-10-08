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
- Access: every request must come from `127.0.0.1` or `::1`, carry a `Host` header of
  `127.0.0.1:<port>` or `localhost:<port>` with the server's own port, and carry no
  `Origin` header other than `http://127.0.0.1:<port>` or `http://localhost:<port>`.
  Every route except `/health` also needs `Authorization: Bearer <token>`, with the
  token from `.sentinel-swarm/http-token`. A request that fails a check gets 403 and a
  one-line reason, never 401: a 401 makes an MCP client start the OAuth flow of the MCP
  authorization spec. The checks run in `serve.Guard`, an ASGI middleware in front of
  every route. FastMCP 4.0.5 validates `Host` only when its `host_origin_protection`
  setting is on, which it is not by default, and then answers 421, so the ledger does
  not use it.
- The token: the server creates `.sentinel-swarm/http-token` on its first start, mode
  0600, from 32 random bytes in URL-safe base64, and reuses it on every later start.
  Claude Code keeps the headers a session got at connect and sends them to a restarted
  server, so a new token per process would refuse every live session after a restart. A
  file that does not match `[A-Za-z0-9_-]{32,128}` is replaced at the next server start.
- `session_options` writes the `swarm-ledger` entry of every role's `--mcp-config` as
  `{"type": "http", "url": <url>, "headers": {"Authorization": "Bearer <token>"}}`.
  The token exists before any role starts, so no `headersHelper` and no start race
  apply. `server.json` and the token live in the host repo's working tree, so before
  either reaches a `--mcp-config`, `session_options` checks that the URL is exactly
  `http://127.0.0.1:<port>/mcp` and that the token matches `[A-Za-z0-9_-]{32,128}`. A
  failed check raises an error: `agent_spawn` refuses, and the launcher prints the
  reason and exits 1.
- `ensure_server` probes `/health`, which needs no token, and `python -m
  swarm_ledger.directive` opens the ledger database directly, so neither sends the
  token. The hook shim reads it from `http-token`.
- After a 403, Claude Code records the server in `~/.claude/mcp-needs-auth-cache.json`,
  a JSON object keyed by server name with a `timestamp` per entry, and stops connecting
  to it, in later sessions too. A live session gets a 403 only when someone deletes or
  replaces `http-token` while the run is live. To recover, stop the role sessions,
  remove the `swarm-ledger` key from that file (or delete the file when it holds nothing
  else you need), and resume the sessions. Not verified live: the key Claude Code uses
  for a `--mcp-config` server, and whether a resumed session reconnects once the key is
  gone. See [16-open-items.md](16-open-items.md).
- The ledger server runs no other MCP server. Each plugin shares its own servers
  through its own relay. See "Plugin servers" in
  [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
- Tool calls run one at a time under one lock, on one SQLite connection. A hook
  request on `/hook/<event>` runs outside that lock, on its own connection.
- The server loads the settings file once: the watchdog at start, and the ledger at the
  first tool call. `profile_set` changes the commands for the server process and the
  run's settings snapshot, not the file. The hooks read the file on every call, so the
  Coder's shell gate follows the file's commands.
- The watchdog runs on a thread inside the server.
- The hook route: `POST /hook/<event>` runs the handler `python -m swarm_ledger.hooks
  <event>` runs, on the request body, and returns exactly the bytes that command would
  print. Each request opens its own `Ledger`, so it reads the settings file again, and
  closes it after the handler. It runs on a worker thread, off the event loop, and
  never takes the tool-call lock. The handler's stderr goes to the server log. Besides the access checks above, the route answers 403 unless the caller
  is `127.0.0.1` or `::1` and the `Host` header names `127.0.0.1` or `localhost`, 404
  for an unknown event, and 409
  unless the `X-Sentinel-Swarm-Repo` header names this server's repo root. A 200
  answer carries the header back. See "The shim" in
  [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).
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
`server.port`, `http-token`, `server.log`, and the hook shim `hook.py`.
A worktree's `.git` file resolves to the main checkout, so every worktree shares one
ledger. The folder is excluded through `.git/info/exclude`, never the host's
`.gitignore`. One ledger holds every run in the repo. The swarm writes
the project's code in the host repo itself. Tracking is `local` only.

## Database

- Every connection opens with WAL, `synchronous=NORMAL`, foreign keys on, and a 5-second
  busy timeout. Every write runs inside `BEGIN IMMEDIATE`.
- `migrate` applies `schema.sql` at schema version 1 and adds later columns and tables
  idempotently.
- Tables, grouped: the plan (`runs`, `phases`, `phase_deps`, `modules`, `module_deps`,
  `files`, `file_deps`, `guidelines`); agents (`agents`, `agent_events`, `briefs`,
  `wakeups`); review (`handoffs`, `reviews`, `scores`, `test_runs`, `versions`,
  `attempts`, `issues`, `ideas`); agreements (`change_requests`, `departures`,
  `departure_decisions`, `deferrals`, `overrides`); communication (`messages`,
  `directives`, `watchdog_findings`, `notifications`); the Driver (`drive_requests`,
  `drive_findings`, `drive_stops`); the code graph (`graph_gaps`). Read `schema.sql`
  for the columns.
- `module_deps` and `file_deps` hold the dependencies a dependent's brief waits on, and
  `graph_gaps` the searches the code graph did not answer. Columns that hold a gate's
  state: `briefs.contract` and `briefs.last_read_by_child_at`; `deferrals.kind`,
  `parties_json`, `arbiter_agent_id`, `open_issues_json`, `issue_ids_json`, and
  `directive_id`; `drive_requests.map_test_at`.
- Ledger enum values, such as states, outcomes, and decisions, use snake_case.
- `agent_events` is append-only, and nothing deletes rows when a run finishes.
- Each run records the plugin version and a settings snapshot.

## Tools

`mcp/src/swarm_ledger/server.py` registers every tool as a thin wrapper over a `Ledger`
method. A refused call raises a tool error with the reason. A gate never returns a
partial success.

Before any tool runs, the server checks the stamped `agent_id`, for every client:

- A swarm session still in state `registered` may call only `brief_ack` and the five
  tools that take no identity (`ledger_info`, `brief_get`, `who_owns`,
  `directive_submit`, `events`). Any other tool fails with "you are not bound to the
  ledger yet: call brief_ack(caller=...)". `pre_ledger` denies the same calls first;
  see [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md).
- A live agent may call only its role's tools (`identity.ROLE_TOOLS`).

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
| Reporting | `status_tree`, `report_build`, `analytics_query` (one read-only SELECT, Oracle only), `events(target_agent_id, limit)`, `ledger_info` |
| Driver | `drive_request`, `drive_issue`, `drive_checkin`, `drive_done`, `drive_unavailable` |

- No tool schema lists `agent_id`. The `pre_ledger` hook stamps it through
  `updatedInput`, and Claude Code 2.1.283 and later delivers a key that the schema does
  not declare. The server's `StampedAgentId` middleware pops `agent_id` from each call's
  arguments before validation and holds it in a context variable for that one request.
  A tool reads it from there, never from its arguments.
- `caller`, `targeted`, and `finding_ids` carry parameter descriptions in the schema, and
  so do `contract`, `depends_on`, `parties`, and `directive_id`.
- Signatures that carry a gate: `module_add(phase_id, name, depends_on)`,
  `claim_file(path, test_path, for_name, depends_on)`, `brief_create(..., contract)`,
  `deferral_propose(body, kind, file_id, parties)`, and
  `agreement_decide(deferral_id, decision, reason, directive_id)`. `brief_get` returns
  `depends_on_contracts`, and `deferral_propose` with `parties` returns `arbiter` and
  `next`.
- `score_record`'s schema lists every rubric key, built from `rubric.DIMENSIONS`:
  `ratings` pairs each dimension with its own criteria, `applicable` requires every
  dimension key, and `targeted` lists the dimension keys. The description no longer
  repeats the key list. The ledger still validates every rating and answers with its own
  refusal message; the schema does not reject a key itself.
- Each role may call only the ledger tools that `identity.ROLE_TOOLS` names for it. Each
  role template's `tools` line lists exactly those tools as `mcp__swarm-ledger__<tool>`,
  never the whole server, and `test_plugin_surface.py` checks the sets against the
  ledger's own role checks. A role's set holds every tool that the ledger's role checks
  grant the role, and every tool that its template calls.
- The ledger also refuses a live agent's call to a tool outside its role's set, in
  `server._call`, with "the <role> role may not call <tool>". An `agent_id` that no live
  agent holds passes this check.
- `events` filters on `target_agent_id`, so the filter no longer shares the stamped
  identity's name.
- A tool that writes nothing carries the MCP annotation `readOnlyHint: true`, so Claude
  Code runs several of them from one message in parallel: `ledger_info`, `brief_get`,
  `who_owns`, `issue_list`, `cr_list`, `run_status`, `status_tree`, `guidelines_get`,
  `plan_unlocked`, `events`, `analytics_query`, and `test_run_get`. A tool that marks a row is not
  read-only: `message_inbox` marks its messages read, and `directive_inbox` sets
  `notified_at`. Mark a new tool read-only only when it writes no row, file, or session.
- `tests_run(scope, target)` is role-bound: `file` to the Coder (its own path or test
  path only), `module` to the Lead, `phase` to the Manager, `full` to the Oracle. It
  runs the profile's test command with `{target}` replaced, or removed for no target,
  with the ledger's own venv dropped from `PATH`, `VIRTUAL_ENV`, and
  `UV_PROJECT_ENVIRONMENT`, and a 600-second
  timeout. It parses pytest and Go output. An agent never reports a test result itself.
- `tests_run` stores the last 20,000 characters of output in the ledger, and returns a
  summary of at most 4,000: the last 500 characters of a pass, or the first traceback
  from the `FAILURES` section and the `short test summary info` section of a failure.
  Output in another format returns its last 4,000 characters. `output_chars` gives the
  stored length, and `test_run_get(test_run_id)` returns the stored run with its full
  output to any agent of the run.
- `tests_run` reuses a passing run instead of running again when an earlier run of the
  same run used the same command on the same working tree: the same `HEAD`, the same
  tracked changes (`git status --untracked-files=no`), and the same contents of every
  file and test file claimed in the run. It records a new row that copies the earlier
  result, with `reused_from` naming it and `duration_ms` 0, and returns `reused: true`.
  Every gate then reads the new row as it reads any run. `force=true` always runs. Any
  edit, shell change, or hand-up that changed a claimed or tracked file changes the
  fingerprint, so a stale pass is never reused. A host outside git never reuses.
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
