# The run lifecycle

## Start

The launcher starts the ledger server, then the Oracle session with the PRD as its first
message. The Oracle then calls, in order:

1. `ToolSearch(query="select:mcp__swarm-ledger__ledger_info", max_results=1)`. This
   call waits for a ledger server that is still connecting. Every role makes it first.
2. `ledger_info()`.
3. `run_start(prd, session_id)`. It opens the run and registers the Oracle.
4. `repo_check(fetch)`, then `repo_branch_create(name)` or a question to the user. See
   "Git in the host repo".
5. `profile_set(test_command, build_command, lint_command)`.
6. `guidelines_set(body)`: the architecture, stack, conventions, commands, and every
   assumption about the PRD. Lower layers read it with `guidelines_get`.
7. `phase_add(name, depends_on)` once per phase, in dependency order.
8. `phase_update(phase_id, "unlocked")` for each phase with no dependency.
9. Arm the watchdog listener. See "Watchdog" in [08-watchdog.md](08-watchdog.md).

## Phases, dependencies, and waves

- A phase is one chunk of work with one Manager. The plan is a dependency graph of
  phases, not a fixed list. A phase unlocks when every phase it depends on is approved.
- A wave is the set of phases whose dependencies are all approved, so they run at the
  same time. `plan_unlocked()` lists the phases that are not approved and whose
  dependencies are all approved.
- Phase states: `planned`, `unlocked`, `working`, `handed_up`, `approved`. A Manager
  sets only its own phase, and only to `working` or `handed_up`. The Oracle sets every
  other state.
- A run has a typical shape. **Foundation:** setup and shared helpers first, because
  other work waits on them. **Scale out:** the approved foundation unlocks parallel
  phases. **Scale down:** integration phases bring the parts together with fewer
  agents.
- A join point is where parallel phases feed a later phase. The Oracle runs the full
  suite there, once every Manager that feeds it has reported. **(proposed wording)**
- The plan changes during the run. Higher layers add work from validated findings, or
  schedule it for a later phase.
- The same ordering applies inside a phase and a module: helpers come before the files
  that use them. **(proposed)**
- Contracts come before implementations. When a brief fixes a helper's contract, the
  dependent Coders test against it with test doubles instead of waiting. "Blocked"
  means the contract is missing or wrong. **(proposed)**

## Modules

- A Manager adds modules to its own phase with `module_add`.
- Module states: `planned`, `returned` (by `module_review` or a departure pushback), and
  `approved` (set by `approve` when no file of the module is still pending).

## Briefs

- `brief_create(child_name, child_role, model, body, phase_id, module_id, file_id)`
  refuses a caller whose role is not the child role's parent, a model outside the
  child's approved list, a name a live agent holds, and a second unacknowledged brief
  for one name.
- A brief's body holds the whole task: the goal, the scope, what the tests must prove,
  the contracts, and the guidelines that apply.
- `brief_get(caller_name, child_name)` needs no identity, so a child reads its brief
  before it is bound.
- `brief_ack(caller)` binds the child's name to its session. Nothing else in the ledger
  works for the child before it. See "Identity" in [05-sessions.md](05-sessions.md).

## Files and claims

- A Lead claims each file with `claim_file(path, test_path, for_name)` before it writes
  the Coder's brief. The claim is the file lock.
- One Coder owns one file. The ledger refuses a second live claim on a path, and a
  second live claim for one Coder name.
- `test_path` is optional for a file with no code, such as a README, a config file, or
  an empty package marker. Its handoff runs no tests, and the reviews judge it. The
  ledger reads the strings `""`, `"none"`, and `"null"` as no test file. The Oracle's
  `phase_review` refuses any such file that the code graph maps with a function or
  class.
- A re-claim of a released path in the same run supersedes the old claim. The old row
  becomes `superseded`, and every gate treats it as settled.
- `release_file(path)` drops a claim that is no longer needed. `who_owns(path)` names
  the live owner.
- File states: `claimed`, `handed_up`, `returned`, `approved`, `incomplete`,
  `released`, `superseded`.

## Handoff

`handoff_submit(file_id, open_issues, departures)` is the Coder's hard check. It
refuses unless:

1. The caller owns the file.
2. No change request on the file that a Coder accepted is still uncompleted.
3. When the file has a `test_path`, the ledger runs `tests_run(scope="file")` on it
   and the run passes: exit code 0, at least one test passed, and zero failed, errored,
   or skipped.
4. The code graph is current for the file. See "The code graph" in [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md).
5. A self review exists and is newer than the Coder's last edit of either path.

