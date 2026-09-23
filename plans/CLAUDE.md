# plans: work index

Read this file before you start work. Update the status column when a document changes state.

## Documents

| File | Topic | Status |
|---|---|---|
| `00-discovery-handoff.md` | Purpose, platform facts, proposed components, sibling tooling survey | Done. Platform section corrected 2026-09-20 |
| `01-roles.md` | What each role does, owns, and hands off. The plan, models, testing, guidelines, escalation, watchdog | Six open questions resolved 2026-09-20. **(proposed)** items await Alex's review |
| `02-rubric.md` | The shared scoring rubric: dimensions, scale, pass bar, honesty rules | Third draft on 2026-09-20. No open questions. **(proposed)** items await Alex's review |
| `03-ledger.md` | Ledger tables, agent registry, per-role settings, lifecycle gates, platform facts for hooks | First draft |
| `04-mcp-and-hooks.md` | MCP servers, ledger tools, the hook list, the rule-to-enforcement map, one file's flow, user directives, prototype results, the no-telephone-game rule, build order, smoke test results | Prototype checks and the smoke test are done as of 2026-09-22. **(proposed)** items await review |
| `05-session-handoff.md` | State of the work, next steps in order, how Alex works, key decisions | Current as of 2026-09-22 |
| `06-sessions-plan.md` | Every role runs as its own session: spawning, waking, crashes and resume, hooks, memory, prototype checks, build order | Decision by Alex on 2026-09-23. The plan is **(proposed)**, three open questions |
| `notes-agent-view-vs-subagents.md` | Context sharing, customization, and interaction: agent view sessions versus subagents | Notes only |
| `notes-prompt-cache-costs.md` | Cost notes to discuss during swarm testing | Notes only |

## Order of design work

Alex settles responsibilities first, then mechanics.

1. Roles and responsibilities (`01-roles.md`).
2. Scoring rubric (`02-rubric.md`).
3. Protocol: briefs, reviews, change requests, notifications, escalation records.
4. Records: the folder structure for plans, progress, reviews, scores, and evidence. Resume from records.
5. Enforcement: hooks for each role. The mode is decided: hard block, and only the Oracle overrides. See `01-roles.md` "Enforcement and overrides".
6. Watchdog: stuck-agent detection and how it reaches the Oracle.
7. Tracking: decided. Local ledger only, as a setting with one value for now. Records are git-ignored. See `03-ledger.md` "Tracking and storage".
8. Sibling plugins: decided. codebase-kg is required, the others are optional. See `01-roles.md` "The code graph". Graph writes across sessions and worktrees need a test.
9. Git: this repo is initialized and `plans/` is committed (2026-09-20). The private remote is `github.com/Alexk413x/sentinel-swarm`, created 2026-09-21. The host-repo git workflow is in `01-roles.md` "Git workflow in the host repo".
10. Dev tooling for this repo: done on 2026-09-21. The plugin skeleton, the ledger server skeleton, pytest, pyright, ruff, and CI exist. The code graph is built at `knowledge/code_graph.db`, and `/codebase-kg:setup` installed the git hooks in `.githooks/` (2026-09-21). A new clone runs `sh .githooks/install.sh` once.
11. Implementation: done on 2026-09-22. The ledger, hooks, prompts, and skills exist, and the hello-world smoke test passes. See `05-session-handoff.md` for what comes next.

## Rules for these documents

- Mark anything that Alex has not reviewed as **(proposed)**.
- Keep responsibilities and mechanics in separate documents.
