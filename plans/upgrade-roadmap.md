# Upgrade roadmap: efficiency, messaging, other platforms

Status: part 1 is a build plan; steps 0 to 7 are done (step 5's live 403 check is still owed). Part 2 is an exploration
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

Step 2 closed all three. They are kept here as the reason for its changes.

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

#### Results, 2026-10-08

Run: `uv run --project mcp python scripts/bench/ledger_load.py`, 5 rounds per agent, 44 s in
all. Windows 11, 16 logical CPUs, Python 3.10.20. The machine was busy with other work: whole-machine
CPU load was 53.9% before the run and 48.9% after. Measured before the step 2 changes.

Each agent is a Coder with its own Streamable HTTP client. A round is `message_post` (from the
Lead to that Coder, because the Coder role has no `message_post`), `message_inbox`, `issue_list`,
`who_owns`, `tests_run(force=true)` with `echo 1 passed {target}` as the test command, and one
`POST /hook/pre_write`. The test command still runs in a shell, and `tests_run` runs git twice for
its fingerprint.

Server:

| Measure | Value |
|---|---|
| Spawn to `server.json` | 3,122 ms |
| Spawn to `/health` answering | 3,641 ms |
| Working set, idle after start | 79.3 MB |
| Working set, peak under 16 agents | 94.9 MB |
| Working set, after the load | 94.9 MB |

Load (ms, median / p90 / max):

| Agents | CPU % | Wall | Every tool call | Tool calls without `tests_run` | `pre_write` hook | `message_post` to `message_inbox` | Errors |
|---|---|---|---|---|---|---|---|
| 1 | 64.7 | 1,555 | 15 / 184 / 273 | 15 / 55 / 190 | 42 / 50 / 50 | 33 / 208 / 208 | 0 |
| 4 | 69.2 | 4,047 | 56 / 354 / 513 | 47 / 219 / 417 | 51 / 168 / 207 | 224 / 475 / 500 | 0 |
| 8 | 69.3 | 7,502 | 222 / 556 / 878 | 162 / 459 / 803 | 53 / 227 / 334 | 494 / 899 / 1,007 | 0 |
| 16 | 70.5 | 15,729 | 273 / 1,478 / 2,282 | 250 / 1,119 / 2,217 | 52 / 190 / 483 | 991 / 1,917 / 2,396 | 0 |

Per tool at 16 agents (ms, median / p90 / max): `message_post` 772 / 1,665 / 2,217;
`tests_run` 1,134 / 1,818 / 2,282; `issue_list` 248 / 322 / 1,716; `message_inbox` 235 / 302 / 842;
`who_owns` 182 / 305 / 769. At 1 agent each is 13 to 17 ms except `tests_run` at 154 ms.

The 16-agent tool call median, 273 ms, misses step 7's 150 ms line. The hook round trip, 52 ms,
meets it: `/hook` opens its own ledger connection on Starlette's thread pool and does not take
`server._CALL_LOCK`, which serializes every tool call.

One role session's MCP side: the launcher writes the ledger's HTTP URL into `--mcp-config`, so a
role session starts no process for the ledger (from the code, not measured). Each hook call
starts one hook shim process: `pre_write` through `.sentinel-swarm/hook.py` on the base Python
took 235 / 324 / 324 ms over 10 calls, with a peak working set of 16.7 MB.

Up-front `select:` bytes, from the live server's `tools/list` and the templates as committed. The
bytes count `name`, `description` and `inputSchema` of each tool, as JSON:

| Role | Allowed ledger tools (bytes) | Up-front `select:` ledger tools (bytes) |
|---|---|---|
| Oracle | 47 (24,350) | 15 (8,382) |
| Manager | 33 (17,000) | 14 (7,902) |
| Lead | 34 (20,867) | 15 (11,743) |
| Coder | 24 (14,116) | 12 (8,539) |
| Driver | 10 (5,476) | 8 (4,186) |

The ledger exposes 67 tools: 38,542 bytes by this count, 48,293 bytes with every field of
`tools/list` (output schemas included). The "Context savings" table below used a third count,
41,802 bytes in all; its per-role numbers are not comparable with these.

### Step 2. Messaging: take what the mailbox design does better

Done on 2026-10-08, with the tests below; the `modules` smoke run for the success line is not run
yet. One change from the text below: when a member owes a wake-up and has unread mail, one block
names both, the owed calls first. Two separate blocks would lose the second, because the stop
after a block passes (`stop_hook_active`). `message_inbox` always returns the first unread
message, even one over 40,000 characters, because the ledger's own messages (returns,
escalations) have no body cap.

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

### Step 2b. Enforce the prompt-only rules

Built on 2026-10-08, with the tests below in `mcp/tests/test_enforcement.py`; not run live yet.
The open questions were settled this way: (1) the hooks reference says `PreToolUse` fires
for the `Skill` tool when the model calls it, so change 3 builds `pre_skill`, reading the
skill name from `tool_input.skill`, the field the `Skill` tool's input schema names;
(2) web-driver navigates to a URL the Driver passes and serves nothing, so `profile_set`
refuses only serve and watch words, and the PRD records the residual gap; (3) `cr_open`
from above the Lead is not direction and still routes to the Coder; (4) the token list is
`--watch`, `serve`, `dev-server`, and a `dev` or `start` script through `npm`, `yarn`, or
`pnpm`. Changes from the text below: the Oracle may also `SendMessage` any live agent
that is not working, because its Stop hook names such agents to wake; a dispute's
arbiter is the closest shared strict ancestor, so a party never judges its own dispute;
and `brief_create` also refuses a Manager brief for a planned phase.

