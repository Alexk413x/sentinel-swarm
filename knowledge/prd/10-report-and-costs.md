# The run report and costs

`report_build()` writes `.sentinel-swarm/report.md`. `run_finish` rebuilds it, and the
Oracle's last stop or session end refreshes it with the Oracle's final tokens. Each run
overwrites the previous run's report. The user copies it out to keep it. **(proposed)**

Sections **(contents proposed)**:

- Outcome, duration, and the pause reason for a paused run.
- Repo: the run's branch and the last `repo_check`.
- One section per phase, timed from its Manager's start: each module and each file with
  its state and latest Lead scores.
- Manager and Oracle reviews.
- Returns and fix attempts: path, fix round, who returned it, target dimensions,
  outcome, and issues.
- Open items: open deferrals, and every issue by round with its attempts and resolution.
- Departures, each with its full decision chain and final state.
- Shortfalls.
- Change requests, with decision, work done, evidence, and verification.
- Overrides, with reasons.
- Directives, with source and outcome.
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
