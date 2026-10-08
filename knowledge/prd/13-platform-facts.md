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
- `PushNotification` from a `--bg` session returned "Not sent — this terminal is
  active" while the user was at the machine. It returned the same with
  `CLAUDE_CLIENT_PRESENCE_FILE` set to a path that does not exist. (2026-09-27)
- Windows caches a toast sender's display name and icon the first time an
  AppUserModelID shows a toast. Changing the registry values afterward changes nothing.
  (2026-09-27)
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
- A hook matcher made only of letters, digits, `_`, `-`, spaces, `,`, and `|` is a
  list of exact tool names; any other matcher is an unanchored JavaScript regular
  expression. A command hook with `async: true` runs in the background: Claude Code
  does not wait for it, does not use its output, and does not enforce its `timeout`, so
  it can finish after a later hook such as `Stop`. From the hooks documentation, not
  yet checked in a live session. (2026-09-28)
- A hook's `additionalContext` is capped at 10,000 characters. Claude Code saves a
  longer one to a file, shows the model the path and a preview of up to 2,000
  characters, and does not ask the model to read the file. `SessionStart` receives
  `source` `startup`, `resume`, `clear`, `compact`, or `fork`. From the hooks
  documentation, not yet checked in a live session. (2026-10-08)
- On Windows, a TCP connection to a closed port on `127.0.0.1` takes about 2 seconds to
  fail with `ConnectionRefusedError`. (2026-09-28)
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
  `stateless_http=True` in `mcp.run`. Restarted on the same port after its whole tree
  was killed, each answered a `Client` opened before the kill on its next call without
  error. (2026-09-25)
- codebase-kg 0.8.0 declares its server as `${CLAUDE_PLUGIN_ROOT}/bin/kg-shim`, a
  standard-library relay, not `uv run`. (2026-09-25)
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
- A channel cannot wake a `--bg` session. A channel reaches a `--bg` session only through `--channels` with a plugin on the allowlist: Anthropic's list, or an organization's `allowedChannelPlugins` in managed settings. A Pro or Max user has no setting that adds a plugin to the allowlist. Verified 2026-09-25.
- Claude Code 2.1.283 reads `--dangerously-load-development-channels` only in an interactive session, after the user accepts a dialog. A `--bg` or `-p` session discards it. Claude Code has no settings key or environment variable for development channels. Verified 2026-09-26.
- `claude agents --json` lists interactive sessions too, with `kind: "interactive"`, `pid`, `sessionId`, `name`, and `status`, and no `id` or `state`. A `claude` started with a parent session's `CLAUDE_CODE_CHILD_SESSION` in its environment is left out of the list and saves no transcript. `claude --resume <id> "<message>"` continues the same session id interactively, but under a generated name unless `--name` is passed again. Verified live 2026-09-26.
- In a `claude` command line, the prompt must come before `--channels` or
  `--dangerously-load-development-channels`. The flag reads a prompt placed after it as
  another channel entry. (2026-09-25)
- A session's stdio MCP server has `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`,
  `CLAUDE_PLUGIN_ROOT`, and `CLAUDE_CODE_SESSION_ATTENDED` in its environment.
  (2026-09-25)
- A standard-library stdio MCP server finishes its handshake 10 to 40 ms after it
  starts. (2026-09-25)
- A `--bg` session in the default permission mode stalls for good on a file write.
  `--permission-mode acceptEdits` clears it. (2026-09-25)
- Before the plugin relays, one swarm session ran 4 MCP server processes for codebase-kg
  0.7.0, or 14 with both a11y servers in their `uvx` form, and 15 to 20 MCP-related
  processes in all. (2026-09-25)
- The Android emulator (`qemu`) used about 3.8 GB of memory. (2026-09-25)
- Claude Code 2.1.284: `claude agents --json` lists a background session with `id`, `kind: "background"`, `sessionId`, `name`, `cwd`, `startedAt`, and `state`, and no `pid` or `status`. The plain listing holds only active sessions; `--all` adds completed ones. `claude --resume <id> --bg` on a session that is still running starts a copy under a new session id. Verified live 2026-09-28.
- `claude plugin uninstall <plugin> --scope project --keep-data`, run from the project
  folder, removes the project's install record and its `enabledPlugins` entry, and keeps
  `<config>/plugins/data/<plugin>-<marketplace>/`. It works after the `directory`
  marketplace's folder is deleted. The cached version folder stays. (2026-09-28)