A PRD audit found rules that only role-template prompt text carries. No hook or ledger gate stops
a role that breaks them. Each change below names its mechanism. A ledger gate comes first, because
it holds for any MCP client, including the part 2 platforms. A CLI helps only the Coder, the one
role with Bash, so no change here uses one.

Cost terms: "select bytes" means the change grows a tool schema in a role's up-front
`ToolSearch select:` line. A refusal message costs nothing until it fires. A hook round trip is one
`POST /hook/<event>` per matched tool call.

Checked against the code on 2026-10-08. None of these checks exist today.

#### Rules and mechanisms

1. **Ordering inside phases and modules; contracts before implementations; join points.**
   PRD 02, "Phases, dependencies, and waves": "The same ordering applies inside a phase and a
   module: helpers come before the files that use them." "Contracts come before implementations.
   When a brief fixes a helper's contract, the dependent Coders test against it with test doubles
   instead of waiting." "A join point is where parallel phases feed a later phase. The Oracle runs
   the full suite there, once every Manager that feeds it has reported." Today `module_add` and
   `claim_file` take no dependencies, and `brief_create` stores only a free-text `body`.
   - Mechanism: ledger gates.
     - `claim_file` takes `depends_on: list[int] | None`, file ids of the same module. It refuses
       an id that is not a claimed file of the caller's module. Store it in a new
       `file_deps (file_id, depends_on_file_id)` table.
     - `module_add` takes `depends_on: list[int] | None`, module ids of the same phase, stored in
       `module_deps`. It refuses an id outside the caller's phase.
     - `brief_create` takes `contract: str | None`, the public contract of the child's file or
       module. For a Coder or Lead brief whose file or module has dependencies, it refuses until
       each dependency's latest brief has a non-empty `contract`. The refusal names the missing
       ones. This is "helpers first" made checkable: a dependent is briefed only after its
       helper's contract is on record.
     - `brief_get` returns `depends_on_contracts`: each dependency's name and contract. The
       dependent Coder reads the contract from the record, not from the parent's retelling.
     - Join points need no new gate. Each dependency's `phase_review(accepted)` already needs a
       passing full run after its own hand-up, and change 9 below makes `phase_update(unlocked)`
       wait for every dependency's approval. So the newest dependency's full run comes after every
       feeding hand-up. Add a test that proves it, and rewrite the PRD line to say so.
   - Judgment that remains: whether the Lead or Manager declared every real dependency, and
     whether a contract is right. A missing `depends_on` lets a dependent start without a
     contract. The Lead review and `review_compare` judge the result.
   - Cost: select bytes for `claim_file` (Lead), `module_add` (Manager) and `brief_create`
     (Oracle, Manager, Lead), about 150 B each for the new parameter text. No hook round trip.
   - Tests: a `claim_file` naming a file of another module is refused; a Coder brief whose
     dependency has no contract is refused and names it; after the helper's brief records a
     contract, the dependent's brief succeeds and its `brief_get` returns the contract; a
     two-dependency phase's newest full run is newer than both hand-ups when it unlocks.

2. **The Lead validates an `accept_incomplete` reason.** PRD 02, "Lead review":
   "`accept_incomplete(handoff_id, reason)` ... The Lead validates the Coder's reason first." PRD 03,
   "accepting work as incomplete uses no attempt." Today `accept_incomplete`
   (`review.py`, `ReviewMixin.accept_incomplete`) checks the role, the module and open departures.
   It takes an empty `reason`, needs no Lead review, and needs no report from the Coder.
   - Mechanism: ledger gate in `accept_incomplete`. It refuses:
     - a `reason` that is empty or whitespace;
     - a handoff with no `review_compare` yet, as `approve` does, so the Lead has scored the
       work blind before it accepts less;
     - a handoff whose `open_issues_json` is empty while the file has no open `issues` row. The
       Coder's open issues are the "Coder's reason". With neither, nobody reported the work as
       incomplete.
   - It records on the deferral the open issue strings and issue ids it found, and the Lead's
     `agent_id` (already `proposed_by`). The deferral already waits on the Manager through
     `agreement_decide`, so a second level reviews the reason.
   - Not checkable: whether the reason is good. That stays the Lead's judgment, and the Manager's
     `agreement_decide` is the check on it.
   - PRD rewrite: replace "The Lead validates the Coder's reason first." with "It refuses an empty
     reason, a handoff without `review_compare`, and a handoff that carries no open issue from
     the Coder and whose file has no open issue. The deferral it opens records them, and the
     Manager decides it."
   - Cost: none. `accept_incomplete` is not in the Lead's up-front `select:`, and no parameter
     changes.
   - Tests: each of the three refusals; a success records the issue strings and ids on the
     deferral.

