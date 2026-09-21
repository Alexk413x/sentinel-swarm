# sentinel-swarm: roles and responsibilities

Status on 2026-09-20: third draft. It covers what each role is responsible for. It does not cover how the plugin implements it. Bullets marked **(proposed)** are Claude's additions that Alex has not reviewed. Everything else comes from Alex's descriptions.

## Principles that apply to every role

1. **Exclusive ownership.** Every unit of work has exactly one owner: a run, a phase, a module, or a file. Nobody else changes it.
2. **Non-owners request, owners change.** An agent that needs a change in work it does not own files a change request with the owner. The owner makes the change. The requester is notified when the change is complete, verifies it, and checks it off.
3. **Planning prevents conflicts.** Each layer plans its breakdown so that two owners rarely need the same file. Change requests are the fallback, not the normal path.
4. **Every layer reviews.** Each layer breaks the work down, follows the project's guidelines, and reviews whether the assigned goals were met. The layers exist to apply different perspectives, so no layer is skipped, even for a single hello-world file.
5. **Evidence, not claims.** A role does not report work as complete without recorded evidence: test results, scores, and review files. A later reader must be able to confirm it from the records alone.
6. **Small scope, visible work.** Each agent holds a small scope. A Coder holds one file. The small scope keeps each agent's context focused and makes what it is doing easy to see.
7. **No state lives only in an agent.** **(proposed)** Every plan, brief, review, score, and decision is recorded. A replacement agent, a later run, or a different swarm can continue from the records.
8. **Only the Coder writes project files.** Every other role works through the shared tooling and records.

## Hierarchy

| Role | Count | Owns | Created by | Reports to |
|---|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user | The user |
| Manager | 1 per phase | One phase | Oracle | Oracle |
| Lead | 1 per folder or module in a phase | One module | Manager | Manager |
| Coder | 1 per file | One file and its unit tests | Lead | Lead |

The minimum run is one of each role.

## The plan

- A phase is one chunk of work with one Manager.
- The Oracle's plan is a dependency graph of phases, not a fixed list. A phase unlocks when the phases it depends on are approved. Phases with no dependency between them run at the same time, such as front-end and back-end work.
- A run has a typical shape:
  - **Foundation.** Setup and shared helper functions come first, because other work is blocked on them.
  - **Scale out.** Finished foundation work unlocks more phases, and the run widens.
  - **Scale down.** Integration phases bring components together into larger components. They need fewer agents, so the run narrows as it closes.
- A join point is where parallel phases feed a later phase, such as an integration phase. The Oracle runs the full test suite at each join point, once every Manager that feeds it has reported in. **(proposed wording)**
- The plan changes during the run. Higher layers adjust it from the findings that come up: they add work now, or they schedule it for a later phase.
- The same shape applies inside a phase and inside a module: a Manager orders its modules, and a Lead orders its files, so that helpers come before the files that use them. **(proposed)**
- Contracts come before implementations. When a Lead's brief fixes the contract for a helper, the Coders that depend on it write their unit tests against that contract with test doubles, and do not wait for the helper. "Blocked" then means that the contract itself is missing or wrong. **(proposed)**

## Models

| Role | Model |
|---|---|
| Oracle | Fable or Opus |
| Manager | Opus |
| Lead | Opus or Sonnet |
| Coder | Sonnet or Haiku |

- The Oracle is an agent that the plugin defines, like the other three roles. The plugin sets its model.
- How the Oracle runs is a mechanics decision with two candidates: **(proposed)**
  - **Main-thread agent.** The session runs as the Oracle (`claude --agent`, or a background session started with that agent). The Oracle talks to the user directly, and Manager, Lead, and Coder fit in the default three subagent layers. Whether the agent's `model` field applies to a main-thread agent needs a prototype.
  - **Subagent of a relay session.** The user's session spawns the Oracle and relays its questions. The chain is then four layers deep, so the plugin must raise `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` to 4. The Oracle cannot question the user directly.
