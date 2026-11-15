# Live-run bugs from ai-collective-chapter (0.7.1)

Bugs found during the 2026-11-14 swarm run in `ai-collective-chapter`, which ran the
installed plugin at 0.7.1. Do not install, update, or reload the plugin there while
its run is live.

## Status

| # | Bug | Status |
|---|---|---|
| 1 | False `needs_user` popups | Fixed on `fix/live-run-bugs`, not committed |
| 2 | Setup accepts command templates the shell gate denies | Fixed on `fix/live-run-bugs`, not committed |
| 3 | Resume after a reboot is refused | Fixed on `fix/live-run-bugs`, not committed |
| 4 | A replaced Coder keeps raising alerts | Fixed on `fix/live-run-bugs`, not committed |
| 5 | Run panel actions do nothing | Open |
| 6 | Stray-process findings name exited PIDs | Open; overlaps 14 |
| 7 | Pausing needs the Oracle | Open |
| 8 | Every Coder lints the whole app | Open |
| 9 | Idle blocked Coders hold the memory queue | Open |
| 10 | Re-claim churn | Watch only |
| 11 | Moving a file breaks the claim, the tests, and the graph | Open; overlaps 13 |
| 12 | A change request stays tied to a superseded file | Open; next after 4 |
| 13 | Deletion Coders block on the shell gate | Open; overlaps 11 |
| 14 | Stray-process attribution is wrong | Open; overlaps 6 |
| 15 | Directives sit open with no acknowledgement | Open |
| 16 | Planned phases show no brief in the panel | Open; panel |
| 17 | The panel's phase list cannot collapse | Open; panel |

Suggested grouping for later sessions: panel work (5, 16, 17); file moves and
deletions (11, 13, 12); process and memory hygiene (6, 14, 9); run control (7, 15);
lint scope (8).

## Fixed on `fix/live-run-bugs`

1. **False `needs_user` popups.** The watchdog read any `waiting` or `blocked` status in
   `claude agents --json` as waiting on the user. Claude Code shows `blocked` for a
   session that ended its turn on a work blocker and for an idle Oracle. Every session
   ran in `bypassPermissions`, yet about 30 false findings became Oracle directives the
   user could not act on. Fix: a user wait needs a held permission request or a pending
   tool call in the transcript. Any other wait becomes a `blocked` finding addressed to
   the parent after `stuck_minutes`, with no user notification. A blocked Oracle with
   no request raises nothing.
2. **Shell operators in command templates.** `cd apps/web && npm run lint` as
   `lint_command` was denied to every Coder, and `cd apps/web && npx vitest run
   {target}` found no tests, because the target resolves from the repo root. Fix: setup
   warns about any shell operator in the test, build, or lint commands. Docs show
   `npx --prefix apps/web vitest run --root apps/web {target}`.
3. **Resume after a reboot refused.** The old Oracle stayed in the listing as a stale
   entry with no `pid` or `status` and `state: "blocked"`, and `run_start` counted it
   as running. The workaround was `claude stop 3d4ff845`. Fix: an entry is running only
   with a `busy`, `idle`, or `waiting` status, or a pid whose process started within a
   minute of the entry's `startedAt`.
4. **A replaced Coder keeps alerting.** When a Lead re-claimed a file under a new file
   id, the old Coder stayed live on the superseded file and kept raising `needs_user`.
   Fix: such a Coder raises only a `superseded` finding asking its Lead to release it.

## Open

5. **Run panel actions do nothing.** Clicking a message event and typing an answer
   sends nothing. Clicking an agent name opens neither its session nor a detail view.
   Start with the panel's event handlers in `hooks/register.tsx` and how they map an
   agent name to a session.
6. **Stray-process findings name exited PIDs.** A directive asked the user to
   `taskkill` 5 PIDs from a released Coder; all 5 had exited. Re-verify each PID and its
   creation time before raising, and never ask the user to kill an unverified PID.
7. **Pausing needs the Oracle.** Only the Oracle can call `run_pause`, so a user who
   must leave waits for its next safe point. Add `ledger.py pause` and `resume`
   commands that the user or a repo-owner session can run directly.
8. **Every Coder lints the whole app.** `lint_command` takes no `{target}`, so each
   handoff runs a full-app lint; six ran at once. Support `{target}` in `lint_command`
   and suggest the per-file form in setup.
9. **Idle blocked Coders hold the memory queue.** Coders stuck on bug 2 sat idle and
   held memory, so spawns queued behind the 1.5 GB headroom. Consider parking or ending
   a Coder that has sat idle on a blocker for N minutes. The `blocked` finding from
   bug 1 is a starting point.
