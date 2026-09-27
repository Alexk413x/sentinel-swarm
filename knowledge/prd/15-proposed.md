# Still (proposed)

These items await Alex's review. Items marked "not built" are design only.

Principles and roles:

- No state lives only in an agent.
- The name patterns, and a name as the unique address stored in the registry.
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
- The responsible-level table for deferrals and scope changes.
- The scope check on `agreement_decide`: a Lead in its module, a Manager in its phase,
  the Oracle in its run.
- Overrides: recorded with a reason, narrow and single-use, never used by the Oracle to
  write a file.
- While the run waits on the user, the phases the problem does not block keep going.
  Built as a scoped pause: `run_pause(reason, phases=[...])` and `phase_resume`.
- A notification states what is blocked, what the user must do, and what resumes.
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
  refusal of any other value, safe points, and the source and sender in the report.
- A `needs_user` directive stays open; the Oracle's Stop hook lets the Oracle stop and
  the watchdog reports no stall while it waits; a `reply_to` reply resolves it.
- Waiting on running children is a valid stop for the Oracle.
- A hook blocks through JSON and exits 0; hooks ignore callers the registry does not
  know.
- `-p no:cacheprovider` in the test command.
- The `graph_upsert` mechanism, multi-file nodes through the Lead, graph search at each
  role's level, recording graph gaps, and setup building a missing graph.
- The watchdog mechanics in "Watchdog", apart from Alex's rules.
- Plugin features: `userConfig` prompts for the test command at install. `monitors`, `subagentStatusLine`, and a plugin `agent` are not used, because each would run in every session of a user who installs the plugin.
- The smoke test's ledger checklist, automated as `python -m swarm_ledger.checklist`: success means an outcome starting with `success`; files must be `approved`; a live watchdog finding fails and a cleared one warns; the session and server checks warn when `claude` is missing. The brief-completeness check is not automated.

Built on 2026-09-25, choices Alex has not reviewed:

- Module and phase reviews score completeness, integration, and open items, 1 to 10 with a reason below 9, stored in the review's `details_json`.
- Blind scoring hides from the Lead the issues a Coder's self review opened (those with a dimension and criterion, opened by a Coder) until the Lead scores the file's current handoff.
- `issue_escalate` and a round advance both owe the receiver a wake-up.
- `override_grant` refuses a target that names the Oracle; only the `write` and `shell` rules are ever overridable.
- A `reply_to` directive resolves the directive it answers whatever its outcome; `directives.question` keeps a `needs_user` question after a later resolve.
- The report's decided deferrals, notifications, final test run, and measures sections, and a report per run with `report.md` as the latest copy.
- Directive sources `user_chat` and `outside_session`, with the old spellings accepted and normalized.
- Per-role `effort` (the `--effort` flag) and `prompt_cache_ttl` (the `promptCacheTtl` setting).
- Smoke runs install as `0.0.1-dev.<epoch seconds>` and remove older dev copies.
- The Driver's details: `drive_request` picks the first approved Driver model; the Driver has the `Skill` tool to run cartographer's `map-test` and `map-explore`; its shell runs only `build_command`; `post_shell`'s claim check stays Coder-only.
- The exact stop-rule math in `compute_loop_status` (`mcp/src/swarm_ledger/drive.py`): the attempt streak and total, the three-exploration stall, fixes causing bugs, and ping-pong.
- The report's Explorations section marks each finding as recurred in a later exploration or not seen again; the ledger does not link a finding to the fix that closed it.
- The watchdog reports a Driver whose check-in is overdue (`driver_overdue`) and does not report it as stuck or spinning.
- A role's session name carries the run's UTC start time, `<slug>-r<run>-<MMDDHHMM>-<name>`, because a stale Remote Control entry from an earlier smoke run kept the same name and `SendMessage` refused the ambiguous name.

Built on 2026-09-27, wake-up delivery through a channel (`plans/channel-wake-delivery.md`), choices Alex has not reviewed:

- One function, `route_wakeup` in `wake.py`, builds every wake instruction, in the order: the `sendmessage` setting, a stopped target, a supported target, then `SendMessage`. Only a wake-up with a `wakeups` row is ever pushed.
- `agents.channel` (`none`, `launched`, `confirmed`) is recorded when the target's bridge connects, and at `run_start` and `agent_spawn` for a bridge that connected first. Only a launch that carries the channel lists `swarm-events`, so a connected bridge means `launched`.
- Only an interactive Oracle launch carries the channel. The launcher then adds `swarm-events` to `--mcp-config`, merges `server:swarm-events` into the development-channels flag, and prints how to answer Claude Code's prompt.
- The bridge is a standard-library stdio MCP server, `python -m swarm_ledger.bridge`, run through `hook.py channel`. It declares only `claude/channel`, has no tools, starts listening after `notifications/initialized`, and reconnects to `GET /events?session=<id>` 2 seconds after a drop. It forwards each event's `content` and its `meta` keys that are identifiers.
- `/events` is newline-delimited JSON with a ping line after 15 seconds without an event.
- A push is confirmed by a user turn from the channel in the target's transcript (origin kind `channel`, or the `swarm-events` tag) that carries the pointer or the `wakeup_id`, stamped no earlier than 5 seconds before the push.
- The member Stop hook waits for confirmation until 30 seconds after the push, then blocks with the `SendMessage` call. The plan let a push younger than 30 seconds pass. A waiting check closes the gap where a sender stops once and never runs its Stop hook again.
- `next` for a pushed wake-up reads "Nothing to send: the ledger delivered this wake-up to <name> through its channel."
- The watchdog reports `wake_unconfirmed` on the target 2 minutes after an unconfirmed, unsent push.
- A `wake_transport` value other than `channel` or `sendmessage` reads as `channel`.
- The Oracle's `Monitor` listener stays as it is, whether or not the Oracle has a channel (plan question 3).
- A future device-queue broker is a second event kind on `swarm-events`, not a separate server (plan question 4). Every event carries a `kind`; a wake-up is `wakeup`. The broker is not built.
- `plugin.json` has no `channels` entry. See "Channels" in [16-open-items.md](16-open-items.md).

Built on 2026-09-27, Driver notifications (see "Driver notifications" in [02-run-lifecycle.md](02-run-lifecycle.md)), choices Alex has not reviewed:

- Two delivery paths, both on by default: an OS notification from the ledger server itself, and a `PushNotification` the Oracle owes. `notify: [os, push]` in the settings file picks them; `[]` turns both off.
- The events: `drive_done` with `blocked`, a new stop-rule directive, or a clean exploration, at most one per exploration in that order; and a new `crashed` or `driver_overdue` watchdog report on a Driver session. `driver_overdue` stands in for `stuck`, which the watchdog never reports on a Driver.
- One `notifications` row per run and event key (`drive_done:<request_id>` or `directive:<directive_id>`), so a repeat notifies no one.
- `drive_done(request_id, blocked=None)` is the Driver's path to report that it cannot continue. It refuses `blocked` until the exploration has a finding.
- The message texts, capped at 199 characters with backticks and asterisks removed. A stop-rule message names the first new rule and counts the rest as "(+N more)".
- The Windows toast runs through Windows PowerShell 5.1 with `-EncodedCommand` under the AppUserModelID `SentinelSwarm.Notifications`, which each toast registers under `HKCU\Software\Classes\AppUserModelId`; macOS uses `osascript`; Linux uses `notify-send` when installed and logs a skip otherwise. Each runs in a daemon thread with a 30-second timeout, and a failure goes to the server log.
- Levels: a stop rule and an overdue Driver are warnings (amber eye); a blocked or crashed Driver is an error (red eye); a clean exploration is a success (green eye).
- The Oracle's watchdog `Monitor` prints each owed push once, which wakes the Oracle.
- The Oracle's Stop hook blocks on an owed push even while the run is paused, while children work, while a directive waits on the user, and after `run_finish`. A stop that follows its own block passes, as for owed wake-ups.
- Any `PushNotification` call from the Oracle pays one debt, whatever its result: the one whose message matches, or else the oldest.
- `PushNotification` is in the Oracle's tools only.
