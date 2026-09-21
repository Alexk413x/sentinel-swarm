---
name: swarm-protocol
description: Background knowledge for sentinel-swarm. Loads when the conversation mentions sentinel-swarm, the swarm, or its roles (Oracle, Manager, Lead, Coder), and when the user asks what the swarm is, how it works, or how its roles relate.
---

# swarm-protocol

Background reference for sentinel-swarm. It does not run a swarm; it summarizes how
one works, so a conversation about the swarm has the shared vocabulary in context.

## Hierarchy

| Role | Count | Owns | Created by |
|---|---|---|---|
| Oracle | 1 per run | The run and the PRD | The user |
| Manager | 1 per phase | One phase | Oracle |
| Lead | 1 per module | One module | Manager |
| Coder | 1 per file | One file and its unit tests | Lead |

## The cycle

Every parent-child pair repeats: brief, work, self-review, hand up, review, then
approve, return, or accept as incomplete. A returned result counts as one fix
attempt. The child stays available until its parent approves its work.

## Ownership

Every unit of work, a run, a phase, a module, or a file, has exactly one owner. A
non-owner that needs a change files a change request with the owner instead of
editing the work itself. Only the Coder writes project files.

## Scoring

Every file is scored on nine dimensions (meets the brief, testing, error handling,
security, architecture, code structure, performance, maintainability, accessibility)
on a 1-100 scale per dimension, built from 1-10 criterion ratings. The target is 90,
the floor is 70. See `plans/02-rubric.md`.

## Escalation

An issue gets 3 rounds of up to 3 non-improving attempts each: round 1 is the Coder
and its Lead, round 2 brings in the Manager, round 3 brings in the Oracle. See
`plans/01-roles.md` "Escalation".

## Source of truth

The full design lives in the `plans/` folder of this repository: `01-roles.md`,
`02-rubric.md`, `03-ledger.md`, and `04-mcp-and-hooks.md`.

## Status

Not implemented. This skill provides background knowledge only; it triggers no
tooling and starts no run.
