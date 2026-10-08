# Messaging and tooling: fewer mechanisms, mods, MCP or CLI

Status: design accepted by Alex on 2026-10-08 (see "Decisions"), not built. Written 2026-10-08 on branch
`feat/kg-start-end-cli`, against Claude Code 2.1.294. Nothing here is verified live unless a line
says so. Step 0 below must pass before any build step starts.

Sources:

- PRD: `knowledge/prd/05-sessions.md` ("Wake-ups owed", "Wake-up delivery", "Messages"),
  `07-hooks-and-enforcement.md`, `13-platform-facts.md`.
- `plans/upgrade-roadmap.md`: step 1 results (hook shim cost, tool-call latency, `select:` bytes),
  step 2 decisions table, step 3, "Context savings".
- Code: `mcp/src/swarm_ledger/wake.py`, `bridge.py`, `ledger.py`, `hooks/events.py`, `launch.py`,
  `sessions.py`, `settings.py`, `watchdog.py`; `templates/agents/coder.md`.
- The `plugin-authoring` skill bundled with Claude Code 2.1.294: `SKILL.md`, `reference.md`, and
  `types/claude-code.d.ts` (the build's own declaration of the mod API). Cited below as "types" with
  the declaration name, and "reference" with the section name.
- ide-agent-tabs `main` (c45116c): `claude-plugin/hooks/hooks.json`, `claude-plugin/hooks/register.tsx`,
  `docs/design.md` ("Claude Code mod"), `CHANGELOG.md`. `plans/messaging-db.md` is no longer on
  `main`; it shipped as PR #12.
- Measured here: tool calls, hook calls and turn latency from the `hello` smoke run in
  `runs/hello/host/.sentinel-swarm/ledger.db` and its four transcripts (run 1, 2026-10-08,
  08:50:17 to 08:55:34).

## Questions

1. What each messaging mechanism does, which code owns it, and what only it can do. What wakes an
   idle `--bg` session, and is there another way?
2. What a Claude Code mod (a plugin of function hooks) can do for gates and messages.
3. Whether ide-agent-tabs' messaging helps `--bg` sessions.
4. The fewest mechanisms that keep every guarantee in PRD 05 and 07.
5. Whether mod hooks can replace the command-hook shim, and the time saved.
6. Which tools, if any, should move from MCP to a CLI, and what hooks can call instead of the model.

## What is true today

### The messaging mechanisms

| Mechanism | What it does | Code that owns it | What only it does |
|---|---|---|---|
| `SendMessage` (a Claude Code tool the model calls) | Puts a one-line pointer into another session by name. An idle `--bg` session wakes on it (13-platform-facts, 2026-09-23). | Ledger records the debt: `_owe_wakeup` (`ledger.py:1511`). `route_wakeup` (`wake.py:157`) writes the call into `next`. `handle_pre_send_message` (`hooks/events.py:316`) checks the target. `handle_post_any` (`hooks/events.py:456`) clears the debt through `wakeups_sent` (`ledger.py:1597`). `_member_stop` (`hooks/events.py:1022`) blocks while a debt is unpaid. | The only verified way to start a turn in an idle, running `--bg` session from another session. |
| `message_post` / `message_inbox` (ledger tools) | Stores a body of up to 32,000 characters in `ledger.db`, checks the recipient's relation, and owes the wake-up in the same transaction. The inbox takes unread rows of the caller's run, up to 40,000 characters, and marks them read in one `write_tx`. | `ledger.py:1716` (`message_post`), `ledger.py:1779` (`message_inbox`), `message_peers` (`ledger.py:1765`). | Bodies, the recipient check inside the ledger's transaction, exactly-once reads, run scoping, and the audit record. `SendMessage` text is not checked (PRD 05, "Messages") and is not stored by the ledger. |
| `swarm-events` channel | The ledger pushes a wake-up event into a session through `GET /events` and the stdio bridge; the bridge emits `notifications/claude/channel`. The ledger confirms the push from the target's transcript, and the sender's Stop hook waits up to 30 s for it. | `EventHub` and `route_wakeup` branch 3 (`wake.py:61`, `wake.py:176`), `transcript_confirms` and `await_confirmation` (`wake.py:241`, `wake.py:301`), `bridge.py`, `_owed_steps` (`hooks/events.py:1002`), `carries_channel` (`launch.py:52`), `wake_transport` (`settings.py:29`), `wake_unconfirmed` (`watchdog.py:563`). | A wake that costs the sender no turn. It reaches only an interactive Oracle that the launcher started with `--dangerously-load-development-channels`, after the user accepts a dialog. It cannot reach a `--bg` session (13-platform-facts, 2026-09-25 and 2026-09-26). The PRD says the transcript shape of a channel turn "is not verified live yet". |
| `agent_resume` (ledger tool) | Runs `claude --resume <id> --bg "<pointer>"` for a session that is not running. | `ledger.py` `agent_resume`; `sessions.py:153`. | Wakes a stopped session. Refuses a running one, because that starts a second copy. |

### What wakes an idle `--bg` session

- A cross-session `SendMessage`. Verified 2026-09-23.
- A line printed by a `Monitor` command running in that session. Verified 2026-09-24. Today only the
  Oracle's watchdog listener uses it, and `pre_monitor` allows no other `Monitor` call.
- Not a channel: Claude Code drops `--dangerously-load-development-channels` in a `--bg` session, and
  a Pro or Max user cannot add a plugin to the channel allowlist. Verified 2026-09-25 and 2026-09-26.
- Not `claude --resume` on a running session: it starts a copy. Verified 2026-09-28.
- Possibly a mod in that session, through `$.prompt.submit`. Not verified in a `--bg` session; see
  finding 2.

A stopped session needs `agent_resume`. Nothing else in the CLI sends a message to a session
(13-platform-facts, 2026-09-23).

### Hook cost today

- Every hook is a command: `python3 .sentinel-swarm/hook.py hook <event> || python ...`. Each call
  starts one process: 235 ms median, 324 ms p90, 16.7 MB peak working set, over 10 `pre_write` calls
  (roadmap step 1).
- The server's own part, `POST /hook/<event>`, is 42 ms median at 1 agent and 52 ms at 16 agents
  (roadmap step 1). The rest, about 185 ms, is process start.
- The Coder's template (`templates/agents/coder.md`) runs a blocking hook on every ledger, write,
  shell, `Monitor` and `SendMessage` call, a second blocking `post_any` after `SendMessage`, writes and
  `Monitor`, and an `async` `post_activity` process after every other tool. Each turn end runs `stop`.

### One measured run: `hello`

| Agent | Tool calls | Ledger | `SendMessage` | `message_inbox` | Start calls (`brief_get`, `brief_ack`, `guidelines_get`, `run_status`) |
|---|---|---|---|---|---|
| Oracle | 28 | 22 | 0 | 1 | 0 |
| Manager | 23 | 18 | 1 | 2 | 4 |
| Lead | 17 | 13 | 1 | 1 | 4 |
| Coder | 11 | 7 | 1 | 0 | 3 |
| Total | 79 | 60 | 3 | 4 | 11 |

- Blocking hook calls: 72 on tool calls (60 `pre_ledger`, 3 `pre_send_message`, 6 `post_any`,
  2 `pre_write`, 1 `pre_monitor`), plus about 16 `SessionStart`, `Stop` and `SessionEnd` calls:
  about 88 in all. Async `post_activity` calls: 73.
- The model took 2.3 s median and 5.5 s p90 from a tool result to its next message. The three
  `SendMessage` calls came 1.5 to 2.0 s after the result before them.
- The run owed 4 wake-ups. None was pushed: this Oracle had no channel.

## Findings

### 1. The three mechanisms do different jobs; only the channel is redundant

- The ledger message tables are the record. Nothing else gives bodies, the recipient check in the
  same transaction, exactly-once reads, and run scoping. They stay. The roadmap's step 2 table
  reached the same answer.
- A wake transport is the floor. Without mods, `SendMessage` called by the sender's model is the only
  verified wake for a running `--bg` session that does not need a process inside that session. So
  `SendMessage` is the floor today, and it costs the sender one tool call per wake-up.
- The channel only saves that one call, and only for wake-ups whose target is an interactive Oracle.
  To do it, it carries a bridge process, the `/events` stream, a dialog at every interactive launch,
  transcript confirmation of an unverified record shape, a 30 s wait in the sender's Stop hook, a
  `channel` column with three states, a setting, and a watchdog finding. In `hello` it would have
  saved 0 calls; in any run it saves at most one call per Oracle wake-up.
- The three can become one mechanism with two parts: the ledger's `wakeups` and `messages` rows as the
  single outbox, and one delivery path for every target. Today that path is the model's
  `SendMessage`. With a verified mod, it is the sender's mod, with `SendMessage` as the fallback.
- A `Monitor` per role session would also wake `--bg` sessions with no sender turn. It is not
  recommended: one more process per session, the session's status stays `busy` while it waits
  (13-platform-facts, 2026-09-25), Windows leaves a stopped `Monitor`'s children running
  (2026-09-24), and every printed line enters the session's context.

### 2. What a mod can do

A mod is a plugin whose `hooks/hooks.json` names a TypeScript module under `modules`. The module
registers function hooks `($, e, next)` that run in Claude Code's own process, in a worker, with no
process per call (reference, "What a plugin of function hooks is").

| Question | Answer | Evidence | Status |
|---|---|---|---|
| Deny a tool call | Yes. A `tool.call` hook returns `{ deny }`; a `classic.PreToolUse` hook returns `deny` with an optional `updatedInput` and `additionalContext`. | types: `EngineEventOf['tool.call']`, `PreToolUseResult` | Confirmed from docs |
| Rewrite a tool's input | Yes: `next({ ...e, ... })`, or `updatedInput`. Both are "validated against the tool's schema". | types: `ToolCallInput`, `PreToolUseResult.updatedInput` | Docs; unverified whether an `agent_id` the ledger schemas do not list passes that check |
| Block a stop | Yes. `classic.Stop` returns `block: "<reason>"`, the same as `decision: "block"`. | types: `ClassicResult.block`, `ClassicResultOf` | Confirmed from docs |
| Add context | Yes, on `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop` and others, through `additionalContext`. | types: `ClassicResultFields` | Confirmed from docs |
| See the classic hook input | Yes. A `classic.<Event>` hook gets the whole stdin input, `session_id`, `transcript_path` and `agent_type` included, except `classic.PreToolUse`, which gets only the tool envelope. | types: `ClassicEventOf` | Confirmed from docs |
| Fail closed | Only with a `.catch` handler. A hook that throws or runs past its 10 s budget is skipped, and the tool runs. `$` calls do not count against the budget. | reference, "Developing one"; types: `HookBudget` | Confirmed from docs |
| Call the ledger | Yes: `$.http.fetch` to `http://127.0.0.1:<port>`, or `$.mcp.call` to a server the session has. | types: `http.fetch`; ide-agent-tabs `docs/design.md`, "The mod's `$.mcp.call` binds to the same session" | `$.mcp.call` verified by ide-agent-tabs; `$.http.fetch` to loopback unverified |
| Send a message to another session | Yes. `$.session.send({ to: { sessionId }, text })` is the same dispatch as the `SendMessage` tool and addresses "one of the person's own sessions" by session id. It resolves `isDelivered`. | types: `SessionSendArgs`, `SessionSendAddress`, `SessionSendResult` | Docs; unverified that it reaches and wakes an idle `--bg` session |
| Gate `SendMessage` | Yes. A `session.send` hook sees every send, the model's and plugins', and refuses with `{ isDelivered: false, reason }`. | types: `EngineEventOf['session.send']` | Confirmed from docs |
| Change an incoming message | Yes. A `session.receive` hook rewrites `text` before it is queued, or consumes it. | types: `SessionReceiveInput` | Confirmed from docs |
| Wake its own idle session | Yes in an interactive tab: `$.prompt.submit` queues "a turn of its own, once the session is idle". ide-agent-tabs delivers mail this way every 2 s. | types: `prompt.submit`; reference, "Work that outlives a dispatch"; ide-agent-tabs `register.tsx` `deliver` and `poll` | Verified by ide-agent-tabs in IDE tabs; unverified in `--bg` |
| Wake another idle session directly | No. A mod acts only in the session that loaded it. It reaches another session only through `$.session.send`. | types: no such call | Confirmed from docs |
| Run in `--bg` sessions | Not stated. The docs name `-p` ("A headless `claude -p` always loads fresh") and SDK hosts. ide-agent-tabs ran a mod in a headless run. | reference, "Developing one"; ide-agent-tabs `docs/design.md`, "Inbound mail" | Unverified for `--bg` |
| Run in `--agent` sessions | Not stated for the module. Classic hook input carries `agent_type` "on the main thread of a session started with `--agent`". | types: `BaseHookInput.agent_type` | Unverified |
| Timers keep running while idle | `$.clock.every` runs "until cancelled or until the module reloads". | reference, "Work that outlives a dispatch" | Docs; unverified in an idle `--bg` session |
| Where it loads | Per plugin. A plugin installed at any scope loads its module; `--plugin-dir` and `CLAUDE_CODE_PLUGIN_DIRS` load one from disk. An agent file cannot name a module. | reference, "Developing one", "Sharing a mod" | Plugin-level confirmed from docs; agent-file loading unverified, assumed not |
| Can a plugin ship one | Yes. ide-agent-tabs 0.8.0 ships `modules: ["./register.tsx"]` beside its command hooks. | ide-agent-tabs `claude-plugin/hooks/hooks.json`, `CHANGELOG.md` | Verified, shipping |
| Loads in a host repo's role sessions | sentinel-swarm installs at project scope in the host repo (PRD 11). A module in its `hooks.json` would load in every session there whose folder is trusted, role sessions and the user's own alike. `--strict-mcp-config` does not stop plugin hooks (13-platform-facts, 2026-09-23). | PRD 11, "Install"; 13-platform-facts | Unverified for function hooks; trust handling unverified |
| API stability | "The API is early access and moves between releases." | reference, "The types are the reference" | Confirmed from docs |

Consequences:

- A mod can carry every gate the shim carries today, with the same server code behind it: the mod
  posts the same JSON to `POST /hook/<event>` and maps the answer to the function-hook result.
- A mod cannot wake another session by itself. It can deliver the sender's owed wake-up without a
  model turn, through `$.session.send` by session id. The ledger's `agent_id` is the session id, so
  the stale-name problem in 13-platform-facts (2026-09-23) goes away for these sends.
- A mod is plugin-wide, not per role. It must look up the caller's role in the ledger by session id,
  and use `agent_type` from `classic.SessionStart` for a session the ledger does not know yet. Every
  hook ignores an unknown caller, as today (PRD 07, "Rules every ledger hook follows").

### 3. ide-agent-tabs

- Its wake for a non-mod session types a line into a tab it hosts (roadmap step 2 table). It has
  nothing for a `--bg` session.
- Its Claude Code mod is the useful part. In a session it hosts, the mod polls its server every 2 s,
  claims unread mail with `take`, submits it as one prompt with `$.prompt.submit`, then sends `ack`,
  or `release` on failure (`register.tsx` `poll`, `deliver`; `docs/design.md`, "Inbound mail"). The
  claim makes delivery at least once and never strands a message.
- The mod limits mail delivery to sessions in its own tabs (`IDE_AGENT_TABS_ID`; "A session outside a
  tab only bridges `ListAgents` and `SendMessage`"). The limit is its own choice, not a platform
  limit. So the pattern may work in a sentinel `--bg` session, but nobody has run it there.
- Its `session.send` hook shows a mod routing `SendMessage` itself. Sentinel's version would gate it
  and record delivery instead.
- Its design doc verifies that a `--bg --strict-mcp-config` session reaches an HTTP MCP server. That
  is MCP, not the mod.

### 4. MCP or CLI for the swarm's tools

- Only the Coder and the Driver have a shell. `pre_shell` lets the Coder run only commands that start
  with the profile's test, build or lint command or read-only git, and the Driver only
  `build_command`. Any `;`, `&`, `|`, `<`, `>`, backtick, newline or `$(` is denied (PRD 07). The
  Oracle, Managers and Leads have no shell, and giving them one would open the write rule that
  `pre_shell` closes. A CLI cannot serve them.
- Identity: `pre_ledger` stamps `agent_id` on each MCP call through `updatedInput`. A CLI call has no
  stamp. The Coder writes its own command line, so any id or token it passes is its own choice. Step 5
  of the roadmap gives the ledger one bearer token per host repo, in a file the Coder can read; that
  does not tell agents apart.
- Turns: an MCP call and a CLI call are each one tool call. A CLI saves no turn.
- Bytes: with tool search, an unloaded MCP tool costs its name in the deferred list. A loaded one
  costs its schema once: the 67 ledger tools average 575 B (38,542 B in all, roadmap step 1). A CLI
  needs its usage text in the template or a skill, which costs bytes in every session whether the
  tool is used or not, plus the command line and plain-text output on each call.
- Time: a ledger MCP call costs one blocking hook (`pre_ledger`, 235 ms) plus 15 ms of server time at
  1 agent. A Coder's CLI call costs `pre_shell` and `post_shell` (two blocking hooks, and `post_shell`
  runs `git status`), the CLI's own process start, about 235 ms like the shim, and the server time:
  about 0.7 s.
- codebase-kg moved `kg_stats`, `kg_validate` and `kg_parity_gaps` to `kg_cli.py` because no role
  calls them (a7727cb). Tool search already makes an unused MCP tool nearly free, so the move saved
  bytes only for sessions that listed those tools up front.
- Hooks already run code, know the session id, and post to the ledger. They can make calls that need
  no judgment and save the model's tool calls. Roadmap step 3 does this for the start calls. The same
  applies to the owed wake-up and to reading the inbox on a wake; see steps 3 and 4 below.

Per-role recommendation:

| Role | Shell | Move to a CLI | Hook or mod does instead | Up-front `select:` bytes saved | Tool calls saved per session |
|---|---|---|---|---|---|
| Oracle | No | None | Inbox on each wake that carries mail (step 4) | 0 | About 1 per wake with mail (1 in `hello`) |
| Manager | No | None | Start calls (roadmap step 3); owed wake-ups (step 3 here); inbox on wake (step 4) | 2,232 B (roadmap step 3) | 5 start + 1 per owed wake-up + 1 per wake with mail (`hello`: 4 + 1 + 2) |
| Lead | No | None | Same as the Manager | 2,232 B | Same (`hello`: 4 + 1 + 1) |
| Coder | Yes, gated | None | Start calls; owed wake-ups; end tools deferred (roadmap step 4) | 1,705 B; 4,062 B more until handoff | 4 start + 1 per owed wake-up (`hello`: 3 + 1) |
| Driver | Build command only | None | Start calls; owed wake-ups to the Oracle | 1,705 B; 973 B until `drive_done` | 4 start + 1 per owed wake-up |
| codebase-kg tools, every role | | None. Keep `kg_search` and the other read tools on MCP over the shared HTTP server (roadmap step 0); keep `kg_stats`, `kg_validate` and `kg_parity_gaps` out of the templates | | `kg_search` stays about 900 B | 0 |

In `hello`, these remove 18 of 79 tool calls (23%): 11 start calls, 3 `SendMessage` calls and 4
`message_inbox` calls. The start calls are roadmap step 3; this plan adds the other 7. At the measured
2.3 s median per tool turn, the 7 calls are about 16 s of model time, and each also re-sends the
session's cached context.

## Recommended design

The floor is two mechanisms: the ledger's `wakeups` and `messages` tables, and one delivery path.
Remove the `swarm-events` channel now (step 1). The sender's mod delivers every owed wake-up with
`$.session.send`, which replaces `SendMessage`. `SendMessage` stays only until step 0 verifies
`$.session.send` wakes an idle `--bg` session; if step 0 fails, `SendMessage` stays as the floor.
`agent_resume` stays for a stopped target.

Each step ships alone, updates the PRD documents it names in the same change, and adds a dated line
to `14-key-decisions.md` for each decision Alex makes. Every step runs the repo's checks before commit:
`uv run pytest`, `uv run pyright`, `uv run ruff check` and `uv run ruff format --check` in `mcp/`, then
`claude plugin validate --strict .` and `claude plugin validate --strict .claude-plugin/plugin.json`.

### Step 0. Verify mods in role sessions, live

Write a throwaway test mod (outside this repo) that logs each check to a file, and run it in a scratch
host repo. Record each result in `13-platform-facts.md` with the date.

1. The module loads in a `claude --bg --agent swarm-coder --strict-mcp-config` session started the
   way `agent_spawn` starts one, in a trusted folder, from a plugin installed at project scope. Check
   that it does not load in an untrusted folder.
2. `session.start` fires there, and `classic.SessionStart` carries `agent_type: swarm-coder`.
3. A `tool.call` hook's `{ deny }` and a `.catch` deny stop a `Write`; `classic.Stop` with `block`
   re-prompts the session; `classic.PreToolUse` `updatedInput` that adds `agent_id` reaches a ledger
   MCP tool whose schema does not list it.
4. `$.http.fetch` reaches `http://127.0.0.1:<port>/health` on the ledger.
5. `$.session.send({ to: { sessionId }, text })` from one `--bg` session wakes a second, idle
   `--bg` session, and the text appears in its transcript. Repeat with an idle interactive session
   as the target.
6. `$.clock.every` keeps firing in an idle `--bg` session for 10 minutes, and `$.prompt.submit` from
   that timer starts a turn there.
7. The time per hook: 50 calls of a `tool.call` hook that posts to `/hook/pre_write`, against the
   same 50 through `hook.py`. Record median, p90 and the worker's memory.

Tests: none in the repo; the record in `13-platform-facts.md` is the output.

Gate: steps 2 to 5 need checks 1 to 5 and 7. Step 4's recipient-side form needs check 6. Step 1
needs none of them.

### Step 1. Remove the `swarm-events` channel

It saves at most one sender call per Oracle wake-up, only for an interactive Oracle, and was never run
end to end.

1. Delete `bridge.py`, `EventHub`, `event_stream`, `/events`, `wakeup_event`, `transcript_confirms`,
   `confirm`, `confirm_pushed` and `await_confirmation` from `wake.py`, the `pushed` branch of
   `route_wakeup`, and the `hook.py channel` command.
2. Delete the `wake_transport` setting, `carries_channel`, the launcher's development-channel dialog
   text, and `CHANNEL_ENTRY` from `dev_channel_args`. Keep `CLAUDE_DEV_CHANNELS` pass-through for
   Alex's own channels.
3. `_owed_steps` stops waiting; the member Stop hook names the `SendMessage` or `agent_resume` call at
   once.
4. Drop `wakeups.pushed_at` and `agents.channel` from new databases; `connect` ignores them in an
   older `ledger.db`.
5. Delete the watchdog's `wake_unconfirmed` finding.
6. PRD: 05 drops "Wake-up delivery" except the `route_wakeup` order (setting, not running,
   `SendMessage`); 06 drops `/events`; 07 drops the shim's `channel` command; 08 drops
   `wake_unconfirmed`; 11 drops `wake_transport` and the dialog; 13 keeps the channel facts; 16 moves the device-queue broker item to "needs a delivery path".

Tests: `route_wakeup` returns `SendMessage` for a live target, `agent_resume` for a stopped one, and
both for unknown liveness; a member Stop with an owed wake-up blocks at once with no wait; an older
`ledger.db` with `pushed_at` and `channel` opens; `test_plugin_surface.py` finds no `swarm-events`
entry in any launch.

Risk: an interactive Oracle's wake-ups cost the sender one call again, until step 3.

### Step 2. A mod as the hook transport

The mod replaces the shim's process, not the server's logic.

1. Add `hooks/register.ts` and `"modules": ["./register.ts"]` to `hooks/hooks.json`, with
   `"hooks": {}` kept empty.
2. At `session.start` and `classic.SessionStart`, the mod caches the session id, `transcript_path`
   and `agent_type`, reads the ledger port from `.sentinel-swarm/server.json` with `$.fs.read`, and
   checks that the URL is `http://127.0.0.1:<integer>`.
3. Each event the role templates hook today maps to one function hook that posts the classic input to
   `POST /hook/<event>` with the `X-Sentinel-Swarm-Repo` header, and maps the reply:
   `permissionDecision: deny` to `{ deny }` on `tool.call`, `updatedInput` to `next({ ...e, ... })`
   or `classic.PreToolUse` `updatedInput`, `decision: block` to `classic.Stop` `block`,
   `additionalContext` to the event's `additionalContext`. `post_activity` runs without awaiting the
   reply, as `async: true` does today.
4. Each gating hook (`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`, `pre_send_message`,
   `pre_skill`, `pre_ledger`) carries `.catch(($, e, next) => next.called ? next(e) : { deny })`,
   with the same reason and `/sentinel-swarm:setup` hint the shim gives. That keeps today's
   fail-closed rule.
5. The role mapping moves from the agent file to the ledger: the server already resolves the caller
   by session id and ignores unknown callers. `pre_monitor`'s `agent_type` check reads the cached
   `agent_type`. The Driver-only `pre_skill` checks the caller's role on the server.
6. Setup writes the role templates without command hooks when the host's Claude Code passes step 0,
   and with them otherwise. A setting `hook_transport: mod | command`, default `mod`, lets the user
   choose. The two never run together for one session, so no hook runs twice.
7. PRD and repo rules: `.claude/CLAUDE.md` ("Every enforcement rule lives in a role template's hooks
   or in a swarm-ledger tool") and PRD 07 ("Where the hooks live", "The shim") gain the mod as the
   default transport. This is Alex's decision; record it in 14.

Tests: `claude plugin test` with `*.test.ts` files: each gating hook denies when the server denies,
allows when it allows, and denies when the server does not answer; the Stop block and
`additionalContext` pass through; a session the ledger does not know passes every hook but
`pre_write`'s records-folder rule, `pre_ledger` and `pre_monitor`. In `mcp/tests/`:
`test_plugin_surface.py` checks `hooks.json` names one module and no command hooks, and that a template
written for `hook_transport: mod` has no `hooks` key. A `hello` smoke run with the mod reports the same
gate events in the ledger as one with the shim.

### Step 3. The sender's mod pays owed wake-ups

1. After any ledger tool returns, the mod's `tool.call` hook posts `POST /hook/owed` with the session
   id. The ledger returns this session's unsent wake-ups whose target is live, each with the target's
   session id and the delivered text (the pointer plus the time signal for an Oracle or Manager).
2. The mod sends each with `$.session.send({ to: { sessionId }, text })`, then posts the ids that
   returned `isDelivered: true` to `POST /hook/wake_sent`. The ledger marks those sent and releases a
   closed Driver, as `post_any` does today.
3. A wake-up whose send failed, or whose target is not running, stays owed. The ledger's `next` and
   the member Stop hook still name `SendMessage` or `agent_resume`, so the model is the fallback.
4. The mod's `session.send` hook applies `pre_send_message`'s relation check to every send, the
   model's and its own, and refuses with `{ isDelivered: false, reason }`. It records a model's
   delivered send the way `post_any` does, but only when `isDelivered` is true; today `post_any`
   clears the debt whatever the result was.
5. `next` reads "Nothing to send: <name> was woken." for a wake-up the mod delivered.
6. PRD 05 ("Wake-ups owed") and 07 (`post_any`, `pre_send_message`, the rule map row "A child wakes
   its parent after each step").

Tests: server tests for `/hook/owed` (only the caller's unsent rows, only live targets, the time
signal only for Oracle and Manager targets) and `/hook/wake_sent` (marks only the caller's rows,
releases a closed Driver); mod tests where a failed send leaves the row owed and the Stop hook blocks;
a `hello` smoke run with 0 `SendMessage` tool calls and 0 owed wake-ups at the end.

### Step 4. Read the inbox inside the wake

Two forms. Build the first; the second needs step 0 check 6.

1. Recipient side, `session.receive`: when a delivery carries a sentinel wake pointer, the mod posts
   `POST /hook/inbox_take` with the session id. The ledger claims the caller's unread messages up to
   the 40,000-character cap with a claim id, as ide-agent-tabs' `take` does. The mod appends the bodies
   to the delivered text with `next({ ...e, text })`, then posts `inbox_ack`, or `inbox_release` when
   `next` failed. A claim nobody settles returns to unread after 2 minutes.
2. Recipient side, timer: the mod polls the ledger every 2 s while idle and submits waiting mail with
   `$.prompt.submit`. This would wake a `--bg` session with no sender action at all, which would
   make step 3 unnecessary. Build it only if check 6 passes and Alex prefers it.

`message_inbox` stays as a tool for `remaining` above 0 and for the fallback path. Exactly-once holds:
the ledger marks rows read only on `ack`, inside one `write_tx`.

Tests: claim, ack and release in the ledger; an unsettled claim returns to unread after 2 minutes;
`message_inbox` skips claimed rows; a wake with two messages delivers both bodies and leaves 0 unread;
a run-2 claim never takes a run-1 row.

### Step 5. The ledger resumes a stopped target itself (optional)

The ledger already starts sessions (`agent_spawn`, `agent_resume`). When `_owe_wakeup` finds the target
not running, by the same `claude agents --json` check `agent_resume` uses, it can resume it at once
with the pointer and mark the wake-up sent. That removes the model's `agent_resume` call. It does not
act when liveness is unknown, because a resume of a running session starts a second copy.

Tests: a stopped target is resumed once and its row marked sent; unknown liveness leaves the row
owed and `next` names both calls.

### Step 6. Tooling

- Move no ledger tool to a CLI, for any role.
- Keep codebase-kg's read tools on MCP for every role.
- Build roadmap steps 3 and 4 as planned; steps 3 and 4 here add the wake-up and inbox calls.

Tests: as in the roadmap and in steps 3 and 4 above.

## Expected savings

Per blocking hook call, the mod saves the process start: about 235 ms minus the server's 42 to 52 ms,
so about 185 ms at the median. The mod's own overhead is not measured (step 0 check 7). Each avoided
process also avoids a 16.7 MB peak.

| Run | Blocking hook calls | Summed wait saved | Async processes avoided | Tool calls saved (steps 3, 4) |
|---|---|---|---|---|
| `hello` (4 agents, measured counts) | About 88 | About 16 s | 73 | 7 (3 `SendMessage`, 4 `message_inbox`) |
| 10 agents: 1 Oracle, 1 Manager, 2 Leads, 6 Coders (scaled from `hello` per role) | About 170 | About 31 s | About 140 | About 14: 9 owed wake-ups, about 5 inbox reads |

- The wait is summed across sessions. Sessions run in parallel in a larger run, so the run's wall time
  falls by less. In `hello`, which runs mostly in sequence, 16 s is about 5% of the 317 s run.
- The 10-agent row is a lower bound for Coders: a `textstats` Coder runs `tests_run` and writes more
  often than the `hello` Coder.
- Tool calls saved are worth more than hook time: 7 calls at the measured 2.3 s median is about 16 s
  of model time in `hello`, plus the cached context each call re-sends.

## Risks

- The mod API is early access and changes between releases. Pin a minimum Claude Code version in
  setup, and keep `hook_transport: command` working.
- A mod guard without `.catch` fails open. Step 2's tests check every gate's `.catch`.
- A plugin-wide module runs in the user's own sessions in the host repo too. Every handler must return
  `next(e)` at once for a caller the ledger does not know.
- The user can edit an agent file's hooks today (PRD 07). A mod is plugin code; the user can only
  disable the plugin, which also stops the ledger's setup. The ledger tools' gates still apply.
- `$.session.send` reports queued, not read. That matches `SendMessage` today.
- Removing the channel before step 3 costs an interactive Oracle's senders one call per wake-up.
- A `.catch` grace of 1 s and a 10 s budget: a slow ledger under load (p90 1.9 s at 16 agents for
  `message_post`) stays inside the budget, because `$` calls do not count against it.

## Decisions

Alex decided on 2026-10-08:

1. Enforcement may live in the plugin's mod, which loads in every session in the host repo, once
   step 0 verifies it in role sessions. `.claude/CLAUDE.md` and PRD 07 record the rule.
2. Remove the `swarm-events` channel now, before mods deliver wake-ups.
3. The sender's mod delivers each owed wake-up with `$.session.send`, which replaces `SendMessage`.
4. The device-queue broker was planned on the channel. It needs a new plan on the mod's delivery
   path; PRD 05 and 16 mark it **(needs implementation)** until that plan exists.

## Open questions

1. Should step 5's automatic resume apply to the Oracle, whose session the user may have closed on
   purpose?

## Unverified

- Every mod behavior in a `--bg` or `--agent` session: loading, `session.start`, classic events,
  timers, `$.prompt.submit`, `$.http.fetch` to loopback (step 0 checks 1 to 6).
- `$.session.send` by session id waking an idle `--bg` or interactive session (check 5).
- Whether a rewrite that adds `agent_id` passes the ledger tools' schema check through a mod
  (check 3).
- Whether a module loads from an agent file. The docs name only plugins, `--plugin-dir` and
  `CLAUDE_CODE_PLUGIN_DIRS`; this plan assumes it does not.
- The mod's per-call time and memory (check 7). The savings table assumes it equals the server's hook
  round trip.
- The 10-agent figures are scaled from one `hello` run, not measured.
- The channel's transcript record shape, which step 1 makes moot.
