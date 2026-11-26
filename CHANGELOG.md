# Changelog

All notable changes to the `sentinel-swarm` plugin.

## [0.9.0] — 2026-11-26 — codebase-kg 0.18 gates in role sessions, MCP 2026-07-28 checks, hook failure reports

### Upgrading

- Run `/sentinel-swarm:setup` again in each host repo. It writes `commit_check: false` for
  codebase-kg and the updated Coder role file.
- The next launch replaces a ledger server left from 0.8.5.
- codebase-kg 0.18.0 or later is needed for codebase-kg's gates in role sessions. Older
  versions keep working as before.

### Added

- Every role session gets codebase-kg's hook relay (`plugin:codebase-kg:codebase-kg-hooks`),
  taken from codebase-kg's own `strict-mcp-config` command. Without it a role session on
  codebase-kg 0.18 ran no search gate and printed a failure notice on every tool call.
- Watchdog finding `hook_unreachable`: an agent whose hooks reach the ledger only through the
  fallback three times in one interval is reported to its parent, or to the Oracle.
- A swarm role session refuses an MCP elicitation. No user answers at that session, and no
  agent approves its own request. A session that is not a swarm role is unchanged.

### Changed

- Setup writes `commit_check: false` to `.claude/codebase-kg.local.md` in place of
  `post_edit_nudge: false`, which codebase-kg 0.18 no longer reads. Roles update the graph
  with `graph_upsert` and do not run `/codebase-kg:refresh`.
- The ledger's HTTP front refuses an unserved MCP version (-32022) before every other check,
  and `server/discover` and that error list the same versions.
- The front accepts `Mcp-Method` and `Mcp-Name` values with surrounding spaces, and decodes
  the Base64 form of `Mcp-Name`. A percent-encoded name is refused.
- The vendored git hooks pin codebase-kg 0.18.1.

## [0.8.5] — 2026-11-18 — the user's permission mode, lint and build by path, panel fixes

### Upgrading a live run

- Pause the run (`ledger.py pause --reason <text>`), update the plugin, run
  `/sentinel-swarm:setup` in the host repo, then launch again. The launch replaces a
  ledger server left from the older version.

### Added

- `lint_command_by_path` and `build_command_by_path` map a path prefix to a lint or build command, with the same key and longest-prefix rules as `test_command_by_path`. The Coder's shell gate allows every lint entry. A full run builds with `build_command`, then every build entry in prefix order, and records one result with a header per part. `profile_set` takes both maps, and setup reports them.
- The run panel reads its snapshot over `GET /panel` on the ledger server, behind the bearer token, with the launcher as the fallback. It fetches a task's brief only when its row opens.

### Changed

- A role session starts in the permission mode the user has set. When `permission_mode.<role>` names a mode, the launch passes it as `--permission-mode`. Otherwise the launch passes no flag, and Claude Code applies `permissions.defaultMode` from `.claude/settings.local.json`, then `.claude/settings.json`, then the user's `settings.json`. The swarm no longer starts a role in `bypassPermissions` by default. Setup reports each role's mode and the file it comes from.
- Native Copilot workers and the device-queue broker are listed as future features. The read-only Copilot status companion stays.

### Fixed

- An answer typed in the panel's Waiting on you box owes the Oracle a wake-up, so an idle Oracle reads it at once instead of at its next poll.
- Every agent name in the panel opens its detail card. A failed panel action shows its reason, an empty answer says it was not sent, and the answer field takes focus.

## [0.8.4] — 2026-11-15 — fixes from the second live-run bug pass

### Upgrading a live run

- Pause the run (`ledger.py pause --reason <text>`), update the plugin, run
  `/sentinel-swarm:setup` in the host repo, then launch again. The launch replaces a
  ledger server left from the older version. While a run is active, a launch refuses a
  server from another version and names both versions.

### Added

- `test_command_by_path` maps a path prefix to a test command, so a repo with more than one app tests each with its own command. One `tests_run` call runs each command its scope reaches and records one result. `profile_set` takes the map, and setup suggests one when it finds several packages with a test script. `lint_command` and `build_command` are still one command each.
- `claim_file` takes `no_test_reason` for a file with no behaviour to test, such as types, constants, or fixtures. The Oracle reads the reason in `status_tree` and the report.
- `agent_release` on a child name with no agent and no queued spawn voids your unacknowledged brief for it.
- The watchdog's `brief_unstarted` finding reports a brief no child started for.
- `server.json` and `/health` carry the server's version and plugin root, and `POST /shutdown` stops the server.