- The plugin's settings hold one list of approved models for each role. The table above is the default list.
- The parent that spawns an agent picks the model from the child role's list. It picks by the complexity of the task at hand, and the brief records the choice.
- The ideal: by the time work reaches a Coder, it is broken down far enough that the Coder runs on the cheaper, faster model.
- The Oracle has no parent. It runs on the first model in its list. **(proposed)**
- Escalation can raise the model. A fresh Coder that the Manager brings in during round 2 or 3 can run on the stronger model in the list. **(proposed)**
- The registry records the model that each agent ran on, so the run analytics can show whether the cheaper models save money or cause more returns. **(proposed)**

## Names and colors

| Role | Color (Alex) | Name pattern **(proposed)** | Example |
|---|---|---|---|
| Oracle | cyan | `oracle` | `oracle` |
| Manager | green | `mgr-<phase>` | `mgr-p2-api` |
| Lead | purple | `lead-<phase>-<module>` | `lead-p2-auth` |
| Coder | orange | `coder-<phase>-<module>-<file>` | `coder-p2-auth-login` |

- Alex asked for dark green for the Manager. The agent `color` field accepts eight values only: red, blue, green, yellow, purple, orange, pink, and cyan. `green` is the closest.
- A name shows the agent's place in the tree. It is also the address that other agents message, so it is unique within a run. The registry stores it. **(proposed)**

## The cycle that every layer repeats

Each parent and child pair follows the same cycle:

1. **Brief.** The parent gives the child a brief: the goal, the scope it owns, what the result must prove, the contracts it must honor, and the guidelines that apply.
2. **Work.** The child does its work. For every role except the Coder, the work is a breakdown, briefs for its own children, and reviews of what comes back.
3. **Self-review.** The child reviews and scores its own result before it hands the result up.
4. **Hand up.** The child hands up the result, its scores, its evidence, and its open issues.
5. **Review.** The parent reviews and scores the result from its own perspective.
6. **Approve, return, or accept as incomplete.**
   - **Approve.** The result meets the brief.
   - **Return.** The parent returns the result with specific issues. A returned result counts as one fix attempt.
   - **Accept as incomplete.** The child reports the work as not complete, with a validated reason. An example is a function that the plan did not include and that other work also needs. The parent validates the reason before it accepts it. This outcome uses no fix attempts. **(the validation step and the attempt rule are proposed)**
   - A validated finding goes up. The higher layers adjust the plan now, or they schedule the fix for a later phase. The ledger tracks every deferred item, and the run cannot finish while one is open. **(tracking rule proposed)**
7. **Release.** A child stays available until its parent approves its work. Then it shuts down.

## Oracle

- **Does:**
  - Reads the PRD and asks the user questions about it.
  - Sets the high-level guidelines for the project before it plans. See "Guidelines".
  - Splits the work into phases and plans them as a dependency graph. See "The plan".
  - Adjusts the plan during the run from the validated findings that the lower layers report.
  - Creates one Manager per phase and briefs it.
  - Audits the scores and reviews from the three layers below. It investigates the low scores to decide what happens next. It does not examine every file in detail.
  - Runs the full test suite at each join point, once every Manager that feeds it has reported in.
  - Decides the outcome: the run is complete, or it needs a new plan or a new phase.
  - Overrides a rule when the run needs it, and records why. See "Enforcement and overrides".
  - Decides whether a problem needs a notification now or a line in the final report.
  - Writes the final summary report.
- **Owns:** PRD meaning, acceptance criteria, the high-level guidelines, the phase plan, the final verdict, and all communication with the user.
- **Arbitrates:** disputes between Managers, including which Manager owns a shared file when they cannot agree. **(proposed)**
- **On a low score or a failed full run:** returns the work to the Manager that owns it, opens a new phase, or accepts the result with a recorded reason. **(proposed)**
- **Escalates to the user:** only an issue that research and rework cannot solve.
- **Must not:** write project files.
- **Done when:** the scores, reviews, and open issues show that the work is correct, the full test suite passes, and the verified evidence is on file.