On success it saves a version of the file and the test file in
`.sentinel-swarm/versions/`, records the handoff as `submitted`, writes each string in
`departures` as a departure row, sets the file and the Coder to `handed_up`, and owes
the Lead a wake-up.

## Lead review

The ledger enforces this order:

1. `score_record(kind="lead")` for the file. It is refused once `review_compare` has
   run for the handoff, so the Lead scores blind.
2. `review_compare(handoff_id)` returns both score sets and the dimensions that
   disagree.
3. `departure_decide` for each departure the handoff carries.
4. One decision:
   - `approve(handoff_id)` refuses without a comparison, when the Lead review since the
     handoff fails the pass rule, while the file has an open issue, while a change
     request on the file is not verified or declined, and while a departure on the
     handoff is `open` or `pushed_back`. It approves the handoff, approves and releases
     the file, marks pushed-back departures of the file `reworked`, releases the Coder
     and stops its session, and approves the module when no file of it is pending.
     A file that fails the pass rule still approves as a **floor pass** when every
     applicable dimension is at or above the rubric floor, no criterion is below the
     criterion floor, and the file is at its last escalation round: its recorded
     attempts have reached the full escalation budget, the `escalation` settings'
     `rounds` times `attempts_per_round`. **(proposed)** A floor pass records a
     shortfall for each dimension still below target and names them in the result's
     `floor_pass_dimensions`; the report shows them.
   - `return_work(handoff_id, issues, targeted)` refuses while a departure on the
     handoff is `open`. It returns the handoff and the file, sets the Coder `idle`,
     records a fix attempt, posts the issues and target dimensions to the Coder, and
     owes the Coder a wake-up. A return counts as one fix attempt.
   - `accept_incomplete(handoff_id, reason)` refuses while a departure on the handoff is
     `open` or `pushed_back`. It marks the file `incomplete` and releases it, marks
     pushed-back departures of the file `reworked`, releases the Coder, and opens a
     deferral. It uses no fix attempt. The Lead validates the Coder's reason first.
     **(proposed)**
5. After a return, the Coder hands off again. The Lead scores again with `targeted`,
   compares, and calls `attempt_record(file_id)`. See "Scoring and review" in [03-scoring-and-review.md](03-scoring-and-review.md).

## Module review

`module_review(module_id, outcome, notes, disagreement_notes, scores)` belongs to the
Manager of the module's phase. **(proposed)**

- `accepted` refuses unless every file of the module is `approved`, `incomplete`, or
  `superseded`; a passing test run of scope `module` for the module, or of scope
  `phase` in the phase, exists after the module's last file decision; `disagreement_notes`
  holds a non-empty note, keyed by `file_id`, for each approved file whose self and
  Lead scores disagreed; and `scores` rates completeness, integration, and open items
  from 1 to 10, with a reason below 9. **(proposed)**
- `accepted` also refuses while a departure in the module waits on the Lead or the
  Manager, or is pushed back and not reworked.
- `returned` sets the module to `returned` and owes the live Lead a wake-up. When the
  Lead has ended, the result says so, and the Manager starts a new Lead.
- After an accepted review, the Manager releases the Lead with `agent_release`.

## Phase hand-up

`phase_update(phase_id, "handed_up")` refuses while a Lead of the phase is live, and
unless every module of the phase has an accepted `module_review` newer than its last
file decision. **(proposed)** It records `handed_up_at` and owes the Oracle a wake-up.
The Manager then posts its phase report to the Oracle.

## Phase review and approval

`phase_review(phase_id, outcome, notes, low_score_notes, scores)` belongs to the Oracle,
for a phase in `handed_up`. **(proposed)**

- `accepted` refuses unless the Oracle recorded a passing `tests_run(scope="full")`
  after the hand-up; no approved or incomplete file of the phase lacks a test file while
  the code graph anchors a function or class in it; `low_score_notes` holds a
  non-empty note, keyed by `file_id`, for each file whose latest Lead review has a
  dimension below the target; and `scores` rates completeness, integration, and open
  items from 1 to 10, with a reason below 9. **(proposed)**
- `accepted` also refuses while a departure of the phase is neither signed off nor
  reworked.
- `returned` sets the phase back to `working` and owes the live Manager a wake-up.

`phase_update(phase_id, "approved")` refuses while a deferral on a file of the phase is
open, while a change request on a file of the phase is not verified or declined, and
without an accepted `phase_review` since the hand-up. **(proposed)** It approves the
phase, then releases the Manager and every live agent under it and stops their
sessions.

