---
name: resume
description: Continues an unfinished sentinel-swarm run from its ledger records. Use for "resume the swarm run", "continue the last run", or "pick the swarm back up".
---

# resume

Continues a run that stopped before the Oracle reported it complete.

## Purpose

Picks an unfinished run back up from its ledger records, so a crashed session, a
replaced agent, or an interrupted run does not lose its plan, briefs, scores, or
open issues.

## Planned steps

1. Read the ledger for the most recent unfinished run in this working tree, per
   `plans/03-ledger.md` "Tracking and storage": records live in one folder at the
   root of the main checkout, so a resume only works on the same machine.
2. Report the run's last known state: its phase plan, its agent tree, and its open
   issues, using the same view `status` produces.
3. Recreate whichever agents the run still needs, from their last recorded brief and
   state, rather than starting the plan over.
4. Continue the brief-work-review cycle from where each agent left off.
5. Flag anything the ledger cannot resolve on its own, such as an agent that
   stopped mid-handoff, for the Oracle or the user to decide.

## Status

Not implemented. The swarm-ledger MCP server this skill depends on does not exist
yet, so this skill has no ledger records to resume from today.
