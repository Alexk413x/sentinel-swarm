# Scoring and review

The rubric is identical for every scorer. Every number is a setting in
`.claude/sentinel-swarm.local.md`.

## Who scores

- Every file gets two reviews: the Coder's self review and the Lead's blind review.
- A Coder's own scores never pass a file. Only the Lead's review does. **(proposed)**
- The Manager does not score files. It reviews whether each Lead met its tasks, and the
  files whose self and Lead scores on the approved handoff disagreed.
- The Oracle does not score files. It audits the scores and investigates the low ones.
- `score_record(file_id, ratings, applicable, kind, targeted)` accepts `self` from the
  file's owner and `lead` from the module's Lead only.
- The Lead scores blind, before it sees the Coder's scores. `score_record` refuses a
  Lead review once `review_compare` has run for the file's latest handoff.

## Dimensions

Nine dimensions, each with fixed criteria: meets the brief, testing, error handling,
security, architecture, code structure, performance, maintainability, and accessibility.
`mcp/src/swarm_ledger/rubric.py` holds every dimension and criterion key and its text.
`score_record`'s JSON schema lists the same keys, built from it. The split of design
into architecture and code structure is **(proposed)**.

- A scorer rates every criterion of every applicable dimension. A dimension that does
  not apply, such as accessibility on a back-end file, is marked not applicable with a
  one-line reason.
- Every dimension is scored on every review, including the dimensions a fix did not
  target. `score_record` refuses a Coder or Lead score set whose `applicable` does not
  list all nine dimensions, and a dimension marked not applicable with an empty reason.
  **(proposed)**

## Scale **(proposed)**

- Each criterion is rated from 1 to 10.
- A dimension's score is the average of its ratings times 10, from 10 to 100.
- There is no combined score and there are no weights. Each dimension passes or fails
  on its own, and a criterion can fail on its own.
- A rating below 9 needs a reason and a `ref` (file and line). `score_record` refuses
  a rating below 9 that lacks either. **(proposed)**

## Module and phase review scores **(proposed)**

- `module_review` and `phase_review` take a `scores` list when the outcome is
  `accepted`: one rating from 1 to 10 for each of three dimensions — completeness,
  integration, and open items — with a reason below 9. The ledger refuses an
  `accepted` outcome missing a required dimension, carrying an unknown one, or rating
  below 9 with no reason.
- The scores are the Manager's or the Oracle's own judgment of the module or the
  phase as a whole; they are not derived from the files' rubric scores. They are
  stored on the review record and the report shows them next to that review.

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
  is below the criterion floor. `approve` requires it of the Lead review, or the floor
  pass below.
- **Disagreement:** a gap of `disagreement_gap` or more on a dimension, or one score at
  or above the target and the other below it. **(proposed)**

## Floor pass **(proposed)**

- A file that fails the pass rule still passes when it ends its last escalation round
  with every applicable dimension at or above `floor` and no criterion below
  `criterion_floor`. `attempts.round` counts fix attempts per file, not per issue, so
  it never caps at `rounds`; a file is at its last round once its recorded attempts
  reach the full escalation budget, `rounds` times `attempts_per_round`.
- `approve` accepts the handoff then. It records which dimensions passed at the floor,
  as a shortfall for each dimension still below `target`, and the report shows them.

## Issues

- Every rating of 4 or lower, in a self or a Lead review, opens an issue on the file,
  one per open criterion. **(proposed)**
- A later Lead review that rates that criterion 5 or higher closes it.
- `issue_close(issue_id, resolution)` closes an issue by hand: the file's Lead, a
  Manager, or the Oracle.
- `approve` refuses while the file has an open issue. `issue_open` records a problem by
  hand. `idea_record` records an idea tried against an issue and its outcome.
- **Blind scoring.** Until the file's Lead has recorded its own score for the file's
  current handoff, `issue_list` hides from that Lead the issues the Coder's self
  review opened for that file. They show once the Lead scores. **(proposed)**

## The improvement loop **(proposed)**

- A score below the target is something to research and improve. The loop stops when
  new scores stop getting better.
- After each fix, `attempt_record(file_id)` compares the last two Lead reviews:
  - **Regression:** a dimension fell by `regression_tolerance` or more, or fell by any
    amount and ended below the floor. The ledger restores the file and test file from
    the previous handoff's saved versions. `attempt_record` does not record the failed
    idea; a role records it with `idea_record` so nobody tries it again.
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
  `escalated_to` (the Manager for round 2, the Oracle for round 3), messages it, and
  owes it a wake-up with a pointer, returned as `next`. Only the file owner and the
  owner's parent chain may call it. It refuses an issue already at round `rounds`.
  **(proposed)**
- `attempt_record` advances a round the same way: on a plateau or a regression that
  exhausts `attempts_per_round`, it sets `escalated_to`, messages the receiver, and
  owes it the same wake-up, listed in the result's `escalated`. **(proposed)**
- The Manager's resources include its other Leads and Coders, a new Lead, a fresh
  Coder, a stronger model, and a structural change such as a split file or a changed
  contract. **(proposed)**
- An issue opened by hand with `issue_open` starts at round 1 for a Coder or a Lead,
  round 2 for a Manager, and round 3 for the Oracle. **(proposed)** An issue a
  self or Lead review opens automatically always starts at round 1.
- A layer with no new idea passes the issue up.
- An issue that ends round 3 below the floor notifies the user now. `attempt_record`
  records it: when a plateau or a regression brings an issue at round `rounds` to
  `attempts_per_round` attempts, and the file's kept Lead review (the earlier one after
  a regression) has the issue's dimension below `floor` or its criterion below
  `criterion_floor`, the ledger records an error notification with the event key
  `issue:<issue_id>`. For an issue with no dimension, any dimension or criterion of the
  file counts. The ledger server shows the OS notification, and the Oracle owes the
  `PushNotification`. See "Driver notifications" in
  [02-run-lifecycle.md](02-run-lifecycle.md) for both paths. **(proposed)**

## Evidence

- Passing tests are the only evidence the handoff requires. Coverage, lint, type checks,
  and security scans support the scores and do not gate. **(proposed)**
- The ledger runs the tests and records the result. A report cannot claim a pass that
  did not happen. **(proposed)**
