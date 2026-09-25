# Scoring and review

The rubric is identical for every scorer. Every number is a setting in
`.claude/sentinel-swarm.local.md`.

## Who scores

- Every file gets two reviews: the Coder's self review and the Lead's blind review.
- A Coder's own scores never pass a file. Only the Lead's review does. **(proposed)**
- The Manager does not score files. It reviews whether each Lead met its tasks, and the
  files whose two most recent scores disagreed.
- The Oracle does not score files. It audits the scores and investigates the low ones.
- `score_record(file_id, ratings, applicable, kind, targeted)` accepts `self` from the
  file's owner and `lead` from the module's Lead only.

## Dimensions

Nine dimensions, each with fixed criteria: meets the brief, testing, error handling,
security, architecture, code structure, performance, maintainability, and accessibility.
`mcp/src/swarm_ledger/rubric.py` holds every dimension and criterion key and its text.
The split of design into architecture and code structure is **(proposed)**.

- A scorer rates every criterion of every applicable dimension. A dimension that does
  not apply, such as accessibility on a back-end file, is marked not applicable with a
  one-line reason.
- Every dimension is scored on every review, including the dimensions a fix did not
  target.

## Scale **(proposed)**

- Each criterion is rated from 1 to 10.
- A dimension's score is the average of its ratings times 10, from 0 to 100.
- There is no combined score and there are no weights. Each dimension passes or fails
  on its own, and a criterion can fail on its own.
- A rating below 9 needs a reason. A file-and-line reference is optional in the ledger.

| Rating | Meaning |
|---|---|
| 9 to 10 | Fully met |
| 7 to 8 | Met, with a minor suggestion |
| 5 to 6 | Partly met; an issue is recorded |
| 3 to 4 | Not met; must be fixed |
| 1 to 2 | Wrong, unsafe, or missing |

## Thresholds

| Setting | Default **(proposed)** | Meaning |
|---|---|---|
| `target` | 90 | The goal for every dimension |
| `floor` | 70 | A fix that drops a dimension below this is a regression |
| `criterion_floor` | 5 | No file passes with a criterion below this |
| `disagreement_gap` | 10 | Two reviews this far apart on a dimension disagree |
| `plateau` | 2 | A smaller gain on a targeted dimension is a plateau |
| `regression_tolerance` | 5 | A drop this large on any dimension is a regression |

- **Pass rule:** every applicable dimension is at or above the target, and no criterion
  is below the criterion floor. `approve` requires it of the Lead review.
- **Disagreement:** a gap of `disagreement_gap` or more on a dimension, or one score at
  or above the target and the other below it. **(proposed)**

## Issues

- Every rating of 4 or lower, in a self or a Lead review, opens an issue on the file,
  one per open criterion. **(proposed)**
- A later Lead review that rates that criterion 5 or higher closes it.
- `issue_close(issue_id, resolution)` closes an issue by hand: the file's Lead, a
  Manager, or the Oracle.
- `approve` refuses while the file has an open issue. `issue_open` records a problem by
  hand. `idea_record` records an idea tried against an issue and its outcome.

## The improvement loop **(proposed)**

- A score below the target is something to research and improve. The loop stops when
  new scores stop getting better.
- After each fix, `attempt_record(file_id)` compares the last two Lead reviews:
  - **Regression:** a dimension fell by `regression_tolerance` or more, or fell below
    the floor. The ledger restores the file and test file from the previous handoff's
    saved versions. The failed idea is recorded so nobody tries it again.
  - **Improved:** no regression, and a targeted dimension rose by `plateau` or more. The
    attempt does not count against the budget.
  - **Plateau:** anything else.
- A plateau or a regression adds one attempt to each open issue of the file. After
  `attempts_per_round` such attempts, the issue moves to the next round.
- A fix is kept only when it is not a regression, so the file's current version is
  always the best so far.
- A file passes at once, in any round, when it meets the pass rule.

## Escalation

Escalation and the improvement loop are one mechanism: the score history shows when an
issue stopped improving, and it explains to the next layer why the issue arrived.

| Round | Attempts | Who works on it |
|---|---|---|
| 1 | 1 to 3 | The Coder and its Lead |
| 2 | 4 to 6 | The Manager steps in with its resources |
| 3 | 7 to 9 | The Oracle adds its suggestions |
| After round 3 | | The Oracle changes the plan or notifies the user |

- A round ends after 3 attempts that did not improve the score. **(counting rule
  proposed)** Only a reviewer's return counts, and accepting work as incomplete uses no
  attempt.
- `issue_escalate(issue_id)` moves an issue to its next round, names the receiver in
  `escalated_to` (the Manager for round 2, the Oracle for round 3), and messages it.
  Only the file owner and the owner's parent chain may call it.
- The Manager's resources include its other Leads and Coders, a new Lead, a fresh
  Coder, a stronger model, and a structural change such as a split file or a changed
  contract. **(proposed)**
- An issue a Manager finds starts at round 2. **(proposed)** The ledger opens every
  issue at round 1.
- A layer with no new idea passes the issue up.

## Evidence

- Passing tests are the only evidence the handoff requires. Coverage, lint, type checks,
  and security scans support the scores and do not gate. **(proposed)**
- The ledger runs the tests and records the result. A report cannot claim a pass that
  did not happen. **(proposed)**
