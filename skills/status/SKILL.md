---
name: status
description: Prints a sentinel-swarm run's tree of agents, file locks, scores, and open items from the ledger. Use for "swarm status", "what is the swarm doing", "show the run tree", or "what is still open on this run".
---

# status

Reports the current state of a sentinel-swarm run from the ledger, without changing
anything.

## Purpose

Gives the user a read-only view into a run in progress, or a finished run, so they
do not need to read agent transcripts to know what happened.

## Planned steps

1. Read the run's registry from the ledger: every Oracle, Manager, Lead, and Coder,
   its role, its state, and its parent, per `plans/03-ledger.md` "Agent registry
   table".
2. Read the ownership claims, so the tree shows which agent currently locks which
   file.
3. Read the latest scores for each file and module against the rubric in
   `plans/02-rubric.md`.
4. Read open items: issues in escalation, deferred items, accepted departures, and
   recorded shortfalls.
5. Print the run as a tree: Oracle, its Managers, their Leads, their Coders, each
   with its state, its lock (if any), its latest scores, and its open items.

## Status

Not implemented. The swarm-ledger MCP server this skill depends on does not exist
yet, so this skill has no ledger to read from today.
