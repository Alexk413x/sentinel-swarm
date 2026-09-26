# Plan: Channels and HTTP MCP servers

Status: the HTTP half is built (2026-09-25) and described in `knowledge/prd/09-mcp-servers-and-code-graph.md` under "Shared HTTP servers", and in `06-ledger-server.md`. Channels are not built: the 2026-09-25 probes show a channel cannot deliver to a `--bg` session for a plugin outside the allowlist. See "Probe results". Everything under "Proposed design" is **(proposed)**. Alex asked for two changes: MCP servers over HTTP instead of stdio, and a push-based, event-driven ledger that every agent registers with, which could replace the watchdog's Monitor watcher.

## What the docs say

Sources: https://code.claude.com/docs/en/channels, https://code.claude.com/docs/en/channels-reference, https://code.claude.com/docs/en/mcp.

### Channels
- A channel is an MCP server that pushes events into a session. The server declares the capability `experimental: {"claude/channel": {}}` and sends `notifications/claude/channel`. The event arrives in the model's context as a `channel` tag with its source.
- A session opts in at launch: `--channels plugin:<name>@<marketplace>`. On Team and Enterprise plans an admin must allow channels with `channelsEnabled`. Channels are a research preview.
- A channel can be two-way. It exposes tools, such as a reply tool, that the session calls. It can also relay permission prompts, through the capability `claude/channel/permission`.
- Events that arrive while the session is busy queue, and they are delivered together on its next turn.
- A channel connects over stdio: it is one server process per session. The docs describe no way for one server to push to many sessions or address one.
- Channels do not run in `-p` mode.

### HTTP servers
- `.mcp.json` and an agent's MCP config accept `{"type": "http", "url": "..."}`, with `${VAR}` and `${VAR:-default}` expansion.
- A server has no documented way to tell which session is calling, except headers the caller sets.
- The ledger already works this way: one HTTP server per host repo, and every session connects by URL.

## Unknowns to probe live

1. **Does a channel event wake an idle background session?** Start `claude --bg` with a test channel, let the session go idle, push one event, and check whether a new turn starts. Everything below depends on this.
2. **Does `--channels` work with `claude --bg --agent`, and with a plugin installed at project scope?** Also, is there a development flag for a channel that is not in a marketplace?
3. **Can codebase-kg serve over HTTP, and share one server between sessions?** Answered for HTTP on 2026-09-25: the shim's `mcp-http` wrapper serves codebase-kg, a11y-tools, and a11y-kg over HTTP with no plugin change. Sharing between live sessions is still unprobed. Earlier note: its entry point is a FastMCP server that calls `mcp.run()` with the default stdio transport, and it has no transport option. FastMCP supports HTTP, so either the shim runs `codebase_kg.server.mcp.run(transport="http", ...)` inside the plugin's own environment, with no codebase-kg change, or codebase-kg gains a `--transport` flag. It still needs a probe that two sessions can share one instance, including `kg_upsert_node` writes under the ledger's graph lock.
4. **What does one channel bridge cost per session** in memory and startup time, compared with the processes it replaces?

## Probe results (2026-09-25)

Probed with a standard-library stdio channel server that declares `claude/channel` and pushes one `notifications/claude/channel` event per file dropped in a folder. Every session ran in the trusted `runs/hello/host` with `--bg --model haiku --permission-mode acceptEdits --strict-mcp-config`, and a `--debug-file`.

1. **Does a channel event wake an idle background session?** No channel event reaches a `--bg` session built on this plugin, idle or busy.
   - `--dangerously-load-development-channels server:probe`: the server connects and its tools and instructions load, but the debug log reads `Channel notifications skipped: server probe not in --channels list for this session`. The flag does not carry into a `--bg` session. An event pushed while the session was idle never arrived, and a turn started later by `SendMessage` did not receive it either, so it was dropped, not queued.
   - `--channels server:probe`: the flag reaches the session, and the log reads `server probe is not on the approved channels allowlist (use --dangerously-load-development-channels for local dev)`.
   - The allowlist is Anthropic's list, or an organization's `allowedChannelPlugins` in managed settings on Team and Enterprise plans. A Pro or Max user has no setting that adds a plugin to it.
