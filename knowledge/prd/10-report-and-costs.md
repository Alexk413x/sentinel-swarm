# The run report and costs

`report_build()` writes `.sentinel-swarm/report-<run_id>.md`, and copies it to
`.sentinel-swarm/report.md` so a tool that always reads the latest report, such as
`scripts/smoke.sh`, keeps working. `run_finish` rebuilds both, and the Oracle's last
stop or session end refreshes both with the Oracle's final tokens. `report.md` is
overwritten by every run; each run's own `report-<run_id>.md` survives it, so earlier
runs' reports stay on disk. The user copies either out to keep it.

Sections:

- Outcome, duration, and the pause reason for a paused run.
- Repo: the run's branch and the last `repo_check`.
- One section per phase, timed from its Manager's start: each module and each file with
  its state and latest Lead scores, and the dimensions of a file that passed at the
  floor.
- Manager and Oracle reviews, each with its outcome, notes, and completeness,
  integration, and open-items scores.
- Returns and fix attempts: path, fix round, who returned it, target dimensions,
  outcome, and issues.
- Explorations: each one's ordinal, request id, state, and focus, and each finding it
  recorded with its id, title, fingerprint, severity, area, and whether it recurred in
  a later exploration or was not seen again. A "Fixes" list shows each
  brief that names findings: the child's name and role, and each finding's id and
  title. A "Stop rules" list shows each `[driver-stop]` directive's
  reason, its outcome, and its evidence lines, including what is left after a stall.
  A Driver that fails to load has no entry here; it shows only as its
  directive and its notification.
- Open items: open deferrals, and every issue by round with its attempts and resolution.
- Decided deferrals: who proposed each one, who decided it, and the decision and
  reason.
- Disputes: each deferral with parties, its kind, the proposer and the parties, the
  arbiter, the reason, and the outcome with the decider and its reason, or `open`.
- Departures, each with its full decision chain and final state.
- Shortfalls.
- Change requests, with decision, work done, evidence, and verification.
- Overrides, with reasons.
- Graph gaps: each `Grep` and `Glob` a role ran, with the agent, the tool, the pattern,
  the path it searched, and how many paths it found, with the first five. A search
  scoped to a file the code graph anchors is marked "(the graph anchors this file)",
  from a lookup of the path in `knowledge/code_graph.db`.
- Stale mapped files: each file that `knowledge/code_graph.db` maps and whose content
  differs from its recorded baseline when the report is built, with the nodes that
  anchor it. A file of this run is marked "(a file of this run)". A file with no
  baseline is not listed. The codebase-kg pre-push hook blocks on these files.
- Directives, with source and outcome. A `driver` directive shows only its first line,
  since its evidence is under Explorations.
- Notifications to the user: every directive resolved `needs_user`, with the question
  asked (kept even after a later `directive_resolve` changes the outcome) and each
  reply it received through `reply_to`; then every row of the `notifications` table,
  with its kind, message, and time.
- Final test run: the last `full`-scope `tests_run` that is no longer running, with
  passed, failed, skipped, and exit code, or a note that none was recorded.
- Measures: each phase's working time; each role's total agent time, summed across its
  agents; returns per file, from the `attempts` table; and cost per phase, summed from
  each agent's cost by its `phase_id`, with the Oracle's cost under a run-level total
  since the Oracle has no phase. A Driver has no phase either, so its cost shows only
  under Agents.
- Agents: model, tokens (input, output, cache read, cache write), elapsed time, tool
  uses, context overflows, and cost, plus a run total.

## Token counting and cost

- The Stop and SessionEnd hooks read the agent's transcript. Claude Code writes one API
  response as one line per content block, each repeating the response's usage, so the
  ledger counts only the last line of each response id.
- Each response is priced at its own model's list rate, with 5-minute and 1-hour cache
  writes priced apart. A cache write with no split is priced at the 1-hour rate. The
  total is stored in `agents.cost_usd`.
- A transcript records token counts per response and no cost, so
  `mcp/src/swarm_ledger/pricing.py` prices every response from its own table. An agent
  row with no stored cost is estimated from its token totals.
- The built-in rates are Anthropic's list prices in USD per million tokens (input, output,
  cache read, 5-minute cache write, 1-hour cache write), read from
  <https://platform.claude.com/docs/en/about-claude/pricing> on 2026-11-12, one row per
  model version:

  | Model | Input | Output | Cache read | 5-min write | 1-hour write |
  |---|---|---|---|---|---|
  | Fable 5.2, Mythos 5.2 | 8 | 40 | 0.20 | 10 | 16 |
  | Fable 5.1, Mythos 5.1 | 10 | 50 | 0.25 | 12.5 | 20 |
  | Fable 5, Mythos 5 | 10 | 50 | 1 | 12.5 | 20 |
  | Opus 5.5 | 4 | 20 | 0.20 | 5 | 8 |
  | Opus 5, 4.8, 4.7, 4.6, 4.5 | 5 | 25 | 0.50 | 6.25 | 10 |
  | Opus 4.1, 4 | 15 | 75 | 1.50 | 18.75 | 30 |
  | Sonnet 5.5 | 2 | 10 | 0.10 | 2.5 | 4 |
  | Sonnet 5 | 2 | 10 | 0.20 | 2.5 | 4 |
  | Sonnet 4.6, 4.5, 4 | 3 | 15 | 0.30 | 3.75 | 6 |
  | Haiku 5.5, prompt up to 100,000 tokens | 0.10 | 0.50 | 0.01 | 0.125 | 0.20 |
  | Haiku 5.5, prompt over 100,000 tokens | 0.50 | 2.50 | 0.05 | 0.625 | 1 |
  | Haiku 4.5 | 1 | 5 | 0.10 | 1.25 | 2 |

  The version comes from the model id (`claude-opus-5-5`, `claude-opus-4-1-20250805`). An
  alias such as `sonnet`, or a version with no row, is priced as its family's newest model.
  Haiku 5.5's higher row applies per response, by that response's input, cache read, and
  cache write tokens; an estimate from token totals uses the lower row. Fast mode, the
  Batch API, and data-residency multipliers are not priced.
- The `pricing` setting overrides the table without a release: `pricing.<family>` names any
  of `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, and a family with
  no built-in row needs all five. A family is a lower-case part of the model name, the
  longest match wins, and a configured row is used before the table for every model it
  matches. Loading the settings applies them to the server's pricing.
- When a transcript has a response whose model has no rate, the hook stores no cost
  from it, and the report falls back to the estimate. A cost the ledger cannot price
  shows as `unknown`, and so does any phase or run total that includes it.