### Changed

- At a parallelism cap, `agent_spawn` queues the spawn instead of refusing it, and the ledger starts it once a slot frees.
- The new findings of one watchdog pass share one digest directive. `needs_user`, `run_stalled`, and the Driver's findings keep their own.
- Every live agent's mod keeps polling for wake-ups, and takes one addressed to its session that stayed unsent for 60 seconds.
- The hook shim's fallback runs the install that started the ledger server.
- A launch warns when the Oracle's role file model is not the first entry of `models.oracle`.

### Fixed

- The phase gate refused a settled file claimed with no `test_path` whenever the code graph anchored a symbol in it, and returned phases after green runs. It now passes a file whose path is gone, a file with a `no_test_reason`, and a test file claimed with its own path as `test_path`. `file_accept_incomplete` drops a deleted file's graph anchors.
- A re-claimed file left its unacknowledged brief and its dependents bound to the superseded file, and no tool could clear them. A re-claim now carries both to the new file.
- A module returned after its files settled stayed `returned`, so its approved phase never showed done in the run panel. An accepted module review and a phase approval now mark the module approved, and existing ledgers are repaired on upgrade.
- Sessions the ledger released stayed listed and held memory. A re-key, a release, and a replaced Oracle now stop every earlier session of the agent, and the watchdog stops an orphaned session it sees on two passes.
- A completed tool call read as a pending request, and raised a false `needs_user`, when Claude Code replayed the call in an `api-request-blob` transcript record.
- `ledger.py resume` left an idle Oracle asleep when its watchdog Monitor was off. A resume now owes the Oracle a wake-up that its own mod delivers.
- A launch after an upgrade reused the older version's ledger server, so hooks and the server ran different versions.
- A parent whose child was parked under memory pressure read as stuck.
- A wake-up to a session parked for memory, or one the Oracle may not message, raised a `wake_unsent` finding the Oracle could not act on.

## [0.8.3] — 2026-11-15 — the run panel marks only the file that stopped short

### Changed

- In the run panel, only a file that is incomplete or was released shows `!`. Its module, phase, and "all phases" show in progress instead, and return to done once a newer claim of the file is approved.
- The in-progress mark `●` is steel blue (`#5f87af`) instead of yellow, so it no longer matches the Driver's colour or the warning colour.

## [0.8.2] — 2026-11-15 — choose where each notification goes

### Added

- Each place a notification can go is its own switch:
  - `notify: [os, push]` holds the ledger's desktop toast (`os`) and the Oracle's push to the Claude app and claude.ai (`push`).
  - `session_notifications` holds each swarm session's own Claude Code alerts (`alerts`) and pushes (`push`), and both are off by default. The plugin sets them in each session's `--settings`, so a run of many sessions no longer floods the desktop and the browser. The Oracle keeps its push while `notify` includes `push`. Claude Code offers no setting for claude.ai browser alerts from a session on Remote Control, so those may still appear.
- `notify_events` turns each of 11 notification types on or off, or limits it to `os` or `push`: `needs_user`, `run_stalled`, `run_paused`, `pause_reminder`, `run_resumed`, `oracle_unreachable`, `rubric_floor`, `driver`, `phase_approved`, `module_approved` (off by default) and `run_finished`. A type that is off, or whose list leaves out `push`, owes no `PushNotification`.
- New notices for an approved phase (with its commit), an approved module and a finished or closed run.
- Setup lists the `notify_events` keys an existing settings file lacks, ready to paste; it never edits the file. Without a `session_notifications` key, both session switches are off.

## [0.8.1] — 2026-11-15 — the model you name wins, and parked agents resume

### Added

- `ledger.py launch --model <name>` (and `/sentinel-swarm:run`) starts the Oracle on a
  model you name, for that launch only, over its role file's `model`. The launcher prints
  the model the Oracle starts on and why.
- A model you name for any other role, in the PRD or a directive, reaches its agents:
  the Oracle, Managers, and Leads pass it with `brief_create(user_named=true)`.
- A model you name that is not on the role's approved list still runs, with a warning
  that suggests adding it to `models.<role>` for future runs.