3. **The Driver runs no dev server, and runs `map-test` before `map-explore`.** PRD 02,
   "Explorations": "The Driver tests the app it built at the start of the exploration, never a dev
   server that reloads on edits." "The Driver first replays recorded routes with `map-test` to
   recheck earlier findings, then runs `map-explore` with the focus list as its goal." PRD 01,
   "Driver": "replays recorded routes with cartographer's `map-test`, then explores".
   - Dev server. `pre_shell` already limits the Driver to commands that start with
     `build_command`, so the Driver cannot start a server from its shell. The gap is a
     `build_command` that is itself a server or a watcher.
     - Mechanism: ledger gate in `profile_set`. It refuses a `build_command` that contains a
       watch or serve token: `--watch`, ` -w `, `watch`, `serve`, `dev`, `start` as a whole word,
       `runserver`, `bootRun`. The list lives in one constant in `ledger.py`.
     - Not checkable: a driver plugin's own launch tool pointing a browser at a dev server URL.
       That stays the Driver's prompt rule. See the open question below.
   - `map-test` first. cartographer's `map-test` also spawns `map-driver` (its `SKILL.md` says so),
     so `pre_agent` cannot tell the two skills apart from the subagent call.
     - Mechanism: a new hook in `templates/agents/driver.md` only: `PreToolUse`, matcher `Skill`,
       ledger event `pre_skill`, in `hooks/events.py` as `handle_pre_skill`. For a Driver with an
       open exploration, it records `drive_requests.map_test_at` when the skill is `map-test` or
       `cartographer:map-test`, and denies `map-explore` or `cartographer:map-explore` while
       `map_test_at` is empty. Any other skill passes. No override rule covers it.
     - Add `map_test_at TEXT` to `drive_requests`.
   - Cost: one hook round trip per Driver `Skill` call, about two per exploration. No select bytes.
   - Tests: `profile_set(build_command="npm run dev")` is refused and `npm run build` is
     accepted; a Driver `Skill(map-explore)` before `map-test` is denied and names `map-test`;
     after `Skill(map-test)` it passes; a non-Driver session's `Skill` call passes.

4. **A new Coder for a running module goes through its Manager and Lead.** PRD 02,
   "Explorations": "The Oracle asks a running module for a new Coder through its Manager, which
   asks the Lead, so the spawn order holds." The spawn order already holds: `brief_create` lets
   the Oracle brief only a Manager or the Driver. Two gaps remain. `brief_create` does not check
   that the scope ids belong to the caller, and the request message can skip a level (change 5
   covers that).
   - Mechanism: ledger gate in `brief_create`.
     - A Manager's Lead brief needs a `module_id` of the Manager's own phase.
     - A Lead's Coder brief needs a `file_id` of the Lead's own module whose live claim names
       `child_name` as `owner_agent_id`.
     - An Oracle's Manager brief needs a `phase_id` of the run, and change 9's unlock rule.
   - PRD rewrite: "The Oracle asks a running module for a new Coder with a message to its
     Manager. The Manager asks the Lead. Only the Lead can brief the Coder, and only for a file
     its module claimed for that name."
   - Cost: none.
   - Tests: a Manager brief naming another phase's module is refused; a Lead brief naming another
     module's file, or a file claimed for another name, is refused.

5. **A Manager does not direct a Coder except through its Lead.** PRD 01, "Manager": "Must not:
   ... direct a Coder without going through the Coder's Lead." The same holds for the Oracle:
   "Must not: ... direct a Lead or a Coder." Today `message_post` accepts any name in the run,
   and `pre_send_message` checks only that the name is in the run.
   - Mechanism: ledger gate in `message_post`, plus the existing `pre_send_message` hook.
     - `message_post` allows a recipient that is the caller's parent, its child, or its sibling
       (same parent). The Oracle may also post to the Driver. It refuses any other name and lists
       the allowed ones. Messages the ledger writes itself (pushbacks, escalations) do not go
       through `message_post` and are unaffected.
     - `handle_pre_send_message` applies the same relation, and also allows a target the caller
       owes an unsent wake-up. The ledger owes some wake-ups across levels: `cr_open` to a file's
       Coder, `cr_verify` by the nearest live ancestor, and issue round moves.
   - Open for Alex: `cr_open` from a Manager or the Oracle routes to the file's live Coder. Either
     the PRD says a change request is not direction, or `_cr_recipient` routes a request from
     above the Lead to the file's Lead. The plan assumes the first.
   - Cost: none. `message_post` is in no up-front `select:`. `pre_send_message` already runs on
     every `SendMessage`; the change adds one query.
   - Tests: a Manager's `message_post` to a Coder is refused and lists its Leads; the Oracle's
     post to a Lead is refused; a Lead's post to a sibling Lead passes; a Manager's `SendMessage`
     to a Coder is denied unless the Manager owes that Coder a wake-up.

6. **Arbitration duties.** PRD 01: the Oracle arbitrates "disputes between Managers, including
   which Manager owns a shared file" (already marked needs implementation); the Manager arbitrates
   "disputes between its Leads, and the contracts between its modules"; the Lead arbitrates
   "disputes between its Coders about contracts and about where a shared function belongs". No
   record type for a dispute exists.
   - Mechanism: ledger gate, sharing change 8's schema. `deferral_propose` takes
     `parties: list[str] | None`, the agent names on the other side. With parties set, the
     proposal is a dispute: the ledger owes the arbiter a wake-up, and `agreement_decide` accepts
     only the lowest common ancestor of the proposer and every party, found through
     `parent_agent_id`. Two Coders of one module reach their Lead; two Leads reach their Manager;
     two Managers reach the Oracle. An open dispute blocks like any open deferral.
   - Not checkable: whether the arbiter decides well. The ledger fixes who decides and that the
     decision has a recorded reason.
   - Template: the Oracle gets a step to decide a dispute between Managers, which removes the
     needs-implementation note in PRD 01.
   - Cost: none in select bytes; `deferral_propose` and `agreement_decide` are loaded on demand.
     Their schemas grow about 200 B when loaded.
   - Tests: a dispute between two Coders is refused to the Manager and accepted from the Lead; one
     between two Managers' Leads reaches only the Oracle; `deferral_propose` with parties owes the
     arbiter a wake-up.