## Manager

- **Does:**
  - Breaks its phase into tasks grouped by folder or module.
  - Creates one Lead per module and briefs it.
  - Coordinates its Leads.
  - Works with other Managers when its phase needs files outside its scope. The Managers agree on one owner for each such file.
  - Runs the tests of all its Leads, across their modules, to confirm that the modules work together.
  - Reviews and scores the work that comes back from each Lead, and works with that Lead to fix issues.
- **Owns:** the phase, its task breakdown, the module boundaries between its Leads, and test health across its modules.
- **Arbitrates:** disputes between its Leads, and the contracts between its modules. **(proposed)**
- **On a regression:** creates a new Lead to fix it.
- **Must not:** write project files, or direct a Coder without going through the Coder's Lead. **(proposed)**
- **Done when:** the Oracle approves the phase review.

## Lead

- **Does:**
  - Breaks its module into per-file tasks.
  - Creates one Coder per file and briefs it. The brief states what the file's unit tests must prove.
  - Reviews and scores each Coder's file and tests. Approves the work or returns it.
  - Runs the module's tests when all of its Coders are done.
- **Owns:** the module, the file assignments inside it, the contracts between its files, and approval of each Coder's work.
- **Arbitrates:** disputes between its Coders about contracts and about where a shared function belongs. **(proposed)**
- **Must not:** write project files.
- **Done when:** the Manager approves the module review.

## Coder

- **Does:**
  - Creates or updates its file. It is the only role that writes code.
  - Creates and edits the unit test files that relate to its file.
  - Ideally writes a failing unit test first, then the code. Test-first is the expected practice, not a hard gate.
  - Finishes with passing tests that cover the happy path and every known, possible edge case. See "Testing".
  - Queries the code graph for existing code before it writes new code, so that the project has no duplicated work.
  - Updates the code graph for its file when it finishes its work, before the review, and again after any later change.
  - Reviews and scores its own work, fixes it, and reviews it again until it is satisfied. This loop has no cap, because the Coder is responsible for one file only.
  - Works with its Lead and with the Coders that own other files on data contracts and on where shared functions belong.
- **Owns:** one file and the unit tests for that file. "File" means any project file that the run touches: new or existing, code or configuration. **(proposed)**
- **Tooling:** the widest tool set of any role. It checks efficiency, duplication, security, and the project's guidelines.
- **Must not:** edit a file that it does not own. It files a change request with the owner.
- **Done when:** its Lead approves the file.

## Testing

The swarm creates unit tests only. End-to-end testing is a separate process outside the swarm.

| Layer | Runs |
|---|---|
| Coder | The unit tests for its file |
| Lead | The module's tests, when all of its Coders are done |
| Manager | The tests of all its Leads, across their modules |
| Oracle | The full test suite at each join point, once every Manager that feeds it has reported in |

What a Coder's tests cover by the time it hands up its file:

- The happy path.
- The known, possible edge cases, such as API errors.
- Error handling that catches the specific error types, plus a catch-all.

The hard check is a hook at the end of the Coder's work, not a gate at the start:

- The hook confirms that tests exist for the file and that the results show they passed.
- Passing tests are the only evidence that the handoff requires.
- Other tool output, such as coverage, lint, type checks, and security scans, supports the scores. It does not gate the handoff. **(proposed reading)**
- The tooling runs the tests and records the result itself. The Coder does not paste results into its report, so a report cannot claim a pass that did not happen. **(proposed)**
- The check also fails when the file has zero tests or skipped tests. Whether the tests prove what the brief asked for is the Lead's judgment in review. **(proposed)**

## Guidelines

