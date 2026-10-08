# Upgrade roadmap: efficiency, messaging, other platforms

Status: part 1 is a build plan; step 0 is done, the rest is not started. Part 2 is an exploration
**(proposed)**, not a build plan. Written 2026-10-07 on branch `feat/kg-start-end-cli`. It replaces
the root `cli-plan.md`.

Sources: codebase-kg branch `feat/push-refresh` (PR #32, not merged): `http-server-plan.md`,
`docs/DESIGN.md`, `CHANGELOG.md` 0.12.0 and 0.13.0. ide-agent-tabs: `plans/messaging-db.md` and
`plans/shared-server.md`.

## Order

1. **Part 1: upgrade sentinel-swarm inside Claude Code.** Fix the messaging gaps the ide-agent-tabs
   mailbox design exposes, then the remaining efficiency steps. Each step ships alone.
2. **Part 2: explore other platforms (proposed).** Use ide-agent-tabs to bring in agents such as
   Codex, Gemini CLI, Copilot CLI and Antigravity, if Alex chooses to. Part 2 starts only after
   part 1.

When a step ships a design change, update the matching `knowledge/prd/` document in the same
change, per `.claude/CLAUDE.md`. Delete this plan once part 1 ships and part 2 is decided.

## What is true today

### Done on `feat/kg-start-end-cli`

| Commit | Change |
|---|---|
| a7727cb | Role templates no longer list `kg_stats`, `kg_validate` or `kg_parity_gaps`. `test_plugin_surface.py` guards it. |
| 98f42d9, 23718fe | **Step 0, done.** `session_options` (`mcp/src/swarm_ledger/agentfiles.py`, `http_entry`) writes a plugin's own HTTP `.mcp.json` entry into the role's `--mcp-config` in place of the `hook.py mcp` stdio relay. Options come only from the user's `~/.claude/settings.json` `pluginConfigs` or the manifest default. They must be plain tokens (integers for number options), cannot change the URL's host, and never expand inside `headersHelper`. Any entry that fails a check keeps the relay. Verified live: a `--strict-mcp-config` session with codebase-kg's `headersHelper` called `kg_search`. PRD 09 and `14-key-decisions.md` record it. |

### codebase-kg results this plan builds on

- One shared stdlib-only HTTP MCP server per machine. Port setting `server_port`, default 47821.
  The block 47821-47829 is assigned per plugin; sentinel's ledger takes no slot (see "Ports").
- Bearer token per user through `headersHelper`. A `SessionStart` hook starts the server. Verified
  build handover; an orphan server exits within 5 s. Elastic worker pool, `max_workers` default 8.
- Measured: idle 26 MB, start 0.18 s, 8 sessions 94 MB against 317 MB with per-session shims.
- 13 MCP tools. `kg_stats`, `kg_validate` and `kg_parity_gaps` run through
  `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_cli.py" query <tool>`. The six
  write tools stay on MCP: a refresh calls them in a loop, and the unattended push refresh has no Bash.
- Tool search is on by default. A session loads tool names only, and schemas load on `ToolSearch`.
  Each sentinel role runs one `ToolSearch select:` of its whole working set at start, so a schema
  removed from that set saves bytes in every role session of a run.
- Lessons:
  - A 403 puts a server in `~/.claude/mcp-needs-auth-cache.json`, and Claude Code stops connecting
    to it, for later sessions too. Never let a client reach the server without a valid token.
  - Claude Code keeps the headers it got at connect and sends them to a restarted server. A token
    must survive a server restart.
  - Never expand a repo-controlled value into a URL or a `headersHelper`.
- codebase-kg's README and `pyproject.toml` state Python 3.10 or later for the base-Python server.
  A live run on 3.9 is reported but not recorded in that repo.

### How sentinel messages today

| Mechanism | What it does | Where it is enforced |
|---|---|---|
| `SendMessage` | Wakes an idle role session with a one-line pointer. The only wake for `--bg` roles. | `pre_send_message` limits `to` to the run's session names. The ledger records the debt (`wakeups`), `post_any` clears it, and the member Stop hook blocks until it is paid. |
| `message_post` / `message_inbox` | Stores a message body in `ledger.db` and owes the recipient a wake-up | Server side: `message_post` refuses names outside the run |
| `swarm-events` channel | The ledger pushes a wake-up into an interactive Oracle through the `/events` stream and the `hook.py channel` bridge. Confirmed from the transcript. | Server side, plus the Stop hook's 30 s confirm wait |
| `agent_resume` | Wakes a stopped session with `claude --resume --bg` | Ledger tool |

The ledger already follows the SQLite rules of the ide-agent-tabs mailbox (`db.py`: WAL,
`synchronous=NORMAL`, `busy_timeout` 5000, `BEGIN IMMEDIATE` for writes). It is one process, so it
has none of the cross-process races that plan solves.

### Gaps found in the code

- `message_inbox` (`ledger.py:1431`) selects by `to_name` alone, not by `run_id`. An agent whose
  name repeats a name from an earlier run in the same repo reads and marks that run's unread
  messages. The Oracle's wake lines (`hooks/events.py:683`) filter by run, so the two disagree.
- The member Stop hook (`_member_stop`) blocks on owed wake-ups, not on the caller's own unread
  messages. An agent can stop with mail waiting. Only the Oracle's Stop hook names another agent's
  unread count.
- `message_post` sets no size limit on `body`, and `message_inbox` returns every unread row at once.

## Ports

The ledger keeps one HTTP server per host repo on a saved dynamic port (`.sentinel-swarm/server.port`).
It takes no slot in 47821-47829.

- Several repos can run swarms at once, each with its own ledger.
- No session reaches the ledger through a plugin `.mcp.json`. The launcher writes the URL into each
  role's `--mcp-config`.
- The saved port keeps the URL stable across a ledger restart.
- One machine-wide ledger would put every run in one failure domain, and tie the watchdog and idle
  exit to no single run.

## Part 1: steps

### Step 1. Measure today

Adapt codebase-kg's `mcp/bench/concurrency.py` and `cli_vs_mcp.py` into
`scripts/bench/ledger_load.py`. It starts one ledger on a scratch repo, registers N agents of a fake
run, and loops each agent over `message_post`, `message_inbox`, `issue_list`, `who_owns`,
`tests_run` (stubbed) and `POST /hook/pre_write`.

Record at 1, 4, 8 and 16 agents, with the machine's CPU load:

- ledger memory idle and under load; start time from spawn to `server.json`;
- median, p90 and slowest tool call and hook round trip;
- `message_post` to `message_inbox` latency;
- processes and memory for one role session's MCP side.

Also record each role's up-front `select:` bytes from the templates as committed (table below).

Done when: the numbers are in this file, and the script runs in under 5 minutes.

### Step 2. Messaging: take what the mailbox design does better

The ide-agent-tabs mailbox does not replace any sentinel mechanism. Each decision:

| Today | Decision | Why |
|---|---|---|
| `SendMessage` wake-ups for `--bg` roles | **Stays** | ide-agent-tabs wakes a session only by typing a line into an IDE or terminal tab it hosts (`messaging.ts`, `wake`). A `--bg` role has no tab, so its delivery would be `queued`, with no wake. |
| `message_post` / `message_inbox` in `ledger.db` | **Stays in the ledger** | The message, the recipient check and the owed wake-up commit in one transaction. A second database would split run state and move the recipient check out of the ledger's gates. |
| `swarm-events` channel | **Stays** | The ledger already holds an in-memory event stream per session. Wake files and a `data_version` poll serve processes that share only a database file. |
| Wake files, `fs.watch`, `data_version` poll | **Not adopted** | Same reason |
| Rate limit (20 a minute) and 60 s dedupe | **Not adopted** | Agents of one run are trusted peers. The watchdog and the escalation budget control a runaway agent. |
| Read before stopping (Stop hook nudge) | **Adopt** | Fixes the second gap above |
| Text cap and read batch cap | **Adopt** | Bounds the context one `message_inbox` call adds |
| Exactly-once take in one `BEGIN IMMEDIATE` | **Already true** | `message_inbox` selects and marks read in one `write_tx` |

Changes:

1. `message_inbox` filters by `run_id` and `to_name`. Add an index on
   `messages (run_id, to_name, read_at)`.
2. `_member_stop` blocks when the caller has unread messages in its run, and names
   `message_inbox`. It follows the existing rule: no block on a stop that follows its own block
   (`stop_hook_active`). The owed-wake-up block comes first when both apply.
3. `message_post` refuses a `body` over 32,000 characters. `message_inbox` returns at most
   40,000 characters of bodies, oldest first, and a `remaining` count; rows past the cap stay unread.
4. Update `05-sessions.md` ("Messages") and `07-hooks-and-enforcement.md`.

Tests:

- `message_inbox` in run 2 returns no row addressed to the same name in run 1.
- A member Stop with one unread message blocks once and names `message_inbox`; the next stop passes.
- A body of 32,001 characters is refused. Three bodies of 15,000 characters return two and
  `remaining: 1`, and the third stays unread.

Success: 0 cross-run reads, and 0 members stopped with unread mail in a `modules` smoke run.

### Step 3. Start-up calls in the `SessionStart` hook

The role's `SessionStart` hook already posts to the ledger, runs as code, and knows the session id,
which is the role's `agent_id`. Roles have no Bash, so a CLI would not help them. Moving the start
calls into the hook saves the schemas and the tool turns.

1. For a registered swarm caller, `handle_session_start` calls `brief_ack`. It returns
   `ledger_info`, `brief_get`, `guidelines_get` and, for a Manager or Lead, `run_status`, as
   `additionalContext`. On `source: resume` it returns the same context without binding again.
2. A failed bind returns the refusal as context. The tools stay allowed, so the agent can retry.
3. Remove the four tools from each template's start sequence and `select:` line. Update the
   `swarm-protocol` skill and `test_plugin_surface.py`.
4. The Oracle keeps its start calls: it is one session, and its start tools need the user's answers.

Tests: a hook test per role for the returned context; a resume test that binds once; a failed-bind
test where the agent's next ledger call is refused until `brief_ack` succeeds.

### Step 4. End tools out of the up-front `select:`

- Coder: `score_record` and `handoff_submit` leave the up-front `select:`. The handoff step starts
  with one `ToolSearch select:` for them.
- Driver: `drive_done` leaves it the same way.

Tests: `test_plugin_surface.py` checks each template's `select:` line against an expected set. One
`textstats` smoke run shows each Coder loading the two tools once, before its first handoff.

### Step 5. Secure the ledger's `/mcp`

Today any local process can call `/mcp` and pass any `agent_id`.

1. The ledger keeps one random token per host repo in `.sentinel-swarm/http-token`, mode 0600,
   created on first start and reused after a restart. Claude Code keeps connect-time headers, so a
   new token per process would 403 every live role session after a restart.
2. `session_options` sends it as a static `Authorization: Bearer` header in `--mcp-config`. The
   token exists before any role starts, so no helper and no start race apply.
3. The token and `server.json` live in the host repo's working tree. Before either reaches a
   `--mcp-config`, check that the token matches `[A-Za-z0-9_-]{32,128}` and that the URL is
   `http://127.0.0.1:<integer port>/mcp`. A file that fails either check stops the spawn with an
   error.
4. Refuse a wrong `Host`, a foreign `Origin`, and a missing or wrong token, with 403. Never 401.
   Check first whether FastMCP already validates `Host`.
5. `/hook` and `/events` keep their loopback checks and take the same token. `hook.py` and the
   `swarm-events` bridge send it.

Tests: one per refusal; a ledger restart where a live session's next call succeeds; a `server.json`
naming a non-loopback host refuses the spawn. Live check: after a forced 403, look in
`~/.claude/mcp-needs-auth-cache.json` for `swarm-ledger`, and record how to clear it.

### Step 6. `graph_upsert` on the base Python

`graph.py` runs codebase-kg's `edits` through `uv run --project <kg>/mcp --frozen --no-dev`, which
starts `uv` and needs a venv in the plugin cache. codebase-kg's tool code is stdlib only since 0.12.0.

1. Run it on the base interpreter with `-I` and `<kg>/mcp/src` on the path, as codebase-kg's workers
   do.
2. Fall back to the `uv run` path when the base Python is older than codebase-kg's stated floor
   (3.10).

Tests: `graph_upsert` result equal on both paths; time per call before and after, recorded here.

### Step 7. Pool and lean front, only if step 1 says so

Build these only when the 16-agent median tool call or hook round trip misses 150 ms.

1. **Pool.** Tool calls and hook handlers run on worker processes, as codebase-kg's `pool.py` does.
   `max_workers` lives in `.claude/sentinel-swarm.local.md`, default 8; 0 runs in-process. The
   `EventHub`, `/events`, the watchdog thread, `on_run_finish` and the stamped `agent_id` stay in
   the front. A worker's result carries its wake-up events back for the front to publish.
   `graph_upsert`'s in-process `KG_LOCK` becomes a file lock (`lock.py`).
2. **Lean front.** Replace FastMCP and uvicorn with codebase-kg's stdlib design (`http_transport.py`,
   `core.py`, a generated `catalog.json` checked against `server.py`). It must also serve `/hook`,
   `/events` and `/health`. Skip it without a pool: the front would still import every tool.

