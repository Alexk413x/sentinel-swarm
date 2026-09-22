---
name: resume
description: Continues an unfinished sentinel-swarm run from its ledger records. Use for "resume the swarm run", "continue the last run", or "pick the swarm back up".
---

# resume

Continues a run that stopped before the Oracle reported it complete.

## How a resume works

A run is resumed by starting the Oracle session again, in the same working tree:

```
claude --agent sentinel-swarm:oracle
```

Tell it to resume rather than pasting the PRD again. The run's state is in
`.sentinel-swarm/ledger.db`, not in any agent's context, so the Oracle continues from
the records.

The records live in one folder at the root of the main checkout, so a resume only
works on the same machine and the same working tree.

## What the Oracle does

1. `run_status()` instead of `run_start()`. It returns the run, its phases, its
   modules, its files, and their states.
2. `status_tree()` and `issue_list()` for the agent tree and what is still open.
3. `message_inbox()` and `directive_inbox()` for anything that arrived while the
   session was gone.
4. For every phase that is unlocked or working but has no live Manager: a fresh
   `brief_create` from the previous brief's content, then a spawn. The same applies
   one layer down, inside each Manager and Lead. A child is re-created from its
   brief, not started over from the plan.
5. Anything the records cannot settle goes to the user: a Coder that stopped between
   its last edit and its handoff, a file whose claim is live but whose owner is gone,
   or an issue in the middle of a round.

## Before re-creating a child

`brief_create` refuses a `child_name` that a live agent still holds, and refuses a
second unacked brief for the same name. Check the agent's state first. Release an
agent that is no longer running with `agent_release(target_agent_id)`, then create
the new brief.

## How the Oracle re-binds

The Oracle calls `run_start` in the new session, as it does for a new run. When a run
is already active, `run_start` does not start a second one: it ends the old Oracle
row, binds the Oracle's name to the new session id, moves the live children and briefs
to the new parent, and returns the existing run with `"resumed": true`. The PRD
argument is ignored on a resume.

## Check the state first

`/sentinel-swarm:status` reads the same database without changing anything. Run it
before a resume to see which phase, file, and agent the run stopped on.