### Changed

- Parents choose each child's model from the role's approved list by the task's
  difficulty, and never off the list unless you named the model.

### Fixed

- A parked agent resumed under memory pressure died on each resume. Each attempt
  re-keyed it to a session id that never wrote a transcript, so every later resume found
  "No conversation found". Every resume path now waits for free memory and takes one
  claim, a resume starts from the newest session id that has a transcript, and an agent
  with none left is reported for replacement from its brief. The watchdog keeps one
  `crashed` finding per agent with the count and cause of failed resumes, and records
  why each resume exited.
- A Manager or Lead waiting on working children was reported `stuck` or `blocked`, and the
  change from one to the other filed a second directive. A parent that waits on a working
  child, a queued spawn, or an open `wait_for` raises neither, and the two are one finding.
- `models.oracle: [fable, opus]` had no effect, since the Oracle's role file names
  `model: opus`. Name the Oracle's model with `--model`, or set it in the role file.

## [0.8.0] — 2026-11-15 — fixes from a live run: real user waits, file moves, phase commits

### Upgrading a live run

- Pause the run (`ledger.py pause --reason <text>`), update the plugin, and run
  `/sentinel-swarm:setup` in the host repo before you resume. Setup rewrites the role
  files in `.claude/agents/`, which gain `directive_ack` (Oracle) and `file_move` (Lead);
  `launch` reruns setup only when a role file is missing. The hook shim is unchanged.
- The ledger migrates a 0.7.1 ledger on its first open: it adds the tables `phase_goals`
  and `file_moves` and new columns on `test_runs` and `directives`. A run started on
  0.7.1 resumes.
- A file-scope test run fingerprints only its own files now, so the first handoff of
  each file after the upgrade runs its tests once more. A phase review needs a full run
  that also ran `build_command`, so a full run made before the upgrade does not count.
- Phase approval commits the phase's files on the run branch by default. Set
  `commit_phases: false` to keep committing yourself.

### Added

- `ledger.py pause --reason <text>` and `ledger.py resume` pause and resume a live run
  without waiting for the Oracle.
- Phase approval commits the phase's own claimed files, deleted and moved paths
  included, on the run branch, never on the base branch (`commit_phases`).
- A full test run also runs `build_command`, and `phase_review(accepted)` needs that
  build to pass.
- `file_move` (Lead) and `claim_file(moved_from=...)` keep a moved file one claim: the
  Coder deletes the old paths and re-anchors their nodes, and the claim's test path
  follows the move.
- `phase_add(goal=...)`: the run panel shows a planned phase's goal until its Manager is
  briefed.
- `directive_ack`: the Oracle marks a directive it cannot apply at once as queued with a
  note. `ledger.py directive --status` and the run panel show each of your directives as
  unread, seen, queued, or resolved, and the watchdog flags one left unread.
- `{target}` in `lint_command`, so each Coder lints only its own files.
- The run panel: click an agent for a detail card with its state and its last 8
  transcript steps; your typed answer survives refreshes and the box says whether it was
  sent; the phases not started collapse under one row.
- The watchdog parks Coders idle on a blocker while a spawn waits for memory
  (`memory.park_blocked_minutes`).
- Setup warns about shell operators in the test, build, and lint commands, which the
  Coder's shell gate denies, and shows the `npx --prefix <dir>` form.

### Fixed

- A session that ended its turn `waiting` or `blocked` with nothing pending raised a
  false "needs you" popup; about 30 did in one run. A session waits on you only with a
  pending tool request or a held permission request; otherwise its parent is told.
- A usage-limit stop raised "needs you" for every session, and the run sat after the
  reset. The watchdog pauses the run, resumes it after the reset time the message names,
  and wakes the stopped sessions.
- A stale `claude agents` entry left by a reboot counted as a running Oracle, so the
  resume was refused. A session runs only with a live status or a live process.
- A dead parked session could not be resumed, released, or replaced. `agent_release`
  takes a name, and `brief_create` releases a dead holder of the name.
- A file-scope test run was never reused while other Coders edited, so `handoff_submit`
  looped on "still running".
- A change request stayed on a superseded file and blocked the phase; it moves to the
  replacement file, and a granted `cr_complete` override is honored.
