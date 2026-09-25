# Still (proposed)

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
