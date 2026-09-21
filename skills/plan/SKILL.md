---
name: plan
description: Decomposes a PRD into a sentinel-swarm phase graph without starting any Managers. Use for "plan this PRD with the swarm", "show me the phase breakdown", or "what would sentinel-swarm build first".
---

# plan

Produces the phase graph for a PRD, without starting the run.

## Purpose

Lets the user see and adjust the planned breakdown of a PRD before any Manager,
Lead, or Coder spends work on it.

## Planned steps

1. Read the PRD and ask the user any clarifying questions the Oracle would ask
   before planning, per `plans/01-roles.md` "Oracle".
2. Set the high-level project guidelines the plan will assume.
3. Split the work into phases and record them as a dependency graph: a phase
   unlocks when the phases it depends on are approved, and phases with no
   dependency between them can run at the same time.
4. Record the plan in the ledger, per `plans/03-ledger.md`, so `run` or `resume` can
   pick it up later.
5. Report the phase graph to the user. This skill creates no Manager, Lead, or
   Coder, and does no file work.

## Status

Not implemented. The swarm-ledger MCP server this skill depends on does not exist
yet, so this skill cannot record a real plan today.