7. **Name patterns for every role but the Driver.** PRD 01, "Names": `mgr-<phase>`,
   `lead-<phase>-<module>`, `coder-<phase>-<module>-<file>`, with the examples `mgr-p2-api`,
   `lead-p2-auth` and `coder-p2-auth-login`. `brief_create` checks only that the name is free. The
   table is also inconsistent: `<phase>` is `p2-api` for the Manager and `p2` for the Lead.
   - PRD rewrite: phase names are `p<ordinal>-<slug>`, and `phase_add` adds the `p<ordinal>-`
     prefix when the name lacks it. Manager: `mgr-<phase name>`. Lead:
     `lead-p<ordinal>-<module name>`. Coder: `coder-p<ordinal>-<module name>-<file slug>`. Module
     names and slugs match `[a-z0-9]+(-[a-z0-9]+)*`.
   - Mechanism: ledger gates. `phase_add` adds the prefix. `module_add` refuses a name that is
     not a slug. `brief_create` refuses a Manager, Lead or Coder name that does not match the
     pattern built from its phase and module rows, and names the expected form. `claim_file`
     applies the Coder pattern to `for_name`.
   - Cost: none. Only refusal text changes.
   - Tests: one refusal per role; `phase_add("api")` stores `p2-api` as the second phase. Update
     the fixtures in `mcp/tests/` and the smoke PRDs that use other names.

8. **The responsible level for deferrals and scope changes.** PRD 04, "The responsible level"
   table: a file's task or tests, the Lead; a module's scope or a contract between files, the
   Lead, and the Manager when another module is affected; a phase's scope or a contract between
   modules, the Manager; the phase plan or work moved to a later phase, the Oracle; what the PRD
   asks for, the user through the Oracle. `agreement_decide` checks only rank at or above the
   proposer's parent, and scope.
   - Mechanism: ledger gate. `deferral_propose` takes a required
     `kind: "file" | "module" | "cross_module" | "phase" | "plan" | "prd"`. `accept_incomplete`
     opens kind `file`. `agreement_decide` needs a caller at or above both the proposer's parent
     and the kind's level: `file` and `module` Lead, `cross_module` and `phase` Manager, `plan`
     and `prd` Oracle. For `prd` it also needs a `user_chat` directive in the run created after
     the deferral, named in a new `directive_id` argument.
   - Not checkable: whether the proposer chose the right kind. A decider who sees the kind is too
     low denies it with that reason, and the proposer files it again.
   - Cost: none in select bytes; about 150 B on `deferral_propose` and 80 B on
     `agreement_decide` when loaded.
   - Tests: a Lead's `agreement_decide` on a `cross_module` deferral is refused; a Manager's on a
     `plan` deferral is refused; a `prd` decision without a later `user_chat` directive is refused.

9. **(Found) A phase unlocks only when its dependencies are approved.** PRD 02: "A phase unlocks
   when every phase it depends on is approved." PRD 01: "Starts one Manager per unlocked phase."
   `phase_update(phase_id, "unlocked")` sets the state with no dependency check, and
   `agent_spawn` starts a Manager into any phase.
   - Mechanism: ledger gates. `phase_update(unlocked)` refuses while a `phase_deps` row names a
     phase that is not `approved`, and names it. `agent_spawn` refuses a Manager whose phase is
     `planned`.
   - Cost: none.
   - Tests: unlocking a phase with an unapproved dependency is refused; spawning a Manager into a
     planned phase is refused.

10. **A role records what the graph was missing.** PRD 09, "The code graph": "An agent that falls
    back records what the graph was missing." No ledger tool takes such a record.
    - Mechanism: the existing `post_activity` hook. For a `Grep` or `Glob` call by a swarm
      session, `handle_post_activity` writes a `graph_gaps` row: run, agent, tool, pattern, path,
      and the first 20 result paths from `tool_response`. The report lists the gaps per run.
      codebase-kg's own gate decides whether the search may run; this only records it.
    - Not checkable: why the graph fell short, in words. The pattern and the paths found are the
      structural record. A search scoped to a file the graph anchors is recorded too; the report
      marks those rows by looking up each path in the graph.
    - PRD rewrite: "The ledger records each Grep and Glob a role runs, with its pattern and the
      paths it found, as a graph gap. The report lists them." This drops the agent's duty.
    - Cost: none. `post_activity` already runs, async, after every tool.
    - Tests: a Coder's Grep writes one row with its pattern and paths; a non-swarm session's Grep
      writes none; the report shows the gaps section.

11. **(Found) An agent re-reads its brief before each handoff and after each return.** PRD 01,
    principle 9. `brief_get` takes no identity, so the ledger cannot tell who read a brief.
    - Mechanism: `handle_pre_ledger` already sees every `brief_get` and its session. It records
      `briefs.last_read_by_child_at` when the caller's session is the brief's child. `handoff_submit`
      refuses when the Coder has not read its brief since its last return.
    - Cost: none.
    - Tests: a Coder's second handoff after a return without `brief_get` is refused; after
      `brief_get` it passes.

