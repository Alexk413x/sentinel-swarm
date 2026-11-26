# MCP servers and the code graph

## MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- Every role's config also names codebase-kg's hook relay,
  `plugin:codebase-kg:codebase-kg-hooks`, when the installed codebase-kg is 0.18.0 or
  later. See "codebase-kg's hooks in a role session".
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
  `python .sentinel-swarm/hook.py mcp <plugin_id> <server>`. The command is `python` on
  Windows, and `python3` elsewhere when `python3` is on `PATH`, both in a role file
  setup writes and in the entries the ledger adds. The shim runs the command
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
- codebase-kg 0.18.0 declares two servers in `mcp/claude.mcp.json`: `codebase-kg`, the
  HTTP entry for the one shared server per machine, and `codebase-kg-hooks`, a stdio
  relay that lists no tools and answers the plugin's hooks. Its launchers are
  `mcp/launch/kg-shim` and `mcp/launch/kg-hooks`, each with a `.cmd` twin for Windows.
  Read in the installed 0.18.0 on 2026-11-26.
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

## codebase-kg's hooks in a role session

- codebase-kg 0.18.0 answers its hooks (the search gate, the push gate, the commit check,
  and the allow for its graph write tools) through its second MCP server. The hooks name
  that server `plugin:codebase-kg:codebase-kg-hooks`. A plugin's hooks load in a
  `--strict-mcp-config` session and its servers do not, so a role session that lacks a
  server of exactly that name runs none of those gates, and Claude Code prints a failure
  notice for every hook call.
