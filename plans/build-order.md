# Plan: build order for the unbuilt features

Decided by Alex on 2026-09-25: build all of `plans/unbuilt-features.md` items 1 to 20, the Driver role, and the directive source rename, then Channels last. Sonnet agents build; the main session merges, verifies, and records.

## Rounds

| Round | Batch | Items | Isolation |
|---|---|---|---|
| 1 | A: reviews and scoring | 1 floor pass, 2 scored module and phase reviews, 3 ref below 9, 4 hide self-review issues until the Lead scores, 5 Manager issues at round 2, 6 notify on round advance | worktree |
| 1 | B: report, directives, overrides | 7 `reply_to` closes its directive, 8 report sections, 10 overrides, 15 a report per run, 17 measures section, 23 snake_case directive sources | worktree |
| 1 | C: sessions and safety | 9 scoped pause, 11 cap counts other swarms, 12 multi-repo lock, 13 `SendMessage` guard, 14 codebase-kg check at session start, 16 per-role settings | worktree |
| 1 | D: tooling | 18 `--results` checklist, 19 dev version per smoke run, 20 plugin features | worktree |
| 2 | E: the Driver role | 21, per `plans/driver-agent.md` | main, after round 1 merges |
| 3 | F: Channels | 22, per `plans/channels-and-http.md`: probes, then a build if they pass | main |

## Rules for every agent

- Tests go in a new test file per batch, so branches merge cleanly.
- No agent edits `knowledge/code_graph.db`, `knowledge/prd/14-key-decisions.md`, `15-proposed.md`, `16-open-items.md`, or `plans/`. Each reports what to record there, and the main session records it.
- A design choice the plans don't settle takes the simplest form that fits the PRD, and is marked **(proposed)** in the subject's PRD document.

## After each round

Merge each branch, resolve conflicts, run the four checks, rebuild the code graph, record decisions and proposed items, push, and run the quick smoke test. After round 2, run the complete test too.