#### Cost summary

| Change | Mechanism | Select bytes | Hook round trips |
|---|---|---|---|
| 1 Ordering and contracts | `claim_file`, `module_add`, `brief_create`, `brief_get` | about 150 B each to Lead, Manager, Oracle | 0 |
| 2 `accept_incomplete` reason | `accept_incomplete` | 0 | 0 |
| 3 Dev server | `profile_set` | 0 | 0 |
| 3 `map-test` first | New `pre_skill` hook, Driver only | 0 | About 2 per exploration |
| 4 New Coder through the chain | `brief_create` scope checks | 0 | 0 |
| 5 No skip-level direction | `message_post`; existing `pre_send_message` | 0 | 0 new |
| 6 Arbitration | `deferral_propose(parties)`, `agreement_decide` | 0 (about 200 B when loaded) | 0 |
| 7 Names | `phase_add`, `module_add`, `brief_create`, `claim_file` | 0 | 0 |
| 8 Responsible level | `deferral_propose(kind)`, `agreement_decide(directive_id)` | 0 (about 230 B when loaded) | 0 |
| 9 Phase unlock | `phase_update`, `agent_spawn` | 0 | 0 |
| 10 Graph gaps | Existing async `post_activity` | 0 | 0 new |
| 11 Brief re-read | Existing `pre_ledger`; `handoff_submit` | 0 | 0 new |

#### PRD edits

- `01-roles.md`: the Names table (change 7); each role's Arbitrates line names the dispute
  mechanism, and the Oracle's needs-implementation note goes (6); the Manager's and Oracle's
  Must-not lines name the `message_post` gate (5); the Driver's "Does" names the `pre_skill` order
  (3).
- `02-run-lifecycle.md`: the ordering and contracts paragraphs and the join-point line (1);
  `accept_incomplete` (2); `brief_create` scope and name refusals (4, 7); `phase_update(unlocked)`
  and `agent_spawn` (9); the Explorations bullets on the dev server, `map-test` and the new Coder
  (3, 4); `profile_set`'s build command check (3); `handoff_submit`'s brief re-read (11).
- `03-scoring-and-review.md`: the incomplete line points at 02's refusals (2).
- `04-agreements.md`: `deferral_propose(kind, parties)`, the responsible-level table with its
  kind values, and `agreement_decide`'s checks (6, 8).
- `05-sessions.md`, "Messages": the `message_post` relation (5).
- `06-ledger-server.md`: the changed tool signatures and the new tables `file_deps`,
  `module_deps`, `graph_gaps`, and the new columns.
- `07-hooks-and-enforcement.md`: a `pre_skill` row in the hook table; the `pre_send_message`,
  `pre_ledger` and `post_activity` paragraphs; one rule-map row per change.
- `09-mcp-servers-and-code-graph.md`: the graph-gap record (10).
- `10-report-and-costs.md`: the graph-gaps and disputes sections (6, 10).
- `14-key-decisions.md`: a dated line for each rewrite Alex accepts (2, 4, 7, 10, and the
  `cr_open` routing in 5).
- `16-open-items.md`: drop the Oracle arbitration item once change 6 ships.

#### Open questions

1. Does Claude Code fire `PreToolUse` with matcher `Skill` for a skill call, with the skill name in
   `tool_input`? Verify live before building change 3.
2. How does web-driver serve a web build? If it opens a URL the Driver passes, a dev server URL
   passes every check here.
3. `cr_open` routing from a Manager or the Oracle to a Coder (change 5).
4. Is the `build_command` token list too broad? `start` and `dev` can appear in a real build
   script name.

### Step 2c. Build the items marked (needs implementation)

Items 2, 3, 4, 6 and 7 built on 2026-10-08, with their tests in `mcp/tests/test_spec_items.py`;
not run live yet. Item 2's re-claim gate compares the file's `claimed_at` with each finding's
`created_at`, so a Coder brief needs a claim made after every finding it fixes.

Each item is listed in `knowledge/prd/16-open-items.md`. Remove its mark from the PRD when it
ships, and delete its line from `16-open-items.md`.

1. **Oracle arbitration between Managers** (PRD 01). Done with step 2b, change 6.
2. **A fix Coder's brief** (PRD 02). `brief_get` returns the evidence of each finding in the
   brief's `finding_ids`. The fix brief re-claims the file through `claim_file`.
3. **Overrides tied to a run** (PRD 04). `override_consume` matches `run_id` as well as the rule,
   the agent name and the target. Also closes "Overrides across runs".
4. **A Driver only through `drive_request`** (open item). `brief_create` refuses
   `child_role="driver"`; `drive_request` is the only path that starts a Driver.
5. **The device-queue broker** (PRD 05). A second event kind on `swarm-events`. Roles stay `--bg`
   sessions, so it first needs a path to a waiting role that is not the channel.
6. **Graph search at each role's level** (PRD 09). Each role template's start sequence names the
   level it searches at.
7. **Multi-file graph nodes through the Lead** (PRD 09). Give the Lead `graph_upsert` for a node
   whose anchors span files in its module.

Tests: one ledger test per gate in items 2, 3, 4 and 7; `test_plugin_surface.py` checks the
template text for item 6. Item 5 waits on its delivery path.

### Step 3. Start-up calls in the `SessionStart` hook