## Finish

`run_finish(outcome)` refuses while any phase is not approved, any file claim is live,
any directive is open, any deferral is open, any change request is not verified or
declined, or any departure is neither signed off nor reworked. **(proposed)** It also
refuses, when the host has a Driver available, while no exploration has ended clean
since the last fix wave, unless the loop stopped on a stop rule whose directive is
resolved, or the Driver is unavailable for the run: see "Explorations" below. It then
sets the run to `finished`, releases every live agent except the Oracle, builds the
report, and closes the Oracle's row. The
ledger server then stops the Oracle's background session and exits. See "What stops
when" in [05-sessions.md](05-sessions.md).

## Explorations

- The Driver builds the app at the start of each exploration, then runs and tests that
  build. It exists only when the host has cartographer and a driver plugin installed;
  see "Driver" in [01-roles.md](01-roles.md). Only the Oracle sends requests.
- Fixes start while the Driver still explores. The Oracle sees each issue as the Driver
  records it and starts work on it at once: when a fix phase for that module is already
  running, a Coder joins it; otherwise the Oracle starts a new Manager, Lead, and Coder
  for that bug. When the exploration ends and every fix agent has finished, the Driver
  builds and retests. Fix agents run unit tests only.
  - The Oracle asks a running module for a new Coder through its Manager, which asks
    the Lead, so the spawn order holds. **(proposed)**
  - The Driver tests the app it built at the start of the exploration, never a dev
    server that reloads on edits. **(proposed)**
  - A failed build is recorded at once as a finding with the build log, which ends that
    exploration, and the retest starts with a build. **(proposed)**
- Every wave starts from a new plan and new agents, including a fix wave planned from a
  Driver exploration. A review fix inside a wave, such as a return or a pushback,
  resumes the existing agents. The Oracle groups the issues by module, one fix phase per
  module. **(proposed)** Each fix's brief names the findings it fixes with
  `brief_create(..., finding_ids=[...])`. The brief stores the list, and a child's
  brief inherits its parent's list unless it names its own. **(proposed)** The
  finding's evidence in the Coder's brief body and the re-claim of the file are
  **(proposed, not built)**.
- Each request carries a focus list the Oracle writes. The first exploration covers
  every PRD feature; each later one covers the features the last wave touched, every
  open issue to recheck, and a quick smoke pass over everything else; the final clean
  exploration is a full pass. The Driver first replays recorded routes with `map-test`
  to recheck earlier findings, then runs `map-explore` with the focus list as its goal.
  **(proposed)**
- `drive_request(focus)` is the Oracle's call. It refuses an empty focus list, a host
  with no Driver available, a run whose Driver is unavailable, an earlier exploration
  that is still open
  (`drive_requests.state = 'open'`), and a Manager, Lead, or Coder still live in the
  run: a new exploration starts only after every fix has finished. It writes the
  Driver's brief and starts its session the same way `brief_create` and `agent_spawn`
  do, under the name `driver-e<ordinal>`, and returns the loop's current status.
- Every finding has a fixed shape, filled as fully as possible: a fingerprint (the
  check, the location, and what it saw), a title, the steps to reproduce, the expected
  and actual result, a severity (blocker, major, or minor), the area, and the evidence
  (screenshots before and after, the UI tree, log excerpts).
  `drive_issue(request_id, finding)` records one finding as a row in `drive_findings`,
  with the evidence as paths into cartographer's run folder, and owes the Oracle a
  wake-up at once. It refuses a finding with no `fingerprint` or `title`, or with
  another severity. **(proposed)**
- No time limit on an exploration. Every 30 minutes the Driver reports its progress to
  the Oracle and keeps working. The Oracle reviews the testing and the steps so far,
  looking for problems that are not obvious bugs, and starts fixes for them. It also
  decides whether the Driver is stuck; if so, it stops the Driver and starts a fix for
  whatever blocked it. `drive_checkin(request_id, covered, steps, notes)` records a
  check-in and owes the Oracle a wake-up. The watchdog does not report the Driver as
  stuck while its check-ins arrive within 30 minutes plus a 5-minute grace; see
  [08-watchdog.md](08-watchdog.md). **(proposed)**
- `drive_done(request_id, blocked=None)` ends the exploration, releases the Driver's
  session, and stops it. **(proposed)** The Driver passes `blocked`, a short statement
  of what failed, when it cannot continue: a failed build or a device that will not
  boot. `drive_done` refuses `blocked` until the exploration has a finding, so a blocked
  exploration never counts as clean. **(proposed)**
