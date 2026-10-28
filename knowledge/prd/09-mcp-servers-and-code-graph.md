# MCP servers and the code graph

## MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- `swarm-ledger` is an HTTP entry with the repo's ledger token as a static
  `Authorization: Bearer` header. See "Access" and "The token" in
  [06-ledger-server.md](06-ledger-server.md).
- A role also gets the a11y plugin's servers, `a11y-tools` and `a11y-kg`, when the host
  has `a11y@accessibility-tools` active (enabled in settings and installed, see
  "Plugin detection" in [11-setup-and-settings.md](11-setup-and-settings.md)).
  `agent_spawn` and the launcher check this and add the servers through the shim, and
  their tools to `--allowedTools`. See "Plugin servers".
- `setup` writes each role file with the template's `mcpServers` and `tools` only. A
  plugin that the host's `.claude/settings.json` enables does not join a role file.
  User-level plugins, user MCP servers, and claude.ai connectors never join.
- The Driver's session additionally gets cartographer's MCP server (`cartographer`) and
  the active driver plugins' servers (`android-driver-kg`, `ios-driver-kg`,
  `web-driver-kg`), through the same shim mechanism as the a11y servers
  (`agentfiles.OPTIONAL_SERVERS` for a11y, `agentfiles.DRIVER_OPTIONAL_SERVERS` for the
  Driver's own, joined only when `role == "driver"`). Every other role's session never
  gets them, even when the plugins are installed.
- The Driver's own servers are keyed by the name a plugin install gives them,
  `plugin_<plugin>_<server>`: `plugin_cartographer_cartographer`,
  `plugin_android-driver_android-driver-kg`, and so on. Their tools then carry the
  `mcp__plugin_…` names that cartographer's agents grant and its hooks match. The a11y
  servers keep their bare keys until a11y renames its tools to the plugin form.
- Tool names: `mcp__swarm-ledger__<tool>` and `mcp__codebase-kg__<tool>`.
- Every ledger tool stays an MCP tool, for every role, and codebase-kg's read tools stay
  on MCP too. A command-line tool would serve only the Coder and the Driver, the two
  roles with a shell, and `pre_shell` closes the shell to the other roles on purpose.
  A command-line call carries no `pre_ledger` identity stamp, so the caller would pick
  its own identity. It saves no model turn, and it costs the gated shell's hooks plus
  the tool's own process start. A tool no role calls stays out of the templates:
  `kg_stats`, `kg_validate`, and `kg_parity_gaps` run through codebase-kg's CLI.
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
  count. `http_entry` looks for the server in the plugin's root `.mcp.json`, then in the
  manifest's `mcpServers`, which is an inline object, a path to a JSON file, or a list of
  either. A path is relative to the plugin folder, and one that resolves outside the
  folder, is absolute, or names a missing or unreadable file yields no entry. The file
  holds `{"mcpServers": {...}}`. The first source that names the server decides: a stdio
  entry there keeps the shim. A plugin with several servers, such as a11y's `a11y-tools`
  and `a11y-kg`, gets one entry per server, each with its own port option. A value must be a plain token (letters, digits, `.`, `_`, `-`; an integer for a
  `number` option), and the URL's host must stay the template's. `headersHelper` takes
  only `${CLAUDE_PLUGIN_ROOT}`, and an entry whose helper holds any other placeholder
  keeps the shim, and so does an install path that holds `"`, `$`, a backtick or a newline,
  because the helper wraps the path in double quotes in a shell command. Claude Code itself runs the helper on each connect, in the role
  session's environment, so the helper finds the session's owner id and variables the
  way it does in a session the plugin starts: a driver plugin's helper sends the owner
  id, and without it the server refuses every `driver_*` call with `no_owner`. No relay
  process starts. Any entry that fails a check
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
- A plugin version that names a plain `uv run` or `uvx` command runs one server per
  session.
- Graph writes go through `graph_upsert`, which calls codebase-kg's
  `edits.upsert_node` with the host graph's explicit path under the ledger's lock, so
  the shared codebase-kg server only ever serves reads.
- `graph_upsert` runs `edits.upsert_node` in a child process on the base interpreter
  (`sys._base_executable`) with `-I -S` and
  `<codebase-kg>/mcp/src` on `sys.path`, as codebase-kg's own workers run. It needs no
  venv in the plugin cache. It falls back to `uv run --project <codebase-kg>/mcp
  --frozen --no-dev python` when the running Python is older than the floor in
  codebase-kg's `pyproject.toml` (`requires-python = ">=3.10"` in 0.14.0), when that
  file declares any dependency (codebase-kg is standard library only since 0.12.0), or
  when `mcp/src/codebase_kg/edits.py` is missing.

## The code graph

codebase-kg is a required dependency (`>=0.14.0`). It is the plugin
`codebase-kg@alexk413x`, from the `alexk413x` marketplace (`Alexk413x/marketplace`),
installed per project. sentinel-swarm's `plugin.json` depends on it, and Claude Code
refuses to load sentinel-swarm in a project where it is not installed. 0.14.0 is the
floor: role sessions connect to its shared HTTP server directly, and its stats, validate
and parity tools run only through its CLI. An up-to-date graph lets each agent
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
  node that does not exist. It applies the upsert under a lock, with the codebase-kg
  copy that `graph.codebase_kg_root(repo_root)` resolves, run as described above.
  That function reads the install of `codebase-kg@alexk413x` for the repo from
  `~/.claude/plugins/installed_plugins.json` and returns its `mcp` folder, by the rule
  in "Plugin detection" in [11-setup-and-settings.md](11-setup-and-settings.md): the
  plugin must be enabled in settings, and the folder is the install `agentfiles.install_path`
  picks. `SENTINEL_SWARM_KG_ROOT` overrides it.
- Setup sets `post_edit_nudge: false` in the host's `.claude/codebase-kg.local.md`, so
  codebase-kg's edit hook does not send a Coder to `/codebase-kg:refresh`.
- A node that anchors on several files is updated through the Lead, once the Coders
  of those files have handed up. A node on one file, with or without its test file,
  belongs to that file's Coder, and the ledger refuses it from the Lead.
- Anchors are `"<path>#<Symbol>"` for every top-level function and class. A file with no
  functions or classes is anchored by its path alone: `"anchors": ["<path>"]`. A Coder
  never adds code only to have an anchor.
- The Coder updates the graph before its handoff, and again after any later change.
- **Baselines:** codebase-kg records one content hash per mapped file (its `source`
  baseline, SCHEMA.md section 6.3) and reports a file whose content differs as stale;
  its pre-push hook blocks on a stale file. `graph_upsert` sends every node with
  `"rebaseline": true`, so the write re-hashes every file the node anchors. A write
  states that the caller re-read the node against its files. An unchanged node that is
  not written again keeps its old baseline, which is how a reworked file goes stale.
- **Graph current at handoff:** a node anchors on the file, each anchored symbol appears
  in the file as a whole word, and, for a Python file, every public top-level function
  and class is anchored. The file and its test file are also not stale: neither differs
  from its recorded baseline. A file with no baseline is not stale. `handoff_submit`
  refuses a stale file and names its nodes; the Coder re-reads each node against the
  file and calls `graph_upsert` for it again. A node that spans files is the Lead's to
  update. The ledger reads the baseline from the graph's `source` table and hashes the
  file the way codebase-kg does (CRLF folded to LF, then SHA-256), in process. It does
  not re-baseline at `approve`: the gate has just held for the exact content the Lead
  reviewed, and the Lead has no edit tool.
- **Ownership of a node edit:** `graph_upsert` checks both the anchors a node carries and
  the anchors the graph already holds for its id against the caller's files. A node
  without `anchors` counts as anchored where the graph already anchors it. So a Coder
  cannot edit, and so re-baseline, another file's node by leaving the anchors out,
  sending empty anchors, or re-anchoring it on its own file.
- **Stale mapped files in the report:** the report lists every mapped file that is
  stale when it is built, with its nodes, and marks the files of the run. See
  [10-report-and-costs.md](10-report-and-costs.md).
- **Test-file gate:** `phase_review(accepted)` refuses an approved or incomplete file
  with no test file when the graph anchors any symbol in it. Test anchors in the graph
  do not count as tests.
- The `setup` skill builds the graph when the host has none. An empty repo starts with
  an empty graph.
