# sentinel-swarm specification

## 1. What sentinel-swarm is

sentinel-swarm is a Claude Code plugin that takes a PRD of any size to built, tested,
reviewed code. Four roles do the work: the Oracle, the Managers, the Leads, and the
Coders. Every role runs as its own Claude Code session in the host repo's one working
tree. A shared SQLite ledger, served by the `swarm-ledger` MCP server, holds the run's
state and every gate. Hooks in each role's agent file make the ledger unavoidable.

The plugin ships as a private marketplace plugin for Alex's own use. It must work on
Windows and macOS, install into other repos, and adapt to each host project. The swarm
writes unit tests only. End-to-end testing is a separate process outside the swarm.

### How to read this spec

- This file is the source of truth for how the system works. Read it before you change
  anything.
- **(proposed)** marks an item Alex has not reviewed. Everything else is settled.
- The code is the ground truth for behavior. When this file and the code disagree, fix
  whichever one is wrong, and record the fix here.
- For the module layout of the ledger server, read `mcp/ARCHITECTURE.md`. For the exact
  rubric criteria, read `mcp/src/swarm_ledger/rubric.py`. For the tables, read
  `mcp/src/swarm_ledger/schema.sql`.
- When Alex makes a decision, record it here as a plain rule and add a line to
  "Key decisions". Mark your own additions **(proposed)**.

## 2. Principles for every role

1. **Exclusive ownership.** Every unit of work has exactly one owner: a run, a phase, a
   module, or a file. Nobody else changes it.
2. **Non-owners request, owners change.** An agent that needs a change in work it does
   not own files a change request. The owner makes the change. The requester verifies
   it.
3. **Planning prevents conflicts.** Each layer plans its breakdown so that two owners
   rarely need the same file. Change requests are the fallback.
4. **Every layer reviews.** No layer is skipped, even for a single hello-world file.
5. **Evidence, not claims.** A role reports work as complete only with recorded
   evidence: test runs, scores, and reviews. A later reader can confirm it from the
   records alone.
6. **Small scope, visible work.** A Coder holds one file. Each agent's context stays
   focused, and each agent's work is easy to see.
7. **No state lives only in an agent.** **(proposed)** Every plan, brief, review,
   score, and decision is a ledger record, so a replacement agent or a later run can
   continue from the records.
8. **Only the Coder writes project files.** Every other role works through the ledger.
9. **No telephone game.** Every plan and task is a ledger record. A spawn prompt
   carries only the child's name. A wake-up message only points at a record. An agent
   re-reads its brief from the ledger before each handoff and after each return,
   instead of trusting its memory.
10. **Enforcement lives in hooks and ledger gates, not in prompt text.** A prompt says
    what a role must not do. A hook or a ledger gate stops it. Enforcement is a hard
    block, and only the Oracle overrides a rule.

## 3. Roles

### Hierarchy

| Role | Count | Owns | Started by | Reports to | Agent file | Color |
|---|---|---|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user, through the launcher | The user | `swarm-oracle` | cyan |
| Manager | 1 per phase | One phase | Oracle | Oracle | `swarm-manager` | green |
| Lead | 1 per module | One module | Manager | Manager | `swarm-lead` | purple |
| Coder | 1 per file | One file and its unit test file | Lead | Lead | `swarm-coder` | orange |

- The minimum run is one of each role.
- Each role starts only the role below it. The Oracle starts Managers, a Manager starts
  Leads, and a Lead starts Coders. A Coder starts nothing.
- No role has the `Agent` or `Workflow` tool, so no role runs subagents or workflows.
  The `pre_agent` hook denies an `Agent` call from any swarm session.
  `mcp/tests/test_plugin_surface.py` checks that no template lists either tool.
- A Lead reviews its Coders' work itself.
- No role template sets `maxTurns`. The watchdog and the escalation budget are the only
  controls on a runaway agent.

### Names

| Role | Name pattern **(proposed)** | Example |
|---|---|---|
| Oracle | `oracle` | `oracle` |
| Manager | `mgr-<phase>` | `mgr-p2-api` |
| Lead | `lead-<phase>-<module>` | `lead-p2-auth` |
| Coder | `coder-<phase>-<module>-<file>` | `coder-p2-auth-login` |

- A name is unique among the live agents of a run. It is the address in the ledger, and
  the agent passes it as `caller` on every ledger call. **(proposed)**
- A session name is unique on the machine and is the address for `SendMessage`. A
  child's session name is `<repo slug>-r<run_id>-<name>`. The Oracle's session name is
  `<repo slug>-oracle-<MMDD-HHMMSS>`, from its launch time.

### Models

| Role | Default model (agent file `model`) | Approved list (settings `models`) |
|---|---|---|
| Oracle | opus | opus, fable |
| Manager | opus | opus |
| Lead | sonnet | opus, sonnet |
| Coder | sonnet | sonnet, haiku |

- The settings file holds the approved models per role. `brief_create` refuses a model
  outside the child role's list.
- Each agent file's `model` key is the role's default. The launcher starts the Oracle on
  its file's model. `agent_spawn` passes the brief's model with `--model`.
- The parent picks the child's model from the child role's list, by the complexity of
  the task, and the brief records the choice. Ideally, work reaches a Coder broken down
  far enough to run on the cheaper model.
- Escalation can raise the model: a fresh Coder in round 2 or 3 can run on the stronger
  model in its list. **(proposed)**
- The registry records the model of each agent. **(proposed)** For the Oracle's row,
  `run_start` records the first entry of the Oracle's approved list.

### Oracle

- **Does:** reads the PRD and asks the user about it. Checks the repo and creates the
  run's branch. Sets the test, build, and lint commands and the high-level guidelines.
  Plans the work as a dependency graph of phases. Starts one Manager per unlocked phase.
  Audits the scores and reviews from the layers below and investigates the low ones.
  Runs the full test suite after each phase hands up. Reviews and approves each phase.
  Decides departures at its level. Resolves directives. Arms the watchdog listener.
  Grants overrides. Decides between a notification now and a line in the final report.
  Writes the final report and finishes the run.
- **Owns:** PRD meaning, acceptance criteria, the guidelines, the phase plan, the final
  verdict, and all communication with the user.
- **Arbitrates:** disputes between Managers, including which Manager owns a shared
  file. **(proposed)**
- **On a low score or a failed full run:** returns the work to its Manager, opens a new
  phase, or accepts the result with a recorded reason. **(proposed)**
- **Escalates to the user:** only an issue that research and rework cannot solve.
- **Must not:** write a project file, start a subagent, score a file, direct a Lead or a
  Coder, or report a test result from its own reading.
- **Done when:** every phase is approved, the full suite passes, the evidence is on
  file, and `run_finish` accepts the run.
- **Tools:** Read, Grep, Glob, AskUserQuestion, ToolSearch, WebSearch, WebFetch,
  SendMessage, Monitor (the watchdog call only), the ledger, and the codebase-kg read
  tools. No write tool and no shell.

### Manager

- **Does:** breaks its phase into modules (`module_add`). Starts one Lead per module.
  Agrees with other Managers on one owner for any shared file before a Lead claims it.
  Reviews each Lead's result: whether the Lead met its tasks, and the files whose self
  and Lead scores disagreed. Runs phase-scope tests on each module's target. Decides
  departures at its level. Records `module_review`, releases each accepted Lead, and
  hands the phase up to the Oracle.
- **Owns:** the phase, its module breakdown, the boundaries between its Leads, and test
  health across its modules.
- **Arbitrates:** disputes between its Leads, and the contracts between its modules.
  **(proposed)**
- **On a regression:** starts a new Lead to fix it.
- **Must not:** write a project file, score a file, or direct a Coder without going
  through the Coder's Lead. **(proposed)**
- **Done when:** the Oracle approves the phase.
- **Tools:** Read, Grep, Glob, ToolSearch, SendMessage, WebSearch, WebFetch, the
  ledger, and the codebase-kg read tools.

### Lead

- **Does:** breaks its module into per-file tasks. A task is a pair: the source file and
  its unit test file, owned by one Coder. Claims each file, briefs one Coder per file,
  and starts it. Scores each handoff blind, compares, and approves, returns, or accepts
  it as incomplete. Classifies each fix attempt. Decides departures first. Runs the
  module's tests when every file is settled, and reports to its Manager.
- **Owns:** the module, the file assignments inside it, the contracts between its files,
  and the approval of each Coder's work.
- **Arbitrates:** disputes between its Coders about contracts and about where a shared
  function belongs. **(proposed)**
- **Must not:** write a project file, or read a Coder's scores before it records its
  own.
- **Done when:** its Manager accepts the module with `module_review` and releases it.
- **Tools:** the same as the Manager.