- The Oracle sets the high-level guidelines for the project: for example the architecture, the stack, the conventions, and the test and build commands.
- Each lower layer checks the guidelines at its own level of detail. It adds the details that apply to its scope: the phase, the module, or the file. A lower layer does not contradict a higher one. **(proposed)**
- The guidelines are a target, not a gate on the first pass. An agent sometimes needs to get something working first. The review cycle then adjusts the working result toward the guidelines.
- A departure from the guidelines is recorded as an open issue when the work is handed up. **(proposed)**
- A file can pass final approval with a departure, when the responsible level agrees to it and records the reason.
- Any higher level can deny an accepted departure. The denial needs a reason and a solution that overrides the departure. The higher level passes the needed context down, so that the lower agent can fix the work with that solution.
- A higher level that finds no better solution agrees and approves the departure.
- A denied departure goes back as a return, so it counts as one fix attempt. **(proposed)**
- The Oracle audits the accepted departures along with the scores. **(proposed)**

## Review and scoring

The full rubric is in `02-rubric.md`. The responsibilities:

| Layer | Review duty |
|---|---|
| Coder | Reviews and scores its own file |
| Lead | Scores each file in its module before it sees the Coder's scores. Compares the two sets, reviews the discrepancies, and suggests fixes |
| Manager | Reviews whether each Lead met its tasks. Reviews the items that the two most recent scores for a file did not agree on. It does not score every file |
| Oracle | Audits the scores and the reviews. Investigates the low scores to decide what happens next |

- The target score is 90 or higher. A score below the target is something to review, research, and improve.
- The improvement loop stops when new scores stop getting better.
- A change that makes the scores worse can be undone.

## Escalation

Escalation and the improvement loop are one mechanism. After each fix the work is scored again, and the score history shows when an issue has stopped improving. That is what moves it up, and the history explains to the next layer why it arrived. See `02-rubric.md` "The improvement loop".

One issue gets 3 rounds. A round ends after 3 attempts that did not improve the score. An attempt that improves the score does not count. **(the counting rule is proposed)** The issue stays with its owner. Each round brings in the next layer.

| Round | Attempts | Who works on it |
|---|---|---|
| 1 | 1 to 3 | The Coder and its Lead work together |
| 2 | 4 to 6 | The direct Manager steps in and uses its resources to act |
| 3 | 7 to 9 | The Oracle looks at the issue and adds any other suggestions |
| After round 3 | | The Oracle decides whether to change the plan or notify the user |

- The Manager's resources include its other Leads and Coders, a new Lead, a fresh Coder, and a stronger model for the Coder. A structural change, such as a split file or a changed contract, is available in rounds 2 and 3. It is not a separate round. **(proposed)**
- A layer that has no new or better idea passes the issue up.
- The Coder's own review loop does not count as an attempt. Only a return from a reviewer counts.
- A result that is accepted as incomplete uses no attempts. See "The cycle that every layer repeats".
- An issue that a Manager finds starts at round 2. **(proposed)**

### Deferrals and scope changes

- At any point, any agent can suggest that work happens later, or that the scope changes.
- A suggestion takes effect only when the level responsible for that work agrees to it.
- The ledger tracks every suggestion, the agreement, and the resulting deferred item or scope change.
- The responsible level: **(proposed)**

| What changes | Who agrees |
|---|---|
| A file's task or its tests | The Lead |
| A module's scope, or a contract between files | The Lead, and the Manager when another module is affected |
| A phase's scope, or a contract between modules | The Manager |
| The phase plan, or work moved to a later phase | The Oracle |
| What the PRD asks for | The user, through the Oracle |

## Enforcement and overrides

- Hooks enforce the rules with a hard block. An agent that a hook denies must take the proper path, such as a change request.
- The Oracle can override a rule. No other role can.
- Every override is recorded in the ledger with its reason, and the final report lists it. **(proposed)**
- An override is narrow: one rule, one agent, one target, one time. **(proposed)**
- The Oracle cannot use an override to write project files itself. **(proposed)**

## What the Oracle tells the user, and when

The Oracle decides between a notification now and a line in the final report.

