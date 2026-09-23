---
name: swarm-protocol
description: Reference for sentinel-swarm. Loads when the conversation mentions sentinel-swarm, the swarm, or its roles (Oracle, Manager, Lead, Coder), and when the user asks what the swarm is, how it works, what the rubric dimensions are, or what order a role calls the ledger in.
---

# swarm-protocol

The shared vocabulary, names, tool order, and rubric keys for a sentinel-swarm run.

## Hierarchy

| Role | Count | Owns | Created by | Reports to |
|---|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user | The user |
| Manager | 1 per phase | One phase | Oracle | Oracle |
| Lead | 1 per module | One module | Manager | Manager |
| Coder | 1 per file | One file and its unit tests | Lead | Lead |

## Names

| Role | Pattern | Example |
|---|---|---|
| Oracle | `oracle` | `oracle` |
| Manager | `mgr-<phase>` | `mgr-p2-api` |
| Lead | `lead-<phase>-<module>` | `lead-p2-auth` |
| Coder | `coder-<phase>-<module>-<file>` | `coder-p2-auth-login` |

A name is unique within a run and is the address other agents message. The parent
puts it on the first line of the child's prompt: `You are <name>.` The child passes
that name as `caller` on every ledger call and never passes `agent_id`; a hook stamps
the real id.

## Spawning a child

The parent calls `brief_create` first, then the `Agent` tool with
`subagent_type: "sentinel-swarm:<role>"`, a model from that role's approved list, and
the prompt:

```
You are <name>.
Read your brief from the swarm ledger and follow it.
```

The spawn hook denies an `Agent` call with no brief for the child. A parent resumes a
child with `SendMessage(to=<the agent id the spawn returned>, ...)`, and the message
only points at the ledger record. The detail lives in the ledger.

## Tool order by role

Ledger tools are named `mcp__plugin_sentinel-swarm_swarm-ledger__<name>`.

| Role | Order |
|---|---|
| Oracle | `ledger_info` → `run_start` → `profile_set` → `guidelines_set` → `phase_add` per phase → `phase_update(unlocked)` → per phase: `brief_create` + spawn → review: `status_tree`, `run_status`, `issue_list` → `phase_update(approved)` → `plan_unlocked` → `tests_run(full)` at join points → `directive_inbox` at safe points → `run_pause(reason)` when only the user can unblock the run → `report_build` → `run_finish` |
| Manager | `brief_get` → `brief_ack` → `guidelines_get` → `module_add` per module → per Lead: `brief_create` + spawn → review the Lead reports → `tests_run(phase)` → `phase_update(handed_up)` (a Manager sets only its own phase, to `working` or `handed_up`) → `message_post` to the Oracle |
| Lead | `brief_get` → `brief_ack` → `guidelines_get` → per file: `claim_file` then `brief_create` then spawn → on a handoff: `score_record(kind="lead")` then `review_compare` then `approve` / `return_work` / `accept_incomplete` → after a return: `score_record(lead)`, `review_compare`, `attempt_record`, decide → `tests_run(module)` → `message_post` to the Manager |
| Coder | `brief_get` → `brief_ack` → `kg_search` → write the test file → write the source file → `tests_run(scope="file")` until green → `graph_upsert` → `score_record(kind="self")` → `handoff_submit` |

Every role calls `message_inbox` at the start of each turn after a resume.

`run_start` on a paused run resumes it: the run goes back to active. A paused run
keeps every gate.

## The gates the ledger enforces

- Nothing works for a child before `brief_ack`.
- `brief_create` refuses a model that is not on the child role's approved list, and
  refuses a name a live agent already holds.
- `claim_file` refuses a path that already has a live claim. The claim is the file
  lock; `who_owns(path)` names the owner.
- `score_record(kind="lead")` must come before `review_compare`. After
  `review_compare` runs for a handoff, blind scoring is closed.
- `approve` refuses without a comparison, with a lead review that does not pass, or
  while the file has an open issue.
- `tests_run` scopes are role-bound: file to the Coder, module to the Lead, phase to
  the Manager, full to the Oracle.
- `override_grant` is the Oracle's alone.

## What handoff_submit refuses

`handoff_submit` is the hard check for a Coder. It refuses when:

- The tests fail, are missing, or are skipped.
- The file has no `test_path`.
- The code graph is not current for the file: no node, an anchor that does not
  resolve, or a symbol in the file that no node maps.
- The self review is missing or older than the last edit.

On success it records the test run, saves a version of the file and the test file,
and marks the file handed up.

## The cycle

Every parent-and-child pair repeats: brief, work, self-review, hand up, review, then
approve, return, or accept as incomplete. A returned result counts as one fix
attempt. An improving attempt does not. The child stays available until its parent
approves its work.

## Ownership

Every unit of work, a run, a phase, a module, or a file, has exactly one owner. A
non-owner that needs a change asks the owner through `message_post` instead of
editing the work. Only the Coder writes project files.

## Rubric dimensions and criterion keys

`score_record` takes `ratings = [{"dimension", "criterion", "value" 1..10, "reason",
"ref"}]` and `applicable = {dimension: null | "reason not applicable"}`. A dimension
marked applicable needs a rating for every one of its criteria. A rating below 9
needs a reason. A rating of 4 or lower opens an issue.

| Dimension key | Criterion keys |
|---|---|
| `meets_the_brief` | `does_what_was_asked`, `nothing_extra` |
| `testing` | `happy_path`, `edge_cases`, `proves_the_brief`, `no_empty_or_skipped` |
| `error_handling` | `api_and_io_errors`, `specific_and_catch_all`, `failures_reported` |
| `security` | `input_validation`, `injection`, `secrets`, `authn_and_authz`, `unsafe_defaults`, `new_dependencies` |
| `architecture` | `patterns_followed`, `guidelines_followed`, `contracts_honored`, `right_file`, `departures_recorded` |
| `code_structure` | `single_responsibility`, `modular`, `reusable`, `no_duplication` |
| `performance` | `no_needless_work`, `complexity_fits_data_size`, `sensible_resource_use` |
| `maintainability` | `clear_names`, `small_units`, `minimal_comments` |
| `accessibility` | `ui_files_only`, `accessibility_tools_check` |

A dimension score is the average of its criterion ratings times 10, from 0 to 100.
The target is 90, the floor is 70, and the criterion floor is 5. A file passes when
every applicable dimension is at or above the target and no criterion is below the
criterion floor. Mark accessibility not applicable on a file that is not UI.

## Escalation

An issue gets 3 rounds of up to 3 non-improving attempts each: round 1 is the Coder
and its Lead, round 2 brings in the Manager, round 3 brings in the Oracle. After
round 3 the Oracle changes the plan or notifies the user. `attempt_record` classifies
each attempt as improved, plateau, or regression, and restores the previous version
on a regression.

## Records

`.sentinel-swarm/` at the root of the main checkout holds `ledger.db`, `versions/`,
and `report.md`. It is excluded through `.git/info/exclude`, not the host repo's
`.gitignore`.

## Finding code

Every role queries the codebase-kg code graph first. Grep and Glob are the fallback
for when the graph does not have the answer or returns the wrong thing. Only the
Coder writes to the graph, and only through the ledger's `graph_upsert`.

## Source of truth

The design lives in the `plans/` folder of the sentinel-swarm repository:
`01-roles.md`, `02-rubric.md`, `03-ledger.md`, and `04-mcp-and-hooks.md`.