### Coder

- **Does:** searches the code graph for existing code before it writes. Writes the test
  file first when it can, then the source file. Test-first is the expected practice,
  not a gate. Runs its tests through `tests_run` until they pass. Updates the code graph
  for its file with `graph_upsert`. Scores its own work, fixes it, and scores again
  until it is satisfied; this loop has no cap and is not an escalation attempt. Hands
  off. Works with its Lead and other Coders on contracts, through the ledger.
- **Owns:** one file and its unit test file. "File" means any project file the run
  touches: new or existing, code or configuration. **(proposed)**
- **Tests cover:** the happy path, the known possible edge cases such as API errors,
  and error handling that catches the specific error types plus a catch-all.
- **Must not:** edit a file it does not own. It files a change request instead.
- **Done when:** its Lead approves the file or accepts it as incomplete.
- **Tools:** Read, Grep, Glob, Write, Edit, Bash and PowerShell (gated), ToolSearch,
  SendMessage, WebSearch, WebFetch, the ledger, and the codebase-kg read tools. It
  changes the graph only through `graph_upsert`.

### Driver (planned, not built)

A fifth role, the Driver, runs the app and checks changes on it with the android-driver,
ios-driver, and web-driver plugins, on request from any role. Alex decided its
responsibilities. Its mechanics are not designed. Read `plans/driver-agent.md` before
you work on it.

## 4. The run lifecycle

### Start

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
9. Arm the watchdog listener. See "Watchdog".

### Phases, dependencies, and waves

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

### Modules

- A Manager adds modules to its own phase with `module_add`.
- Module states: `planned`, `returned` (by `module_review` or a departure pushback), and
  `approved` (set by `approve` when no file of the module is still pending).

### Briefs

- `brief_create(child_name, child_role, model, body, phase_id, module_id, file_id)`
  refuses a caller whose role is not the child role's parent, a model outside the
  child's approved list, a name a live agent holds, and a second unacknowledged brief
  for one name.
- A brief's body holds the whole task: the goal, the scope, what the tests must prove,
  the contracts, and the guidelines that apply.
- `brief_get(caller_name, child_name)` needs no identity, so a child reads its brief
  before it is bound.
- `brief_ack(caller)` binds the child's name to its session. Nothing else in the ledger
  works for the child before it. See "Identity".

### Files and claims

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

### Handoff

`handoff_submit(file_id, open_issues, departures)` is the Coder's hard check. It
refuses unless:

1. The caller owns the file.
2. No change request on the file that a Coder accepted is still uncompleted.
3. When the file has a `test_path`, the ledger runs `tests_run(scope="file")` on it
   and the run passes: exit code 0, at least one test passed, and zero failed, errored,
   or skipped.
4. The code graph is current for the file. See "The code graph".
5. A self review exists and is newer than the Coder's last edit of either path.

On success it saves a version of the file and the test file in
`.sentinel-swarm/versions/`, records the handoff as `submitted`, writes each string in
`departures` as a departure row, sets the file and the Coder to `handed_up`, and owes
the Lead a wake-up.

### Lead review

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
   - `return_work(handoff_id, issues, targeted)` refuses while a departure on the
     handoff is `open`. It returns the handoff and the file, sets the Coder `idle`,
     records a fix attempt, posts the issues and target dimensions to the Coder, and
     owes the Coder a wake-up. A return counts as one fix attempt.
   - `accept_incomplete(handoff_id, reason)` refuses while a departure on the handoff is
     `open` or `pushed_back`. It marks the file `incomplete` and releases it, releases
     the Coder, and opens a deferral. It uses no fix attempt. The Lead validates the
     Coder's reason first. **(proposed)**
5. After a return, the Coder hands off again. The Lead scores again with `targeted`,
   compares, and calls `attempt_record(file_id)`. See "Scoring and review".

### Module review

`module_review(module_id, outcome, notes, disagreement_notes)` belongs to the Manager
of the module's phase. **(proposed)**

- `accepted` refuses unless every file of the module is `approved`, `incomplete`, or
  `superseded`; a passing test run of scope `module` for the module, or of scope
  `phase` in the phase, exists after the module's last file decision; and
  `disagreement_notes` holds a non-empty note, keyed by `file_id`, for each approved
  file whose self and Lead scores disagreed.
- `accepted` also refuses while a departure in the module waits on the Lead or the
  Manager, or is pushed back and not reworked.
- `returned` sets the module to `returned` and owes the live Lead a wake-up. When the
  Lead has ended, the result says so, and the Manager starts a new Lead.
- After an accepted review, the Manager releases the Lead with `agent_release`.

### Phase hand-up

`phase_update(phase_id, "handed_up")` refuses while a Lead of the phase is live, and
unless every module of the phase has an accepted `module_review` newer than its last
file decision. **(proposed)** It records `handed_up_at` and owes the Oracle a wake-up.
The Manager then posts its phase report to the Oracle.

### Phase review and approval

`phase_review(phase_id, outcome, notes, low_score_notes)` belongs to the Oracle, for a
phase in `handed_up`. **(proposed)**

- `accepted` refuses unless the Oracle recorded a passing `tests_run(scope="full")`
  after the hand-up; no approved or incomplete file of the phase lacks a test file while
  the code graph anchors a function or class in it; and `low_score_notes` holds a
  non-empty note, keyed by `file_id`, for each file whose latest Lead review has a
  dimension below the target.
- `accepted` also refuses while a departure of the phase is neither signed off nor
  reworked.
- `returned` sets the phase back to `working` and owes the live Manager a wake-up.

`phase_update(phase_id, "approved")` refuses while a deferral on a file of the phase is
open, while a change request on a file of the phase is not verified or declined, and
without an accepted `phase_review` since the hand-up. **(proposed)** It approves the
phase, then releases the Manager and every live agent under it and stops their
sessions.

### Finish

`run_finish(outcome)` refuses while any phase is not approved, any file claim is live,
any directive is open, any deferral is open, any change request is not verified or
declined, or any departure is neither signed off nor reworked. It then sets the run to
`finished`, releases every live agent except the Oracle, builds the report, and closes
the Oracle's row. The ledger server then stops the Oracle's background session and
exits. See "What stops when".

### Pause and resume

- When only the user can unblock the run, the Oracle calls `run_pause(reason)`. The run
  becomes `paused`, the Stop hook lets the Oracle stop, and every gate still applies.
  The Oracle tells the user once what to fix.
- The user's next message, or `/sentinel-swarm:resume`, leads the Oracle to call
  `run_start`, which sets the run back to `active`.
- `run_start` on a live run whose Oracle session is gone binds the Oracle's name to the
  calling session, moves the live children and briefs to it, and returns the run with
  `resumed: true`.
- One swarm runs per repo. `run_start` refuses while the run's Oracle still runs in
  another session, and refuses when it cannot list sessions to check.
- A run resumes only on the same machine: the records are local. **(proposed)**

### Git in the host repo

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

## 5. Scoring and review

The rubric is identical for every scorer. Every number is a setting in
`.claude/sentinel-swarm.local.md`.

### Who scores

- Every file gets two reviews: the Coder's self review and the Lead's blind review.
- A Coder's own scores never pass a file. Only the Lead's review does. **(proposed)**
- The Manager does not score files. It reviews whether each Lead met its tasks, and the
  files whose two most recent scores disagreed.
- The Oracle does not score files. It audits the scores and investigates the low ones.
- `score_record(file_id, ratings, applicable, kind, targeted)` accepts `self` from the
  file's owner and `lead` from the module's Lead only.

### Dimensions

Nine dimensions, each with fixed criteria: meets the brief, testing, error handling,
security, architecture, code structure, performance, maintainability, and accessibility.
`mcp/src/swarm_ledger/rubric.py` holds every dimension and criterion key and its text.
The split of design into architecture and code structure is **(proposed)**.

- A scorer rates every criterion of every applicable dimension. A dimension that does
  not apply, such as accessibility on a back-end file, is marked not applicable with a
  one-line reason.
- Every dimension is scored on every review, including the dimensions a fix did not
  target.

### Scale **(proposed)**

- Each criterion is rated from 1 to 10.
- A dimension's score is the average of its ratings times 10, from 0 to 100.
- There is no combined score and there are no weights. Each dimension passes or fails
  on its own, and a criterion can fail on its own.
- A rating below 9 needs a reason. A file-and-line reference is optional in the ledger.

| Rating | Meaning |
|---|---|
| 9 to 10 | Fully met |
| 7 to 8 | Met, with a minor suggestion |
| 5 to 6 | Partly met; an issue is recorded |
| 3 to 4 | Not met; must be fixed |
| 1 to 2 | Wrong, unsafe, or missing |