Tests: every route that calls `wake.route_wakeup` publishes from the front; a pooled and an
in-process ledger return equal results.

## Context savings

| Role | Allowed ledger tools (bytes) | Up-front `select:` ledger tools (bytes) | Plus `kg_search` |
|---|---|---|---|
| Oracle | 47 (27,110) | 15 (9,042) | 900 |
| Manager | 33 (18,818) | 14 (8,865) | 900 |
| Lead | 34 (22,473) | 15 (12,441) | 900 |
| Coder | 24 (15,402) | 12 (9,367) | 900 |
| Driver | 10 (6,106) | 8 (4,734) | 0 |

The ledger exposes 67 tools, 41,802 bytes of `tools/list` JSON (measured 2026-10-07).

| Change | Per session | 10-agent run (1 Oracle, 1 Manager, 2 Leads, 6 Coders) |
|---|---|---|
| Step 3: start calls in the hook | 1,705 B and 4 tool turns (Coder, Driver); 2,232 B and 5 turns (Manager, Lead) | 16.9 KB and 39 tool turns |
| Step 4: Coder end tools loaded at handoff | 4,062 B until handoff | 24.4 KB until handoff |
| Step 4: Driver `drive_done` loaded at the end | 973 B until the exploration ends | 973 B |

At about 4 bytes a token, step 3 saves about 4,200 tokens across a 10-agent run. Each session's
share is re-sent as cached input on every turn, and the 39 fewer tool turns save more than the bytes.