Built on 2026-10-08, with its tests in `mcp/tests/test_hooks.py`; not run live yet. Choices made
while building:

- The hook binds any `registered` swarm session that is not the Oracle, on any `source`, and
  returns the context without binding for a session already bound, on any `source` (`resume`,
  `compact`, `clear`).
- "Refused until `brief_ack` succeeds" had no gate: a `registered` row already resolved for most
  tools. `pre_ledger` now denies a `registered` session every ledger tool but `brief_ack` and the
  five that take no identity. It is a hook gate, not a ledger gate, so it does not hold for a
  client without the hook; a ledger gate in `identity.resolve` would, and is left for step 5's
  identity work.
- Claude Code caps `additionalContext` at 10,000 characters and saves a longer one to a file the
  model is not told to read. The hook stays under 9,500: a result that does not fit is left out,
  and the context names the call to make instead.
- `ledger_info` in the context leaves out the tool count, which only `server.py` knows.
- The step 2b brief re-read gate: when the brief is in the context, the hook records
  `briefs.last_read_by_child_at`, as `pre_ledger` does for `brief_get`. On a first start this
  changes nothing, because the gate applies only after a return. On a session restarted after a
  return (`agent_resume`, or a compaction), the brief text is fresh in the context, so it counts
  as the re-read. A Coder woken by `SendMessage` in a live session gets no `SessionStart` and
  still calls `brief_get`.

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

Built on 2026-10-08. `test_plugin_surface.py` checks each template's up-front `select:` and its
`max_results` against an expected set, that no child's set names a start-call tool, and that the
Coder's and Driver's end-tool `select:` comes before the first call of each tool. The `textstats`
smoke run is not run yet.

- Coder: `score_record` and `handoff_submit` leave the up-front `select:`. The handoff step starts
  with one `ToolSearch select:` for them.
- Driver: `drive_done` leaves it the same way.

Tests: `test_plugin_surface.py` checks each template's `select:` line against an expected set. One
`textstats` smoke run shows each Coder loading the two tools once, before its first handoff.

### Step 5. Secure the ledger's `/mcp`

Built on 2026-10-08, with the tests below; the live check is still owed. FastMCP 4.0.5 does not
validate `Host` by default: its `HostOriginGuardMiddleware` runs only when
`host_origin_protection` is on (default off), and it answers a wrong `Host` with 421. So the
ledger has its own pure ASGI middleware, `serve.Guard`, in front of every route. It answers 403
to a remote client, a `Host` other than `127.0.0.1:<port>` or `localhost:<port>`, an `Origin`
other than the ledger's own, and, on every route but `/health`, a missing or wrong bearer token.
`/health` stays open to the token-less probe in `ensure_server`, behind the same address
checks. The token lives in `mcp/src/swarm_ledger/auth.py`; `session_options` calls
`auth.ledger_entry`, which checks the URL and the token. The launcher reaches the ledger only
through `/health` and `session_options`, and `python -m swarm_ledger.directive` writes the
database directly, so both work unchanged. `scripts/bench/ledger_load.py` sends the token too.

Tests: `test_serve.py` (each refusal at the middleware, the token file, and a live ledger that
refuses token-less `/mcp`, `/events` and `/hook` calls and keeps the token across a kill and
restart, where the next call with the old header succeeds); `test_agentfiles.py` (the header,
and each refused URL and token); `test_sessions.py` (a `server.json` naming `10.0.0.5` and a
missing token each refuse `agent_spawn`); `test_hook_shim.py` and `test_wake.py` (the shim and
the bridge send the token; the shim falls back without one or on a 403).

Live check, still owed: force a 403 for a role session, read the `swarm-ledger` key in
`~/.claude/mcp-needs-auth-cache.json`, and confirm the recovery recorded in PRD 06. The file
on this machine is a JSON object keyed by server name, each entry a `timestamp`; plugin servers
appear as `plugin:<plugin>:<server>`. PRD 16 lists the check.

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

Built on 2026-10-08. The installed codebase-kg 0.14.0 states `requires-python = ">=3.10"` and
`dependencies = []` in `mcp/pyproject.toml`, and its `pool.worker_command` runs
`[sys._base_executable or sys.executable, "-I", "-S", "-c", <entry>, <mcp/src>]`.
`graph_upsert` now runs the same way: `-I -S`, `<kg>/mcp/src` inserted on `sys.path`. It falls
back to `uv run` when the running Python is below that floor, when the `pyproject.toml` declares
a dependency, or when `src/codebase_kg/edits.py` is missing. Tests in `mcp/tests/test_graph.py`,
including an integration test that both paths return equal results.

Time per call, 2026-10-08: Windows 11, Python 3.10.20, codebase-kg 0.14.0, 10 calls after one
warm-up, each an update of one node in a copy of this repo's `knowledge/code_graph.db`. Most of
a call is codebase-kg's own validation before and after the write.

| Run | Machine CPU | `uv run` (before) median / max | Base Python (after) median / max | Process start and `import edits` alone, before / after |
|---|---|---|---|---|
| 1 | busy | 880 / 1,844 ms | 749 / 939 ms | not measured |
| 2 | 100% | 3,264 / 5,464 ms | 1,248 / 2,015 ms | 1,255 / 479 ms |
| 3 | 63-77% | 579 / 867 ms | 369 / 528 ms | 428 / 304 ms |

Run 3 is the least loaded: the base path saves about 210 ms per call, and the first call of a
burst drops from 631 to 395 ms.

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

