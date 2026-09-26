# Platform facts

Each fact was verified on the date shown, on Windows 11 unless noted.

- A background session reports state `done` when its turn ends, while its process still
  runs and takes messages. (2026-09-24)
- `claude --resume <id> --bg "<message>"` continues a stopped session under the same id,
  with its saved name, agent, permission mode, MCP config, and tools. On a running
  session, it starts a second copy. (2026-09-23)
- A background session that has ended its turn can exit while it waits.
  `claude --resume` brings it back. (2026-09-23)
- A line printed by a `Monitor` command wakes an idle background session. (2026-09-24)
- The Oracle's armed watchdog `Monitor` keeps its session status `busy` while the
  Oracle is idle. (2026-09-25)
- A cross-session `SendMessage` to an idle background session wakes it. `SendMessage`
  accepts only a session's name, not its session id or short id. (2026-09-23)
- The CLI has no command that sends a message to a session. (2026-09-23)
- Claude Code writes one API response as one transcript line per content block, each
  repeating the response's usage. (2026-09-24)
- Sonnet, Opus, and Fable run with a 1,000,000-token context window in Claude Code, with
  or without `[1m]`. Haiku runs with 200,000. (2026-09-24)
- `--strict-mcp-config` with `--mcp-config` starts only the listed MCP servers. Plugin
  agents, skills, and hooks still load. An idle session used 289 MB this way, against
  1,222 MB with every plugin's servers. (2026-09-23)
- An agent file's `mcpServers` key does not load when that agent runs as the session
  through `--agent`. Its `tools`, `model`, `color`, and `hooks` apply. (2026-09-23)