### Thresholds

| Setting | Default **(proposed)** | Meaning |
|---|---|---|
| `target` | 90 | The goal for every dimension |
| `floor` | 70 | A fix that drops a dimension below this is a regression |
| `criterion_floor` | 5 | No file passes with a criterion below this |
| `disagreement_gap` | 10 | Two reviews this far apart on a dimension disagree |
| `plateau` | 2 | A smaller gain on a targeted dimension is a plateau |
| `regression_tolerance` | 5 | A drop this large on any dimension is a regression |

- **Pass rule:** every applicable dimension is at or above the target, and no criterion
  is below the criterion floor. `approve` requires it of the Lead review.
- **Disagreement:** a gap of `disagreement_gap` or more on a dimension, or one score at
  or above the target and the other below it. **(proposed)**

### Issues

- Every rating of 4 or lower, in a self or a Lead review, opens an issue on the file,
  one per open criterion. **(proposed)**
- A later Lead review that rates that criterion 5 or higher closes it.
- `issue_close(issue_id, resolution)` closes an issue by hand: the file's Lead, a
  Manager, or the Oracle.
- `approve` refuses while the file has an open issue. `issue_open` records a problem by
  hand. `idea_record` records an idea tried against an issue and its outcome.

### The improvement loop **(proposed)**

- A score below the target is something to research and improve. The loop stops when
  new scores stop getting better.
- After each fix, `attempt_record(file_id)` compares the last two Lead reviews:
  - **Regression:** a dimension fell by `regression_tolerance` or more, or fell below
    the floor. The ledger restores the file and test file from the previous handoff's
    saved versions. The failed idea is recorded so nobody tries it again.
  - **Improved:** no regression, and a targeted dimension rose by `plateau` or more. The
    attempt does not count against the budget.
  - **Plateau:** anything else.
- A plateau or a regression adds one attempt to each open issue of the file. After
  `attempts_per_round` such attempts, the issue moves to the next round.
- A fix is kept only when it is not a regression, so the file's current version is
  always the best so far.
- A file passes at once, in any round, when it meets the pass rule.

### Escalation

Escalation and the improvement loop are one mechanism: the score history shows when an
issue stopped improving, and it explains to the next layer why the issue arrived.

| Round | Attempts | Who works on it |
|---|---|---|
| 1 | 1 to 3 | The Coder and its Lead |
| 2 | 4 to 6 | The Manager steps in with its resources |
| 3 | 7 to 9 | The Oracle adds its suggestions |
| After round 3 | | The Oracle changes the plan or notifies the user |

- A round ends after 3 attempts that did not improve the score. **(counting rule
  proposed)** Only a reviewer's return counts, and accepting work as incomplete uses no
  attempt.
- `issue_escalate(issue_id)` moves an issue to its next round, names the receiver in
  `escalated_to` (the Manager for round 2, the Oracle for round 3), and messages it.
  Only the file owner and the owner's parent chain may call it.
- The Manager's resources include its other Leads and Coders, a new Lead, a fresh
  Coder, a stronger model, and a structural change such as a split file or a changed
  contract. **(proposed)**
- An issue a Manager finds starts at round 2. **(proposed)** The ledger opens every
  issue at round 1.
- A layer with no new idea passes the issue up.

### Evidence

- Passing tests are the only evidence the handoff requires. Coverage, lint, type checks,
  and security scans support the scores and do not gate. **(proposed)**
- The ledger runs the tests and records the result. A report cannot claim a pass that
  did not happen. **(proposed)**

## 6. Agreements

### Change requests

A change request is a ledger record plus a wake-up to the right agent. It closes only
with verified evidence.

| Tool | Caller | Effect |
|---|---|---|
| `cr_open(path, body)` | Any role | Routes to the file's live owner, else its module's live Lead, else its phase's live Manager, else the Oracle. Refuses a path no module plans, and a caller who is the recipient. Owes the recipient a wake-up |
| `cr_accept(cr_id, accept, reason)` | The recipient | `open` to `accepted` or `declined`. A decline needs a reason. Owes the requester a wake-up |
| `cr_complete(cr_id, notes)` | The recipient | `accepted` to `completed`. A Coder recipient needs its own passing file-scope test run of the path or its test file since acceptance. Any other recipient needs such a run by anyone, or an approved handoff of the file since acceptance. Owes the requester a wake-up |
| `cr_verify(cr_id, ok, notes)` | The requester, or its nearest live ancestor | `completed` to `verified`, or back to `accepted` with the notes and a wake-up to the recipient |
| `cr_list(state)` | Any role | The caller's own requests. The Oracle sees every request in the run |

- `handoff_submit` refuses while a Coder's accepted request on the file is not
  completed. `approve`, `phase_update(approved)`, and `run_finish` refuse while a
  request in their scope is `open`, `accepted`, or `completed`.
- The report lists each request with its reason, decision, work done, evidence test
  run, and verification.

### Departures

A departure records a break from the guidelines. The guidelines are a target, not a
first-pass gate: an agent may get something working first, and the review cycle moves
it toward the guidelines.

- A Coder lists departures in `handoff_submit`. Each becomes a row linked to the
  handoff, in state `open`, waiting on the Lead.
- `departure_record(body, file_id, guideline_id)` records one outside a handoff: a
  Coder for its own file, a Lead, Manager, or Oracle for work in its scope. A Coder's
  departure waits on the Lead, a Lead's on the Manager, and a Manager's on the Oracle.
  A departure the Oracle records is signed off at once. **(proposed)**
- A departure passes up a sign-off chain: the Lead, then the Manager, then the Oracle.
  `departure_decide(departure_id, decision, reason, solution)` takes `agree` or
  `push_back`, and only the next level may call it.

| State | Waits on | `agree` moves it to |
|---|---|---|
| `open` | The file's Lead, or the recorder's parent level | `lead_agreed`, or the next state up |
| `lead_agreed` | The phase's Manager | `manager_agreed` |
| `manager_agreed` | The Oracle | `signed_off` |

- Every decision needs a reason, and `departure_decisions` keeps each one.
- A pushback needs a suggested solution and sets `pushed_back`.
  - A Lead pushback is a return. The Lead calls `return_work` next, and it counts as a
    fix attempt.
  - A Manager or Oracle pushback resumes the chain below the decider, down to the same
    Coder. The ledger reopens the file for that Coder, even after approval, and refuses
    when another live claim holds the path. It un-releases the agents below the
    decider, records a fix attempt, posts the departure, decider, reason, and solution
    to each, sets the module to `returned` and a handed-up phase to `working`, and owes
    a wake-up from each level to the next. The result's `next` is the decider's
    wake-up.
  - A pushback on a file whose handoff is still `submitted` returns that handoff.
    **(proposed)**
  - A pushback on a departure with no file resumes the chain to the recorder but blocks
    no gate, since no approval can mark it reworked. **(proposed)**
- Approving the reworked file's next handoff, or accepting it as incomplete, marks the
  pushed-back departure `reworked`.
- The Manager accepts a module only after it decides every departure in the module. The
  Oracle accepts a phase, and finishes the run, only after every departure is signed off
  or reworked.

### Shortfalls

`shortfall_record(body, file_id)` records a solution that works but that nobody found
better. Any role may call it. It needs no decision, and the report lists it for work
outside the run.

### Deferrals and scope changes

- Any agent can suggest that work happens later, or that the scope changes, with
  `deferral_propose(body, file_id)`. `accept_incomplete` opens one too.
- A suggestion takes effect only when the responsible level agrees.
  `agreement_decide(deferral_id, "agreed" | "denied", reason)` needs a caller whose role
  ranks at or above the proposer's parent role.
- An open deferral on a file blocks `phase_update(approved)` for that file's phase. Any
  open deferral blocks `run_finish`.

The responsible level **(proposed)**:

| What changes | Who agrees |
|---|---|
| A file's task or its tests | The Lead |
| A module's scope, or a contract between files | The Lead, and the Manager when another module is affected |
| A phase's scope, or a contract between modules | The Manager |
| The phase plan, or work moved to a later phase | The Oracle |
| What the PRD asks for | The user, through the Oracle |

### Overrides

- `override_grant(rule, target_agent_name, target, reason)` is the Oracle's alone. The
  `pre_ledger` hook denies it to any other caller.
- An override is narrow: one rule, one agent, one target, one use. **(proposed)** The
  ledger consumes it on first use.
- Two rules take overrides: `write` (the target is the repo-relative path) in the write
  gate, and `shell` (the target is the exact command) in the shell gate.
- Every override is a ledger record, and the report lists it with its reason.
  **(proposed)**
