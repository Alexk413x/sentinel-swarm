# Live-run bugs from ai-collective-chapter (0.7.1)

Bugs found during the 2026-11-14 swarm run in `ai-collective-chapter`, which ran the
installed plugin at 0.7.1. Do not install, update, or reload the plugin there while
its run is live.

## Status

| # | Bug | Status |
|---|---|---|
| 1 | False `needs_user` popups | Fixed on `fix/live-run-bugs` |
| 2 | Setup accepts command templates the shell gate denies | Fixed on `fix/live-run-bugs` |
| 3 | Resume after a reboot is refused | Fixed on `fix/live-run-bugs` |
| 4 | A replaced Coder keeps raising alerts | Fixed on `fix/live-run-bugs` |
| 5 | Run panel actions do nothing | Improved on `fix/live-run-bugs`; root cause not confirmed |
| 6 | Stray-process findings name exited PIDs | Fixed on `fix/live-run-bugs` |
| 7 | Pausing needs the Oracle | Fixed on `fix/live-run-bugs` |
| 8 | Every Coder lints the whole app | Fixed on `fix/live-run-bugs` |
| 9 | Idle blocked Coders hold the memory queue | Fixed on `fix/live-run-bugs` |
| 10 | Re-claim churn | Watch only |
| 11 | Moving a file breaks the claim, the tests, and the graph | Fixed on `fix/live-run-bugs` |
| 12 | A change request stays tied to a superseded file | Fixed on `fix/live-run-bugs` |
| 13 | Deletion Coders block on the shell gate | Fixed on `fix/live-run-bugs` |
| 14 | Stray-process attribution is wrong | Fixed on `fix/live-run-bugs` |
| 15 | Directives sit open with no acknowledgement | Fixed on `fix/live-run-bugs` |
| 16 | Planned phases show no brief in the panel | Fixed on `fix/live-run-bugs` |
| 17 | The panel's phase list cannot collapse | Fixed on `fix/live-run-bugs` |
| 18 | The phase gate never runs the build | Fixed on `fix/live-run-bugs` |
| 19 | Per-phase commits are impossible while later phases write | Fixed on `fix/live-run-bugs`: approval commits the phase |
| 20 | A dead parked session cannot be resumed, released, or replaced | Fixed on `fix/live-run-bugs` |
| 21 | File-scope test runs are never reused while other Coders edit | Fixed on `fix/live-run-bugs` |
| 22 | A usage-limit stop reads as user waits, and the run does not recover | Fixed on `fix/live-run-bugs` |
| 26 | `models.oracle` is ignored when the Oracle's role file names a model | Fixed in 0.8.1 |
| 27 | A parked agent dies on each resume and re-keys to dead ids | Fixed in 0.8.1 |

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

    Root cause, from the transcripts of `coder-p1-platform-header-shim-rm` and
    `coder-p1-platform-guard-shim-rm` (files 82 and 83, both `incomplete`):
    - The Windows Coder ran `Remove-Item` in PowerShell. The gate's denial listed the
      allowed shell commands and `git rm`, but not the ledger's `file_delete(path)`,
      which the Coder template names and which is the working path.
    - The other Coder deleted its file, but `handoff_submit` refused twice because the
      graph node `studio_guard_shim` still anchored the deleted file, and a Coder cannot
      delete a node. This is bug 11's graph problem. Fix together with 11: a deletion
      handoff removes the deleted file's anchors, or lets the Coder delete a node that
      anchors only its own deleted file; and the shell gate names `file_delete` when it
      denies a delete command (`Remove-Item`, `rm`, `del`).
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

18. **The phase gate never runs the build.** p1 was approved with tests, typecheck, and
    lint passing, but `npm run build` failed: a client component imported a feature
    barrel that re-exported a server component using `next/headers`. `build_command`
    is set in the run profile, but no gate runs it. Fix: run `build_command` at phase
    review and fail the gate on a build error.
19. **Per-phase commits are impossible while later phases write in the same tree.**
    Once p1 was approved, p3 to p5 Coders edited files p1 had changed (`types.ts`) and
    added migration 0006, so the repo owner could not commit p1 cleanly, and running
    the migrations would apply a half-written later one. Options: checkpoint at each
    phase approval (pause new claims briefly, or snapshot to a branch or worktree), or
    have the run commit approved phases on a run branch itself.
20. **A dead parked session cannot be resumed, released, or replaced.** A parked
    Lead's process died. `agent_resume` said "still running", trusting a stale message
    pipe; `agent_release` could not find the agent; `brief_create` refused a
    replacement because "a live agent already holds the name". A Coder died the same
    way. Workaround: the repo owner released both records in the ledger by hand. Fix:
    judge liveness by process (pid and creation time) in `agent_resume`,
    `agent_release`, and the name check; let `agent_release` handle parked agents; and
    have the watchdog release agents whose process is gone and whose heartbeat is
    stale. Check first how much the bug 3 liveness rule already covers: the "stale
    message pipe" may be a separate check.

21. **File-scope test runs are never reused while other Coders edit, so `handoff_submit`
    loops.** `tests_run` fingerprinted a file run with `git status` and every file
    claimed anywhere in the run, so with three phases editing, the fingerprint changed
    before a run recorded: `_require_handoff_evidence` never found a reusable run,
    started a fresh one, and refused with "still running" on every call. One file had 5
    green runs at 7/7 and no handoff. The live run carries a hotfix in the installed
    0.7.1, which installing a release overwrites. Fix: a file run hashes `HEAD`, its own
    file, test file, products, and the files it depends on (`file_deps`), not the rest
    of the tree; module, phase, and full runs keep the whole-tree fingerprint. Earlier
    file runs stop matching once, after an upgrade.
22. **A usage-limit stop reads as user waits, and the run does not recover after the
    reset.** The account hit its usage limit and every session went idle at once. The
    watchdog raised `needs_user` for the Oracle, the Leads, and the Coders, plus unsent
    wake-ups. Nothing moved for over 4 hours after the reset, until someone messaged the
    Oracle by hand. Fix: detect a usage-limit stop from the transcript or the listing,
    pause the run with that reason instead of raising findings, and resume on its own
    after the reset (or with `ledger.py resume`).

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
