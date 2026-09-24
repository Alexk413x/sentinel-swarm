---
name: swarm-protocol
description: Reference for sentinel-swarm. Loads when the conversation mentions sentinel-swarm, the swarm, or its roles (Oracle, Manager, Lead, Coder), and when the user asks what the swarm is, how it works, what the rubric dimensions are, or what order a role calls the ledger in.
---

# swarm-protocol

The shared vocabulary, names, tool order, and rubric keys for a sentinel-swarm run.

## Hierarchy

| Role | Count | Owns | Started by | Reports to | Agent file | Color |
|---|---|---|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user, through the launcher | The user | `swarm-oracle` | cyan |
| Manager | 1 per phase | One phase | Oracle | Oracle | `swarm-manager` | green |
| Lead | 1 per module | One module | Manager | Manager | `swarm-lead` | purple |
| Coder | 1 per file | One file and its unit tests | Lead | Lead | `swarm-coder` | orange |

## Sessions

Every role runs as its own Claude Code session, with its own row in agent view. No
role starts a subagent: no role has the `Agent` tool, and the `pre_agent` hook denies
an `Agent` call from any swarm session.

- Each role's model, tools, permission mode, MCP servers, and hooks live in its
  project agent file, `.claude/agents/swarm-<role>.md` in the host repo. `setup`
  writes the files from the plugin's templates, and the user edits them.
- Every session runs in the host repo root, in one shared working tree.
- Every session starts with only two MCP servers: the repo's shared ledger server
  over HTTP, and codebase-kg. The launcher and `agent_spawn` pass them with
  `--strict-mcp-config`.
- A child that finishes a step wakes its parent with a cross-session `SendMessage`.
  A parent that has started its children ends its turn instead of waiting in it.
- Release stops a session. A crashed session is resumed with `agent_resume`, which
  keeps its conversation.

## Names

| Role | Pattern | Example |
|---|---|---|
| Oracle | `oracle` | `oracle` |
| Manager | `mgr-<phase>` | `mgr-p2-api` |
| Lead | `lead-<phase>-<module>` | `lead-p2-auth` |
| Coder | `coder-<phase>-<module>-<file>` | `coder-p2-auth-login` |

A name is unique within a run and is the address other agents use in the ledger.
The child's prompt starts with it: `You are <name>.` The child passes that name as
`caller` on every ledger call and never passes `agent_id`; a hook stamps the real id.

Each session also has a session name, which is unique on the machine:
`<repo slug>-r<run id>-<name>`, and `<repo slug>-oracle` for the Oracle. `SendMessage`
addresses a session only by its session name. The ledger maps each session name to
its agent.

## Starting a child

1. The parent calls `brief_create` with the child's name, role, a model from that
   role's approved list, and the brief.
2. The parent calls `agent_spawn(caller=<its name>, child_name=<the child's name>)`.
   It checks the role, the model, the brief, and the parallelism cap when the user
   set one, then starts the child's session with `claude --bg --agent swarm-<role>`
   and the prompt:

   ```
   You are <name>.
   Read your brief from the swarm ledger and follow it.
   ```

3. The parent ends its turn. The child's message wakes it.

## Wake-ups and next

`handoff_submit`, `return_work`, `message_post`, and `phase_update(handed_up)` leave
work for another agent. Each records that the caller owes that agent a wake-up and
returns a `next` field with the exact call to make:

- `SendMessage(to="<session name>", message="<one-line pointer>")` when the
  recipient's session is running.
- `agent_resume(...)` when it is not.

The caller makes that call before it ends its turn. The `post_any` hook clears the
debt when it sees the `SendMessage`, and the `stop` hook blocks a Manager, Lead, or
Coder that still owes one. The message only points at the ledger record, for example
"Handoff 1 for hello.py is waiting in the ledger." The detail lives in the ledger.

## The first call in every session

`ToolSearch(query="select:mcp__swarm-ledger__ledger_info", max_results=1)`. The
ledger server can still be connecting when a session opens, and this call waits until
it connects.

## Tool names

- Ledger tools: `mcp__swarm-ledger__<name>`, from the server `swarm-ledger`.
- codebase-kg tools: `mcp__codebase-kg__<name>`, from the server `codebase-kg`.

## Tool order by role

| Role | Order |
|---|---|
| Oracle | `ToolSearch` → `ledger_info` → `run_start` → `repo_check` → `repo_branch_create` or ask the user → `profile_set` → `guidelines_set` → `phase_add` per phase → `phase_update(unlocked)` → per phase: `brief_create` + `agent_spawn` → review: `status_tree`, `run_status`, `issue_list` → `tests_run(full)` → `phase_review` → `phase_update(approved)` → `plan_unlocked` → `directive_inbox` at safe points → `run_pause(reason)` when only the user can unblock the run → `report_build` → `run_finish` |
| Manager | `ToolSearch` → `brief_get` → `brief_ack` → `guidelines_get` → `module_add` per module → per Lead: `brief_create` + `agent_spawn` → review the Lead reports → `tests_run(module)` → `module_review` → `agent_release` the Lead → `tests_run(phase)` → `phase_update(handed_up)` (a Manager sets only its own phase, to `working` or `handed_up`) → `message_post` to the Oracle → the `SendMessage` that `next` names |
| Lead | `ToolSearch` → `brief_get` → `brief_ack` → `guidelines_get` → per file: `claim_file` then `brief_create` then `agent_spawn` → on a handoff: `score_record(kind="lead")` then `review_compare` then `approve` / `return_work` / `accept_incomplete` → after a return: `score_record(lead)`, `review_compare`, `attempt_record`, decide → `tests_run(module)` → `message_post` to the Manager → the `SendMessage` that `next` names |
| Coder | `ToolSearch` → `brief_get` → `brief_ack` → `kg_search` → write the test file → write the source file → `tests_run(scope="file")` until green → `graph_upsert` → `score_record(kind="self")` → `handoff_submit` → the `SendMessage` that `next` names |

Every role calls `message_inbox` at the start of each turn after a wake-up or a resume.

`run_start` on a paused run resumes it: the run goes back to active. A paused run
keeps every gate.

## The gates the ledger enforces

- Nothing works for a child before `brief_ack`.
- `agent_spawn` refuses a child without an unacknowledged brief from the caller, a
  session name a live session already uses, and a start past the parallelism cap.
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
- `agent_spawn` refuses to start a Manager until the run has a recorded `repo_check`.
  It does not require `obvious_start`; a not-obvious start is the Oracle's call to
  make, not a block.
- `phase_update(handed_up)` refuses unless every module of the phase has an accepted
  `module_review` newer than its last change. `phase_update(approved)` refuses
  without an accepted `phase_review` of the phase recorded since it was handed up.

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
approves its work, and release stops its session.

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
`report.md`, `server.json` (the ledger server's URL), and `hook.py` (the hook shim). It is excluded through `.git/info/exclude`, not the host repo's
`.gitignore`.

## Finding code

Every role queries the codebase-kg code graph first. Grep and Glob are the fallback
for when the graph does not have the answer or returns the wrong thing. Only the
Coder writes to the graph, and only through the ledger's `graph_upsert`.

## Source of truth

The design lives in the `plans/` folder of the sentinel-swarm repository:
`01-roles.md`, `02-rubric.md`, `03-ledger.md`, `04-mcp-and-hooks.md`, and
`06-sessions-plan.md`.