## Success numbers

| Measure | Today | Target |
|---|---|---|
| Processes for a role session's HTTP plugin servers | 0 (step 0 done) | 0 |
| Cross-run `message_inbox` reads | Possible | 0 |
| Members stopped with unread mail, `modules` smoke run | Not measured | 0 |
| Coder up-front `select:` | 10,267 B, 4 start calls | 4,500 B, 0 start calls |
| Manager / Lead up-front `select:` | 9,765 / 13,341 B | 7,533 / 11,109 B |
| Tool call median, 1 agent | Step 1 | No slower than step 1 |
| Tool call and hook median, 16 agents | Step 1 | Below 150 ms; build step 7 if missed |
| Calls that fail at 16 agents | Step 1 | 0 |
| Unauthenticated `/mcp`, `/hook`, `/events` calls accepted | All | 0 |
| Ledger idle memory and start (step 7 only) | Step 1; import alone 2.96 s | Below 35 MB; below 0.3 s |

Every step runs the repo's checks before commit: `uv run pytest`, `uv run pyright`,
`uv run ruff check` and `uv run ruff format --check` in `mcp/`, then
`claude plugin validate --strict .` and `claude plugin validate --strict .claude-plugin/plugin.json`.

## Risks

- Binding in a hook moves `brief_ack`'s refusal from a tool error to hook context. Step 3's
  failed-bind test covers it.
