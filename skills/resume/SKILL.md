---
name: resume
description: Continues an unfinished sentinel-swarm run from its ledger records, after a pause or a crash. Use for "resume the swarm run", "continue the last run", "pick the swarm back up", or "a swarm session died".
---

# resume

Continues a run that stopped before the Oracle reported it complete: a run the Oracle
paused with `run_pause`, a run whose Oracle session crashed or exited, or a run where
one child session died.

The run's state is in `.sentinel-swarm/ledger.db`, not in any session's context. Each
session also keeps its own conversation, so a resumed session continues where it
stopped and the ledger supplies the truth. The records live in one folder at the root
of the main checkout, so a resume works only on the same machine and in the same
working tree.

## Check the state first

`/sentinel-swarm:status` reads the ledger without changing anything. `claude agents`
lists the sessions that are still running. Compare the two to see which sessions the
ledger counts as live but are gone.

## Resume the whole run

Start the Oracle again from the host repo root, with a prompt that says to resume:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/mcp" --frozen --no-dev \
  python -m swarm_ledger.launch --bg "Resume the sentinel-swarm run in this repo."
```

The launcher starts the ledger server again when it is not running. Do not paste the
PRD again: `run_start` ignores it on a resume.

For a paused run whose Oracle session is still open, the user can instead type
"continue" in the Oracle's row in agent view.

## What run_start does on a resume

The Oracle calls `run_start` in the new session, as it does for a new run.

- A paused run goes back to active and keeps every gate.
- When the run's Oracle session is gone, `run_start` binds the Oracle's name to the
  new session, moves the live children and briefs to it, and returns the existing run
  with `"resumed": true`.
- While another run's Oracle session is still live in `claude agents --json`,
  `run_start` refuses. One swarm runs per repo.

## What the Oracle does next

1. `run_status()`, `status_tree()`, and `issue_list()` for the run, its agents, and
   what is still open.
2. `message_inbox()` and `directive_inbox()` for anything that arrived while the
   session was gone.
3. For each agent the ledger counts as live whose session is not running:
   `agent_resume(caller="oracle", target_name=<its name>)`. It runs `claude --resume
   <session id> --bg "<pointer>"`, which continues the same session and its
   conversation. `agent_resume` refuses a session that is still running, because a
   resume of a live session starts a copy.
4. A session that is waiting for a permission answer is not dead. Tell the user which
   row in agent view is waiting, and do not resume it.
5. When a session cannot be resumed, release it with
   `agent_release(target_agent_id)`, then `brief_create` from the previous brief's
   content and `agent_spawn` a fresh session. A child is re-created from its brief,
   not started over from the plan. `brief_create` refuses a `child_name` that a live
   agent still holds, so release first.
6. Anything the records cannot settle goes to the user: a Coder that stopped between
   its last edit and its handoff, a file whose claim is live but whose owner is gone,
   or an issue in the middle of a round.

Every parent does the same for its own children once it is running again: a Manager
for its Leads, and a Lead for its Coders.