- `session_options` adds that server to every role's `--mcp-config`. The entry comes from
  codebase-kg's own command, `<install>/mcp/launch/kg_cli.py strict-mcp-config --port
  <port>`, run on the base interpreter with `-I -S`: `agentfiles.kg_hook_relay` takes
  the `plugin:codebase-kg:codebase-kg-hooks` key of the printed `mcpServers`. The port is
  the user's `server_port` option, else the manifest's default, by the rule for
  `${user_config.<key>}` in "Plugin servers".
- The entry is left out when the install has no `mcp/src/codebase_kg/strict_config.py`
  (0.17.1 and older answer their hooks with scripts and need no server), when the port
  option is not a plain integer, and when the command fails or prints no such entry. No
  process starts in the first two cases.
- The relay lists no tools, so nothing joins `--allowedTools`.
- The `codebase-kg` HTTP entry keeps its `headersHelper`. A strict session that passes
  the HTTP entry without it makes Claude Code record the server as needing
  authentication, and later strict sessions skip it.
- The search gate counts graph queries per session in the codebase-kg server. `SKIP_KG`
  does not open it; `search_gate: off` in the host's `.claude/codebase-kg.local.md` does.
- **A role never pushes or commits.** `pre_shell` refuses `git commit` and `git push`
  for every role, and the ledger makes the phase commit itself, outside any session's
  shell. codebase-kg's push gate denies a `git push` while a mapped file is stale, and
  turns a `SKIP_KG=1` or `KG_STALE_ACK=<n>` prefix into a permission prompt that a
  session with no user cannot pass. A role that meets either message does not run
  `/codebase-kg:refresh` and does not retry with a skip prefix: it brings its nodes up to
  date with `graph_upsert`, and the user pushes. The Coder's template and the
  `swarm-protocol` skill say so.

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
- The Coder, the Lead, and the Oracle write to the graph, and only through
  `graph_upsert(nodes)`. It takes codebase-kg's `kg_upsert_node` node shape (`id`,
  `kind`, `section`, `description`, `anchors`, `edges`). The ledger refuses a Coder's
  anchor outside its file and test file (see "A Coder on a node that spans files"
  below), a Lead's anchor on a file another module claims, an Oracle's anchor on any file
  a module claims, a description over codebase-kg's 240-character cap, and an edge to a
  node that does not exist. A claimed file is the file or test file of a live claim in
  the run: not released, superseded, approved, or accepted as incomplete. The Lead's own
  files are its module's claims that are not released or superseded. It applies the upsert under a lock, with the codebase-kg
  copy that `graph.codebase_kg_root(repo_root)` resolves, run as described above.
  That function reads the install of `codebase-kg@alexk413x` for the repo from
  `~/.claude/plugins/installed_plugins.json` and returns its `mcp` folder, by the rule
  in "Plugin detection" in [11-setup-and-settings.md](11-setup-and-settings.md): the
  plugin must be enabled in settings, and the folder is the install `agentfiles.install_path`
  picks. `SENTINEL_SWARM_KG_ROOT` overrides it.
- Setup sets `commit_check: false` in the host's `.claude/codebase-kg.local.md`, so
  codebase-kg's commit check does not send a role to `/codebase-kg:refresh`, which no
  role may run. The host's own sessions lose the commit check too.
- A node that anchors on several files is updated through the Lead, once the Coders
  of those files have handed up. Each file is one of its module's or one no module
  claims. A node on one of its module's files, with or without its test file, belongs to
  that file's Coder, and the ledger refuses it from the Lead.
- The Oracle updates a node whose anchors all lie on files no module claims.
- **A Coder on a node that spans files** may add anchors on its own file and test file,
  and re-record its file's baseline, because both belong to one file. The node it sends
  carries only `id` and `anchors`: every anchor the node already has, plus the new ones.
  A node of only `id` re-records the baseline. The ledger refuses a removed anchor, an
  added anchor on another file, any other field, and a node that does not anchor the
  Coder's file. Because a write re-baselines every file the node anchors, it also
  refuses while another anchored file is stale, and names that file; the Lead updates
  the node then.
- **A Coder on a moved file.** When the claim replaces paths (`file_moves`, see "Claims"
  in [02-run-lifecycle.md](02-run-lifecycle.md)), those paths count as the Coder's for
  one purpose: a node whose anchors lie only on the Coder's paths and the replaced paths
  is the Coder's to rewrite, so it re-anchors a single-file node from the old path to
  the new one. On a node that also anchors other files, the Coder may leave out the
  anchors on replaced paths, and nothing else it could not do before. The Lead's file set
  includes the replaced paths, so a node that spans files and still names one is the
  Lead's to update. `handoff_submit` refuses while a replaced file exists on disk or a
  node still anchors one.
- **Deleted files.** `graph_upsert` cannot write a node that anchors a file missing on
  disk, because its re-baseline cannot read the file. A Coder never calls it for a
  deletion: `handoff_submit` removes the anchors on the claim's own deleted paths. A node
  that anchored only deleted paths is deleted (codebase-kg `delete_node`, with the edges
  that point at it); any other node is rewritten with its remaining anchors and no
  re-baseline. The ledger runs both under the graph lock, in the child process used
  for `graph_upsert`. This is the only node deletion a role gets, and it covers only the
  paths the Coder deleted from its own claim.
- **Description cap:** codebase-kg refuses a description over 240 characters
  (`schema.MAX_DESCRIPTION`). `graph_upsert` refuses one up front with the cap in the
  message, and every role's guidance states the cap.
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
  in the file, and, for a Python file, every public top-level function and class is
  anchored. A symbol appears when each of its dotted segments appears as a whole word,
  which is codebase-kg's own rule (`tools._symbol_in_source`), so a
  `path#Class.method` anchor resolves. A `Class.method` anchor also maps `Class`. The
  refusal for a file with no anchors or an unmapped symbol lists the exact anchors to
  add, as `path#Symbol`, or the bare path for a file with none. The file and its test file are also not stale: neither differs
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
  do not count as tests. A file whose path no longer exists, and a file claimed with
  `no_test_reason`, pass the gate.
- The `setup` skill builds the graph when the host has none. An empty repo starts with
  an empty graph.
