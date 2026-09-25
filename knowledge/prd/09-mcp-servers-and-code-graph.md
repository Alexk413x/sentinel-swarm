# MCP servers and the code graph

## MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- A role also gets the a11y plugin's servers, `a11y-tools` and `a11y-kg`, when the host
  has `a11y@accessibility-tools` installed at user scope or for the host's path.
  `agent_spawn` and the launcher check the installed-plugins registry and add the
  servers through the shim, and their tools to `--allowedTools`.
- `setup` writes each role file with the template's `mcpServers` and `tools` only. A
  plugin that the host's `.claude/settings.json` enables does not join a role file.
  User-level plugins, user MCP servers, and claude.ai connectors never join.
- Only the future Driver will load the driver plugins.
- Tool names: `mcp__swarm-ledger__<tool>` and `mcp__codebase-kg__<tool>`.
- Each role keeps a fixed `tools` allowlist in its agent file.
- `cartographer` is an optional dependency for end-to-end testing outside the run.

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