- Releasing a Driver, with `agent_release` or `run_finish`, closes its open exploration
  as `abandoned`. The Oracle stops a stuck or crashed Driver this way, then requests the
  next exploration. An abandoned exploration never counts as clean, and it is not a wave
  in the stall count; its findings still count as seen. **(proposed)**
- `drive_unavailable(reason)` records that the Driver's plugin servers failed to load.
  The Driver calls it for its own exploration, or the Oracle calls it. It abandons any
  open exploration, releases its Driver, and records an error notification for the
  user. From then on `drive_request` refuses and `run_finish` needs no exploration.
  The mechanism is **(proposed)**.
- **Stop rules.** The loop stops on lack of progress, not a fixed count. A bug still
  there after 3 fix attempts in a row stops getting fixes and goes to the user with its
  evidence. A bug gets at most 5 fix attempts in all. The loop stops when 3 fix waves in
  a row fix nothing, and the Oracle reports what is left. The loop also watches for
  patterns of bugs; on one, the Oracle pauses fixes in that area and reports it.
  - The ledger tracks each finding by fingerprint and counts each later exploration
    that still finds it as one fix attempt. The patterns are a regression (a fixed
    finding returns), fixes causing bugs (new findings in the areas of the findings the
    last wave fixed, at least as many as it fixed), and ping-pong (two findings that take
    turns across the last 4 explorations). `drive_done` files each condition as an open
    directive from source `driver`, computed by the pure function `compute_loop_status`
    in `mcp/src/swarm_ledger/drive.py` over every request and finding of the run, and
    deduplicated by its exact reason text. **(proposed)**
  - Each directive body starts with `[driver-stop] <reason>` and lists the evidence.
    For a stopped finding or a regression: the title, fingerprint, and area, the fix
    attempts in a row and in all, the explorations it recurred in, and the evidence
    paths. For a stall: every finding of the latest exploration, which is what is left.
    **(proposed)**
  - `drive_done` records each directive's targets in `drive_stops`: the fingerprint for
    a per-finding stop, and each fingerprint the pattern names, with its area, for a
    pattern. `brief_create` and `agent_spawn` enforce the rules on a brief's
    `finding_ids` **(proposed)**:
    - A finding whose fingerprint hit a per-finding stop gets no more fixes for the rest
      of the run, even after its directive resolves. A finding that reaches the stop in
      the open exploration is refused too, before `drive_done` files the directive.
    - A pattern pauses fixes in its area: a finding whose fingerprint or area a pattern
      names is refused while the pattern's directive is open. Resolving the directive
      lifts the pause.
- The loop ends on a clean exploration (no finding) or on a stop rule, whichever comes
  first. **(proposed)**

### Driver notifications

The user is notified when the Driver finishes or hits an error. See "What the Oracle
tells the user" in [04-agreements.md](04-agreements.md). The mechanics below are
**(proposed)**.

- **Events.** `drive_done` records at most one notification per exploration, in this
  order: `blocked` (error), a stop rule it filed a new directive for (warning), or a
  clean exploration (done). Among new stop rules, a stall comes first, and its message
  lists what is left. An exploration with findings and no new stop rule records none.
  `drive_unavailable` records one (error) per run. The watchdog records one for each
  new report on a Driver session: `crashed` (error) or `driver_overdue` (warning).
  `driver_overdue` is the Driver's stuck report; the watchdog never reports a Driver as
  `stuck`. See [08-watchdog.md](08-watchdog.md).
- **Levels.** Each kind maps to a level: done is success, warning is warning, and error
  is error. The level picks the notification's logo: the plugin icon with a green,
  amber, or red eye, `assets/icon-<level>.png`.
- **Dedupe.** The `notifications` table keeps one row per run and event key:
  `drive_done:<request_id>`, `directive:<directive_id>`, `drive_unavailable:<run_id>`,
  or `issue:<issue_id>` for an issue that ends round 3 below the floor. A repeat of the
  same event records nothing and notifies no one.
- **Message.** One line, at most 199 characters, with no markdown. It leads with what
  the user acts on, for example `Driver done: exploration 3 clean, 0 open bugs, 4 fixed
  this run`, `Driver stopped: 3 attempts in a row with no progress on <finding title>
  [<fingerprint>], seen in explorations 1, 2, 3, 4 (+1 more)`, `Driver loop ended: 3
  explorations in a row fixed nothing; 2 left: <title>; <title>`, `Driver blocked:
  exploration 2 cannot continue: the build failed`, or
  `Driver overdue: driver-e2, exploration 2 has had no check-in for 41 minutes`.
