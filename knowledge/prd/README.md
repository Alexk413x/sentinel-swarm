# sentinel-swarm PRD

sentinel-swarm is a Claude Code plugin that takes a PRD of any size to built, tested,
reviewed code. Four roles do the work: the Oracle, the Managers, the Leads, and the
Coders. Every role runs as its own Claude Code session in the host repo's one working
tree. A shared SQLite ledger, served by the `swarm-ledger` MCP server, holds the run's
state and every gate. Hooks in each role's agent file make the ledger unavoidable.

The plugin ships as a private marketplace plugin for Alex's own use. It must work on
Windows and macOS, install into other repos, and adapt to each host project. The swarm
writes unit tests only. End-to-end testing is a separate process outside the swarm.

## How to read these documents

- This folder is the source of truth for how the system works. Read the document for
  a subject before you change it. The table below lists every document.
- **(proposed)** marks an item Alex has not reviewed. Everything else is settled.
- The code is the ground truth for behavior. When a document and the code disagree,
  fix whichever one is wrong, and record the fix in the document.
- For the module layout of the ledger server, read `mcp/ARCHITECTURE.md`. For the exact
  rubric criteria, read `mcp/src/swarm_ledger/rubric.py`. For the tables, read
  `mcp/src/swarm_ledger/schema.sql`.
- When Alex makes a decision, record it as a plain rule in the document for its
  subject, and add a dated line to [14-key-decisions.md](14-key-decisions.md). Mark
  your own additions **(proposed)** and list them in [15-proposed.md](15-proposed.md).
- Plans for work not built yet live in `plans/`, not here.

## Documents

| File | Covers |
|---|---|
| [01-roles.md](01-roles.md) | Principles for every role; the Oracle, Manager, Lead, and Coder; hierarchy, names, and models |
| [02-run-lifecycle.md](02-run-lifecycle.md) | A run from PRD to finish: phases, dependencies, and waves; modules, briefs, file claims, handoff, reviews, pause, and git |
| [03-scoring-and-review.md](03-scoring-and-review.md) | The rubric: who scores, dimensions, scale, thresholds, issues, the improvement loop, escalation, and evidence |
| [04-agreements.md](04-agreements.md) | Change requests, departures and their sign-off chain, shortfalls, deferrals, overrides, and directives |
| [05-sessions.md](05-sessions.md) | Every role as its own session: starting a child, identity, states, wake-ups, messages, liveness, resume, and what stops when |
| [06-ledger-server.md](06-ledger-server.md) | The per-repo ledger server: lifetime, records folder, database, and tools |
| [07-hooks-and-enforcement.md](07-hooks-and-enforcement.md) | Where the hooks live, the shim, what each hook does, and the rule-to-enforcement map |
| [08-watchdog.md](08-watchdog.md) | Watchdog detection, reporting, how a report reaches the Oracle, and settings |
| [09-mcp-servers-and-code-graph.md](09-mcp-servers-and-code-graph.md) | MCP servers per role, and how the swarm uses codebase-kg |
| [10-report-and-costs.md](10-report-and-costs.md) | The run report and how tokens and cost are counted |
| [11-setup-and-settings.md](11-setup-and-settings.md) | Install, setup, launch, skills, and the settings file |
| [12-testing.md](12-testing.md) | The checks for this repo, the quick and complete smoke tests, and what to check after a run |
| [13-platform-facts.md](13-platform-facts.md) | Verified Claude Code platform facts the design relies on |
| [14-key-decisions.md](14-key-decisions.md) | Alex's decisions, one dated line each |
| [15-proposed.md](15-proposed.md) | Items marked (proposed) that await review |
| [16-open-items.md](16-open-items.md) | Work that is not done yet |
