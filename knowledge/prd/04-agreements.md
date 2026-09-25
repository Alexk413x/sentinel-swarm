# Agreements

## Change requests

A change request is a ledger record plus a wake-up to the right agent. It closes only
with verified evidence.

| Tool | Caller | Effect |
|---|---|---|
| `cr_open(path, body)` | Any role | Routes to the file's live owner, else its module's live Lead, else its phase's live Manager, else the Oracle. Refuses a path no module plans, and a caller who is the recipient. Owes the recipient a wake-up |
| `cr_accept(cr_id, accept, reason)` | The recipient | `open` to `accepted` or `declined`. A decline needs a reason. Owes the requester a wake-up |
| `cr_complete(cr_id, notes)` | The recipient | `accepted` to `completed`. A Coder recipient needs its own passing file-scope test run of the path or its test file since acceptance. Any other recipient needs such a run by anyone, or an approved handoff of the file since acceptance. Owes the requester a wake-up |
| `cr_verify(cr_id, ok, notes)` | The requester, or its nearest live ancestor | `completed` to `verified`, or back to `accepted` with the notes and a wake-up to the recipient |
| `cr_list(state)` | Any role | The caller's own requests. The Oracle sees every request in the run |

- `handoff_submit` refuses while a Coder's accepted request on the file is not
  completed. `approve`, `phase_update(approved)`, and `run_finish` refuse while a
  request in their scope is `open`, `accepted`, or `completed`.
- The report lists each request with its reason, decision, work done, evidence test
  run, and verification.

## Departures

A departure records a break from the guidelines. The guidelines are a target, not a
first-pass gate: an agent may get something working first, and the review cycle moves
it toward the guidelines.

- A Coder lists departures in `handoff_submit`. Each becomes a row linked to the
  handoff, in state `open`, waiting on the Lead.
- `departure_record(body, file_id, guideline_id)` records one outside a handoff: a
  Coder for its own file, a Lead, Manager, or Oracle for work in its scope. A Coder's
  departure waits on the Lead, a Lead's on the Manager, and a Manager's on the Oracle.
  A departure the Oracle records is signed off at once. **(proposed)**
- A departure passes up a sign-off chain: the Lead, then the Manager, then the Oracle.
  `departure_decide(departure_id, decision, reason, solution)` takes `agree` or
  `push_back`, and only the next level may call it.

| State | Waits on | `agree` moves it to |
|---|---|---|
| `open` | The file's Lead, or the recorder's parent level | `lead_agreed`, or the next state up |
| `lead_agreed` | The phase's Manager | `manager_agreed` |
| `manager_agreed` | The Oracle | `signed_off` |

- Every decision needs a reason, and `departure_decisions` keeps each one.
- A pushback needs a suggested solution and sets `pushed_back`.
  - A Lead pushback is a return. The Lead calls `return_work` next, and it counts as a
    fix attempt.
  - A Manager or Oracle pushback resumes the chain below the decider, down to the same
    Coder. The ledger reopens the file for that Coder, even after approval, and refuses
    when another live claim holds the path. It un-releases the agents below the
    decider, records a fix attempt, posts the departure, decider, reason, and solution
    to each, sets the module to `returned` and a handed-up phase to `working`, and owes
    a wake-up from each level to the next. The result's `next` is the decider's
    wake-up.
  - A pushback on a file whose handoff is still `submitted` returns that handoff.
    **(proposed)**
  - A pushback on a departure with no file resumes the chain to the recorder but blocks
    no gate, since no approval can mark it reworked. **(proposed)**
- Approving the reworked file's next handoff, or accepting it as incomplete, marks the
  pushed-back departure `reworked`.
- The Manager accepts a module only after it decides every departure in the module. The
  Oracle accepts a phase, and finishes the run, only after every departure is signed off
  or reworked.

## Shortfalls

`shortfall_record(body, file_id)` records a solution that works but that nobody found
better. Any role may call it. It needs no decision, and the report lists it for work
outside the run.

## Deferrals and scope changes

- Any agent can suggest that work happens later, or that the scope changes, with
  `deferral_propose(body, file_id)`. `accept_incomplete` opens one too.
- A suggestion takes effect only when the responsible level agrees.
  `agreement_decide(deferral_id, "agreed" | "denied", reason)` needs a caller whose role
  ranks at or above the proposer's parent role.
- The deferral must also sit in the caller's scope: a Lead decides only in its own
  module, a Manager only in its own phase, and the Oracle anywhere in its run. The
  deferral's file sets its module and phase. A deferral with no file takes the
  proposer's. **(proposed)**
- An open deferral on a file blocks `phase_update(approved)` for that file's phase. Any
  open deferral blocks `run_finish`.

The responsible level **(proposed)**:

| What changes | Who agrees |
|---|---|
| A file's task or its tests | The Lead |
| A module's scope, or a contract between files | The Lead, and the Manager when another module is affected |
| A phase's scope, or a contract between modules | The Manager |
| The phase plan, or work moved to a later phase | The Oracle |
| What the PRD asks for | The user, through the Oracle |

## Overrides

- `override_grant(rule, target_agent_name, target, reason)` is the Oracle's alone. The
  `pre_ledger` hook denies it to any other caller.
- An override is narrow: one rule, one agent, one target, one use. **(proposed)** The
  ledger consumes it on first use.
- Two rules take overrides: `write` (the target is the repo-relative path) in the write
  gate, and `shell` (the target is the exact command) in the shell gate.
- Every override is a ledger record, and the report lists it with its reason.
  **(proposed)**
- The Oracle must not use an override to write a project file itself. **(proposed)**
  The Oracle's agent file has no write tool.

## Directives

- `directive_submit(source, sender_name, body, reply_to)` steers the run from any input.
  Sources: `user-chat`, `outside-session`, `skill`, and `watchdog`. It needs no
  identity, so an ordinary session can call it.
- A directive does not interrupt the agents. The Oracle reads `directive_inbox()` at
  safe points and turns each directive into a plan change, a guideline change, a brief,
  or a message. It never forwards a directive's text down the tree.
- Every directive carries full authority, whatever its source. The Oracle acts without
  a confirmation step.
- `directive_resolve(directive_id, outcome, resolution)` closes it. The outcome is
  `applied`, `scheduled`, `declined`, or `needs_user`, and the ledger refuses any other
  value. **(proposed)** The report lists each directive with its source and outcome.
- A `needs_user` outcome keeps the directive open, and its resolution states the
  question for the user. While a directive waits on the user, the Oracle's Stop hook
  lets the Oracle stop, and the watchdog reports no stall. **(proposed)**
- A directive whose `reply_to` names a waiting directive resolves the waiting one. The
  reply reaches the Oracle through `directive_inbox()` as a new open directive. The
  Oracle can also call `directive_resolve` again on a waiting directive, for example
  after the user answers in the Oracle's session. `directive_submit` refuses a
  `reply_to` that names no directive of the run. **(proposed)**
- `run_finish` refuses while a directive is open.

## What the Oracle tells the user

| Situation | The Oracle |
|---|---|
| Only the user can unblock it, and the run can continue afterwards | Notifies the user now, with `run_pause` |
| An issue ends round 3 below the floor | Notifies the user now |
| It works, but nobody found better: a shortfall or a signed-off departure | Lists it in the final report |

A notification states what is blocked, what the user needs to do, and what resumes
after. **(proposed)**
