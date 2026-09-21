# sentinel-swarm: scoring rubric

Status on 2026-09-20: third draft, after two rounds of review with Alex. Bullets marked **(proposed)** are Claude's additions that Alex has not reviewed. Every number in this file is a plugin setting, so that Alex can tune it between runs.

## Who reviews what

- Every file gets two reviews: the Coder's self-review and the Lead's review.
- The Lead records its scores before it sees the Coder's scores. The two sets are then compared at the Lead's level. The Lead reviews the discrepancies and suggests fixes where a fix is possible.
- The Manager does not score every file. It reviews two things:
  - Whether each Lead met its tasks.
  - The items that the two most recent scores for a file did not agree on.
- The Oracle scores nothing directly. It audits the scores and investigates the low ones.
- "Did not agree" means a gap of 10 points or more on a dimension, or one score at or above the target and the other below it. **(proposed)**

## Dimensions

Every scorer uses the same dimensions and criteria.

| # | Dimension | Criteria |
|---|---|---|
| 1 | Meets the brief | Does what the brief asked. Does nothing that the brief did not ask for |
| 2 | Testing | Happy path. Known, possible edge cases. Tests prove what the brief said they must prove. No empty or skipped tests |
| 3 | Error handling | API and I/O errors handled. Specific error types caught, plus a catch-all. Failures reported, not swallowed |
| 4 | Security | Input validation. Injection. Secrets. Authentication and authorization. Unsafe defaults. New dependencies |
| 5 | Architecture | The project's architecture patterns are followed. Guidelines followed. Contracts honored. Code lives in the right file. Departures recorded |
| 6 | Code structure | Each function does one thing. Modular. Reusable. No duplicated code |
| 7 | Performance | No needless work. Complexity fits the data size. Sensible use of memory, I/O, and network |
| 8 | Maintainability | Clear names. Small units. Minimal comments: only where the project's rules call for one |
| 9 | Accessibility | UI files only. Checked with the accessibility-tools plugin when it is installed |

- Alex approved the first draft's dimensions and added the code aspects: reusability, functions that do one thing, modularity, architecture patterns, no duplication, and minimal comments. Rows 5, 6, and 8 hold them. The split of the old "Design" row into "Architecture" and "Code structure" is **(proposed)**.
- A scorer marks a dimension "not applicable" with a one-line reason, such as Accessibility for a back-end file.

## Scale

From Alex:

- Each item is scored on its own, on a 1 to 10 or 1 to 100 scale, for granularity.
- The scores stay separate, so that no item can be missed and no failing item can hide behind the others.
- Every dimension is scored every time, including the dimensions that a fix did not target.

Design: **(proposed)**

- A scorer rates each criterion from 1 to 10. Ten levels is as fine as a model scores reliably. It cannot tell an 87 from an 89, so a 1 to 100 rating adds noise, not control.
- The tooling computes each dimension's score from 0 to 100, as the average of its criterion ratings times 10. The thresholds apply to that number.
- There is no combined file score and there are no weights. Each dimension passes or fails on its own.
- A criterion can also fail on its own, so that one bad item cannot hide inside a good dimension.

| Rating | Meaning |
|---|---|
| 9 to 10 | Fully met. Nothing to change |
| 7 to 8 | Met, with a minor suggestion |
| 5 to 6 | Partly met. An issue is recorded |
| 3 to 4 | Not met. Must be fixed |
| 1 to 2 | Wrong, unsafe, or missing |

- Every rating below 9 carries a one-line reason and a file and line reference.
- Every rating of 4 or lower creates an issue in the ledger.

## Thresholds

Alex agreed to these values in principle, relative to the scale. All are settings.

| Setting | Default **(proposed)** | Meaning |
|---|---|---|
| Target | 90 | The goal for every dimension |
| Floor | 70 | No file passes with a dimension below this |
| Criterion floor | 5 | No file passes with a criterion rated below this |
| Disagreement gap | 10 | Two reviews that differ by this much on a dimension are "not agreed" |
| Plateau | 2 | A gain smaller than this, on a dimension that a fix targeted, is a plateau |
| Regression tolerance | 5 | A drop of this much on any dimension makes a fix "significantly worse" |

A file passes when every applicable dimension is at or above the target and no criterion is below the criterion floor.

## The improvement loop

From Alex:

- A score below the target is something to review. The agents research solutions and improve the work.
- The loop reaches diminishing returns: at some point new scores stop getting better. The loop stops there.
- When an update or a fix makes things worse, the change can be undone.
- Escalation and diminishing returns are one mechanism. The score history shows when an issue has stopped improving, and that is what moves it up. The same history explains to the next layer why the issue arrived.
- A fix for one or a few dimensions must not make the other dimensions significantly worse. The ideal solution makes all of them work.

Proposed rules: **(proposed)**

- Every hand-up is a saved version of the file, so any version can be restored.
- After each fix, every dimension is scored again. The result is one of three:
  - **Improved.** At least one targeted dimension rose by the plateau value or more, and no dimension fell by the regression tolerance or below the floor. The fix is kept. The loop continues in the same round, and the attempt does not count against the budget.
  - **Plateau.** No targeted dimension rose by the plateau value, and nothing regressed. The attempt counts as one of the round's 3.
  - **Regression.** A dimension fell by the regression tolerance or more, or dropped below the floor. The Coder restores the version from before the fix. The failed idea is recorded, so that nobody tries it again. The attempt counts as one of the round's 3.
- A fix is kept only when it is not a regression, so the current version is always the best one so far.
- A round ends after 3 attempts that did not improve the scores. The issue then moves up with its score history: every version, every score, and every idea that was tried.
- The loop cannot run forever. A score cannot pass 100, so the number of improving attempts is bounded, and the attempts that do not improve are capped at 9.
- **Stopping at or above the floor.** When the last round ends and every dimension is at or above the floor, the file passes with each shortfall recorded. The responsible level agrees to it, and a higher level can deny it with a reason and a solution. This is the same rule as an accepted departure. The Oracle audits the recorded shortfalls.
- **Stopping below the floor.** The Oracle decides whether to change the plan or notify the user.
- A file passes at once, in any round, when it meets the pass rule above.

## Rules that keep the scores honest

- **Blind scoring.** The Lead scores before it sees the Coder's scores. Alex confirmed this.
- **No self-approval.** A Coder's own scores never pass a file. Only the Lead's review does. **(proposed)**
- **Evidence.** Tool output supports a rating where a tool exists: test results, coverage, type checks, lint, and security scans. Only passing tests gate the handoff. **(proposed)**

## Module and phase reviews **(proposed)**

A Lead hands a module up, and a Manager hands a phase up. Each gets a short review on the same scale:

| Dimension | Module (reviewed by the Manager) | Phase (audited by the Oracle) |
|---|---|---|
| Completeness | Every file task is approved, or accepted as incomplete with a tracked item | Every module is approved, and the phase meets its acceptance criteria |
| Integration | The module's tests pass, and its files honor their contracts | The Manager's cross-module tests pass, and the full suite passes at the join point |
| Open items | Deferred items, accepted departures, and recorded shortfalls are listed | The same, across the phase |

## Open questions

None. Alex answered the scale, threshold, and weighting questions on 2026-09-20. The **(proposed)** items above await review.
