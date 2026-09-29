# swarm-ledger: implementation layout

The design lives in `knowledge/prd/` at the repo root. This file maps the design onto modules so that each
piece can be built and tested alone.

## Modules

| Module | Holds |
|---|---|
| `db.py` | `ledger_path`, `connect`, `write_tx`, `migrate` with an idempotent upgrade for columns and tables added after version 1 (and for the dropped `runs.driver_unavailable_*` columns, which become a declined `[driver-unavailable]` directive), `ensure_git_exclude` |
| `schema.sql` | The tables, including `agents.session_name`, `agents.bg_id`, `agents.channel`, `wakeups` (the wake-ups each agent owes, with `pushed_at` for a channel push), `watchdog_findings`, `directives.notified_at`, `runs.watch_heartbeat_at`, `briefs.finding_ids_json`, and `drive_requests`, `drive_findings`, and `drive_stops` for the Driver's exploration queue and its stop rules |
| `settings.py` | `Settings` loaded from `.claude/sentinel-swarm.local.md` frontmatter, with the defaults from `templates/sentinel-swarm.local.md.example` |
| `identity.py` | `Caller`: resolves a call's `caller` name and stamped `agent_id` against the `agents` table. `require_role`, and `ROLE_TOOLS` with `require_role_tool`, the per-role ledger tool sets |
| `ledger.py` | `Ledger`: one object per server process, holding the connection, the repo root, and the settings. Every tool is a method that returns a plain dict |
| `review.py` | `ReviewMixin`: tests, the code graph, scoring, the handoff/approve/return cycle, the notification for an issue that ends its last round below the floor, and the report. Its `_release_agent` also abandons a released Driver's open exploration |
| `agreements.py` | **(proposed)** `AgreementsMixin`: change requests (`cr_open`, `cr_accept`, `cr_complete`, `cr_verify`, `cr_list`), departures and shortfalls (`departure_record`, `departure_decide`, `shortfall_record`), and the gates that block `handoff_submit`, `approve`, `phase_update(approved)`, and `run_finish` while one is unresolved. `Ledger` inherits `AgreementsMixin` before `ReviewMixin`, because `ReviewMixin` declares stub bodies for the gate methods (so `review.py`'s own methods type-check against it alone), and MRO resolves the first base's attribute first |
| `rubric.py` | Rating validation, which requires every dimension in every score set. The JSON schemas of `score_record`'s `ratings`, `applicable`, and `targeted`, built from `DIMENSIONS`. Dimension math: criterion ratings to a 0 to 100 dimension score, the pass rule, the disagreement rule, and the improved, plateau, or regression classification |
| `repo.py` | `RepoMixin`: `repo_check` and `repo_branch_create`. `check_repo` is the pure git inspection underneath, callable without a `Ledger` **(proposed)** |
| `oversight.py` | `OversightMixin`: the Manager's `module_review` and the Oracle's `phase_review`, and the shared readiness checks the `phase_update` gates for `handed_up` and `approved` call **(proposed)** |
| `drive.py` | **(proposed)** `DriveMixin`: the Driver's exploration queue (`drive_request`, `drive_issue`, `drive_checkin`, `drive_done`, `drive_unavailable`), the pure `compute_loop_status` the stop rules run on, the stop directives and their `drive_stops` targets, the `[driver-unavailable]` directive `drive_unavailable` files, `open_findings` and `findings_named` (which `brief_create`, `brief_get`, `status_tree`, and the report read), the fix gate `brief_create` and `agent_spawn` call on a brief's `finding_ids`, and the `run_finish` gate that requires a clean exploration, a resolved stop rule, or a `[driver-unavailable]` directive resolved `declined`, and `release_closed_driver`, which releases a Driver once its exploration is closed and its last wake-up to the Oracle is sent |
| `testing.py` | Runs the profile's test command for a scope and parses the result |
| `graph.py` | `graph_upsert` under a process lock, and the graph-current check for a file |
| `versions.py` | Saves and restores file versions in `.sentinel-swarm/versions/` |
| `server.py` | FastMCP tool registration. Each tool is a thin wrapper over a `Ledger` method. The `StampedAgentId` middleware takes `agent_id` out of every call. The read-only tools carry `readOnlyHint` |
| `serve.py` | `python -m swarm_ledger.serve`: one HTTP ledger server per repo, on the port saved in `.sentinel-swarm/server.port` or a free one, its URL in `.sentinel-swarm/server.json`; `ensure_server` starts it when needed. It starts the watchdog thread, and serves `POST /hook/<event>`, which runs a hook handler on its own connection for the shim's fast path |
| `watchdog.py` | The watchdog, a thread in the ledger server: `scan` turns `claude agents --json` and the ledger into findings, `record` dedups them in `watchdog_findings` and files each new one as a `watchdog` directive, and `Watchdog.tick` also records a notification for a Driver report, wakes a stopped Oracle, and exits an idle server |
| `wake.py` | **(proposed)** Wake-up delivery: `route_wakeup`, the one switch between a channel push, `SendMessage`, and `agent_resume`; `EventHub`, where the `/events` route registers each session's stream; and confirmation of a push from the target's transcript |
| `bridge.py` | **(proposed)** `python -m swarm_ledger.bridge`: the `swarm-events` stdio channel server. It reads the ledger's `/events` stream for its session and writes each event as a `notifications/claude/channel` |
| `clock.py` | `utcnow`, `stamp`, and `parse_stamp`, the ledger's UTC time helpers |
| `notify.py` | **(proposed)** User notifications for Driver events and for an issue that ends its last round below the floor: `record` keeps one `notifications` row per event, `show` runs the platform's OS notification in a daemon thread through the replaceable `runner`, and `owed`, `mark_sent`, and `announce` track the `PushNotification` call the Oracle owes |
| `watch.py` | `python -m swarm_ledger.watch`: the Oracle's `Monitor` listener. Prints one line per new watchdog directive and per owed `PushNotification`, beats `runs.watch_heartbeat_at`, and exits when the run is not active |
| `sessions.py` | The `claude` CLI: start, list, stop, and resume background sessions. Every call goes through `_run`, which tests replace |
| `agentfiles.py` | Reads a host repo's `.claude/agents/swarm-<role>.md` and builds a role session's flags |
| `setup.py` | `python -m swarm_ledger.setup`: writes the role files from `templates/agents/`, the hook shim, and the settings a host repo needs |
| `launch.py` | `python -m swarm_ledger.launch`: starts the ledger server and the Oracle's session |
| `checklist.py` | `python -m swarm_ledger.checklist`: the smoke test's checks on the latest run, as text or, with `--json`, as one document with the run's metrics |
| `metrics.py` | **(proposed)** `run_metrics`: a run's cost, wall time, tool calls, tool latency, hook time, returns, escalations, watchdog findings, overflows, and test runs, from the ledger and the role transcripts |
| `hooks/` | `HANDLERS` and `run_event`, which the server's hook route and the entry point `python -m swarm_ledger.hooks <event>` share. The entry point reads hook input from stdin and answers with JSON on stdout. Exit code is always 0 |

## Identity contract

- Every tool that needs an identity takes `caller: str`, the agent's name. No tool schema lists `agent_id`.
- The `pre_ledger` hook stamps `agent_id` from its input, or `session_id` for a session, through `updatedInput`. Every role runs as a session, so its `agent_id` is its `session_id`. An agent never passes `agent_id` itself, and a value it passes is overwritten.
- `server.StampedAgentId`, a FastMCP middleware, pops `agent_id` from each call's arguments before validation and holds it in a context variable for that one request. A tool reads it through the `Depends(_stamped_agent_id)` default of its hidden `agent_id` parameter.
- `identity.ROLE_TOOLS` names the ledger tools each role may call. Each role template's `tools` line lists exactly those tools, and `server._call` refuses a live agent's call to a tool outside its role's set.
- `brief_ack` binds the name to the `agent_id`: it succeeds only when a brief exists for that name, no live agent holds the name, and the `agent_id` is not bound to another name.
- After the bind, every call resolves the `agent_id` to its registry row and refuses a `caller` that does not match.
- The Oracle is bound at `run_start`, with the session id as its `agent_id`.
- `agent_spawn` registers a child's row with the new session's id before the child starts, and the child's `brief_ack` binds it.

## Errors

A refused call raises `LedgerError(message)`. The server returns it as a tool error so that the agent sees the reason. A gate never returns a partial success.

## Records folder

`.sentinel-swarm/` at the main checkout root: `ledger.db`, `versions/`, `report.md` (the latest run's report), `report-<run_id>.md` for each run, `server.json`, `server.port`, `server.log`, and the hook shim `hook.py`. It is excluded through `.git/info/exclude`.