#### Results, 2026-10-08

Built both: step 1's 16-agent median missed 150 ms, and the pool alone kept FastMCP's 80 MB idle
front. The pool is `mcp/src/swarm_ledger/pool.py`, the front's call path `front.py`, the lean
HTTP front `http_front.py`, and the catalog `catalog.py` with `catalog.json`. Since step 2 removed
the `swarm-events` channel, the front has no `EventHub` or `/events` to keep: a worker records a
wake-up's `wakeups` row and returns its `next`, and the front runs each OS notifier command a
worker's reply lists, once. `run_finish` returns to the front, which calls `on_run_finish`.
`KG_LOCK` is the file lock `.sentinel-swarm/kg.lock`. Each worker gets the front's settings
snapshot instead of reading the file, and `profile_set` and `tests_run` read the commands from the
run's snapshot, so no `Ledger` field splits across workers.

Run: `scripts/bench/ledger_load.py`, 5 rounds per agent, token on every request. The machine was
far busier than in step 1: whole-machine CPU load was 77% to 99% during every run, against about
70% in step 1, so the absolute numbers are slower than step 1's. The FastMCP rows ran through a
temporary FastMCP path in `serve.py`, removed before commit. FastMCP with `max_workers: 0` is the
code before this step: tool calls serialized under one lock.

Server:

| Front, `max_workers` | Spawn to `server.json` | Spawn to `/health` | Idle | After load | Workers after load |
|---|---|---|---|---|---|
| FastMCP, 0 (before) | 818 ms | 8,280 ms | 79.7 MB | 95.6 MB | 0 |
| FastMCP, 8 (pool alone) | 1,417 ms | 8,879 ms | 79.7 MB | 92.5 MB | 13, 262.5 MB |
| Lean, 8 (shipped) | 1,402 ms | 1,470 ms | 24.7 MB | 26.6 MB | 12, 242.3 MB |
| Lean, 0 | 1,516 ms | 1,609 ms | 24.5 MB | 27.6 MB | 0 |

The FastMCP rows write `server.json` before importing FastMCP, so `/health` is their start time.
At this load a bare `python -I -S -c pass` took 0.4 to 1.1 s, and the front's imports added
about 0.3 s to it; the 0.3 s start target cannot be judged on this machine until it is idle.

Load (ms, median / p90 / max), every tool call and the `pre_write` hook:

| Agents | FastMCP, 0 (before) | FastMCP, 8 | Lean, 8 (shipped) | Lean, 0 |
|---|---|---|---|---|
| 1, tool | 26 / 533 / 1,043 | 24 / 276 / 1,255 | 8 / 588 / 1,134 | 8 / 708 / 1,043 |
| 1, hook | 56 / 64 / 64 | 52 / 466 / 466 | 56 / 753 / 753 | 79 / 103 / 103 |
| 4, tool | 93 / 441 / 1,164 | 56 / 604 / 851 | 7 / 747 / 1,235 | 113 / 1,761 / 2,818 |
| 4, hook | 66 / 243 / 260 | 78 / 758 / 772 | 61 / 878 / 928 | 83 / 133 / 214 |
| 8, tool | 305 / 1,424 / 2,469 | 127 / 422 / 799 | 27 / 904 / 2,367 | 286 / 1,621 / 4,059 |
| 8, hook | 68 / 191 / 547 | 87 / 133 / 477 | 63 / 122 / 831 | 74 / 95 / 184 |
| 16, tool | 611 / 3,021 / 5,077 | 353 / 943 / 2,086 | 217 / 1,073 / 2,617 | 353 / 4,533 / 10,110 |
| 16, hook | 71 / 261 / 492 | 151 / 258 / 297 | 75 / 137 / 228 | 70 / 150 / 385 |
| 16, `message_post` to `message_inbox` | 2,248 / 4,812 / 6,163 | 712 / 1,291 / 1,563 | 492 / 765 / 949 | 1,983 / 8,256 / 10,801 |
| 16, wall | 30.2 s | 12.9 s | 11.6 s | 38.7 s |

No run had an error. At 16 agents the lean front with the pool cut the median tool call from
611 to 217 ms and the wall time from 30.2 to 11.6 s on the same machine and load, and the pool
alone reached 353 ms. Neither met 150 ms at this CPU load; the 1-agent p90 and max come from a
worker's first start and from `tests_run`. The pool alone missed 150 ms and kept the 80 MB idle
front, so the lean front stays: it meets the 35 MB idle target and starts answering 6 s sooner.

The front imports no fastmcp, uvicorn, Starlette, or pydantic; its only third-party import is
PyYAML, for the settings file, and the workers import the same. fastmcp now serves only the
tests and `python -m swarm_ledger.catalog`. The launch still uses `uv run`: making the server
launch on the base interpreter, per the Plugins launch rule, needs the settings frontmatter
parsed without PyYAML and fastmcp moved to the dev dependencies.

## Context savings

Measured 2026-10-08 with `scripts/bench/ledger_load.py --agents 1 --rounds 1 --shim-reps 1`,
before and after steps 3 and 4, on the step 2c tree. Bytes count `name`, `description` and
`inputSchema` of each ledger tool from the live server's `tools/list`, as in step 1. `kg_search`
and the built-in tools are not counted (`kg_search` is about 900 B).

