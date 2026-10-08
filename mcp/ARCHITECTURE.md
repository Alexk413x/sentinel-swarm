# swarm-ledger: implementation layout

The design lives in `knowledge/prd/` at the repo root. This file maps the design onto modules so that each
piece can be built and tested alone.

## Modules

| Module | Holds |
|---|---|
| `db.py` | `ledger_path`, `connect`, `write_tx`, `migrate` with an idempotent upgrade for columns and tables added after version 1 (and for the dropped `runs.driver_unavailable_*` columns, which become a declined `[driver-unavailable]` directive), `ensure_git_exclude` |
| `schema.sql` | The tables, including `agents.session_name`, `agents.bg_id`, `wakeups` (the wake-ups each agent owes), `mod_sessions` (the sessions the mod checked in for), `messages.claim_id` and `claimed_at` (the mod's inbox claims), `watchdog_findings`, `directives.notified_at`, `runs.watch_heartbeat_at`, `briefs.finding_ids_json`, and `drive_requests`, `drive_findings`, and `drive_stops` for the Driver's exploration queue and its stop rules |
| `settings.py` | `Settings` loaded from `.claude/sentinel-swarm.local.md` frontmatter, with the defaults from `templates/sentinel-swarm.local.md.example` |
| `identity.py` | `Caller`: resolves a call's `caller` name and stamped `agent_id` against the `agents` table. `require_role`, and `ROLE_TOOLS` with `require_role_tool`, the per-role ledger tool sets. `require_mod_session` refuses `run_start` and `brief_ack` from a session with no `mod_sessions` row |
| `ledger.py` | `Ledger`: one object per worker process (or per server with `max_workers: 0`), holding the connection, the repo root, the settings, and the server's pid for the run lock. Every tool is a method that returns a plain dict |
| `review.py` | `ReviewMixin`: tests, the code graph, scoring, the handoff/approve/return cycle, the notification for an issue that ends its last round below the floor, and the report. Its `_release_agent` also abandons a released Driver's open exploration |
| `agreements.py` | `AgreementsMixin`: change requests (`cr_open`, `cr_accept`, `cr_complete`, `cr_verify`, `cr_list`), departures and shortfalls (`departure_record`, `departure_decide`, `shortfall_record`), and the gates that block `handoff_submit`, `approve`, `phase_update(approved)`, and `run_finish` while one is unresolved. `Ledger` inherits `AgreementsMixin` before `ReviewMixin`, because `ReviewMixin` declares stub bodies for the gate methods (so `review.py`'s own methods type-check against it alone), and MRO resolves the first base's attribute first |
| `rubric.py` | Rating validation, which requires every dimension in every score set. The JSON schemas of `score_record`'s `ratings`, `applicable`, and `targeted`, built from `DIMENSIONS`. Dimension math: criterion ratings to a 0 to 100 dimension score, the pass rule, the disagreement rule, and the improved, plateau, or regression classification |
| `repo.py` | `RepoMixin`: `repo_check` and `repo_branch_create`. `check_repo` is the pure git inspection underneath, callable without a `Ledger` |
| `oversight.py` | `OversightMixin`: the Manager's `module_review` and the Oracle's `phase_review`, and the shared readiness checks the `phase_update` gates for `handed_up` and `approved` call |
| `drive.py` | `DriveMixin`: the Driver's exploration queue (`drive_request`, `drive_issue`, `drive_checkin`, `drive_done`, `drive_unavailable`), the pure `compute_loop_status` the stop rules run on, the stop directives and their `drive_stops` targets, the `[driver-unavailable]` directive `drive_unavailable` files, `open_findings` and `findings_named` (which `brief_create`, `brief_get`, `status_tree`, and the report read), the fix gate `brief_create` and `agent_spawn` call on a brief's `finding_ids`, the fresh-claim gate on a fix Coder's brief, `_write_brief`'s Driver brief for `drive_request` (the public `brief_create` refuses a Driver), and the `run_finish` gate that requires a clean exploration, a resolved stop rule, or a `[driver-unavailable]` directive resolved `declined`, and `release_closed_driver`, which releases a Driver once its exploration is closed and its last wake-up to the Oracle is sent |
| `testing.py` | Runs the profile's test command for a scope and parses the result |
| `graph.py` | `graph_upsert` under the file lock `.sentinel-swarm/kg.lock`, run on the base Python with codebase-kg's source on the path (`uv run` below its stated floor), and `codebase_kg_root`, which finds codebase-kg's `mcp` folder from the install of `codebase-kg@alexk413x` for the repo, and the graph-current check for a file |
| `versions.py` | Saves and restores file versions in `.sentinel-swarm/versions/` |
| `server.py` | FastMCP tool registration, the source of `catalog.json` and the in-process client the tests use; the running server does not import it. Each tool is a thin wrapper over a `Ledger` method through `front.call_method`. The `StampedAgentId` middleware takes `agent_id` out of every call. The read-only tools carry `readOnlyHint` |
| `catalog.py`, `catalog.json` | The tool catalog the lean front serves: the `tools/list` result as `server.py` lists it, the argument schemas pydantic checks, and the tools that take the stamped `agent_id`. `validate` checks arguments the way pydantic's lax mode does. `python -m swarm_ledger.catalog` writes the JSON |
| `http_front.py` | The lean stdlib HTTP front: MCP on `/mcp` in both protocol eras, `POST /hook/<event>`, and `/health`, behind `serve.refusal` |
| `front.py` | What the front does with a call: send it to a pool worker (or run it in-process under `_CALL_LOCK` with `max_workers: 0`), run the notifier commands a reply lists, call `on_run_finish` after `run_finish`, keep `last_call_at` for the watchdog, and send hooks to the hook pool |
| `pool.py` | The elastic worker pool, after codebase-kg's: `Pool`, the worker command, which carries the front's settings snapshot, `run_tool` (the role and bind gates, then the `Ledger` method), and `worker_main`, the worker's request loop |
| `serve.py` | `python -m swarm_ledger.serve`: one HTTP ledger server per repo, on the port saved in `.sentinel-swarm/server.port` or a free one, its URL in `.sentinel-swarm/server.json`; `ensure_server` starts it when needed. It starts the two worker pools and the watchdog thread and runs the lean front. `refusal` answers 403 to a remote caller, a wrong `Host` or `Origin`, and a request without the bearer token |
| `auth.py` | The repo's ledger token in `.sentinel-swarm/http-token`, and the checked `swarm-ledger` entry with its bearer header that `session_options` writes |
| `watchdog.py` | The watchdog, a thread in the ledger server: `scan` turns `claude agents --json` and the ledger into findings, `record` dedups them in `watchdog_findings` and files each new one as a `watchdog` directive, and `Watchdog.tick` also records a notification for a Driver report, wakes a stopped Oracle (or records that no session is recorded to wake), and exits an idle server |
| `wake.py` | Wake-up delivery: `route_wakeup`, the one switch between `SendMessage` and `agent_resume`; `mod_step`, the `next` text for a caller whose mod sends the wake-up; and the time signal, `time_signal` and `signal_for`, which end the Oracle's and a Manager's wake-ups with the run's elapsed time |
| `clock.py` | `utcnow`, `stamp`, and `parse_stamp`, the ledger's UTC time helpers |
| `notify.py` | User notifications for Driver events and for an issue that ends its last round below the floor: `record` keeps one `notifications` row per event, `show` runs the platform's OS notification in a daemon thread through the replaceable `runner`, and `owed`, `mark_sent`, and `announce` track the `PushNotification` call the Oracle owes |
| `watch.py` | `python -m swarm_ledger.watch`: the Oracle's `Monitor` listener. Prints one line per new watchdog directive and per owed `PushNotification`, beats `runs.watch_heartbeat_at`, and exits when the run is not active |
| `sessions.py` | The `claude` CLI: start, list, stop, and resume background sessions, and `plugin_load_problem`, which reads `claude plugin list` for the launcher. Every call goes through `_run`, which tests replace |
| `agentfiles.py` | Reads a host repo's `.claude/agents/swarm-<role>.md` and builds a role session's flags |
| `setup.py` | `python -m swarm_ledger.setup`: writes the role files from `templates/agents/` (which carry no hooks) and strips any ledger command hook from an existing role file, then writes the hook shim and the settings a host repo needs |
| `launch.py` | `python -m swarm_ledger.launch`: refuses to start unless `sessions.plugin_load_problem` finds sentinel-swarm enabled, then starts the ledger server and the Oracle's session |
| `checklist.py` | `python -m swarm_ledger.checklist`: the smoke test's checks on the latest run, as text or, with `--json`, as one document with the run's metrics |
| `metrics.py` | `run_metrics`: a run's cost, wall time, tool calls, tool latency, hook time, returns, escalations, watchdog findings, overflows, and test runs, from the ledger and the role transcripts |
| `hooks/` | `HANDLERS` and `run_event`, which the server's hook route and the entry point `python -m swarm_ledger.hooks <event>` share. The entry point reads hook input from stdin and answers with JSON on stdout. Exit code is always 0. Besides the classic events, `owed`, `wake_sent`, `inbox_take`, `inbox_ack`, and `inbox_release` serve the plugin's mod (`hooks/register.ts` at the repo root), which posts every hook to the route itself |

## Identity contract

- Every tool that needs an identity takes `caller: str`, the agent's name. No tool schema lists `agent_id`.
- The `pre_ledger` hook stamps `agent_id` from its input, or `session_id` for a session, through `updatedInput`. Every role runs as a session, so its `agent_id` is its `session_id`. An agent never passes `agent_id` itself, and a value it passes is overwritten.
- The lean front (`http_front.call_tool`) pops `agent_id` from each call's arguments before validation and passes it to the worker beside them; the tools in `catalog.json`'s `stamped` list get it as their `agent_id` argument. In the fastmcp registrations, `server.StampedAgentId` does the same with a context variable that a tool reads through the `Depends(_stamped_agent_id)` default of its hidden `agent_id` parameter.
- `identity.ROLE_TOOLS` names the ledger tools each role may call. Each role template's `tools` line lists exactly those tools, and `pool.run_tool` refuses a live agent's call to a tool outside its role's set.
- `brief_ack` binds the name to the `agent_id`: it succeeds only when a brief exists for that name, no live agent holds the name, and the `agent_id` is not bound to another name.
- After the bind, every call resolves the `agent_id` to its registry row and refuses a `caller` that does not match.
- The Oracle is bound at `run_start`, with the session id as its `agent_id`.
- `agent_spawn` registers a child's row with the new session's id before the child starts. The child's `session_start` hook calls `brief_ack` to bind it, after the mod's check-in recorded the `mod_sessions` row that `brief_ack` and `run_start` require, and `pre_ledger` denies a still-registered child every tool but `brief_ack` and the tools that take no identity.

## Errors

A refused call raises `LedgerError(message)`. The server returns it as a tool error so that the agent sees the reason. A gate never returns a partial success.

## Records folder

`.sentinel-swarm/` at the main checkout root: `ledger.db`, `versions/`, `report.md` (the latest run's report), `report-<run_id>.md` for each run, `server.json`, `server.port`, `http-token`, `server.log`, `kg.lock`, and the hook shim `hook.py`. It is excluded through `.git/info/exclude`.
