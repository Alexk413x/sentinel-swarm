# Resume a stopped wake-up target from the ledger

Status: not built, optional. The PRD documents what exists: wake-up delivery and the inbox in
`knowledge/prd/05-sessions.md`, the mod in `07-hooks-and-enforcement.md`, and the verified
platform facts in `13-platform-facts.md`.

## Goal

Remove the model's `agent_resume` call. Today a stopped wake-up target costs its sender one
extra turn: the mod's send fails, the Stop hook names `agent_resume`, and the sender's model
calls it. The ledger can resume the target itself.

## What is true today

- The sender's mod posts `owed` after each ledger tool call and sends each owed wake-up with
  `$.session.send` by session id (`handle_owed` in `mcp/src/swarm_ledger/hooks/events.py`,
  `hooks/register.ts`).
- A send to a session that is not running answers "no live session". The mod retries after 2, 4
  and 8 seconds, then leaves the wake-up owed.
- The sender's Stop hook then names `SendMessage` or `agent_resume` through
  `wake.route_wakeup`. `Ledger.agent_resume` refuses a running session, resumes the target with
  the text of every wake-up the caller owes it, and marks them sent.
- `claude --resume <id> --bg` on a running session starts a second copy
  (`13-platform-facts.md`), so a resume needs a liveness answer from `claude agents --json`.
  `Ledger._is_live` returns `None` when that call fails.
- `Ledger._owe_wakeup` runs inside the owing tool's write transaction.

## Steps

1. When a wake-up is owed to a target with a recorded session name that `Ledger._is_live`
   reports `False`, resume the target with the wake-up's pointer, as `agent_resume` does, and mark
   the wake-up sent. Start the resume after the owing call's transaction commits, not inside
   `_owe_wakeup`. `handle_owed` is the existing point after each call.
2. Do nothing when liveness is unknown (`None`). The wake-up stays owed, and `next` and the Stop
   hook name both calls, as today.
3. Leave `next` reading "Nothing to call" for a wake-up the ledger resumed, as for one the mod
   delivered.
4. Update `05-sessions.md` ("Wake-ups owed", "Mod wake-up delivery", "Liveness, resume, and
   release") and `07-hooks-and-enforcement.md` (the `owed` hook). Add a dated line to
   `14-key-decisions.md` once Alex decides the open question below. Remove the item from
   `16-open-items.md`.

## Tests

- A stopped target is resumed once and its wake-up row is marked sent.
- Unknown liveness leaves the row owed, and `next` names both calls.
- A running target is never resumed.
- A resume that fails leaves the row owed.

## Open question

Should the automatic resume apply to the Oracle, whose session the user may have closed on
purpose?