- A Coder deleting a file was denied `Remove-Item` with no pointer to `file_delete`, and
  its handoff was refused while a node anchored the deleted file. The gate names
  `file_delete`, and a deletion handoff drops the deleted file's anchors.
- Stray-process findings named processes that had exited or belonged to the system.
  Each process is verified by pid and start time; a release never stops a process whose
  start time is unknown.
- A replaced Coder on a superseded file kept raising findings; it raises one asking its
  Lead to release it.
- The run panel listed waiting sessions with no request and dropped what you typed on
  each refresh.
- A claim or move could name a path outside the repo or in its records, which a Coder
  could then delete. Claimed paths are plain repo paths outside `.git`, `.claude`, and
  `.sentinel-swarm`, and a phase commit passes them to git as literal paths.

## [0.7.1] — 2026-11-13 — a role starts no higher than your own default mode

### Changed

- A role with no `permission_mode.<role>` setting starts in your own
  `permissions.defaultMode` when a settings file names one, and in `bypassPermissions`
  only when none does. In 0.7.0 it started in `bypassPermissions` whatever your default
  mode was. Only `permission_mode.<role>` raises a role above your default mode.

### Fixed

- A wake-up send that took longer than 15 seconds was recorded as unsent although it
  landed, so the agent sent it again and the watchdog reported `wake_unsent`. The mod
  now marks it sent when it lands and does not send it twice. A slow hook from another
  plugin on `SendMessage` causes the delay.
- CI could not resolve `astral-sh/setup-uv@v10`; the workflow pins `v10.3.3`.
- `test_the_detached_runner_records_the_run_by_itself` failed when the runner finished
  before `tests_run` read the row back.

## [0.7.0] — 2026-11-12 — every role starts in bypass mode, and the gates fail closed

### Changed

- Every role session starts with `--permission-mode bypassPermissions`. Where policy sets
  `permissions.disableBypassPermissionsMode`, it starts with `acceptEdits`. The
  `permission_mode.<role>` setting still overrides the default, and `manual` is accepted.
- A gating hook that cannot reach the ledger retries once, then denies the call and names
  `/sentinel-swarm:setup`. It allowed the call before.
- A phase moves only along `planned`, `unlocked`, `working`, `handed_up`, `approved`. The
  ledger sets `working` at the phase's first `module_add`. The Oracle may re-lock an
  unlocked phase that has not started.
- A Lead's `approve`, `return_work`, and `accept_incomplete` need a submitted handoff.
- A subagent of a role session is gated as its parent session. A swarm-typed session with
  no live agent row is denied its shell calls.
- Costs come from a table of Anthropic's list prices per model version, read on
  2026-11-12: for example Fable 5.2 at 8/40 and Haiku 5.5 at 0.10/0.50, with its higher
  rate for a prompt over 100,000 tokens. The table had one row per family.
- `migrate` runs the schema only when its digest differs from the database's
  `user_version`, so an open with nothing to migrate takes no write lock.
- `post_shell` reports each stray path once per agent.
- The release branch drops `mcp/src/swarm_ledger/server.py`, which needs the dev-only
  fastmcp.

### Added

- The `pricing.<family>` and `watchdog.context_windows.<family>` settings override the
  price table and the context windows without a release.
- Nine indexes on the ledger's hot lookups. An existing ledger gains them on its next open.
- Through a Windows `claude.cmd` launcher, the ledger runs the native `claude.exe` beside
  the shim when it finds it. Otherwise it rewrites the spawn prompt and the resume message
  into plain text, passes the `--mcp-config` and `--settings` JSON as files under
  `.sentinel-swarm/launch/`, and refuses a launch whose other arguments hold a character
  cmd.exe reads.

### Fixed

- A deferred spawn raised a plain error that callers read as a failure; it now raises
  `SpawnDeferred`.
- A run's lock is released as soon as the run closes, and `release_file` checks the
  module that holds the file.
- `attempt_record` classifies an attempt once and compares against the kept version. A
  failed `cr_verify` needs newer evidence. A score set must rate at least one dimension.
  A deferral decision is final.
- `phase_add` checks its dependencies, a brief check is scoped to its run, and
  `run_pause` refuses an empty list.
- `resume_parked` re-lists sessions before it resumes, and a parked row counts as stopped.
- Every mod handler catches its own errors, the panel timer is guarded, and the mod
  re-reads the ledger's port and token after a failed post.
