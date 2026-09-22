---
name: run
description: How to start a sentinel-swarm run that takes a PRD to built, tested, reviewed code. Use for "run the swarm", "start sentinel-swarm on this PRD", "build this PRD with the swarm", or "take this PRD to done".
---

# run

Starts a full sentinel-swarm run: a PRD in, built and reviewed code out.

## Before you start

1. codebase-kg is installed and enabled. sentinel-swarm declares it as a required
   dependency.
2. The host repo has a code graph at `knowledge/code_graph.db`. Run
   `/sentinel-swarm:setup` when it does not, and that skill builds one.
3. `.claude/sentinel-swarm.local.md` exists and its `test_command` is filled in. The
   command must contain `{target}`, for example
   `python -m pytest -q -p no:cacheprovider {target}`. Every test run in the swarm
   uses it, so a missing or wrong value blocks every handoff.
4. The working tree is on the branch the run should build on, and it is up to date
   with `main`. The whole swarm works in one working tree and commits nothing.

## Start the run

The Oracle must be the session itself. Below the session there is room for exactly
three subagent layers, and the Manager, the Lead, and the Coder fill them. An Oracle
that runs as a subagent leaves no layer for the Coder.

Interactive:

```
claude --agent sentinel-swarm:oracle
```

Then paste the PRD as the first message.

Headless:

```
claude -p --agent sentinel-swarm:oracle "<the PRD text>"
```

In a headless run nobody answers questions, so the Oracle makes reasonable
assumptions and records each one in the run's guidelines.

## From an ordinary session

This skill cannot run the swarm from inside another session. Tell the user to start
a session with the command above and hand it the PRD.

There is one thing an ordinary session can do while a run is live: send the Oracle a
directive through the ledger's `directive_submit`, with `source="skill"` or
`source="outside-session"` and the sender's name. A directive steers the plan, a
future phase, or the guidelines. It does not interrupt the agents; the Oracle applies
it at its next safe point.

## What to expect

1. The Oracle reads the PRD, sets the project guidelines, and plans the work as a
   dependency graph of phases.
2. It creates one Manager per unlocked phase. Each Manager creates one Lead per
   module, and each Lead creates one Coder per file.
3. Every file gets two reviews: the Coder's self review and the Lead's blind review.
   Passing tests and a current code graph gate every handoff.
4. The Oracle runs the full test suite at each join point and again at the end, then
   writes the final report.

Phases that do not depend on each other run at the same time, so several Coders write
different files at once. Nothing is committed: the user commits at the end.

## Where the records are

`.sentinel-swarm/` at the root of the working tree:

- `ledger.db` holds every brief, claim, score, review, test run, issue, and decision.
- `versions/` holds a saved copy of each file at each handoff.
- `report.md` is the Oracle's final report, written by `report_build`.

The folder is excluded through `.git/info/exclude`, so it never reaches the host
repo's history. Use `/sentinel-swarm:status` to read the run's state while it works.
