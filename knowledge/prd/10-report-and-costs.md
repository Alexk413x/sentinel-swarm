# The run report and costs

`report_build()` writes `.sentinel-swarm/report-<run_id>.md`, and copies it to
`.sentinel-swarm/report.md` so a tool that always reads the latest report, such as
`scripts/smoke.sh`, keeps working. `run_finish` rebuilds both, and the Oracle's last
stop or session end refreshes both with the Oracle's final tokens. `report.md` is
overwritten by every run; each run's own `report-<run_id>.md` survives it, so earlier
runs' reports stay on disk. The user copies either out to keep it. **(proposed)**

Sections **(contents proposed)**:

- Outcome, duration, and the pause reason for a paused run.
- Repo: the run's branch and the last `repo_check`.
- One section per phase, timed from its Manager's start: each module and each file with
  its state and latest Lead scores.
- Manager and Oracle reviews.
- Returns and fix attempts: path, fix round, who returned it, target dimensions,
  outcome, and issues.
- Open items: open deferrals, and every issue by round with its attempts and resolution.
- Decided deferrals: who proposed each one, who decided it, and the decision and
  reason. **(proposed)**
- Departures, each with its full decision chain and final state.
- Shortfalls.
- Change requests, with decision, work done, evidence, and verification.
- Overrides, with reasons.
- Directives, with source and outcome.
- Notifications to the user: every directive resolved `needs_user`, with the question
  asked (kept even after a later `directive_resolve` changes the outcome) and each
  reply it received through `reply_to`. **(proposed)**
- Final test run: the last `full`-scope `tests_run`, with passed, failed, skipped, and
  exit code, or a note that none was recorded. **(proposed)**
- Measures: each phase's working time; each role's total agent time, summed across its
  agents; returns per file, from the `attempts` table; and cost per phase, summed from
  each agent's cost by its `phase_id`, with the Oracle's cost under a run-level total
  since the Oracle has no phase. **(proposed)**
- Agents: model, tokens, elapsed time, tool uses, context overflows, and cost, plus a
  run total.

## Token counting and cost

- The Stop and SessionEnd hooks read the agent's transcript. Claude Code writes one API
  response as one line per content block, each repeating the response's usage, so the
  ledger counts only the last line of each response id.
- Each response is priced at its own model's list rate, with 5-minute and 1-hour cache
  writes priced apart. A cache write with no split is priced at the 1-hour rate. The
  total is stored in `agents.cost_usd`.
- `mcp/src/swarm_ledger/pricing.py` holds the rates per model family. An agent row with
  no stored cost is estimated from its token totals.