- A subagent's `SessionStart` no longer changes its parent's adopted role.

## [0.6.1] — 2026-11-01 — the mod is declared unsupported under Codex

### Fixed

- Codex failed to parse `hooks/hooks.json` ("unknown field `modules`") at every start, because the
  file names the Claude Code mod `register.tsx`, which Codex cannot load. A Codex manifest,
  `.codex-plugin/plugin.json`, now names `.codex-plugin/hooks/hooks.json` instead: it declares no
  hooks and states that the run panel and role-session hooks are not supported under Codex. Claude
  Code is unchanged.

## [0.6.0] — 2026-10-30 — answer permission prompts from the run panel

### Added

- The run panel answers a role session's permission prompt. When Claude Code would ask
  about a call in a role session, the mod's `tool.check` hook holds it, records a
  `permission_requests` row, and waits for the panel's **Allow** or **Deny**. After
  `permission_hold_seconds` (default 300; 0 turns holding off), or on any error, Claude
  Code shows its own prompt. Questions and plan approvals stay in agent view.
- `ledger.py permission --id <n> (--allow|--deny) [--note]` answers a held request.
- The watchdog reports a held request as `needs_user`, with its notification and
  reminders, and does not report the held session as stuck.

### Changed

- The ledger applies its schema on every open, so a ledger from an earlier release gains
  new tables, such as `permission_requests`, without being deleted. A release that adds
  a column to an existing table still needs a fresh ledger.

### Fixed

- The mod type-checks against the Claude Code 2.1.314 types: the gate loop passed a
  matcher of type `string | RegExp` where the API takes a tool name.

## [0.5.2] — 2026-10-30 — notice a blocked session

### Fixed

- The watchdog and the run panel treat a session whose `claude agents --json` status or
  state is `blocked` as waiting on the user, as they treat `waiting`. A background
  session idle on a question to the user showed `state: blocked` and raised no
  notification.

## [0.5.1] — 2026-10-30 — notice every session that waits on you

### Fixed

- The watchdog reports a session as waiting on the user whenever `claude agents --json`
  shows its `status` or `state` as `waiting`, whatever its `waitingFor` text says. A
  prompt type the watchdog did not know raised no notification before.
- A session blocked on the user repeats its OS notification every `remind_minutes` until
  it stops waiting. It notified once before.

## [0.5.0] — 2026-10-30 — fixes for the stalls in the 0.4.0 runs

Fixes every issue that the two 0.4.0 runs of 2026-10-28 and 2026-10-29 found. See
`plans/swarm-run-issues.md` for the evidence and `knowledge/prd/16-open-items.md` for
what still needs a live run.

### Added

- The watchdog files a `needs_user` finding when a role session waits on a permission
  prompt or a question, with the request text from its transcript. It files a
  `run_stalled` finding when no agent works and no wake-up is owed. Both notify the user.
- `user_ask`, the Oracle's way to ask the user in a background run. The mod's `pre_ask`
  gate denies `AskUserQuestion` in a background session.
- A `permission_mode.<role>` setting. Role sessions inherit the user's permission mode
  by default, and setup reports each role's starting mode.
- `run_pause(clear_check=...)`: the watchdog resumes a paused run once its check passes.
  Every pause and `needs_user` directive notifies the user, with reminders every
  `watchdog.remind_minutes`.
- A detached test runner (`ledger.py testrun`). `tests_run` returns within 20 s with the
  result or a `running` state, and the caller gets a wake-up when the result is recorded.
  Settings `test_timeout_s`, `full_target`, `test_command_by_scope`, `allowed_skips` and
  `slow_tests`.
- `file_delete`, `file_accept_incomplete`, `run_close` and `ledger.py close`, product
  claims for generated files (`claim_file(product_of=)`), and a `commands` setting of
  named host commands the Coder's and Manager's shell gates allow.
- `brief_create(replaces=)`, and re-pointing of live children to any replacement parent.
- The `parked` agent state, release of parked Managers and Leads whose work is done, and
  release of a released session's process tree with a `stray_process` finding.
- Machine-wide spawn admission across every live swarm, and a `parallelism_cap` default
  from total memory.
- A "Waiting on you" section in the run panel, with the request under the agent's row
  and an answer field for the Oracle's questions.

### Changed