- A project agent file's frontmatter hooks run in a background `--agent` session only in
  a trusted folder. `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `Stop`, and `SessionEnd` fire, each with `session_id`, `transcript_path`, and
  `agent_type` set to the agent's name. `agent_id` is empty. `SessionEnd` fires on
  `claude stop` with reason `other`. (2026-09-23)
- Inside an agent-file hook, `CLAUDE_PLUGIN_ROOT` points at an unrelated plugin.
  `CLAUDE_PROJECT_DIR` is correct. (2026-09-23)
- A background session refuses to start in an untrusted folder. Trust is stored per
  folder path and does not pass down from a trusted parent. (2026-09-23)
- Without `worktree.bgIsolation: "none"`, a background session moves into its own git
  worktree and branch before it writes. With it, two sessions write in one working tree
  without trouble. (2026-09-23)
- A background session waiting on a permission prompt shows `status: waiting` and
  `waitingFor: "permission prompt"` in `claude agents --json`, and holds. (2026-09-23)
- A session loads agent files once, at startup. The launcher does not resolve agents
  from `--plugin-dir`; an installed plugin works. (2026-09-22, 2026-09-23)
- Claude Code asks before any write inside a loaded plugin's own folder. A session that
  cannot answer is denied, so a host repo must not sit inside the plugin folder.
  (2026-09-22)
- The Write tool cannot write into `.claude/`, even with `acceptEdits`. (2026-09-22)
- A `PreToolUse` hook's `updatedInput` rewrites a plugin MCP tool's input. (2026-09-21)
- The `Stop` hook input carries `stop_hook_active`, true when the stop follows a block
  by a Stop hook. The ledger's stop hook relies on it to avoid a block loop.
  (relied on since 2026-09-21)
- With `python3 ... || python ...`, a hook that exits 2 makes the shell run the second
  command with an empty stdin. (2026-09-21)
- A Python hook costs about 200 to 300 ms per call, mostly interpreter start.
  (2026-09-21)
- `kg_upsert_node` copies, edits, and replaces the graph file with no lock, so
  concurrent writers lose changes silently. It does not re-baseline a file's hash; only
  `codebase-kg-build --rebaseline` does, for every file. (2026-09-21)
- SQLite with WAL, a 5-second busy timeout, and `BEGIN IMMEDIATE` took 3,600 of 3,600
  writes from 12 processes without error. (2026-09-21)
- Per-file pytest runs in parallel in one working tree are reliable while another file
  is half-written, unless a test imports that file. (2026-09-21)
- One HTTP ledger server took 400 concurrent clients across two repos with no errors, at
  about 126 MB. (2026-09-23)
- A session with every plugin's servers used 530 to 980 MB of memory. (2026-09-23)
- On Windows, a process started with `DETACHED_PROCESS` opens a terminal window for
  every console program it runs. `CREATE_NO_WINDOW` does not. (2026-09-24)
- On Windows, stopping a `Monitor` does not kill its child processes. (2026-09-24)
- On Windows, killing a `uv run` process leaves its Python child and grandchild
  running. A job object with `KILL_ON_JOB_CLOSE` stops the whole tree, both on
  `TerminateJobObject` and when its last handle closes. (2026-09-25)
- codebase-kg 0.7.0 and a11y 0.6.2 serve over FastMCP's HTTP transport when their
  `mcp.run` is replaced before `main()` runs. `uvx --from <path> python` runs Python in
  the tool's environment. (2026-09-25)
- A FastMCP 4.0.5 HTTP server in its default stateful mode answers a client that uses
  the `2025-06-18` handshake with an `mcp-session-id`. After the server restarts on the
  same port, it answers that session id with 404 "Session not found", and a request
  with no session id with 400. With `stateless_http=True` it issues no session id, and
  after a restart it answers both requests with 200, with no new `initialize`.
  (2026-09-25)
- A FastMCP 4 `Client` negotiates a protocol version that uses no session id. One
  `Client` object kept working across a server restart, stateful or stateless, when it
  made no call while the server was down. A call while the server was down closed its
  session, and the `Client` did not reconnect. (2026-09-25)
- a11y-kg 0.6.2 (FastMCP 3.4.5) and codebase-kg 0.7.0 (FastMCP 4.0.3) accept
  `stateless_http=True` in `mcp.run`. Through `mcp-entry` and the supervisor, each
  came back on the same port after its whole tree was killed, and a `Client` opened
  before the kill made its next call without error. (2026-09-25)
- codebase-kg 0.8.0 declares its server as `${CLAUDE_PLUGIN_ROOT}/bin/kg-shim`, a
  standard-library relay, not `uv run`. The shim's `mcp-http` refuses that command.
  (2026-09-25)
- On Windows, `python3` on `PATH` can be the Microsoft Store stub, which only prints a
  hint. Setup writes `python` for MCP server commands on Windows. (noted 2026-09-21,
  not measured)
- `claude plugin install --scope project` fails on Windows while another session runs a
  server from the same cache folder, and an open session in the host holds the plugin's
  files. (2026-09-21, 2026-09-23)
- A stale session, local or over Remote Control, can keep an old session name and
  receive messages meant for a new session with that name. (2026-09-23)
- A session run with `--agent` loads the user's global `CLAUDE.md`, so a persona from
  it can appear in messages to the user. (2026-09-22)
- On Windows, Python 3.12's `shutil.which` returns an extensionless file before its `.cmd` twin. A plugin that ships a POSIX launcher beside a `.cmd`, such as codebase-kg 0.8.0's `bin/kg-shim`, then fails with WinError 193. The shim prefers the PATHEXT variants. Verified 2026-09-25.
- A channel reaches a `--bg` session only through `--channels` with a plugin on the allowlist: Anthropic's list, or an organization's `allowedChannelPlugins` in managed settings. `--dangerously-load-development-channels` does not carry into a `--bg` session, and the debug log reads `server <name> not in --channels list for this session`. An event pushed to an unregistered channel is dropped, not queued. Verified 2026-09-25.
- Claude Code 2.1.283 reads `--dangerously-load-development-channels` only in an interactive session, and applies it after the user accepts the development-channels dialog. A non-interactive session, `--bg` or `-p`, discards it. Claude Code has no settings key or environment variable for development channels. Read from the binary on 2026-09-26; the `--bg` case also confirmed live.
