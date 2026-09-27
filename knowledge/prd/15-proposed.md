# Still (proposed)

These items await Alex's review. Items marked "not built" are design only.

Principles and roles:

- No state lives only in an agent.
- The name patterns other than the Driver's, and a name as the unique address stored in
  the registry.
- The registry records each agent's model, and the Oracle's from its agent file.
- Escalation can raise a fresh Coder's model in rounds 2 and 3.
- Arbitration duties: the Oracle between Managers, a Manager between Leads and module
  contracts, a Lead between Coders.
- The Oracle's options on a low score or a failed full run.
- A Manager does not direct a Coder except through its Lead.
- "File" means any project file the run touches.
- The join-point wording, helpers-first ordering inside phases and modules, and
  contracts before implementations.
- The Lead validates an accept-as-incomplete reason; it uses no fix attempts; every
  deferred item is tracked.
- A lower layer adds guideline detail and never contradicts a higher one; a departure is
  recorded at hand-up.
- The Manager's round-2 resources, including structural changes.
- An issue a Manager finds starts at round 2, and one the Oracle finds at round 3.
- `issue_escalate` refuses an issue already at the last round.
- The responsible-level table for deferrals and scope changes.
- The scope check on `agreement_decide`: a Lead in its module, a Manager in its phase,
  the Oracle in its run.
- Overrides: recorded with a reason, narrow and single-use, never used by the Oracle to
  write a file.
- While the run waits on the user, the phases the problem does not block keep going.
  Built as a scoped pause: `run_pause(reason, phases=[...])` and `phase_resume`.
- A pause notification states what the user must fix, and that "continue" or
  `/sentinel-swarm:resume` continues the run. A Driver notification is the ledger's
  one-line message, which the Oracle sends unchanged.
- The Oracle asks the user once about a `needs_user` directive, and tells the user which
  session to open when the watchdog reports one waiting on a permission prompt.
- The final report's contents.
- The git workflow's examples and consequences: no worktrees, one graph copy, versions
  saved in the records folder.

Rubric:

- The "did not agree" definition, the architecture and code structure split, the 1-to-10
  scale, the rating table, reasons below 9, issues at 4 or lower, and the default
  thresholds.
- The improvement loop: saved versions, improved, plateau, and regression, restore on
  regression, and the attempt counting rule.
- A file that ends the last round at or above the floor passes with each shortfall
  recorded. "The last round" is the file's attempts reaching `rounds * attempts_per_round`.
- No self-approval, and tool output as supporting evidence only.

Ledger and sessions:

- Setup writes a new role file's MCP server `command` as `python` on Windows, and `python3` elsewhere when `python3` is on `PATH`.
- The records folder location, `.git/info/exclude`, the report in the records folder,
  WAL settings, same-machine resume, append-only events, and the run's settings
  snapshot.
- The registry columns, the lifecycle stage names, and the run analytics measures. The
  report's Measures section summarizes them; `analytics_query` reads the raw tables.
- The session mechanics: `agent_spawn`, owed wake-ups and `next`, the Stop hook's
  wake-up rule, `agent_resume`, release that stops the session, the ledger's HTTP server,
  and the project agent file details.
- `parallelism_cap` counts the run's live agents and other swarms' live sessions, matched by the swarm session-name shape `<slug>-r<run_id>-<name>`; `role_parallelism_cap.<role>` caps a role.
- A machine-level lock per repo under `<CLAUDE_CONFIG_DIR or ~/.claude>/sentinel-swarm/locks/`, keyed by the main checkout (`git rev-parse --git-common-dir`), holding the run id and server pid; a dead pid's lock is taken.
- `pre_send_message` refuses a `SendMessage` to a session outside the caller's run; the valid targets are every session name recorded for the run, live or not.
- `repo_check`, `repo_branch_create`, and the Manager spawn gate on `repo_check`.
- `module_review`, `phase_review`, and the hand-up and approval gates, apart from the
  departure rules Alex decided.
- The change request tool set including `cr_list`, and the departure details: refusals
  on `return_work` and `accept_incomplete`, a pushback returning a submitted handoff, a
  pushback on a departure with no file, and a departure the Oracle records being signed
  off at once.
- The directive mechanics: fields, the Oracle as the only reader, the outcomes and the
  refusal of any other value, safe points, the source and outcome in the report's Directives section, and each
  reply's sender in its Notifications section.
- A `needs_user` directive stays open; the Oracle's Stop hook lets the Oracle stop and
  the watchdog reports no stall while it waits; a `reply_to` reply resolves it.
