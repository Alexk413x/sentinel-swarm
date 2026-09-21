---
name: run
description: Starts a sentinel-swarm run that takes a PRD to finished, tested, reviewed code. Use for "run the swarm", "start sentinel-swarm on this PRD", "build this PRD with the swarm", or "take this PRD to done".
---

# run

Starts a full sentinel-swarm run: PRD in, built and reviewed code out.

## Purpose

Takes a PRD from the user, plans it, builds it through the Oracle-Manager-Lead-Coder
hierarchy, and reports the outcome.

## Planned steps

1. Confirm the host repo is ready: `setup` has run, or the repo already has the
   records folder and a code graph.
2. Confirm the repo is up to date with `main` and on a clean or expected starting
   point, per `plans/01-roles.md` "Git workflow in the host repo".
3. Start the Oracle. The Oracle reads the PRD, asks the user clarifying questions,
   and sets the project's high-level guidelines.
4. The Oracle plans the run as a dependency graph of phases and creates one Manager
   per phase.
5. Each Manager, Lead, and Coder follows the brief-work-review cycle in
   `plans/01-roles.md`, scored against the rubric in `plans/02-rubric.md`, recorded
   in the ledger described in `plans/03-ledger.md`.
6. The Oracle runs the full test suite at each join point, and again at the end of
   the run.
7. The Oracle writes the final summary report: the outcome, what was built, open
   items, overrides, deferrals, and notifications sent.

## Status

Not implemented. The swarm-ledger MCP server this skill depends on does not exist
yet, so this skill cannot start a real run today.