- The Oracle must not use an override to write a project file itself. **(proposed)**
  The Oracle's agent file has no write tool.

### Directives

- `directive_submit(source, sender_name, body, reply_to)` steers the run from any input.
  Sources: `user-chat`, `outside-session`, `skill`, and `watchdog`. It needs no
  identity, so an ordinary session can call it.
- A directive does not interrupt the agents. The Oracle reads `directive_inbox()` at
  safe points and turns each directive into a plan change, a guideline change, a brief,
  or a message. It never forwards a directive's text down the tree.
- Every directive carries full authority, whatever its source. The Oracle acts without
  a confirmation step.
- `directive_resolve(directive_id, outcome, resolution)` closes it. The Oracle's
  outcomes are applied, scheduled, declined, or needs-user. **(proposed)** The report
  lists each directive with its source and outcome.
- `run_finish` refuses while a directive is open.

### What the Oracle tells the user

| Situation | The Oracle |
|---|---|
| Only the user can unblock it, and the run can continue afterwards | Notifies the user now, with `run_pause` |
| An issue ends round 3 below the floor | Notifies the user now |
| It works, but nobody found better: a shortfall or a signed-off departure | Lists it in the final report |

A notification states what is blocked, what the user needs to do, and what resumes
after. **(proposed)**

## 7. Sessions

### Starting a child

`agent_spawn(caller, child_name)` refuses unless the caller is the parent named in the
child's unacknowledged brief. It also refuses a Manager before a `repo_check`, a child
that is already registered, a session name a running session holds, and a start past
`parallelism_cap` when the user set one. It then runs, in the repo root:

```
claude "You are <name>. Read your brief from the swarm ledger and follow it." --bg \
  --name <session name> --agent swarm-<role> --model <brief model> \
  --permission-mode <file value> --strict-mcp-config --mcp-config <config> \
  --allowedTools <file tools> --settings '{"worktree":{"bgIsolation":"none"}}'
```

- The prompt goes before the options, because `--allowedTools` takes a space-separated
  list and swallows a prompt that follows it.
- `<config>` holds the ledger's HTTP URL, the agent file's `mcpServers`, and the a11y
  servers when the host has a11y installed.
- It waits up to 15 seconds for the session to appear in `claude agents --json`, then
  records the child's row: `agent_id` = the session id, `session_name`, `bg_id`, state
  `registered`. A session that does not appear is stopped and the call fails.
- The parent ends its turn after its spawns. A child's message wakes it.
- The claude binary is `SENTINEL_SWARM_CLAUDE` when set, else `claude`.

### Identity

- Every tool takes `caller`, the agent's name, and `agent_id`. The `pre_ledger` hook
  stamps `agent_id` with the session id through `updatedInput`, over any value the
  agent passes. `ledger_info`, `brief_get`, `who_owns`, `directive_submit`, and
  `events` take no identity.
- `brief_ack` binds a name to a session. After that, every call resolves `agent_id` to
  its row and refuses a `caller` that does not match, or an agent that has ended.
- The Oracle is bound at `run_start`.

### Agent states

`registered` → `working` ↔ `idle` → `handed_up` → `released`.

- `brief_ack` sets `working`. The Stop hook sets `working` to `idle` when a turn ends.
  The next tool use or session start sets it back to `working`.
- `handoff_submit` sets the Coder to `handed_up`. `return_work` sets it to `idle`.
- Release sets `released` and `ended_at`.

### Wake-ups owed

The ledger cannot send a message to a session: `SendMessage` is a session tool, and the
CLI has no command for it. So each step that leaves work for another agent records a
wake-up the caller owes, and returns a `next` field with the exact call to make.

- Steps that owe a wake-up: `handoff_submit`, `return_work`, `message_post`,
  `phase_update(handed_up)`, a returned `module_review` or `phase_review`, `cr_open`,
  `cr_accept`, `cr_complete`, a failed `cr_verify`, and each level of a departure
  pushback chain.
- `next` is `SendMessage(to="<session name>", message="<one-line pointer>")` when the
  recipient's session runs, and `agent_resume(target_name=...)` when it does not.
- The `post_any` hook clears the debt when it sees a `SendMessage` to that session name.
- The Stop hook blocks a Manager, Lead, or Coder that still owes a wake-up and names each
  call. **(proposed)**
- The Coder's Stop hook also blocks once, per Coder, when the Coder stops while working
  with no handoff, and names `handoff_submit` or a `message_post` to its Lead.

### Messages

- `message_post(to_name, body)` sends to any agent registered in the run and refuses
  any other name, listing the registered ones. Any agent may message any other.
- `message_inbox()` returns and marks read the caller's unread messages. Every role
  reads it at the start of each turn after a wake-up.
- A `SendMessage` body is one line that points at the ledger record, for example
  "Handoff 1 for hello.py is waiting in the ledger." The detail lives in the ledger.

### The Oracle's Stop hook

- It never blocks while the run is paused.
- It does not block for pending work while any Manager, Lead, or Coder is working:
  waiting on running children is a valid stop. **(proposed)**
- Otherwise it blocks while the run has unlocked phases, submitted handoffs, live file
  claims, handed-up phases without an accepted review, or live agents none of which is
  working. The reason names each idle agent to wake and the call to wake it, and
  suggests `run_pause` when only the user can unblock the run.
- It also blocks while the run is active and the watchdog listener is not armed, even
  while children work. See "Watchdog".
- It does not block a stop that follows its own block (`stop_hook_active`).

### Liveness, resume, and release

- A session is running when `claude agents --json` shows a `pid` and its status and
  state are not `stopped`, `exited`, `crashed`, `failed`, `killed`, `dead`, or
  `completed`. State `done` counts as running.
- `agent_resume(target_name)` resumes a live agent of the run whose session is not
  running, with `claude --resume <session id> --bg "<pointer>"`. The pointer is the text
  of every wake-up the caller owes the target, or "Re-read your brief and your inbox in
  the ledger." It refuses a running session, because a resume of a running session
  starts a second copy. Any live agent of the run may call it, except on itself.
- Release sets the row `released` and stops the session with `claude stop <bg_id>`,
  which frees its memory. The Oracle's session is never stopped this way.
- `agent_release(target_agent_id)` releases a child of the caller. The other releases
  are automatic.

### What stops when

| Event | What stops |
|---|---|
| `approve` or `accept_incomplete` | The Coder's session |
| `agent_release` by a Manager after `module_review` accepts | The Lead's session |
| `phase_update(approved)` | The phase's Manager and every live agent under it |
| `run_finish` | Every agent still live except the Oracle |
| About 3 seconds after `run_finish` | The server waits up to 5 minutes for the Oracle's last turn to end, stops the Oracle's background session, and exits. An interactive Oracle is the user's terminal and keeps running |
| `idle_exit_minutes` with no active run, or a paused run, and no session of the run running | The ledger server |
| A run that is not `active`, or a newer listener | The watchdog listener |

### Parallelism

- No limit by default. `agent_spawn` starts every session the plan calls for.
- When the user sets `parallelism_cap`, `agent_spawn` refuses at the cap, counting the
  run's live agents, the Oracle included. The parent tries again after a release.
  **(proposed)**
- A swarm session starts only its role's MCP servers, about 300 MB of memory each.

## 8. The ledger server

- One server runs per host repo, over HTTP on `127.0.0.1`, with MCP at `/mcp` and a
  health check at `/health`. Every session of the run connects to it. One process is
  required: `graph_upsert` protects the code graph with a lock inside it.
- `python -m swarm_ledger.serve [--repo <root>]` binds the port saved in
  `.sentinel-swarm/server.port`, or a free port when that one is taken, and writes
  `.sentinel-swarm/server.json` with `url`, `port`, `pid`, and `started_at`. A second
  start finds the first one answering, prints its URL, and exits 0. The saved port
  lets a resumed session reach the ledger at the URL it started with.
- `serve.ensure_server(repo_root)` starts the server detached when it does not answer,
  waits until it answers, and returns its URL. On Windows it starts with a hidden
  console (`CREATE_NO_WINDOW`).
- Errors go to `.sentinel-swarm/server.log`.
- Tool calls run one at a time under one lock, on one SQLite connection.
- The server loads the settings file once, at start. `profile_set` changes the commands
  for the server process and the run's settings snapshot, not the file. The hooks read
  the file on every call, so the Coder's shell gate follows the file's commands.
- The watchdog runs on a thread inside the server.
- Lifetime: the launcher starts the server before the Oracle. It exits after
  `run_finish`, and after `idle_exit_minutes` with no active run, or a paused run, and
  no session of the run running. A ledger tool call restarts the idle clock. A failed
  `claude agents --json` counts as no session running.