- Waiting on running children is a valid stop for the Oracle.
- A hook blocks through JSON and exits 0; hooks ignore callers the registry does not
  know, or that have ended, except `pre_write`'s records-folder rule, `pre_ledger`, and
  `pre_monitor`'s `agent_type` check.
- `pre_ledger` answers `allow`, so a ledger call never asks for permission. It stamps
  the hook input's `agent_id`, else its `session_id`, and removes `agent_id` for the
  five tools that take no identity.
- `agent_spawn` refuses a child whose phase is paused. A wake-up is owed only to a live
  agent with a session name, never to the caller. When `claude agents --json` fails,
  `next` names both the `SendMessage` call and `agent_resume`. The Oracle's Stop hook
  does not count phases under a scoped pause as pending work.
- `-p no:cacheprovider` in the test command.
- The `graph_upsert` mechanism, multi-file nodes through the Lead, graph search at each
  role's level, recording graph gaps, and setup building a missing graph.
- The watchdog mechanics in "Watchdog", apart from Alex's rules.
- Plugin features: `userConfig` prompts for the test command at install. `monitors`, `subagentStatusLine`, and a plugin `agent` are not used, because each would run in every session of a user who installs the plugin.
- The smoke test's ledger checklist, automated as `python -m swarm_ledger.checklist`: success means an outcome starting with `success`, `succeeded`, or `complete`, in any case; files must be `approved`; a live watchdog finding fails and a cleared one warns; the session and server checks warn when `claude` is missing. The brief-completeness check is not automated.

Built on 2026-09-25, choices Alex has not reviewed:

- Module and phase reviews score completeness, integration, and open items, 1 to 10 with a reason below 9, stored in the review's `details_json`.
- Blind scoring hides from the Lead the issues a Coder's self review opened (those with a dimension and criterion, opened by a Coder) until the Lead scores the file's current handoff.
- `issue_escalate` and a round advance both owe the receiver a wake-up.
- `override_grant` refuses a target that names the Oracle. It accepts any rule name, but only the write and shell gates consume a grant, so a grant for another rule is recorded and never used.
- A `reply_to` directive resolves the directive it answers whatever its outcome; `directives.question` keeps a `needs_user` question after a later resolve.
- The report's decided deferrals, notifications, final test run, and measures sections, and a report per run with `report.md` as the latest copy.
- The report's phase sections list the dimensions of a file that passed at the floor; its review sections list each review's outcome, notes, and scores; a Driver's cost shows only under Agents; a cost the ledger cannot price shows as `unknown`, and so does any total that includes it.
- An open departure a Coder recorded before its handoff joins that handoff. A Lead pushback on a file already released resumes the chain like a Manager pushback.
- Directive sources `user_chat` and `outside_session`, with the old spellings accepted and normalized.
- Per-role `effort` (the `--effort` flag) and `prompt_cache_ttl` (the `promptCacheTtl` setting).
- Smoke runs install as `0.0.1-dev.<epoch seconds>` and remove older dev copies.
- The Driver's details: its approved models, `driver: [sonnet, opus]`; `drive_request` picks the first approved Driver model; the Driver has the `Skill` tool to run cartographer's `map-test` and `map-explore`; its shell runs only `build_command`; `post_shell`'s claim check stays Coder-only.
- The exact stop-rule math in `compute_loop_status` (`mcp/src/swarm_ledger/drive.py`): the attempt streak and total, the three-exploration stall, the regression check, fixes causing bugs (compared by `area`), and ping-pong (across the last 4 explorations).
- The Driver's fix-loop details: the Oracle groups findings by module, one fix phase per module, and asks a running module for a new Coder through its Manager and Lead; the Driver tests the build it made at the start of the exploration, never a dev server; a failed build is a finding that ends the exploration; the Driver replays recorded routes with `map-test` before it runs `map-explore` on the focus list. Not built: each fix Coder's brief body carries the finding's evidence and re-claims the file.
- `drive_request` refuses an empty focus list. `drive_issue` refuses a finding with no fingerprint or title, or with a severity other than blocker, major, or minor.
- The report's Explorations section marks each finding as recurred in a later exploration or not seen again; the ledger does not link a finding to the fix that closed it.
- The watchdog reports a Driver whose check-in is overdue (`driver_overdue`) and does not report it as stuck or spinning.
- A role's session name carries the run's UTC start time, `<slug>-r<run>-<MMDDHHMM>-<name>`, because a stale Remote Control entry from an earlier smoke run kept the same name and `SendMessage` refused the ambiguous name.

Built on 2026-09-27, wake-up delivery through a channel (see "Wake-up delivery" in [05-sessions.md](05-sessions.md)), choices Alex has not reviewed:

- One function, `route_wakeup` in `wake.py`, builds every wake instruction, in the order: the `sendmessage` setting, a stopped target, a supported target, then `SendMessage`. Only a wake-up with a `wakeups` row is ever pushed.
- `agents.channel` (`none`, `launched`, `confirmed`) is recorded when the target's bridge connects, and at `run_start` and `agent_spawn` for a bridge that connected first. Only a launch that carries the channel lists `swarm-events`, so a connected bridge means `launched`.
- Only an interactive Oracle launch carries the channel. The launcher then adds `swarm-events` to `--mcp-config`, merges `server:swarm-events` into the development-channels flag, and prints how to answer Claude Code's prompt.
- The bridge is a standard-library stdio MCP server, `python -m swarm_ledger.bridge`, run through `hook.py channel`. It declares only `claude/channel`, has no tools, starts listening after `notifications/initialized`, and reconnects to `GET /events?session=<id>` 2 seconds after a drop, reading the ledger's port from `server.json` again each time. It forwards each event's `content` and its `meta` keys that are identifiers.
- `/events` is newline-delimited JSON with a ping line after 15 seconds without an event.
- A push is confirmed by a user turn from the channel in the target's transcript (origin kind `channel`, a `turnOrigin` or `promptSource` of `channel`, or the `swarm-events` tag), outside a sidechain, that carries the pointer or the `wakeup_id`, stamped no earlier than 5 seconds before the push.
- The member Stop hook waits for confirmation until 30 seconds after the push, then blocks with the `SendMessage` call. A waiting check closes the gap where a sender stops once and never runs its Stop hook again.
- `next` for a pushed wake-up reads "Nothing to send: the ledger delivered this wake-up to <name> through its channel."
- The watchdog reports `wake_unconfirmed` on the target 2 minutes after an unconfirmed, unsent push.
- A `wake_transport` value other than `channel` or `sendmessage` reads as `channel`. Case, `_`, and `-` are ignored, so `send_message` reads as `sendmessage`.
- The Oracle's `Monitor` listener stays as it is, whether or not the Oracle has a channel (plan question 3).
- A future device-queue broker is a second event kind on `swarm-events`, not a separate server (plan question 4). Every event carries a `kind`; a wake-up is `wakeup`. The broker is not built.
- `plugin.json` has no `channels` entry. See "Channels" in [16-open-items.md](16-open-items.md).

Built on 2026-09-27, Driver notifications (see "Driver notifications" in [02-run-lifecycle.md](02-run-lifecycle.md)), choices Alex has not reviewed:

- Two delivery paths, both on by default: an OS notification from the ledger server itself, and a `PushNotification` the Oracle owes. `notify: [os, push]` in the settings file picks them; `[]` turns both off. A single value reads as a one-item list, and an empty or unset key keeps both.
- The events: `drive_done` with `blocked`, a new stop-rule directive (a stall first), or a clean exploration, at most one per exploration in that order; and a new `crashed` or `driver_overdue` watchdog report on a Driver session. `driver_overdue` stands in for `stuck`, which the watchdog never reports on a Driver.
- One `notifications` row per run and event key (`drive_done:<request_id>`, `directive:<directive_id>`, `drive_unavailable:<directive_id>`, or `issue:<issue_id>`), so a repeat notifies no one.
- `drive_done(request_id, blocked=None)` is the Driver's path to report that it cannot continue. It refuses `blocked` until the exploration has a finding.
- The message texts, capped at 199 characters with backticks and asterisks removed. A stop-rule message names the first new rule and counts the rest as "(+N more)".
- The Windows toast runs through Windows PowerShell 5.1 with `-EncodedCommand` under the AppUserModelID `SentinelSwarm.Notifications`, which each toast registers under `HKCU\Software\Classes\AppUserModelId`; macOS uses `osascript`; Linux uses `notify-send` when installed and logs a skip otherwise. Each runs in a daemon thread with a 30-second timeout, and a failure goes to the server log.
- Levels: a stop rule and an overdue Driver are warnings (amber eye); a blocked or crashed Driver is an error (red eye); a clean exploration is a success (green eye).
- The Oracle's watchdog `Monitor` prints each owed push once, which wakes the Oracle.
- The Oracle's Stop hook blocks on an owed push even while the run is paused, while children work, while a directive waits on the user, and after `run_finish`. A stop that follows its own block passes, as for owed wake-ups.
- Any `PushNotification` call from the Oracle pays one debt, whatever its result: the one whose message matches, or else the oldest.
- `PushNotification` is in the Oracle's tools only.