- A Coder that skips the handoff `ToolSearch` gets an error on an unloaded tool, then loads it.
- A stale token in a live session after someone deletes `http-token` gives a 403, which can mark
  `swarm-ledger` as needing auth. Step 5 records the recovery.
- The pool splits state across processes. A wake-up published from a worker is lost silently.

## Part 2: other platforms through ide-agent-tabs (proposed)

An exploration, not a build plan. Its aim is to decide whether sentinel can run some roles on agent
CLIs other than Claude Code, through ide-agent-tabs, and what it would cost in guarantees.

### What ide-agent-tabs offers

- Agent profiles (`mcp/src/profiles.ts`): Claude Code, Codex, Codex (local), Antigravity CLI,
  Copilot CLI, Gemini CLI, Grok Build, Pi, Hermes, OpenCode, Qwen Code and Goose.
- Hook registration for Codex, Gemini, Copilot, Antigravity, Grok, Hermes, Qwen and Goose
  (`hookConfig.ts`). The events differ per CLI (`messaging/hook.ts`, `HOOK_EVENTS`): Gemini has
  `BeforeTool`, Copilot `preToolUse`, Grok and Qwen `PreToolUse`, Codex only
  `UserPromptSubmit`, `PermissionRequest`, `PostToolUse`, `Stop` and `Interrupt`.
- A machine-wide SQLite mailbox (`plans/messaging-db.md`) that every CLI's MCP server reads and
  writes, presence per session, a Stop-hook nudge to read mail before stopping, and a wake that
  types a line into the agent's tab.
- Tab tools to open, resume and close an agent session in an IDE or terminal.

### Sorting sentinel's guarantees