| Situation | Example | The Oracle |
|---|---|---|
| Only the user can unblock it, the fix is simple, and the run can continue afterwards | A device is not connected. A permission is needed | Notifies the user now |
| The layers have no options left | An issue ends round 3 below the floor | Notifies the user now |
| It works, but nobody found a better solution | A recorded shortfall. An accepted departure | Puts it in the final report, to be worked on separately |

- While the run waits for the user, the phases that the problem does not block keep going. **(proposed)**
- A notification states what is blocked, what the user needs to do, and what resumes after. **(proposed)**

## Final report

The Oracle writes a final summary report at the end of the run. **(contents proposed)**

- The outcome, and the evidence for it: the full test suite result and the phase reviews.
- What was built, by phase.
- Items to work on separately: solutions that work but could be better, recorded shortfalls, and accepted departures.
- Every override, with its reason.
- Deferred items and scope changes, with who agreed to each.
- Notifications that were sent, and how each was resolved.
- The run analytics. See `03-ledger.md`.

## Watchdog

The watchdog is a background job, not a role. It exists only to keep control of the run.

- The Oracle starts it at the start of a run.
- It checks at an interval whether any agent has stopped moving.
- It messages the Oracle about an agent that is not moving. The Oracle works out what to do.
- It detects and reports only. It never resumes, replaces, or stops an agent itself.
- No role has a time limit or a turn limit, so the watchdog and the escalation budget are the only controls on a runaway agent.
- It separates an agent that is stuck (no activity) from an agent that is spinning (activity without progress, such as the same test failing repeatedly). The Coder's uncapped review loop depends on this check. **(proposed)**
- It flags an agent whose context passes a threshold, so that the agent's parent can replace it with a fresh agent that continues from the ledger records. **(proposed)**
- Detection is a plain script that reads heartbeats from the ledger. It uses no model and wakes the Oracle only when it finds a problem, so a quiet run costs nothing. **(proposed)**
- The Oracle can hand a stalled agent to that agent's parent to resume or replace. **(proposed)**
- The watchdog cannot recover an Oracle that has crashed, because the Oracle starts it. When the Oracle runs as an agent view session, the supervisor restarts a crashed session, and agent view notifies the user when the Oracle waits on input. **(proposed)**

## Shared tooling and records

- All roles share the MCP tooling. Each role applies it from its own perspective. The Coder has the most tools.
- The tooling keeps its own folder structure in the host repo for plans, progress, reviews, scores, and evidence.
- The records prove that each item was completed correctly. They let a later run, or a different swarm, resume or finish the work.

## Git workflow in the host repo

Alex's rule for the first version: keep it simple.

- The whole swarm works on the branch that is checked out, in one working tree. It does all of the work there.
- The swarm does not commit or push. The user commits and pushes at the end of the run.
- Before a run starts, the Oracle checks that the repo is up to date with `main`.
- When the right starting point is obvious, the Oracle creates a branch from `main` for the run.
- When it is not obvious, the Oracle asks the user what is needed. Examples: uncommitted changes, a checkout on another branch, or a repo that is behind its remote. **(examples proposed)**

Consequences: **(proposed)**

- No agent works in a git worktree, so nothing needs a merge, and the repo has one copy of the code graph. This removes the worktree risk in "The code graph".
- An agent view session moves into a worktree before it edits, unless `worktree.bgIsolation` is `"none"`. For the first version, either the plugin sets that value, or Manager, Lead, and Coder all run as subagents under one Oracle session. Three subagent layers fit the default depth limit.
- "Every hand-up is a saved version" cannot rely on commits. The tooling saves each version in the ignored records folder.

## The code graph (codebase-kg)