2. **`--channels` with `--bg` and a project-scope plugin.** A project-scope plugin connects as a normal MCP server. It registers as a channel only if it is on the allowlist. The prompt must come before either channel flag, or the flag takes the prompt as another channel entry.
3. **Can the channel server tell which session it serves?** Yes. Its environment carries `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, and `CLAUDE_CODE_SESSION_ATTENDED`.
4. **Prompts and cost.** A default-permission `--bg` session stalls for good on a file write; `acceptEdits` clears it. `--bg` refuses an untrusted folder with `Workspace not trusted`, which does not affect a real run, since setup runs in a trusted host. The stdlib server finishes its handshake 10 to 40 ms after start.
5. **Events pushed while busy.** Not reachable: no event was delivered at all.

**Recommendation (proposed): do not build Channels now.** A swarm role is a `--bg` session, and sentinel-swarm is not on the channels allowlist, so a `swarm-events` channel would connect and never deliver. Keep `SendMessage`, the Oracle's `Monitor` listener, and `agent_resume`. Revisit when either change ships: `--dangerously-load-development-channels` works in `--bg` sessions, or a user-level setting can allowlist a plugin. The retest is the probe above: check the debug log for `Channel notifications skipped`.

## Proposed design **(proposed)**

### HTTP: one shared server per repo

Built on 2026-09-25. What remains:
- Probe a live run: two or more swarm sessions share one codebase-kg over HTTP, and a
  resumed session reaches it at its saved port. The tests start the servers and call
  `kg_stats`, but no real `claude` session has connected to a shared server yet.
- Measure memory and process counts in a live run. The per-session count drops from 4
  (codebase-kg) or 14 (with both a11y servers in the `uvx` form) MCP server processes
  to 0.
- Built on 2026-09-25 **(proposed)**: the ledger restarts a shared server that crashes,
  on the same port, and runs each server stateless so that a restarted server accepts
  a client from before the restart. After 3 restarts in 5 minutes it gives up, removes
  the URL from `server.json`, and files a watchdog directive. See "Restarts" in
  `knowledge/prd/09-mcp-servers-and-code-graph.md`. Still unprobed: whether a live
  Claude Code session keeps using a restarted server, or marks it failed.
- codebase-kg 0.8.0 runs its own `kg-shim` relay to one server per machine, which
  `mcp-http` cannot wrap. Decide whether the ledger still shares codebase-kg.
- Run the smoke test on macOS to check the POSIX process-group and owner-watch paths
  live.
- Run codebase-kg, and a11y when the host has it, as one HTTP server per host repo, as the ledger runs. Sessions list them by URL in `--mcp-config`.
- This saves about 5 processes per session. On 2026-09-25 each session ran 15 to 20 MCP-related processes, and a run keeps up to 7 sessions alive.
- The ledger server, or a small supervisor next to it, starts and stops them, with the same lifetime as the ledger: they exit after `run_finish` or after the idle timeout.
- Decided by Alex on 2026-09-25: build the HTTP change first, then plan Channels.
- codebase-kg, `a11y-tools`, and `a11y-kg` are all FastMCP servers whose `main()` calls `mcp.run()`. One generic wrapper serves any of them over HTTP without changing the plugins. It runs in the plugin's own environment, resolves the server's console script, for example `codebase-kg` → `codebase_kg.server:main`, and replaces `mcp.run` with a call that passes `transport="http"`, `host="127.0.0.1"`, and a port. Then it calls the server's own `main()`, so the server's argument handling, such as a11y-kg's graph path, still works.
- The ledger server starts each shared server when it starts, records its URL in `server.json`, and stops the whole process tree when it exits. `agent_spawn` and the launcher list each server by URL, and fall back to the stdio shim when a shared server did not start.
- Swarm roles use only codebase-kg's read tools, and graph writes go through the ledger's `graph_upsert` command, so sessions sharing one server is safe.

### Channels: a per-session bridge to the ledger
- Each swarm session starts with one channel, `swarm-events`, run through the shim. It holds one connection to the ledger server and subscribes as its agent.
- The ledger pushes that agent's events: a wake-up owed to it, a new message, a returned handoff, a pushback, a watchdog finding for the Oracle, and a directive.
- This would replace:
  - the Oracle's `Monitor` watcher and its heartbeat rules, since findings arrive as channel events;
  - most `SendMessage` wake-ups, since the ledger pushes directly and the Stop hook's owed-wake-up rule becomes a delivery the ledger does itself;
  - `SendMessage` session-name addressing, and the risk of a guessed name, which is item 13 in `plans/unbuilt-features.md`.
- It would keep `agent_resume` for a session that has stopped, because a channel reaches only a running session.
- It would keep the watchdog's detection, which runs in the ledger server. Only its delivery changes.
- The reply tool is not needed: agents answer through ledger tools, as today.

### Rollout, if the probes pass
1. Build a minimal channel server and run probes 1 and 2.
2. Move the Oracle's watchdog delivery to the channel, and remove the `Monitor` requirement.
3. Move wake-ups owed to channel delivery. Keep `SendMessage` as a fallback while both paths are proven.
4. Move a11y to shared HTTP servers. Built on 2026-09-25. codebase-kg shares itself from 0.8.0 through its own relay, so the ledger does not share it.
5. Update `knowledge/prd/` for each step.

## Questions for Alex

1. Channels are a research preview and may change. Is that acceptable for the swarm's core wake path, with `SendMessage` kept as a fallback?
2. If an admin setting ever blocks channels, should the swarm fall back to today's Monitor and `SendMessage` path automatically?
3. HTTP first, or Channels first? HTTP saves memory without changing behavior. Channels change how every role wakes.
