# MCP servers and the code graph

## MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- A role also gets the a11y plugin's servers, `a11y-tools` and `a11y-kg`, when the host
  has `a11y@accessibility-tools` installed at user scope or for the host's path.
  `agent_spawn` and the launcher check the installed-plugins registry and add the
  servers through the shim, and their tools to `--allowedTools`. See "Plugin servers".
- `setup` writes each role file with the template's `mcpServers` and `tools` only. A
  plugin that the host's `.claude/settings.json` enables does not join a role file.
  User-level plugins, user MCP servers, and claude.ai connectors never join.
- The Driver's session additionally gets cartographer's MCP server (`cartographer`) and
  the installed driver plugins' servers (`android-driver-kg`, `ios-driver-kg`,
  `web-driver-kg`), through the same shim mechanism as the a11y servers
  (`agentfiles.OPTIONAL_SERVERS` for a11y, `agentfiles.DRIVER_OPTIONAL_SERVERS` for the
  Driver's own, joined only when `role == "driver"`). Every other role's session never
  gets them, even when the plugins are installed.
- The Driver's own servers are keyed by the name a plugin install gives them,
  `plugin_<plugin>_<server>`: `plugin_cartographer_cartographer`,
  `plugin_android-driver_android-driver-kg`, and so on. Their tools then carry the
  `mcp__plugin_…` names that cartographer's agents grant and its hooks match. The a11y
  servers keep their bare keys until a11y renames its tools to the plugin form.
- An Oracle launch that carries the channel also lists `swarm-events`, the stdio entry
  `python .sentinel-swarm/hook.py channel`. It has no tools. No other launch lists it.
  See "Wake-up delivery" in [05-sessions.md](05-sessions.md).
- Tool names: `mcp__swarm-ledger__<tool>` and `mcp__codebase-kg__<tool>`.
- Each role keeps a fixed `tools` allowlist in its agent file. It names each ledger tool
  the role may call, from `identity.ROLE_TOOLS`, never the whole `mcp__swarm-ledger`
  server. See "Tools" in [06-ledger-server.md](06-ledger-server.md).
- `cartographer` is not a plugin dependency; the manifest declares no optional
  dependencies. The Driver uses it for end-to-end testing inside the run, after each
  wave; a host without it, or without a driver plugin, simply has no Driver, and the
  run finishes on unit tests alone.

## Plugin servers

- The ledger runs no shared MCP servers. Each plugin shares its own servers through its
  own relay.
- A session reaches every plugin server through the shim's stdio entry,
  `python .sentinel-swarm/hook.py mcp <plugin_id> <server>`. The shim runs the command
  that the plugin's own `.mcp.json` names, in the plugin's own environment. When that
  command is the plugin's relay, the relay does the sharing.
- When the plugin's own entry for that server is `"type": "http"`, `session_options`
  writes that entry into the role's `--mcp-config` instead of the shim's: `url` and
  `headersHelper` with `${CLAUDE_PLUGIN_ROOT}` set to the install folder and each
  `${user_config.<key>}` in `url` set to the option in the user's own `settings.json`
  `pluginConfigs`, else the manifest's `userConfig` default. A host repo's settings never
  count. A value must be a plain token (letters, digits, `.`, `_`, `-`; an integer for a
  `number` option), and the URL's host must stay the template's. `headersHelper` takes
  only `${CLAUDE_PLUGIN_ROOT}`. No relay process starts. Any entry that fails a check
  keeps the shim. codebase-kg 0.12.0 and later serves HTTP on one server per machine
  and resolves each session's graph from its roots. Verified live on 2026-10-07: a
  `--strict-mcp-config` session with codebase-kg's `headersHelper` called `kg_search`.
- codebase-kg 0.8.0 and later names its relay, `bin/kg-shim`, in its `.mcp.json`. The
  relay is one small process that connects to one codebase-kg server per machine and
  server build: the plugin version plus a digest of its Python files. Its handshake
  sends the session's working folder and any explicit graph path (the relay's first
  argument, else `CODEBASE_KG_PATH`), and the shared server resolves each tool call's
  graph from that connection, so every session reads its own host's
  `knowledge/code_graph.db`. When the shared server cannot be reached within
  `CODEBASE_KG_SHARED_TIMEOUT` seconds (default 10), the relay starts a private server
  for that session. Verified in codebase-kg 0.8.0's `shim.py`, `daemon.py`, and
  `server.py` on 2026-09-25, and in 0.8.2's `shim.py` on 2026-09-27.
- The accessibility-tools plugins share themselves the same way from their AP-21
  release. Each names a relay in its `.mcp.json`. The relay connects to one server per machine
  and server build, and falls back to a private stdio server when it cannot reach the
  shared one.
- An older plugin version that names a plain `uv run` or `uvx` command runs one server
  per session.
- Graph writes go through `graph_upsert`, which calls codebase-kg's
  `edits.upsert_node` with the host graph's explicit path under the ledger's lock, so
  the shared codebase-kg server only ever serves reads.

## The code graph

codebase-kg is a required dependency (`>=0.8.2`). 0.8.2 is the floor because it is the
first release whose relay shares one server per machine and runs on Windows (the
`.cmd` fix); 0.7.x starts its own `uv run` servers in every session. An up-to-date graph lets each agent
find what exists while many agents change the code at once. The graph file,
`knowledge/code_graph.db`, belongs to the host repo and is committed with its code.

- Every role searches the graph first, at its own level, and uses Grep or Glob only
  when the graph lacks the answer. Each role template's start sequence names the
  level: the Oracle searches at the system level (components and their dependencies),
  a Manager at the component level (the components and folders of its phase), a Lead
  at the file level (the files of its module and the helpers that exist), a Coder at
  the symbol level (functions and classes it can reuse), and the Driver at the screen
  level (the module or file behind a screen in a finding). codebase-kg's own search gate hook
  enforces this order.
- The ledger records each Grep and Glob a role runs, with its pattern and the paths it
  found, as a graph gap: the `post_activity` hook writes a `graph_gaps` row. The report
  lists them. Why the graph fell short is not recorded in words; the pattern and the
  paths found are the structural record. A search scoped to a file the graph anchors is
  recorded too, and the report marks it.
- Only the Coder and the Lead write to the graph, and only through
  `graph_upsert(nodes)`. It takes codebase-kg's `kg_upsert_node` node shape (`id`,
  `kind`, `section`, `description`, `anchors`, `edges`). The ledger refuses a Coder's
  anchor outside its file and test file, a Lead's anchor outside the files and test
  files its module claimed (released and superseded claims excluded), and an edge to a
  node that does not exist. It applies the upsert under a lock
  in the ledger process, through `uv run` in the `mcp` folder of the highest numbered
  version under `~/.claude/plugins/cache/codebase-kg/codebase-kg/`, or in
  `SENTINEL_SWARM_KG_ROOT`.
- Setup sets `post_edit_nudge: false` in the host's `.claude/codebase-kg.local.md`, so
  codebase-kg's edit hook does not send a Coder to `/codebase-kg:refresh`.
- A node that anchors on several files is updated through the Lead, once the Coders
  of those files have handed up.
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
  an empty graph.