| Role | Allowed ledger tools (bytes) | Up-front `select:` before (bytes) | Up-front `select:` after (bytes) |
|---|---|---|---|
| Oracle | 47 (25,518) | 15 (9,018) | 15 (9,018), unchanged |
| Manager | 33 (18,785) | 14 (8,869) | 9 (6,876) |
| Lead | 35 (23,201) | 15 (12,692) | 10 (10,699) |
| Coder | 25 (15,322) | 12 (8,788) | 6 (3,031) |
| Driver | 10 (5,690) | 8 (4,400) | 3 (1,916) |

The ledger exposes 67 tools: 40,929 bytes by this count, 50,551 bytes with every field of
`tools/list`. The moved tools: `ledger_info` 196 B, `brief_get` 610 B, `brief_ack` 415 B,
`guidelines_get` 323 B, `run_status` 449 B, `score_record` 3,683 B, `handoff_submit` 530 B,
`drive_done` 940 B.

| Change | Per session | 10-agent run (1 Oracle, 1 Manager, 2 Leads, 6 Coders) |
|---|---|---|
| Step 3: start calls in the hook | 1,544 B and 3 ledger calls (Coder, Driver); 1,993 B and 4 ledger calls (Manager, Lead) | 15.2 KB and 30 ledger calls |
| Step 4: Coder end tools loaded at handoff | 4,213 B until the first handoff | 25.3 KB until handoff |
| Step 4: Driver `drive_done` loaded at the end | 940 B until the exploration ends | 940 B |

The calls the hook makes were `brief_get`, `brief_ack` and `guidelines_get`, plus `run_status` for
a Manager or Lead; the templates loaded `ledger_info` but did not call it. Their results still
reach the session, as hook context instead of tool results. At about 4 bytes a token, step 3
saves about 3,800 tokens of schemas across a 10-agent run, re-sent as cached input on every turn.

## Success numbers

| Measure | Today | Target |
|---|---|---|
| Processes for a role session's HTTP plugin servers | 0 (step 0 done) | 0 |
| Cross-run `message_inbox` reads | 0 by test (step 2) | 0 |
| Members stopped with unread mail, `modules` smoke run | Not measured | 0 |
| Coder up-front `select:` (ledger tools) | 3,031 B, 0 start calls (steps 3 and 4; was 8,788 B, 3 calls) | 4,500 B, 0 start calls |
| Manager / Lead up-front `select:` (ledger tools) | 6,876 / 10,699 B (step 3; was 8,869 / 12,692 B) | 7,533 / 11,109 B |
| Tool call median, 1 agent | 8 ms (step 7; 15 ms in step 1) | No slower than step 1 |
| Tool call and hook median, 16 agents | 217 ms and 75 ms at 99% CPU (step 7; 611 ms and 71 ms before it at the same load; 273 ms and 52 ms in step 1 at 70%) | Below 150 ms |
| Calls that fail at 16 agents | 0 (step 1) | 0 |
| Unauthenticated `/mcp`, `/hook`, `/events` calls accepted | 0 by test (step 5) | 0 |
| Ledger idle memory and start (step 7 only) | 24.7 MB; 1.4 s to `server.json` at 96% CPU (step 7; was 79.3 MB and 3.1 s) | Below 35 MB; below 0.3 s |

Every step runs the repo's checks before commit: `uv run pytest`, `uv run pyright`,
`uv run ruff check` and `uv run ruff format --check` in `mcp/`, then
`claude plugin validate --strict .` and `claude plugin validate --strict .claude-plugin/plugin.json`.

## Risks

- Binding in a hook moves `brief_ack`'s refusal from a tool error to hook context. Step 3's
  failed-bind test covers it.
- A Coder that skips the handoff `ToolSearch` gets an error on an unloaded tool, then loads it.
- A stale token in a live session after someone deletes `http-token` gives a 403, which can mark
  `swarm-ledger` as needing auth. Step 5 records the recovery.
- The pool splits state across processes. Step 7 keeps notifications and `on_run_finish` in the
  front, and reads the profile commands from the run's snapshot.
- With `max_workers: 8` the tool and hook pools can run 16 workers of about 20 MB each under load;
  they exit after 60 s idle.

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

## Results: hello smoke run after part 1 (2026-10-08)

Run 2 of commit 083384b, the mod as the only hook transport, compared with the baseline run
of 2026-10-08 before part 1.

| Measure | Baseline | After part 1 |
|---|---|---|
| Duration | 6m 53s | 4m 24s |
| Tool calls | 80 | 67 |
| Oracle | 28 calls, $0.65 | 26 calls, $0.56 |
| Manager | 23 calls, $0.47 | 18 calls, $0.47 |
| Lead | 18 calls, $0.24 (Sonnet) | 16 calls, $0.45 (Opus) |
| Coder | 11 calls, $0.16 | 7 calls, $0.14 |
| Total cost | $1.52 | $1.62 |

The Manager chose Opus for the Lead in the second run and Sonnet in the first; both are on
the approved list. With the Lead on Sonnet the run costs about $1.44.

## Results: modules smoke run after part 1 (2026-10-08)

`smoke.sh --bg --prd modules` on commit 164ec7b passed all 10 checks: 4 phases, 7 files and 16
agents approved and released, 19 tests passed. p1 and p2 ran in parallel, p3 unlocked after p1
and p2 were approved, and p4 after p3. Duration 20m 9s, 147 tool calls, $4.47, against PRD 12's
estimate of about 23 minutes and $6.