- The role templates carry no `permissionMode`.
- A Lead may update a graph node whose files are its module's or unclaimed. The Oracle
  holds `graph_upsert` for nodes no module owns, and a Coder may anchor its own file on
  any node. `graph_upsert` refuses a description over 240 characters.
- `release_file` refuses while the owning Coder is live.
- The run panel dims ended, parked and not-started rows, shows a file task by its file
  name, and shows the run's PRD on the Oracle's row until the run has an outcome.
- `scripts/smoke.sh` uses its own marketplace name on each run.

### Fixed

- Method anchors (`path#Class.method`) pass the handoff graph check.
- A binary file hands off without being decoded.
- A released claim on a missing path, and a change request on a file with no tests, no
  longer hold a module open.
- Spawns, resumes and wake-ups retry when `~/.claude.json` fails to parse, and the run
  pauses only if it still fails.

## [0.4.0] — 2026-10-27 — run panel

### Added

- A run panel: a side pane that shows each run as a tree of agents, with each agent's
  phase, module, or file as a task. A task's status rolls up from the work below it, and
  clicking a task shows its brief and latest review. The panel opens in any repo; with no
  active run it says how to start one and lists the repo's past runs. See
  `knowledge/prd/17-run-panel.md`.
- The `open_panel` and `swarm_status` function tools, which other plugins can call to open
  the panel and to learn whether a run is active in a repo.

### Changed

- The mod is `hooks/register.tsx`, with the panel's pure helpers in `hooks/panel.ts`.

## [0.3.1] — 2026-10-23 — release under the intended version number

### Changed

- The code is the same as 0.3.0. Install 0.3.1 instead of 0.3.0; see 0.3.0 below for what
  changed since 0.2.2.

## [0.3.0] — 2026-10-23 — cleanup of dead code, duplicates, and the ledger upgrade path

### Removed

- The ledger no longer upgrades a ledger from an earlier release. A ledger created before
  0.3.0 can lack columns and tables, and the ledger fails on them. Delete
  `.sentinel-swarm/ledger.db` in the host repo to start fresh.
- `directive_submit` no longer accepts the `user-chat` and `outside-session` spellings. Use
  `user_chat` and `outside_session`.
- The `tracking` setting, which nothing read, and the `agents` columns `phase_at_start`,
  `context_pct_peak`, `context_pct_at_end`, and `duration_ms`, which nothing wrote.
- The `agent_type` argument of `brief_ack`.

### Changed

- The ledger code is split into smaller modules (`report`, `messages`, `issues`, `host`,
  `queries`, `paths`), and duplicated helpers in the ledger, the hooks, the watchdog, the
  server, and the tests are merged. The tool catalog is unchanged except for `brief_ack`.
- `same_path` returns false for an empty path or one it cannot read, `parent_chain` stops
  on a parent cycle, and the report's duration no longer fails on a malformed timestamp.
- The finished plans are folded into the PRD, and CI runs on pushes to `main` and on pull
  requests only.

## [0.2.2] — 2026-10-14 — role sessions reach HTTP plugin servers by manifest path

### Fixed

- A role session connects to a plugin's HTTP MCP server directly when the plugin's
  `plugin.json` names its servers by path (`"mcpServers": "./mcp/claude.mcp.json"`), as
  codebase-kg, cartographer, a11y and the three driver plugins do. Before, only a root
  `.mcp.json` or an inline `mcpServers` object counted, so those roles fell back to the
  stdio shim. A path that leaves the plugin folder is ignored. The entry keeps its
  `headersHelper`, so Claude Code runs it in the role session and a driver server gets the
  session's owner id. A plugin installed in a folder whose path holds `"`, `$`, a backtick
  or a newline keeps the stdio shim, so no broken helper command is written.
- The `.sentinel-swarm/hook.py mcp` shim reads a plugin's servers from a manifest path or
  list too, so the stdio fallback works for those plugins.

## [0.2.1] — 2026-10-08 — tool catalog argument descriptions and Linux and macOS tests

### Fixed

- The ledger's tool catalog lists an optional argument's description at the argument's
  top level, the same on every Python version. `module_add`, `brief_create`, `claim_file`,
  `deferral_propose` and `agreement_decide` showed it inside a nested `anyOf`.
- The test suite passes on Linux and macOS: the tests expect `python3` as the MCP
  command and POSIX quoting for a test target there, and supply a stand-in `notify-send`
  on Linux.