**Enforced server side, so platform-independent.** Every ledger gate holds for any MCP client:
`brief_ack` binding, `agent_spawn` needing a brief, the model list in `brief_create`, `score_record`
completeness and order, `handoff_submit` needing passing tests and a current graph, `approve`,
change requests, departures, deferrals, overrides, `issue_escalate`, `phase_update`, `run_finish`,
the `drive_*` gates, `message_post`'s recipient check, and the role tool set in `server._call`
**(proposed)**. The ide-agent-tabs mailbox's limits and exactly-once reads are server side too.

**One server-side guarantee leans on a hook.** Identity: `pre_ledger` stamps `agent_id` through
`updatedInput`. Without that hook, an agent can pass another agent's id. Replacement: a token per
agent, issued at spawn and sent as that session's bearer header, which the ledger maps to the
`agent_id`. This also works for Claude roles, and step 5's token is its first half.

**Dependent on Claude Code hooks or agent frontmatter.**

| Guarantee | Claude Code mechanism | Candidate replacement for another CLI |
|---|---|---|
| Only the Coder writes, and only its own files | `pre_write` | The CLI's own pre-tool hook where it can deny (Gemini, Copilot, Grok, Qwen; unverified); else the CLI's sandbox or writable-roots setting; plus a ledger check of `git status` against claims at `handoff_submit` (detects, does not prevent) |
| Shell limits | `pre_shell`, `post_shell` | The CLI's approval or sandbox mode; the same handoff-time diff check |
| No subagents | `pre_agent`, no `Agent` in `tools` | The CLI's config, if it has one; else accept and detect |
| Role tool allowlist | Frontmatter `tools` | MCP-only: `server._call` refuses out-of-role tools; give the CLI only the ledger and codebase-kg servers |
| Only run members get messages | `pre_send_message` | No `SendMessage` exists there. Route all messages through `message_post` or the ide-agent-tabs mailbox. |
| Wake-up debt is paid | `post_any`, member Stop hook | The ledger wakes a non-Claude target itself through ide-agent-tabs; the watchdog reports a debt that stays unpaid |
| Read mail before stopping | Member Stop hook (step 2) | ide-agent-tabs' Stop nudge already covers these CLIs |
| Coder does not stop without a handoff | Stop hook | The CLI's stop hook where it can block (ide-agent-tabs' `stopOutput`); else the watchdog |
| Heartbeat, activity, idle state | `post_activity`, Stop | ide-agent-tabs presence, which its hooks update for each CLI |
| Token and cost accounting | Transcript parsing, `PreCompact` | Per-CLI usage output, or "unknown" in the report |
| Session start, liveness, resume, stop | `claude --bg`, `claude agents --json`, `--resume`, `claude stop` | `open_tab`, presence PID, `resume_tab`, `close_tab`; these need an IDE or terminal host |
| Watchdog listener, `PushNotification` | `Monitor`, Oracle Stop hook | Keep the Oracle on Claude Code |

### Starting shape to test

Keep the Oracle, Managers and Leads on Claude Code. Allow a non-Claude CLI only as a Coder, in a
tab, with only the ledger and codebase-kg servers, and a handoff-time diff check in the ledger. A
Coder is the role with the narrowest tools, and every one of its results passes a Lead review.

Alex accepted this shape as the starting point on 2026-10-07, with one condition the shape must
meet before anything else: a non-Claude Coder communicates and works with its Lead, Manager and
Oracle as a Claude Coder does. Concretely, it must:

- receive and acknowledge its brief, and read its inbox, through the ledger;
- answer a Lead's question and ask one, and receive a Manager's or the Oracle's directive;
- submit its handoff and take a returned review back to work;
- be woken when a message arrives while it is idle.

Build part 2 in small, separately tested stages, in that order, and stop at the first stage a
candidate CLI cannot pass. Measure each stage with one Coder on the `hello` smoke run before any
multi-agent run.

### Open questions

1. Which CLIs' pre-tool hooks can deny a call, and do they see the file path for each write tool?
2. Can every candidate CLI send a static bearer header to an HTTP MCP server, or does each need a
   stdio relay?
3. Can ide-agent-tabs start an agent with no IDE or terminal host, as `claude --bg` does? If not,
   a non-Claude role needs an open IDE.
4. Should the ledger call ide-agent-tabs to wake a non-Claude target, or write to its mailbox
   directly? Direct writes would tie the ledger to that plugin's schema.
5. Does the per-agent token replace `pre_ledger` stamping for Claude roles too, so one identity path
   serves every platform?
6. How does the report count cost for a CLI that prints no usage?
7. Is a handoff-time diff check strong enough for a Coder whose writes no hook can stop?
