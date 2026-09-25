# Plan: designed but not built

Status: every item here was designed and marked **(proposed)**, or described in an old plan, but the code does not do it. `knowledge/prd/` describes what the code does now. For each item, Alex decides: **build**, **drop**, or **later**. A dropped item leaves the PRD; a built one moves into the PRD as a rule.

Recommendations are mine, **(proposed)**.

## Reviews and scoring

| # | Item | Today | Building it means | Recommendation |
|---|---|---|---|---|
| 1 | A file that ends the last round at or above the floor (70) passes, with each shortfall recorded | `approve` needs the target (90) on every dimension; `shortfall_record` only records | `approve` accepts a floor pass after round 3 when a shortfall names each dimension below target | **Build.** Without it, a hard file can only be accepted as incomplete. |
| 2 | Scored module and phase reviews on completeness, integration, and open items | `module_review` and `phase_review` record an outcome and notes | Two more rubric dimensions and scores on the review rows | **Later.** The notes already carry the judgment; scores add cost. |
| 3 | A rating below 9 needs a file-and-line reference | A reason is required; the reference is optional | `score_record` refuses a rating below 9 without `ref` | **Build.** Small, and it makes reviews checkable. |
| 4 | Blind scoring hides the other score set until both exist | Order is enforced: no Lead score after `review_compare`. Self-review issues are visible through `issue_list` | Hide self-review issues from the Lead until it scores | **Drop.** Order enforcement covers the intent. |
| 5 | An issue a Manager finds starts at round 2 | Every issue starts at round 1 | `issue_open` sets the round from the opener's role | **Build.** One line and a test. |
| 6 | Round advance notifies the next level | `attempt_record` moves an issue to the next round silently; only `issue_escalate` notifies | Set `escalated_to` and owe a wake-up when a round advances | **Build.** Otherwise the Manager may not learn a round 2 started. |

## Agreements, directives, and the report

| # | Item | Today | Building it means | Recommendation |
|---|---|---|---|---|
| 7 | A directive's `reply_to` closes the notification it answers | `reply_to` is stored only | `directive_submit` with `reply_to` resolves the original | **Build**, with the needs-user fix. |
| 8 | The report lists decided deferrals with who agreed, the notifications sent, and the final full-suite result | Open deferrals only; no notifications section | Three more report sections | **Build.** Cheap, and you asked for complete reports. |
| 9 | While the run waits on the user, phases the problem does not block keep going | `run_pause` pauses the whole run | A pause scoped to the blocked phases | **Later.** Needs care with waves; rare so far. |
| 10 | Overrides are honored by the agent, records, and identity hooks too | Only the write and shell hooks honor overrides. Nothing stops the Oracle granting itself a write override, except that it has no Write tool | Refuse an override whose target is the Oracle itself; extend to other hooks only if a real need appears | **Build** the self-grant refusal; **drop** the rest. |

## Sessions and safety

| # | Item | Today | Building it means | Recommendation |
|---|---|---|---|---|
| 11 | `parallelism_cap` counts other swarms' sessions on the machine | Counts this run's live agents | Count swarm sessions from `claude agents --json` | **Later.** One swarm at a time so far. |
| 12 | A lock for runs that span several repos | None | A shared lock file | **Drop** until a multi-repo run exists. |
| 13 | A hook refuses a `SendMessage` to a session outside the caller's run | None | `PreToolUse` on `SendMessage` checks the target against the run's sessions | **Build** if Channels do not replace `SendMessage`. See the Channels research. |
| 14 | `SessionStart` checks that codebase-kg is installed | It checks for the graph file and the git exclude | Check the plugin registry too | **Build.** A clear error beats a missing tool. |
| 15 | Per-run records folders, `.sentinel-swarm/runs/<timestamp>/` | One `ledger.db` for every run, one `report.md` overwritten per run | Move the ledger and report per run, or add a run id to the report file name | **Build** the report part: keep `report-<run_id>.md` so earlier reports survive. |
| 16 | Other per-role settings: effort, cache lifetime, a per-role parallelism cap | Not built | Settings keys passed to `claude --bg` | **Later.** Revisit with cost data. |

## Tooling and tests

| # | Item | Today | Building it means | Recommendation |
|---|---|---|---|---|
| 17 | Run analytics measures as a report section | `analytics_query` reads raw tables | A measures section: time per stage, returns per file, cost per phase | **Build** a small version with the cost data we now have. |
| 18 | The smoke test's ledger checklist, automated | Checked by hand after each run | `smoke.sh --results` prints pass or fail per check | **Build.** Every run today repeated the same manual queries. |
| 19 | Smoke runs install under their own version | They install as `0.0.1` over the working copy | A dev version suffix per run | **Later.** No conflict seen since the fresh-folder fix. |
| 20 | Plugin features: `monitors/monitors.json`, `subagentStatusLine`, `agent` in `settings.json`, `userConfig` | Not used | Varies | **Later.** Fold into the Channels and HTTP work. |

## How to use this plan

Go through the table in order and record each decision here. Then build the **build** items in small commits, each with a test and a PRD update, and remove the item from this file. Move **later** items to `knowledge/prd/16-open-items.md`, and delete **drop** items from the PRD's proposed list.