- The plugin's `.mcp.json` also declares a stdio `swarm-ledger` entry. Swarm sessions
  do not use it, because they start with `--strict-mcp-config`.

### Records folder

`.sentinel-swarm/` at the root of the main checkout holds `ledger.db`, `versions/`,
`report.md`, `server.json`, `server.port`, `server.log`, and the hook shim `hook.py`.
A worktree's `.git` file resolves to the main checkout, so every worktree shares one
ledger. The folder is excluded through `.git/info/exclude`, never the host's
`.gitignore`. **(proposed)** One ledger holds every run in the repo. The swarm writes
the project's code in the host repo itself. Tracking is `local` only.

### Database

- Every connection opens with WAL, `synchronous=NORMAL`, foreign keys on, and a 5-second
  busy timeout. Every write runs inside `BEGIN IMMEDIATE`. **(proposed)**
- `migrate` applies `schema.sql` at schema version 1 and adds later columns and tables
  idempotently.
- Tables, grouped: the plan (`runs`, `phases`, `phase_deps`, `modules`, `files`,
  `guidelines`); agents (`agents`, `agent_events`, `briefs`, `wakeups`); review
  (`handoffs`, `reviews`, `scores`, `test_runs`, `versions`, `attempts`, `issues`,
  `ideas`); agreements (`change_requests`, `departures`, `departure_decisions`,
  `deferrals`, `overrides`); communication (`messages`, `directives`,
  `watchdog_findings`). Read `schema.sql` for the columns.
- `agent_events` is append-only, and nothing deletes rows when a run finishes.
  **(proposed)**
- Each run records the plugin version and a settings snapshot. **(proposed)**

### Tools

`mcp/src/swarm_ledger/server.py` registers every tool as a thin wrapper over a `Ledger`
method. A refused call raises a tool error with the reason. A gate never returns a
partial success.

| Group | Tools |
|---|---|
| Run and plan | `run_start`, `run_status`, `run_pause`, `run_finish`, `profile_set`, `guidelines_set`, `guidelines_get`, `phase_add`, `phase_update`, `plan_unlocked`, `module_add` |
| Repo | `repo_check`, `repo_branch_create` |
| Briefs and agents | `brief_create`, `brief_get`, `brief_ack`, `agent_spawn`, `agent_resume`, `agent_release` |
| Ownership | `claim_file`, `release_file`, `who_owns` |
| Messages and directives | `message_post`, `message_inbox`, `directive_submit`, `directive_inbox`, `directive_resolve` |
| Tests and graph | `tests_run`, `graph_upsert` |
| Review | `score_record`, `handoff_submit`, `review_compare`, `approve`, `return_work`, `attempt_record`, `accept_incomplete`, `version_restore`, `module_review`, `phase_review` |
| Issues | `issue_open`, `issue_list`, `issue_close`, `idea_record`, `issue_escalate` |
| Agreements | `cr_open`, `cr_accept`, `cr_complete`, `cr_verify`, `cr_list`, `departure_record`, `departure_decide`, `shortfall_record`, `deferral_propose`, `agreement_decide`, `override_grant` |
| Reporting | `status_tree`, `report_build`, `analytics_query` (one read-only SELECT, Oracle only), `events`, `ledger_info` |

- `tests_run(scope, target)` is role-bound: `file` to the Coder (its own path or test
  path only), `module` to the Lead, `phase` to the Manager, `full` to the Oracle. It
  runs the profile's test command with `{target}` replaced, or removed for no target,
  with the ledger's own venv dropped from `PATH` and `VIRTUAL_ENV`, and a 600-second
  timeout. It parses pytest and Go output. An agent never reports a test result itself.
- `version_restore(version_id)` restores a saved version onto the Coder's own file.

## 9. Hooks and enforcement

### Where the hooks live

- Each role's hooks live in the frontmatter of its project agent file,
  `.claude/agents/swarm-<role>.md`. `hooks/hooks.json` carries no hooks, so no hook
  runs twice.
- Claude Code runs an agent file's frontmatter hooks only in a trusted folder.
- The hooks are the user's to edit, and a user can weaken a hook gate in their own
  project. The ledger tools' gates apply whatever the files say. A rerun of setup
  restores every ledger hook the template has.

| Event | Matcher | Ledger hook event | Roles |
|---|---|---|---|
| `SessionStart` | all | `session_start` | all |
| `PreToolUse` | `Agent` | `pre_agent` | all |
| `PreToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit` | `pre_write` | all |
| `PreToolUse` | `Bash\|PowerShell` | `pre_shell` | all |
| `PreToolUse` | `Monitor` | `pre_monitor` | all |
| `PreToolUse` | `mcp__swarm-ledger__.*` | `pre_ledger` | all |
| `PostToolUse` | all | `post_any` | all |
| `PostToolUse` | `Bash\|PowerShell` | `post_shell` | Coder |
| `PreCompact` | all | `pre_compact` | all |
| `Stop` | all | `stop` | all |
| `SessionEnd` | all | `session_end` | all |

### The shim

Every hook command is
`python3 .sentinel-swarm/hook.py hook <event> || python .sentinel-swarm/hook.py hook <event>`.
`.sentinel-swarm/hook.py` is a standard-library shim that setup copies from
`templates/hook_shim.py`. It has three commands:

- `hook <event>` finds the sentinel-swarm install for this repo in
  `~/.claude/plugins/installed_plugins.json` (scope `local`, then `project` with a
  matching `projectPath`, then `user`), and runs
  `uv run --project <installPath>/mcp --frozen --no-dev python -m swarm_ledger.hooks <event>`
  with stdin and stdout passed through. A plugin upgrade changes the registry, not the
  agent files.
- `mcp <plugin_id> <server>` starts another plugin's MCP server the same way, from its
  `.mcp.json`, with `${CLAUDE_PLUGIN_ROOT}` and `${VAR:-default}` expanded.
- `watch` runs `python -m swarm_ledger.watch` and passes each line through with no
  timeout.

When the registry, the install, `uv`, or the ledger hook fails, or a hook runs longer
than 50 seconds, a gating event (`pre_agent`, `pre_write`, `pre_shell`, `pre_monitor`,
`pre_ledger`) answers `deny` with the reason, and every event adds a `systemMessage`
that says to run `/sentinel-swarm:setup`. The shim exits 0.

### Rules every ledger hook follows

- A hook blocks by printing a JSON decision and exiting 0. It never uses exit code 2.
  **(proposed)**
- A broken ledger hook allows: `swarm_ledger.hooks` catches every exception, writes one
  line to stderr, and prints nothing.
- A hook ignores a caller the registry does not know, so a non-swarm session in the
  same repo passes. **(proposed)**

### What each hook does

- `session_start`: for a swarm session, records the transcript path and sets an idle
  agent to working. For any other session, it reports an active or paused run, reports
  a missing `knowledge/code_graph.db`, and adds `.sentinel-swarm/` to the git excludes.
- `pre_agent`: denies `Agent` to every swarm session.
- `pre_write`: while a run is active or paused, denies anyone a write into the records
  folder. Denies a write by any role but the Coder, and a Coder's write outside its
  claimed path and test path, naming the owner. An override of rule `write` lets one
  write through.
- `pre_shell`: while a run is active or paused, denies the shell to every role but the
  Coder. A Coder may run a command that starts with the profile's test, build, or lint
  command, or read-only git (`status`, `diff`, `log`, `show`, `ls-files`, `branch`). A
  command with `;`, `&`, `|`, `<`, `>`, a backtick, a newline, or `$(` is denied. An
  override of rule `shell` lets one command through.
- `pre_monitor`: allows exactly one `Monitor` call from a swarm session, the Oracle's
  watchdog call. It denies every other one and names the allowed call. A session not
  yet in the ledger counts as a swarm session when its `agent_type` is `swarm-<role>`.
- `pre_ledger`: stamps `agent_id`, and denies `override_grant` to anyone but the
  Oracle.
- `post_any`: writes the heartbeat and current activity, sets an idle agent to working,
  records the transcript path, records the watchdog arm time, clears owed wake-ups on a
  `SendMessage`, and after a Coder's write marks its file stale, so the handoff needs a
  newer self review.
- `post_shell`: after a Coder's shell call, lists changed paths with `git status`.
  A change outside the Coder's claim, other claimed files of the run, the records
  folder, and `knowledge/` is recorded as a `violation` event and posted to its Lead.
- `pre_compact`: adds one to the agent's `context_overflow_count`.
- `stop`: records tokens and cost from the transcript, then applies the stop rules in
  "Sessions". After a run finishes, the Oracle's stop refreshes the report.