10. **Re-claim churn (watch only).** About 15 files were superseded mid-phase, mostly
    from bug 2 and the reboot. Check whether the cause needs a fix once 2 and 3 ship.
11. **Moving a file breaks the claim, the tests, and the graph.** In p1, Coders moved
    files into `features/` and `components/layout/`.
    - `graph_upsert` refused a node that still anchored the deleted old path
      (`site_chrome` → `components/SiteHeader.tsx`), so those files ended `incomplete`.
    - `tests_run` refused the moved file's new test path, because the claim's
      `test_path` still named the old path (`EventContributions`).
    - A stale TS6053 error pointed at the deleted test file until a later run.

    Suggested fix: a move or rename claim for Leads that carries both paths,
    `graph_upsert` re-anchoring from the old path to the new one, and the claim's
    `test_path` following the move.
12. **A change request stays tied to a superseded file and pauses the run.** CR 3 was
    filed against file 40 (`types.ts`). File 61 superseded it on the same path, and
    the work finished on file 61 (handoff 35, phase run 409 at 919/919).
    `cr_complete` (`agreements.py`, `_cr_evidence`) checks only file 40 and ignored
    override 4, which was granted for this case. p1 could not hand up, so the Oracle
    paused the run. Workaround: CR 3 set to verified by hand in the ledger. Fix: carry
    open change requests to the replacement file at supersede, accept a same-path
    handoff as evidence, and make `cr_complete` honor granted overrides.
13. **Deletion Coders block on the shell gate.** Coders asked to delete an old file
    after a move were stuck at the gate, so the Manager released them and handed the
    deletions to the repo owner. `_git_rm_paths` looks meant to allow this; find why it
    denied the command (the path form, the flags, or the claim). With bug 11, deleting
    a moved file has no working path, so stale copies stayed on disk and show as `!` on
    an approved module.
14. **Stray-process attribution is wrong.** The watchdog attributed `svchost.exe`, a
    network git process, and `gh` to Coders: PID reuse or the user's own processes.
    Match each PID's creation time and parent chain before raising a finding, and never
    suggest `taskkill /F /T` on an unverified tree.
15. **Directives sit open with no acknowledgement.** Scope directives stayed open for
    over an hour while the Oracle was busy. The Oracle should acknowledge each
    directive as queued or applied, with a time.
16. **Planned phases show no brief.** The panel shows "No brief recorded." for every
    planned phase. The panel reads briefs correctly; a phase gets a brief only when the
    Oracle briefs its Manager, and `phase_create` records only a name. In the live run,
    p3 to p13 had no brief rows. Fix: `phase_create` takes a short goal, stored on the
    phase, and the panel shows it until the Manager brief exists. Touches the ledger,
    the Oracle template, and the panel.
17. **The panel's phase list cannot collapse.** The Oracle row is always expanded, and
    its unstarted phases list under it (`hooks/panel.ts`, `flatten`), so a long phase
    list fills the panel. Add a collapsible phases group in `panel.ts` and
    `register.tsx`.

## Belongs to codebase-kg

- The codebase-kg search gate denied greps aimed outside the repo
  (`~/.claude/plugins/cache/...`), although its rules say those searches are free. Fix
  it in the codebase-kg repo, not here.
- Seen in this repo on 2026-11-14: the gate denies a whole Bash command when any part
  of it is a search. A `cat >> knowledge/prd/14-key-decisions.md <<EOF ... EOF` append
  followed by a `grep` was refused as one unit, so the append did not run either. Only
  the hook error reported it. The gate should judge the search alone, or say in its
  denial that nothing in the command ran.

## Before this branch merges

- Refresh `knowledge/code_graph.db` for the nodes this branch changes: `ledger_watchdog`
  (the `blocked` and `superseded` findings, the `needs_user` evidence rule), the
  sessions and procs modules (the running rule, `created_epoch_ms`), `ledger_setup`
  (the shell-operator warning), `setup_skill`, the PRD documents 05, 08, 11, 13, and
  14, and this plan. The codebase-kg pre-commit hook flags the drift otherwise.

## Open question

- An automated security review flagged `mcp/src/swarm_ledger/permissions.py`: with no
  `permission_mode.<role>` and no `defaultMode` in any settings file, a role starts in
  `bypassPermissions`. It suggests falling back to `default` (or `acceptEdits` for the
  Coder). This is the 0.7.1 release's behavior, not part of this branch; it needs a
  decision.
