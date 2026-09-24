# swarm-ledger: implementation layout

The design lives in `plans/`. This file maps the design onto modules so that each
piece can be built and tested alone.

## Modules

| Module | Holds |
|---|---|
| `db.py` | `ledger_path`, `connect`, `write_tx`, `migrate` with an idempotent upgrade for columns and tables added after version 1, `ensure_git_exclude` |
| `schema.sql` | The tables, including `agents.session_name`, `agents.bg_id`, and `wakeups` (the wake-ups each agent owes) |
| `settings.py` | `Settings` loaded from `.claude/sentinel-swarm.local.md` frontmatter, with the defaults from `templates/sentinel-swarm.local.md.example` |
| `identity.py` | `Caller`: resolves a call's `caller` name and stamped `agent_id` against the `agents` table. `require_role` |
| `ledger.py` | `Ledger`: one object per server process, holding the connection, the repo root, and the settings. Every tool is a method that returns a plain dict |
| `rubric.py` | Dimension math: criterion ratings to a 0 to 100 dimension score, the pass rule, the disagreement rule, and the improved, plateau, or regression classification |
| `repo.py` | `RepoMixin`: `repo_check` and `repo_branch_create`. `check_repo` is the pure git inspection underneath, callable without a `Ledger` **(proposed)** |
| `oversight.py` | `OversightMixin`: the Manager's `module_review` and the Oracle's `phase_review`, and the shared readiness checks the `phase_update` gates for `handed_up` and `approved` call **(proposed)** |
| `testing.py` | Runs the profile's test command for a scope and parses the result |
| `graph.py` | `graph_upsert` under a process lock, and the graph-current check for a file |
| `versions.py` | Saves and restores file versions in `.sentinel-swarm/versions/` |
| `server.py` | FastMCP tool registration. Each tool is a thin wrapper over a `Ledger` method |
| `serve.py` | `python -m swarm_ledger.serve`: one HTTP ledger server per repo on a free port, its URL in `.sentinel-swarm/server.json`; `ensure_server` starts it when needed |
| `sessions.py` | The `claude` CLI: start, list, stop, and resume background sessions. Every call goes through `_run`, which tests replace |
| `agentfiles.py` | Reads a host repo's `.claude/agents/swarm-<role>.md` and builds a role session's flags |
| `setup.py` | `python -m swarm_ledger.setup`: writes the role files from `templates/agents/`, the hook shim, and the settings a host repo needs |
| `launch.py` | `python -m swarm_ledger.launch`: starts the ledger server and the Oracle's session |
| `hooks/` | One entry point, `python -m swarm_ledger.hooks <event>`, that reads hook input from stdin and answers with JSON on stdout. Exit code is always 0 |

## Identity contract

- Every tool takes `caller: str`, the agent's name, and `agent_id: str | None`.
- Hook 6 stamps `agent_id` from its input, or `session_id` for a session, through `updatedInput`. Every role runs as a session, so its `agent_id` is its `session_id`. An agent never passes `agent_id` itself, and a value it passes is overwritten.
- `brief_ack` binds the name to the `agent_id`: it succeeds only when a brief exists for that name, no live agent holds the name, and the `agent_id` is not bound to another name.
- After the bind, every call resolves the `agent_id` to its registry row and refuses a `caller` that does not match.
- The Oracle is bound at `run_start`, with the session id as its `agent_id`.
- `agent_spawn` registers a child's row with the new session's id before the child starts, and the child's `brief_ack` binds it.

## Errors

A refused call raises `LedgerError(message)`. The server returns it as a tool error so that the agent sees the reason. A gate never returns a partial success.

## Records folder

`.sentinel-swarm/` at the main checkout root: `ledger.db`, `versions/`, `report.md`, `server.json`, `server.log`, and the hook shim `hook.py`. It is excluded through `.git/info/exclude`.