- **OS path.** The ledger server shows the message as a desktop notification itself,
  in a background thread that never blocks or fails a ledger call. Each platform shows
  the sender as "Sentinel Swarm" where it can:
  - Windows: a toast through Windows PowerShell's WinRT toast API, under the
    AppUserModelID `SentinelSwarm.Notifications`. Each toast first registers that ID
    under `HKCU\Software\Classes\AppUserModelId`, with the display name "Sentinel Swarm"
    and `assets/icon.png` as its icon. The toast shows the level's logo. Windows caches
    an ID's name and icon from its first toast, so a new name or icon needs a new ID.
  - macOS: `osascript`, with the title "Sentinel Swarm". The sender stays Script Editor,
    and no logo shows.
  - Linux: `notify-send --app-name "Sentinel Swarm" --icon <level logo>`, when
    `notify-send` is installed.

  A failure goes to the server log, `.sentinel-swarm/server.log`, and nothing else
  happens.
- **Push path.** When `notify` lists `push`, each notification also records a push the
  run's Oracle owes. The Oracle's watchdog `Monitor` prints the call once, which wakes
  the Oracle:
  `PushNotification(message="<message>", status="proactive")`. The Oracle's Stop hook
  blocks until `post_any` sees a `PushNotification` call from the Oracle. Any call
  counts as sent, whatever its result text, because Claude Code answers "Not sent —
  this terminal is active" when it judges the user is at the machine. A call pays the
  debt whose message it matches, or else the oldest one.
- **Setting.** `notify: [os, push]` in the settings file picks the paths. An empty list
  turns both off. See [11-setup-and-settings.md](11-setup-and-settings.md).

## Pause and resume

- When only the user can unblock the run, the Oracle calls `run_pause(reason)`. The run
  becomes `paused`, the Stop hook lets the Oracle stop once it owes the user no
  `PushNotification` call, and every gate still applies.
  The Oracle tells the user once what to fix.
- When only some phases are blocked, the Oracle calls `run_pause(reason, phases=[...])`
  instead. The run stays `active`; only the named phases get a `paused_at` and
  `pause_reason`. `agent_spawn` refuses a brief whose `phase_id` names a paused phase.
  `plan_unlocked`, the Oracle's Stop hook, and the watchdog's stall check all treat a
  paused phase's handoffs, claims, and unlocked state as waiting, not as pending work,
  so they neither nag the Oracle nor report a stall over it. `phase_resume(phase_ids)`
  clears the pause on named phases once the blocker is gone. **(proposed)**
- The user's next message, or `/sentinel-swarm:resume`, leads the Oracle to call
  `run_start`, which sets the run back to `active`. A scoped pause does not touch the
  run's own state, so it never needs `run_start` to clear it; only `phase_resume` does.
- `run_start` on a live run whose Oracle session is gone binds the Oracle's name to the
  calling session, moves the live children and briefs to it, and returns the run with
  `resumed: true`.
- One swarm runs per repo. `run_start` refuses while the run's Oracle still runs in
  another session, and refuses when it cannot list sessions to check.
- A run resumes only on the same machine: the records are local. **(proposed)**

## Git in the host repo

- The whole swarm works on the checked-out branch, in one working tree. It never
  commits or pushes. The user commits at the end.
- `repo_check(fetch)` reports the branch, a clean tree, the dirty paths, the base
  branch (the `base_branch` setting, else `main`, else `master`), ahead and behind
  counts against the base and the upstream, `obvious_start`, and one line of `advice`.
  It records the result on the run. **(proposed)**
- `obvious_start` is true when the tree is clean, the checkout is on the base branch,
  and it is not behind its upstream. The Oracle then calls `repo_branch_create(name)`,
  which refuses unless the last check reported `obvious_start`, the tree is still clean,
  and no Manager, Lead, or Coder of the run is live. **(proposed)**
- Otherwise the Oracle asks the user, naming the reason: uncommitted changes, another
  branch, behind the remote, no base branch, or no git repo. **(examples proposed)** A
  headless Oracle records its assumption in the guidelines and proceeds.
- `agent_spawn` refuses a Manager until the run has a recorded `repo_check`. It does not
  require `obvious_start`. **(proposed)**
- No agent works in a git worktree, so nothing needs a merge and the repo has one copy
  of the code graph. Each hand-up saves a version in the records folder, since commits
  are not available. **(proposed)**