- `session_end`: records tokens and cost and the end reason. After a run finishes, it
  refreshes the report.

### Rule-to-enforcement map

| Rule | Enforced by |
|---|---|
| Only the Coder writes project files | `pre_write`, and no write tool or shell in the other agent files |
| A Coder writes only its own file and test file | `pre_write` for edit tools; `pre_shell` and `post_shell` for the shell |
| Nobody edits the records by hand | `pre_write` |
| A role starts only its own child role | `brief_create` and `agent_spawn`; `pre_agent` denies `Agent` |
| A model comes from the approved list | `brief_create` |
| No agent starts without a brief | `agent_spawn` and `brief_ack` |
| No agent fakes its identity | `pre_ledger` stamps `agent_id`; every tool matches `caller` to it |
| A handoff needs passing tests and a current graph | `handoff_submit` |
| No approval without a handoff and two sets of scores | `approve` |
| The Lead scores before it sees the Coder's scores | `score_record` refuses a Lead review after `review_compare` |
| A rule gives way only to the Oracle | `override_grant`, consumed by `pre_write` and `pre_shell` |
| Look in the graph before writing | codebase-kg's own search gate hook |
| A message goes to an agent of the run | `message_post` |
| Only the owner's chain escalates an issue | `issue_escalate` |
| A finished Lead or Manager is released | `phase_update(handed_up)` refuses a live Lead; `phase_update(approved)` releases the phase; `run_finish` releases the rest |
| A non-owner requests, the owner changes | `cr_open` routing and the write gate |
| A change request closes with evidence | `cr_complete` and `cr_verify`; gates on `handoff_submit`, `approve`, `phase_update(approved)`, `run_finish` |
| A departure is signed off or reworked | `departure_decide`; gates on `approve`, `return_work`, `accept_incomplete`, `module_review`, `phase_review`, `run_finish` |
| The Oracle checks the repo before a Manager starts | `agent_spawn` |
| A phase hands up only after every module review | `phase_update(handed_up)` |
| A phase is approved only after the Oracle reviews it | `phase_update(approved)` |
| Code the graph maps has a test file by the end | `phase_review(accepted)` |
| A child wakes its parent after each step | The Stop hook, from the owed wake-ups |
| The Oracle keeps working while the run has work | The Oracle's Stop hook |
| The Oracle's watchdog listener stays armed | The Oracle's Stop hook and `pre_monitor` |

## 10. Watchdog

The watchdog is a background job, not a role. It detects and reports. It never resumes,
replaces, or stops a Manager, Lead, or Coder. It resumes the Oracle when the Oracle's
session is not running, so that a report can reach the Oracle at all. For any other
agent's failure, it reports to the Oracle, and the Oracle acts.

The mechanics below are **(proposed)**.

### Detection

A thread in the ledger server (`watchdog.py`) runs every `interval_seconds`. It reads
`claude agents --json` and scans the active run. It skips a paused run, and a failed
listing skips one pass.

| Kind | Condition | Next step the report names |
|---|---|---|
| `crashed` | A Manager, Lead, or Coder is `working`, or `registered` for more than 2 minutes, and its session is missing or not running. An `idle` or `handed_up` agent whose session exited is normal | `agent_resume(target_name=...)` |
| `stuck` | The session runs, the agent is `working`, and its last heartbeat is older than `stuck_minutes` | Message it, or have its parent replace it |
| `waiting_permission` | The session waits on a permission prompt. Not also reported as stuck | Tell the user to open that session and answer |
| `spinning` | The agent's last `spin_failures` test runs for one scope and target all failed | Ask its parent to `return_work` or `issue_escalate` |
| `context_high` | The latest request of the agent, the Oracle included, fills `context_pct` of its window | Have its parent replace it. For the Oracle: `run_pause`, then resume in a fresh session |
| `stalled` | No session of the run runs; or sessions run, but no member session is busy, no member is `working`, and nothing changed for 2 minutes | Wake the agent whose work is pending: the reviewer of a submitted handoff, an agent with unread messages, or the Lead of a file with an open issue |

- The context size is the input, cache read, and cache creation tokens of the last
  assistant response in the agent's transcript. The window is 200,000 tokens when the
  model names Haiku, else 1,000,000, unless the `context_window` setting names one.
- For the idle stall, "nothing changed" reads the latest agent event, message, and test
  run, and the last write of the Oracle's transcript. The Oracle's own session status
  does not count, because its armed Monitor keeps it `busy`.
- A stall is reported only when two passes in a row see it, since a run passes through
  "no session running" while one session exits and another resumes.

### Reporting

- `watchdog_findings` keeps one live row per run, agent, and kind. A finding the next
  pass does not see is cleared. One that comes back gets a new row and a new report.
- Each new finding becomes a directive with source and sender `watchdog`. Its body names
  the agent, its session, the kind, the detail, and the next step. The Oracle resolves
  each one; open directives block `run_finish`.

### How a report reaches the Oracle

- The listener is `python -m swarm_ledger.watch`, run as `hook.py watch`. Every 2
  seconds it prints one line per unnotified watchdog directive, marks it notified, and
  writes `runs.watch_heartbeat_at`. It exits when the run is not active. The newest
  listener owns the run, and an older one exits.
- The Oracle arms it right after `run_start`, and again whenever it expires, with
  exactly this call:
  `Monitor(command="python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch", description="sentinel-swarm watchdog", timeout_ms=1800000)`.
  Each printed line wakes the Oracle.
- The Oracle's Stop hook blocks while the run is active and the listener's heartbeat is
  missing or older than 60 seconds, or the armed Monitor expires within 30 seconds.
- `directive_inbox` marks the directives it returns as notified.
- When a watchdog directive stays unnotified for one interval and the Oracle's session
  is not running, the server runs
  `claude --resume <Oracle session> --bg "The watchdog reported N finding(s). Read directive_inbox."`,
  at most once every 5 minutes. After 3 attempts with no Oracle heartbeat since the
  first, it pauses the run with the reason "the watchdog could not wake the Oracle".

### Settings

Under `watchdog` in `.claude/sentinel-swarm.local.md`: `interval_seconds` (30),
`stuck_minutes` (15), `spin_failures` (5), `context_pct` (80), `context_window` (unset:
taken from the model), `idle_exit_minutes` (15).

## 11. MCP servers per role

- Every role has `swarm-ledger` and `codebase-kg`. Every session starts with
  `--strict-mcp-config`, so no server loads that its config does not list.
- A role also gets the a11y plugin's servers, `a11y-tools` and `a11y-kg`, when the host
  has `a11y@accessibility-tools` installed at user scope or for the host's path.
  `agent_spawn` and the launcher check the installed-plugins registry and add the
  servers through the shim, and their tools to `--allowedTools`.
- `setup` also adds the MCP servers of every plugin that the host's
  `.claude/settings.json` enables to a newly written role file's `mcpServers` and
  `tools`. User-level plugins, user MCP servers, and claude.ai connectors never join.
- Only the future Driver will load the driver plugins.
- Tool names: `mcp__swarm-ledger__<tool>` and `mcp__codebase-kg__<tool>`.
- Each role keeps a fixed `tools` allowlist in its agent file.
- `cartographer` is an optional dependency for end-to-end testing outside the run.

## 12. The code graph

codebase-kg is a required dependency (`>=0.7.0`). An up-to-date graph lets each agent
find what exists while many agents change the code at once. The graph file,
`knowledge/code_graph.db`, belongs to the host repo and is committed with its code.

- Every role searches the graph first, at its own level **(proposed)**, and uses Grep
  or Glob only when the graph lacks the answer. codebase-kg's own search gate hook
  enforces this order. An agent that falls back records what the graph was missing.
  **(proposed)**
- Only the Coder writes to the graph, and only through `graph_upsert(nodes)`. It takes
  codebase-kg's `kg_upsert_node` node shape (`id`, `kind`, `section`, `description`,
  `anchors`, `edges`). The ledger refuses an anchor outside the Coder's file and test
  file, and an edge to a node that does not exist. It applies the upsert under a lock
  in the ledger process, through `uv run` in the codebase-kg plugin's cache folder, or
  `SENTINEL_SWARM_KG_ROOT`. **(mechanism proposed)**
- A node that anchors on several files is updated through the Lead. **(proposed)**
- Anchors are `"<path>#<Symbol>"` for every top-level function and class. A file with no
  functions or classes is anchored by its path alone: `"anchors": ["<path>"]`. A Coder
  never adds code only to have an anchor.
- The Coder updates the graph before its handoff, and again after any later change.
- **Graph current at handoff:** a node anchors on the file, each anchored symbol appears
  in the file as a whole word, and, for a Python file, every public top-level function
  and class is anchored. The file-content hash is a review signal for the Lead and the
  git hooks, not a gate.