## [0.2.0] — 2026-10-08 — public release

### Changed

- The plugin is public and installs from the `alexk413x` marketplace:
  `claude plugin install sentinel-swarm@alexk413x`. That marketplace installs from the
  `release` branch, which `.github/workflows/release.yml` builds from `main` without
  development files. The self-hosted `.claude-plugin/marketplace.json` is removed.
- The licence changes from proprietary to the no-resale licence: Apache 2.0 terms plus the
  no-resale and prohibited-use conditions.
- The plugin's mod (`hooks/register.ts`) is the only hook transport. It runs every role's
  ledger hooks inside the role's own session, in about 6 ms per hook, and falls back to the
  `.sentinel-swarm/hook.py` shim when the ledger server does not answer. The role files carry
  no command hooks, setup strips any it finds, and the launcher refuses to start unless
  `claude plugin list` shows sentinel-swarm enabled. Requires Claude Code 2.1.294 or later.
- The mod pays owed wake-ups with `$.session.send`, by session id, and puts the recipient's
  unread messages into the wake-up, so the recipient spends no turn on `message_inbox`.
- The ledger server is a standard-library HTTP front with an elastic pool of worker
  processes (`max_workers`, default 8). It serves `/mcp`, `/hook` and `/health` from a tool
  catalog checked against `server.py`. Idle memory drops from about 80 MB to 25 MB.
- The ledger imports only the standard library and runs on Python 3.9 or newer. The skills,
  the shim and the server start it with `<python> -I -S mcp/launch/ledger.py <module>` on the
  base interpreter, so it needs no venv, no `uv` and no install step. A small parser reads
  the YAML frontmatter of the settings file and the role files in place of PyYAML.
  `ledger.py serve --detach` starts the server in the background.
- Each repo's ledger keeps a bearer token in `.sentinel-swarm/http-token`. The server answers
  403 to a remote caller, a wrong `Host`, a foreign `Origin`, or a missing or wrong token.
- Hooks and ledger gates enforce the rules that were prompt text only: dependency order and
  contracts, brief scope, message peers, dispute arbitration, agent names, deferral levels,
  phase unlock order, the Driver's build command and `map-test` order, graph gaps, and the
  brief re-read before a handoff.
- The swarm-events channel, the `/events` route and `wake_transport` are removed. The ledger
  refuses a registered session every tool but `brief_ack` and the identity-free tools.
- Role sessions connect to a plugin's HTTP MCP server directly, and drop the codebase-kg
  tools that moved to its CLI. `graph_upsert` runs codebase-kg on the base interpreter.
- A Lead's `graph_upsert` takes only nodes that span two or more of its module's files.
- The PRD in `knowledge/prd/` is the maintained specification. Items the code does not
  implement yet are marked **(needs implementation)** and listed in `16-open-items.md`.
- codebase-kg installs from the `alexk413x` marketplace as `codebase-kg@alexk413x`.

## [0.1.0] — 2026-09-28 — tooling for Claude Code 2.1.284

### Changed

- Hooks answer through the running ledger server (`POST /hook/<event>`) and fall back to a
  subprocess, fail-closed. Activity bookkeeping runs as an async hook.
- The plugin declares no MCP server. A directive command steers a live run from an ordinary
  session.
- Each role's `tools` list names only its own ledger tools, and `score_record` carries its
  rubric keys in the schema. Read-only tools carry `readOnlyHint`.
- `run_status` and `status_tree` leave out the PRD and the settings snapshot. `tests_run`
  returns a capped summary, `test_run_get` the full output, and a passing run on an unchanged
  tree is reused.
- Every role runs at `medium` effort by default, and a brief can raise a child's effort. The
  Coder's prompt cache TTL defaults to 5 minutes.
- The Oracle and Managers see the run's elapsed time, and an optional time budget, on their
  wake-ups.
- The Driver uses cartographer's `map-driver` and `map-reviewer`.
- Setup denies the roles as subagents. `run` and `resume` are user-invoked only.
- A benchmark (`scripts/bench.sh`) with held-out PRDs and hidden acceptance tests, run
  metrics from `checklist --json`, and a skill-trigger eval suite (`evals/`).
- Requires codebase-kg `>=0.8.2`. The plugin runs in Claude Code only.