Built on 2026-09-27, the eight rules the PRD audit found unbuilt, choices Alex has not reviewed:

- `drive_unavailable(reason)` is callable by the Driver for its own open exploration
  or by the Oracle. It abandons any open exploration and releases its Driver. What it
  records changed with the 2026-09-27 decision; see the next list.
- `score_record` refuses a score set whose `applicable` does not list all nine
  dimensions, and a dimension marked not applicable with an empty reason.
- Releasing a Driver sets its open exploration to `abandoned`, with `done_at`. An
  abandoned exploration never counts as clean and is not a wave in the stall count; its
  findings still count as seen. `clean_latest` reads the latest `done` or `abandoned`
  exploration.
- A fix names its findings with `brief_create(..., finding_ids=[...])`, stored in
  `briefs.finding_ids_json`. A child's brief inherits its parent's list unless it names
  its own. `brief_create` and `agent_spawn` refuse a finding of another run, a finding
  whose fingerprint hit a per-finding stop (for the rest of the run), a finding that
  reaches a per-finding stop in the open exploration, and a finding whose fingerprint
  or area an open pattern directive names.
- `drive_stops` holds one row per fingerprint or area a stop directive names, with its
  kind: `finding`, `pattern`, or `stalled`. A regression and ping-pong name their
  fingerprints and areas; fixes causing bugs names the new findings in the fixed areas.
  Stop directives are deduplicated by reason through this table, and by the old
  one-line body for a directive filed before it.
- A stop directive's body lists the evidence after its first line; a per-finding stop's
  notification adds the fingerprint and the explorations it recurred in.
- A stall's directive and notification list every finding of the latest exploration as
  what is left, and the stall is the first stop rule in the exploration's notification.
- The report's Explorations section lists each `[driver-stop]` directive with its evidence, its
  Notifications section lists every `notifications` row, and its Directives section
  shows only the first line of a `driver` directive.
- An issue ends round 3 when a plateau or a regression brings an issue at round
  `rounds` to `attempts_per_round` attempts. It is below the floor when the kept Lead
  review (the earlier one after a regression) has the issue's dimension below `floor`
  or its criterion below `criterion_floor`, or, for an issue with no dimension, any
  dimension or criterion. `attempt_record` then records an error notification,
  `issue:<issue_id>`, and returns it in `notifications`.

Built on 2026-09-27, Alex's two decisions of 2026-09-27 (a Driver that fails to load
blocks the run; fixes always name their finding ids), choices Alex has not reviewed:

- `drive_unavailable(reason)` files an open directive from source `driver`, sender the
  caller's name, body `[driver-unavailable] <reason>`, and records an error
  notification with event key `drive_unavailable:<directive_id>`. A second call while
  that directive is open returns it and files nothing new.
- The outcome mapping. The ledger reads the run's latest `[driver-unavailable]`
  directive. Open, including `needs_user`: `drive_request` refuses and names the
  directive, and `run_finish` refuses as for any open directive. `declined`:
  `drive_request` refuses for the rest of the run, and the `run_finish` Driver gate
  passes with no exploration. `applied` or `scheduled`: explorations resume, and
  `run_finish` again needs a clean exploration or a resolved stop rule. A user reply
  through `reply_to` closes the directive but keeps the outcome `needs_user`, which
  counts as resumed until the Oracle resolves the directive `declined`.
- The migration drops `runs.driver_unavailable_at` and `driver_unavailable_reason`. A
  run that had them set gets a resolved `declined` `[driver-unavailable]` directive, so
  it keeps skipping explorations. On SQLite older than 3.35, which cannot drop a
  column, the columns stay unused and are cleared.
- The report's "Stop rules" list shows only `[driver-stop]` directives, so a Driver that
  fails to load shows only in the Directives and Notifications sections.
- An open finding is the latest finding row of a fingerprint that no later `done`
  exploration left out and that no per-finding stop rule closed, including one that
  reaches the stop in the open exploration. A pattern pause does not close a finding.
- Only the Oracle must pass `finding_ids` while a finding is open. A Manager or Lead that
  omits it inherits its own brief's list. `drive_request` passes `[]` for the Driver's
  brief. An empty list is stored as no list.
- The refusal lists every open finding as `<id> <title>`. An unknown id is refused from
  any role. A named finding that a later exploration cleared is accepted.
- `brief_get` returns `findings`: the id, fingerprint, title, severity, and area of each
  finding its brief names. `status_tree` returns `open_findings` and `fixes`, each brief
  that names findings, with its child's name and role and each finding. The report's
  Explorations section shows each finding's id and a "Fixes" list of the same briefs.