- **Test-file gate:** `phase_review(accepted)` refuses an approved or incomplete file
  with no test file when the graph anchors any symbol in it. Test anchors in the graph
  do not count as tests.
- The `setup` skill builds the graph when the host has none. An empty repo starts with
  an empty graph. **(proposed)**

## 13. The run report and costs

`report_build()` writes `.sentinel-swarm/report.md`. `run_finish` rebuilds it, and the
Oracle's last stop or session end refreshes it with the Oracle's final tokens. Each run
overwrites the previous run's report. The user copies it out to keep it. **(proposed)**

Sections **(contents proposed)**:

- Outcome, duration, and the pause reason for a paused run.
- Repo: the run's branch and the last `repo_check`.
- One section per phase, timed from its Manager's start: each module and each file with
  its state and latest Lead scores.
- Manager and Oracle reviews.
- Returns and fix attempts: path, fix round, who returned it, target dimensions,
  outcome, and issues.
- Open items: open deferrals, and every issue by round with its attempts and resolution.
- Departures, each with its full decision chain and final state.
- Shortfalls.
- Change requests, with decision, work done, evidence, and verification.
- Overrides, with reasons.
- Directives, with source and outcome.
- Agents: model, tokens, elapsed time, tool uses, context overflows, and cost, plus a
  run total.

### Token counting and cost

- The Stop and SessionEnd hooks read the agent's transcript. Claude Code writes one API
  response as one line per content block, each repeating the response's usage, so the
  ledger counts only the last line of each response id.
- Each response is priced at its own model's list rate, with 5-minute and 1-hour cache
  writes priced apart. A cache write with no split is priced at the 1-hour rate. The
  total is stored in `agents.cost_usd`.
- `mcp/src/swarm_ledger/pricing.py` holds the rates per model family. An agent row with
  no stored cost is estimated from its token totals.

## 14. Setup, launch, and settings

### Install

1. Install sentinel-swarm at project scope in the host repo. `--plugin-dir` is not
   enough, because the launcher does not resolve its agents. codebase-kg must be
   installed too.
2. Trust the host folder once: run `claude` in it and accept the trust prompt.
3. Run setup, then the launcher.

### Setup

`python -m swarm_ledger.setup [--repo <root>] [--check-trust]`, or the
`/sentinel-swarm:setup` skill, which also creates the settings file, fills in the
commands from the detected stack, confirms codebase-kg, and builds the graph. Setup:

