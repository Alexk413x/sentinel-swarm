# MCP servers and the code graph

## MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- A role also gets the a11y plugin's servers, `a11y-tools` and `a11y-kg`, when the host
  has `a11y@accessibility-tools` installed at user scope or for the host's path.
  `agent_spawn` and the launcher check the installed-plugins registry and add the
  servers, and their tools to `--allowedTools`. Each server is the shared HTTP server
  when one answers, and the shim's stdio entry otherwise. See "Shared HTTP servers".
- `setup` writes each role file with the template's `mcpServers` and `tools` only. A
  plugin that the host's `.claude/settings.json` enables does not join a role file.
  User-level plugins, user MCP servers, and claude.ai connectors never join.
- The Driver's session additionally gets cartographer's MCP server (`cartographer`) and
  the installed driver plugins' servers (`android-driver-kg`, `ios-driver-kg`,
  `web-driver-kg`), through the same shim mechanism as the a11y servers
  (`agentfiles.OPTIONAL_SERVERS` for a11y, `agentfiles.DRIVER_OPTIONAL_SERVERS` for the
  Driver's own, joined only when `role == "driver"`). Every other role's session never
  gets them, even when the plugins are installed. **(proposed)**
- Tool names: `mcp__swarm-ledger__<tool>` and `mcp__codebase-kg__<tool>`.
- Each role keeps a fixed `tools` allowlist in its agent file.
- `cartographer` is an optional dependency. The Driver uses it for end-to-end testing
  inside the run, between waves; a host without it, or without a driver plugin, simply
  has no Driver, and the run finishes on unit tests alone.

## Shared HTTP servers

Decided by Alex on 2026-09-25: build the HTTP change first. The mechanism below is
**(proposed)**.

- The ledger server runs one shared HTTP instance of `a11y-tools` and of `a11y-kg` per
  host repo, when the host has `a11y@accessibility-tools` installed. Every session
  connects to them by URL, so a session starts no a11y server process of its own.
- codebase-kg shares itself. Decided by Alex on 2026-09-25: the ledger does not run a
  shared codebase-kg. From codebase-kg 0.8.0, each session's stdio entry runs
  codebase-kg's own relay, `bin/kg-shim`, one small process that connects to one
  codebase-kg server per machine and plugin version. The relay's handshake sends the
  session's working folder and any explicit graph path, and the shared server resolves
  each tool call's graph from that connection, so every session reads its own host's
  `knowledge/code_graph.db`. When the shared server cannot be reached within 10 seconds,
  the relay starts a private server for that session. Verified in codebase-kg 0.8.0's
  `shim.py`, `daemon.py`, and `server.py` on 2026-09-25.
- Each server runs in its plugin's own environment, from its plugin's `.mcp.json`, with
  no change to the plugin. The ledger runs the shim:
  `python .sentinel-swarm/hook.py mcp-http <plugin_id> <server> <port> <ledger pid>`.
- The shim rewrites the plugin's command so that the plugin's environment runs the shim
  again, in the internal mode `mcp-entry`, in place of the console script:
  - `uv run --project <root>/mcp --frozen --no-dev codebase-kg` becomes
    `uv run --project <root>/mcp --frozen --no-dev python <shim> mcp-entry <port> <pid>
    codebase-kg`.
  - `uvx --from <root>/mcp-kg a11y-kg <graph>` becomes
    `uvx --from <root>/mcp-kg python <shim> mcp-entry <port> <pid> a11y-kg <graph>`.
  - A `uvx` command with no `--from` gets `--from <script>`. Any other command form
    stays on stdio.
- `mcp-entry` finds the console script's entry point in `console_scripts`, imports its
  module, and replaces the module's `mcp.run` with a call that passes
  `transport="http"`, `host="127.0.0.1"`, and the port. It sets `sys.argv` to the
  script name and the remaining arguments, and calls the server's own `main()`. The
  server keeps its argument handling, such as a11y-kg's graph path, and FastMCP's
  default path `/mcp`.
- `session_options` lists a server as `{"type": "http", "url": ...}` when
  `server.json` records its URL and the URL answers. Otherwise the session keeps the
  shim's stdio entry. The server names stay `a11y-tools` and `a11y-kg`, so the tool
  allowlists stay the same.
- Graph writes go through `graph_upsert`, which runs codebase-kg's CLI with the host
  graph's explicit path under the ledger's lock, so the shared codebase-kg server only
  ever serves reads.
- Per session, the stdio entries ran 4 processes for codebase-kg (shim, `uv`, the venv
  launcher, and Python) and 5 for each a11y server in the `uvx` form (shim, `uvx`,
  `uv`, and two Python processes), measured on Windows on 2026-09-25. With shared
  servers, a session runs none, and the repo runs one set of trees.
- `mcp-entry` runs each server with `stateless_http=True`, so the server issues no MCP
  session id. A restarted server answers a client that initialized against the
  server's previous process. A stateful server answers that client's old session id
  with 404 "Session not found". **(proposed)**
- The `uv run` rewrite still works for any FastMCP plugin in that form, such as
  codebase-kg 0.7.0; its tests pin that version explicitly.

### Restarts

The ledger restarts a shared server that dies, on the port it had, so its URL in
`server.json` and in every running session stays valid. The mechanism is
**(proposed)**.

- A supervisor thread in the ledger server checks each server every 2 seconds. A server
  counts as dead when its process exited, or when its URL has not answered for 30
  seconds.
- It stops the dead server's whole tree, then starts the server again through the same
  path as at start: the shim's `mcp-http`, a job object on Windows, and a process group
  on POSIX.
- Back-off: the first restart starts at once, the second after 5 seconds, and the third
  after 15 seconds. A restart that does not answer within 60 seconds is stopped and
  counts as an attempt.
- Give-up: when a server dies after 3 restarts in 5 minutes, the ledger stops restarting
  it. It removes the server from `servers` in `server.json`, so a new session uses the
  stdio entry, and it files a watchdog directive for the Oracle.
- Each restart and give-up goes to `server.log`, with a UTC time stamp.
- A restart files no directive. The URL stays valid, so the Oracle has nothing to do,
  and every directive must be resolved before `run_finish`. A give-up files one,
  because a running or resumed session keeps the dead URL and has no tools from that
  server. The directive names that consequence and the next step: replace an agent that
  needs the server.
- `stop_all` signals the supervisor to stop before it stops the servers. Once stopping
  starts, the supervisor restarts nothing, and a start that races the stop is stopped.
- The Claude Code docs say that a remote server that drops is reconnected up to 5
  times, 1 second apart at first and doubling each time, and then marked failed. The
  2-second check and the immediate first restart keep a restart inside that window
  when the server starts in a few seconds. No live session has tested this.

## The code graph

codebase-kg is a required dependency (`>=0.7.0`). An up-to-date graph lets each agent
find what exists while many agents change the code at once. The graph file,
`knowledge/code_graph.db`, belongs to the host repo and is committed with its code.

- Every role searches the graph first, at its own level **(proposed)**, and uses Grep
  or Glob only when the graph lacks the answer. codebase-kg's own search gate hook
  enforces this order. An agent that falls back records what the graph was missing.
  **(proposed)**
- Only the Coder writes to the graph, and only through `graph_upsert(nodes)`. It takes
  codebase-kg's `kg_upsert_node` node shape (`id`, `kind`, `section`, `description`,
  `anchors`, `edges`). The ledger refuses an anchor outside the Coder's file and test
  file, and an edge to a node that does not exist. It applies the upsert under a lock
  in the ledger process, through `uv run` in the codebase-kg plugin's cache folder, or
  `SENTINEL_SWARM_KG_ROOT`. **(mechanism proposed)**
- A node that anchors on several files is updated through the Lead. **(proposed)**
- Anchors are `"<path>#<Symbol>"` for every top-level function and class. A file with no
  functions or classes is anchored by its path alone: `"anchors": ["<path>"]`. A Coder
  never adds code only to have an anchor.
- The Coder updates the graph before its handoff, and again after any later change.
- **Graph current at handoff:** a node anchors on the file, each anchored symbol appears
  in the file as a whole word, and, for a Python file, every public top-level function
  and class is anchored. The file-content hash is a review signal for the Lead and the
  git hooks, not a gate.
- **Test-file gate:** `phase_review(accepted)` refuses an approved or incomplete file
  with no test file when the graph anchors any symbol in it. Test anchors in the graph
  do not count as tests.
- The `setup` skill builds the graph when the host has none. An empty repo starts with
  an empty graph. **(proposed)**