- `claude plugin marketplace remove <name>` also uninstalls every plugin from that
  marketplace, in every project, and deletes their data folders. With no install left,
  it only removes the record from `known_marketplaces.json` and the project's
  `extraKnownMarketplaces`. (2026-09-28)
- `claude plugin eval` refuses a plugin folder that holds a hard-linked file, such as a
  uv venv linked from uv's cache. In its runs, a plugin whose `dependencies` are not
  installed loads no skill. (2026-09-28, Claude Code 2.1.284)
- Claude Code 2.1.294, mods (function hooks) in role sessions, from a throwaway test
  mod (`plans/mods-live-check.md`): a plugin's module loads in a
  `claude --bg --agent swarm-coder --strict-mcp-config` session in a trusted folder,
  from `--plugin-dir` and from a project-scope install. `claude --bg` refuses an
  untrusted folder; `claude -p --plugin-dir` runs the mod there. (2026-10-08)
- `classic.SessionStart` carries `agent_type: "swarm-<role>"` in a role session.
  `session.start` reports `isInteractive: true` and `surface: "terminal"` in a `--bg`
  session. (2026-10-08)
- A `tool.call` hook's `{ deny }`, a `.catch` deny when the hook throws, a
  `classic.Stop` `block`, `classic.SessionStart` `additionalContext`, and an
  `updatedInput` or `next({ ...e, agent_id })` that adds `agent_id` to an MCP tool's
  input all work in a `--bg` role session. The engine does not enforce
  `additionalProperties: false` on a rewritten MCP input. (2026-10-08)
- `$.http.fetch` reaches a loopback server from a role session; a mod hook's post took
  6.3 ms median and 26 ms p90 over 50 calls. The shim took 383 ms median, 488 ms p90,
  and 18.8 MB per call; the `bash -c "python3 ... || python ..."` form 766 ms median.
  The mod's worker runs inside the session's own process. (2026-10-08)
- `$.session.send({ to: { sessionId } })` from one `--bg` session wakes an idle `--bg`
  session: `isDelivered: true` in about 3 s, then the target takes a turn with the text
  inside `<cross-session-message ... from-name=...>`. The mod's own sends do not raise
  its `session.send` hook. (2026-10-08)
- A send by session id to an interactive session is delivered too. A session started
  with `CLAUDE_CODE_CHILD_SESSION=1` inherited from a parent Claude Code session never
  registers: it has no `~/.claude/sessions/<pid>.json`, `claude agents --json` does not
  list it, and a send by its id answers "no live session". A send made before a target
  registers, 10 to 14 s after it starts, answers the same. The id `claude agents --json`
  lists, the sessions file's, and `$.session.id()` are the same value. (2026-10-08)
- `$.clock.every` keeps firing in an idle `--bg` session for at least 18 minutes, and
  `$.prompt.submit` from a timer starts a turn there. (2026-10-08)
- `$.session.send` runs as a `SendMessage` tool call through the sending session's own
  `tool.call` hooks. The call's `to` is the target's `uds:\.\pipe\LOCAL\cc-msg-...`
  address, not the session id or name, and its `tool_use_id` starts with
  `toolu_plugin_`. A hook that refuses the call makes the send answer
  `isDelivered: false` with the refusal as `reason`. (2026-10-08)
- sentinel-swarm's mod against a real ledger server, Haiku `--bg --agent` sessions
  from `--plugin-dir`, launched with the inherited `CLAUDE*` variables cleared: the
  mod's `pre_send_message` denied a `SendMessage` to a name outside the run;
  `session_start` context reached each session; 0.7 s after a Lead's `message_post`,
  the mod sent the owed wake-up to the Manager by session id and the ledger marked it
  sent; the Lead made no `SendMessage` call and its Stop did not block; the Manager
  woke with the pointer and the elapsed time, its `session.receive` hook appended the
  unread message after `message_inbox() returned:`, and the ledger marked it read.
  (2026-10-08)
- A plugin whose `dependencies` entry is not installed is not loaded, mod included: the
  debug log says `Plugin not available for MCP: <plugin>@inline - error type:
  dependency-unsatisfied`. (2026-10-08)
- `claude --bg` ignores `--session-id`: it prints "--bg manages the session id;
  ignoring --session-id". `claude --resume <id> --bg "<prompt>"` on a stopped
  background session started a session under a new session id on 2.1.294. (2026-10-08)
- Claude Code 2.1.295 refuses to load a plugin whose `dependencies` entry is not
  installed for the project. `claude plugin list` shows "✘ failed to load — Dependency
  <id> is not installed", and the plugin's mod and skills do not run. (2026-10-08)