- Writes `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and
  `swarm-coder.md` from `templates/agents/<role>.md`. For an existing file it keeps the
  user's frontmatter, adds any key the template has that the file lacks, adds every
  ledger hook the template has that the file lacks, and replaces the prompt body.
- Writes the shim to `.sentinel-swarm/hook.py`, overwriting local edits.
- Merges `{"worktree": {"bgIsolation": "none"}}` into `.claude/settings.local.json`.
- Adds `.sentinel-swarm/` and `.claude/agents/swarm-*.md` to `.git/info/exclude`.
- Reports whether the repo is trusted, from `~/.claude.json`, and prints the command
  that trusts it. `--check-trust` only checks.

The role files take effect in the next session. A session loads agent files once, at
startup.

### Launch

`python -m swarm_ledger.launch [--repo <root>] [--bg | --headless [--transcript <file>]] "<prompt>"`,
or the `/sentinel-swarm:run` skill.

- It runs setup when the shim or a role file is missing.
- `--bg` refuses an untrusted repo and prints the trust command.
- It starts the ledger server, then the Oracle with its agent file's model and the same
  flags as `agent_spawn`.
- Interactive by default. `--bg` starts a background session. `--headless` runs
  `claude -p` with a stream-json transcript and the prompt on stdin. Nobody answers
  questions in a headless run, so the Oracle records each assumption in the guidelines.

### Skills

| Skill | Purpose |
|---|---|
| `swarm-protocol` | Shared vocabulary, names, tool order, and rubric keys; loads when the swarm is mentioned |
| `run` | Starts a run with the launcher |
| `plan` | The Oracle's conventions for a phase graph |
| `status` | Reads `ledger.db` read-only and prints the run's state |
| `resume` | Continues a paused or crashed run, one session at a time |
| `setup` | Prepares a host repo |

### Settings file

`.claude/sentinel-swarm.local.md` in the host repo, YAML frontmatter. The template is
`templates/sentinel-swarm.local.md.example`. The file is not committed.

| Key | Default | Meaning |
|---|---|---|
| `tracking` | `local` | The only value |
| `runtime.<role>` | `session` | Recorded on each agent row; every role runs as a session |
| `models.<role>` | See "Models" | Approved models per role |
| `rubric.*` | See "Thresholds" | `target`, `floor`, `criterion_floor`, `disagreement_gap`, `plateau`, `regression_tolerance` |
| `escalation.rounds` | 3 | Escalation rounds |
| `escalation.attempts_per_round` | 3 | Non-improving attempts per round |
| `watchdog.*` | See "Watchdog" | Watchdog timing, thresholds, and server idle exit |
| `test_command` | empty | Must contain `{target}`, for example `python -m pytest -q -p no:cacheprovider {target}` |
| `build_command`, `lint_command` | empty | Optional; the Coder's shell gate allows them |
| `parallelism_cap` | empty | No limit when empty |
| `base_branch` | empty | `repo_check` tries `main`, then `master` |

`-p no:cacheprovider` keeps parallel pytest runs from contending on `.pytest_cache`.

## 15. Testing this repo

### The checks

Run all four from `mcp/` before each commit:

```
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```

`mcp/tests/test_plugin_surface.py` guards the surface contract: the role templates'
frontmatter and hooks, the empty `hooks.json`, and the manifest fields.

This repo maps itself in `knowledge/code_graph.db`. Run `sh .githooks/install.sh` once
per clone. After a change to a mapped file, refresh the graph with
`/codebase-kg:refresh`; the `pre-push` hook reports drift. When the codebase-kg tools
are missing in a session, run `/reload-plugins`.

### The smoke test

`bash scripts/smoke.sh [--headless | --bg | --results] ["<prompt>" | --prd <name>]`

- It empties `runs/hello/` (git-ignored), builds a host repo in `runs/hello/host/` with
  a README, a `pyproject.toml`, a settings file with the pytest test command, a
  one-node code graph, and `git init -b main`. It copies the plugin to a fresh temp
  folder, installs it at project scope, runs setup, commits, and starts the Oracle
  through the installed copy's launcher.
- No flag opens an interactive Oracle. `--bg` checks trust first, then starts a
  background session; `bash scripts/smoke.sh --results` prints the results when it
  finishes. `--headless` runs `claude -p` and writes `runs/hello/transcript.jsonl`.
- The default prompt asks for `hello.py`, which writes `Hello, world!` to
  `hello_world.txt`. After the run, `smoke.sh` runs `hello.py` or `hello_world.py` and
  prints the file, because the shell gate lets the swarm run only test commands.
- `KG_PLUGIN_DIR` picks a codebase-kg version other than the newest in the cache.
  `CLAUDE_BIN` or `SENTINEL_SWARM_CLAUDE` picks the claude binary.
- From PowerShell, call Git Bash explicitly:
  `& "C:\Program Files\Git\bin\bash.exe" scripts/smoke.sh`. Plain `bash` can resolve
  to WSL.
- A headless run needs an explicit `--allowedTools` list. Do not use
  `--dangerously-skip-permissions`.

| Test | Command | Time | Cost |
|---|---|---|---|
| Quick | `bash scripts/smoke.sh --bg` | About 6 minutes | About $1.50 |
| Complete | `bash scripts/smoke.sh --bg --prd modules` | About 23 minutes | About $6 |

- `--prd modules` runs `scripts/prds/modules.md` in three waves: `hello` and `world` in
  parallel, then `name` (a `World` subclass), then the command that uses all three.
- `--prd textstats` runs `scripts/prds/textstats.md`, a larger two-module PRD.

### After a run

Judge a run from `runs/hello/host/.sentinel-swarm/ledger.db`, not from the transcript.
Check that:

- The run is `finished` with outcome `success`, and every phase is `approved`.
- Every file is `approved`, `incomplete`, or `superseded`, and no claim is live.
- Every agent row has an `ended_at`, a model, and token counts, and every row but the
  Oracle's has a parent.
- Each file has a self review and a Lead review, a compared and approved handoff, and a
  passing file-scope test run. Each phase has a passing full-scope run by the Oracle
  after its hand-up.
- Every module has an accepted `module_review` and every phase an accepted
  `phase_review`.
- `watchdog_findings` and the directives match what happened, and every directive is
  resolved.
- `report.md` exists and matches the ledger.

Then confirm that nothing is left running:

- No `swarm_ledger` process remains. In PowerShell:
  `Get-CimInstance Win32_Process | ? CommandLine -match swarm_ledger`.
- `.sentinel-swarm/server.json` is gone.
- The run's sessions are stopped in `claude agents`. Do not `claude rm` them: Alex
  reviews them in agent view.

## 16. Platform facts

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

## 17. Key decisions

- 2026-09-20: Four roles: Oracle, Manager, Lead, Coder, with their colors.
- 2026-09-20: One Manager per phase; phases without a dependency between them run at the same time.
- 2026-09-20: Test-first is the ideal, not a gate; the hard check is tests that exist and pass at handoff.
- 2026-09-20: Escalation is 3 rounds of 3 non-improving attempts, a budget of 9.
- 2026-09-20: The registry records each agent's activity, times, tokens, context use, and overflows.
- 2026-09-20: The settings hold a list of approved models per role; the parent picks by task complexity.
- 2026-09-20: Enforcement is a hard block, and only the Oracle overrides.
- 2026-09-20: Tracking is the local ledger only.
- 2026-09-20: codebase-kg is required; other sibling plugins are optional.
- 2026-09-20: No shell above the Coder; Oracle, Manager, and Lead work through MCP tools only.
- 2026-09-20: The Coder's shell is gated, and ledger claims are the file locks.
- 2026-09-20: The Oracle's Stop hook keeps it working until every item passes, stalls, or waits on the user.
- 2026-09-20: The Lead scores blind, before it sees the Coder's scores.
- 2026-09-20: Each dimension is scored separately, every time.
- 2026-09-21: The ledger identity is the agent's name.
- 2026-09-21: Directives steer the Oracle from any source, carry full authority, and carry watchdog reports.
- 2026-09-21: Every piece is tested alone before the smoke test.
- 2026-09-21: The graph hash is a review signal; `handoff_submit` checks anchors and coverage only.
- 2026-09-21: `graph_upsert` is a ledger tool that takes the lock and applies the upsert in one call.
- 2026-09-21: No telephone game: every plan and task is a ledger record.
- 2026-09-22: Each role keeps a fixed `tools` allowlist in its agent file.
- 2026-09-22: The allowlist defaults to the plugins enabled at the host's project scope; user-level plugins and connectors never join.
- 2026-09-22: Each host gets its own role files in `.claude/agents/`, generated from the templates.
- 2026-09-22: The smoke test runs in `runs/hello/` and must leave a `hello_world.txt` made by running the code.
- 2026-09-22: `message_post` refuses a name not registered in the run.
- 2026-09-22: Only the file owner's chain escalates an issue.
- 2026-09-22: A persona from the user's own settings may appear in messages to the user.
- 2026-09-23: Every role runs as its own session in the one working tree.
- 2026-09-23: Each role's hooks live in its project agent file's frontmatter.
- 2026-09-23: A child wakes its parent with a cross-session `SendMessage`, and hooks enforce it.
- 2026-09-23: The ledger names which idle agent to wake.
- 2026-09-23: A blocked run pauses with `run_pause`, and a resume continues it from the ledger.
- 2026-09-23: A finished Lead or Manager is released, which stops its session.
- 2026-09-23: Runs are interactive by default.
- 2026-09-23: No parallelism cap by default.
- 2026-09-23: A swarm session starts only the MCP servers its role needs.
- 2026-09-23: One swarm runs per repo.
- 2026-09-24: No role runs subagents or workflows; each role starts only the role below it.
- 2026-09-24: A Lead reviews its Coders' work itself.
- 2026-09-24: The settings file holds the approved models; each agent file holds the role's default model.
- 2026-09-24: The Oracle's default model is Opus, with Fable approved.
- 2026-09-24: Every role has swarm-ledger and codebase-kg; a11y joins when the host has it installed; only the Driver gets the driver plugins.
- 2026-09-24: The Driver's responsibilities, with its mechanics still to design.
- 2026-09-24: The watchdog resumes the Oracle when it can and reports every other failure to the Oracle.
- 2026-09-24: The ledger server exits when the run finishes, and after `idle_exit_minutes` on an abandoned or paused run.
- 2026-09-24: A finished run leaves nothing running; the server stops the Oracle's background session.
- 2026-09-24: The context window follows the model; `context_window` overrides it.
- 2026-09-24: A rerun of setup restores every ledger hook.
- 2026-09-24: A change request is a ledger record plus a wake-up, routed owner, Lead, Manager, Oracle, and closed only with verified evidence.
- 2026-09-24: The run report lists each change request and each Lead return.
- 2026-09-24: A file with no code may be claimed without a test file; `phase_review` refuses mapped code with no test file.
- 2026-09-24: Departures pass up the Lead, Manager, Oracle sign-off chain; a pushback carries a solution and resumes the chain down to the same Coder.
- 2026-09-24: The Manager accepts a module, and the Oracle a phase or the run, only after every departure is decided.

## 18. Still (proposed)

These items await Alex's review. Items marked "not built" are design only.

Principles and roles:

- No state lives only in an agent.
- The name patterns, and a name as the unique address stored in the registry.
- The registry records each agent's model.
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
- An issue a Manager finds starts at round 2. Not built: the ledger opens every issue at
  round 1.
- The responsible-level table for deferrals and scope changes.
- Overrides: recorded with a reason, narrow and single-use, never used by the Oracle to
  write a file.
- While the run waits on the user, the phases the problem does not block keep going.
  Not built: `run_pause` pauses the whole run.
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
  recorded. Not built: `approve` requires the target on every dimension.
- No self-approval, and tool output as supporting evidence only.
- Scored module and phase reviews on completeness, integration, and open items. Not
  built: `module_review` and `phase_review` record an outcome and notes, not scores.

Ledger and sessions:

- The records folder location, `.git/info/exclude`, the report in the records folder,
  WAL settings, same-machine resume, append-only events, and the run's settings
  snapshot.
- Other per-role settings: effort, cache lifetime, and a per-role parallelism cap. Not
  built.
- The registry columns, the lifecycle stage names, and the run analytics measures. The
  measures are not built as a report; `analytics_query` reads the raw tables.
- The session mechanics: `agent_spawn`, owed wake-ups and `next`, the Stop hook's
  wake-up rule, `agent_resume`, release that stops the session, the shared HTTP server,
  and the project agent file details.
- `parallelism_cap` counts the run's live agents. Not built: counting other swarms'
  sessions on the machine.
- A lock record for runs that span several repos. Not built.
- A hook that refuses a `SendMessage` to a session outside the caller's run. Not built.
- `repo_check`, `repo_branch_create`, and the Manager spawn gate on `repo_check`.
- `module_review`, `phase_review`, and the hand-up and approval gates, apart from the
  departure rules Alex decided.
- The change request tool set including `cr_list`, and the departure details: refusals
  on `return_work` and `accept_incomplete`, a pushback returning a submitted handoff, a
  pushback on a departure with no file, and a departure the Oracle records being signed
  off at once.
- The directive mechanics: fields, the Oracle as the only reader, the outcomes, safe
  points, and the source and sender in the report. A `reply_to` reply closing its
  notification is not built: the ledger stores `reply_to` only.
- Waiting on running children is a valid stop for the Oracle.
- A hook blocks through JSON and exits 0; hooks ignore callers the registry does not
  know.
- `-p no:cacheprovider` in the test command.
- The `graph_upsert` mechanism, multi-file nodes through the Lead, graph search at each
  role's level, recording graph gaps, and setup building a missing graph.
- The watchdog mechanics in "Watchdog", apart from Alex's rules.
- Plugin features to consider: `monitors/monitors.json`, `subagentStatusLine` and
  `agent` in the plugin's `settings.json`, and `userConfig`. Not built.
- The smoke test's ledger checklist, and a check that every brief body is complete and
  no spawn prompt carries task detail. Not automated.

## 19. Open items

- **CI.** Confirm CI passes on Ubuntu, Windows, and macOS once the GitHub billing block
  resets. Until then, the four local checks are the only checks.
- **macOS.** The code has POSIX branches, but no live run has happened on a Mac. Run one
  smoke test there.
- **Watchdog rescue of a dropped API call.** The idle-stall check is built to catch a
  run whose Oracle API call dropped mid-response. It has not caught one in a live run.
- **PreCompact.** Whether `PreCompact` fires in a long session. It needs a session that
  fills its context.
- **Ledger server early exit.** Once, the server exited about a minute after it started,
  with no error in its log. It has not recurred.
- **The Driver role.** Planned, not built. See `plans/driver-agent.md`.