- codebase-kg is a required dependency. The other sibling plugins stay optional.
- Reason: many agents change the code at the same time. An up-to-date graph lets each agent find what exists and search the codebase reliably.
- Every agent searches the code graph first. It uses Grep or Glob only when the graph does not have what it needs, or returns the wrong thing.
- An agent that falls back records what the graph was missing, so that the graph can be corrected. **(proposed)**
- codebase-kg's own search gate hook already enforces this order: it denies a plain search of mapped code until the agent has queried the graph.
- A Coder updates the graph when it finishes its file work, before the review.
- When a review leads to more changes, the Coder updates the graph again.
- The Coder updates the nodes that anchor on its own file. This follows the ownership rule. A node that anchors on several files is updated through the Lead. **(proposed)**
- Every role queries the graph at its own level: the Oracle and the Managers for impact and boundaries, the Lead for placement and contracts, and the Coder to find existing code before it writes new code. **(proposed)**
- A current graph for the file is a second hard check at handoff, next to passing tests. codebase-kg already detects a file whose contents no longer match its node, so a hook can check it without a model. **(proposed)**
- The `setup` skill builds the graph when the host repo has none. An empty repo starts with an empty graph. **(proposed)**
- The graph file, `knowledge/code_graph.db`, belongs to the host repo and is committed with the code. It is not part of the ignored swarm records.

Facts from the codebase-kg plugin, read on 2026-09-20:

- Its write tools (`kg_upsert_node`, `kg_delete_node`, and the link and reference tools) are atomic and validated, one call at a time. They fit per-file updates by many agents.
- Its export, edit, and build path takes a snapshot of the whole graph. A build from an old snapshot silently reverts later writes. During a run, agents must use the write tools only.
- Its search gate hook denies a plain Grep or Glob on mapped code until the agent queries the graph. Swarm agents get "look before you write" from it at no cost.

Risks for the mechanics discussion:

- Several sessions write to one graph file at the same time. Whether the write tools are safe across processes needs a test.
- Each agent view session works in its own worktree, so each has its own copy of the committed graph file. Merging phases then conflicts on a binary file. Options: keep Manager sessions in one working tree, or rebuild the graph after each merge.

## Readings treated as accepted

Alex has seen these and has not objected. Reverse any of them on request.

1. The Lead, not the Coder, reviews each file's tests and runs the module's tests.
2. A Coder's "one file" is a pair: the source file and its unit test file.
3. "Only the Coder writes files" applies to the host project's files. Other roles record through the tooling, and a hook blocks them from writing directly.
4. The Oracle is a plugin-defined agent that talks to the user. Corrected by Alex on 2026-09-20: it is part of the plugin, not whatever session the user happens to be in.

## Open questions about responsibilities

1. **Managers per phase.** Resolved on 2026-09-20: one Manager per phase, and phases run in parallel where the plan allows. See "The plan".
2. **Test first and "working first".** Resolved on 2026-09-20: test-first is the ideal, not a gate. The hard check is the hook at handoff: tests exist and pass. See "Testing".
3. **Escalation design.** Resolved on 2026-09-20: three rounds of 3. See "Escalation". One point to confirm: the budget is 9, and the earlier round 4 is removed.
4. **Accepted departures.** Resolved on 2026-09-20: a departure can stay when the responsible level accepts it. A higher level can deny it with a reason and a solution. See "Guidelines".
5. **"Aware of the context of each agent".** Resolved on 2026-09-20: both meanings. The registry records what each agent is doing, its start and end time, its tokens, its context percentage, and its context overflow count. The data supports later tuning. See `03-ledger.md` "Run analytics".
6. **Model choice.** Resolved on 2026-09-20: the setting is a list of approved models for each role, and the parent picks from it by task complexity. See "Models".

## Deferred: how it works

Alex wants to settle responsibilities first. These topics come later:

- How briefs, reviews, change requests, and notifications travel between agents.
- How hooks enforce the rules for each role.
- How the watchdog runs and how it reaches a stalled Oracle.
- The records folder structure.
- Who commits, and when.
- How the separate end-to-end process connects to a finished run.
- Cost controls, including a parallelism cap. See `notes-prompt-cache-costs.md`.
