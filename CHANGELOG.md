# Changelog

## 0.1.0 (2026-09-28)

Tooling for Claude Code 2.1.284, Opus 5.5, and Sonnet 5.5, with no loss of function.

- Hooks answer through the running ledger server (`POST /hook/<event>`) and fall back
  to the `uv run` path, still fail-closed. Activity bookkeeping runs as an async
  `post_activity` hook; `post_any` stays synchronous for the tools the Stop hook reads.
- The ledger's venv lives in the plugin data folder, keyed by `uv.lock`, not in the
  versioned plugin cache.
- The plugin declares no MCP server. `python -m swarm_ledger.directive` steers a live
  run from an ordinary session.
- Each role's `tools` list names only its own ledger tools, the ledger refuses a tool
  outside a role's set (proposed), `agent_id` is gone from every tool schema, and
  `score_record` carries its rubric keys in the schema. Read-only tools carry
  `readOnlyHint`.
- `run_status` and `status_tree` leave out the PRD and the settings snapshot;
  `tests_run` returns a capped summary, `test_run_get` returns the full output, and a
  passing run on an unchanged tree is reused (`force=true` runs anyway).
- Every role runs at `medium` effort by default, a brief can raise a child's effort,
  and the ledger records it. The Coder's prompt cache TTL defaults to 5 minutes.
- The Oracle and Managers see the run's elapsed time (and an optional
  `time_budget_minutes`) on their wake-ups; the Driver's results carry its next
  check-in time.
- Each role loads its working set of tools with one `ToolSearch`.
- The Oracle's Stop hook lets it stop while a just-spawned child starts.
- A background session without a `pid` in `claude agents --json` (Claude Code
  2.1.284) counts as running, so wake-ups no longer resume a live session into a copy.
- The Driver's cartographer and driver servers use plugin-install keys, and
  `pre_agent` accepts `cartographer:map-driver` and `cartographer:map-reviewer`.
- Setup denies the roles as subagents, and turns off codebase-kg's post-edit nudge.
- `--headless` refuses an untrusted repo. `run` and `resume` are user-invoked only.
- The `plan` skill's conventions move into the Oracle template, and the skill is gone.
- A benchmark (`scripts/bench.sh`) with held-out PRDs and hidden acceptance tests,
  `checklist --json` run metrics, a skill-trigger eval suite (`evals/`), and smoke-run
  teardown.
- Requires codebase-kg `>=0.8.2`. The plugin states that it runs in Claude Code only.

## 0.0.1 (unreleased)

- Design documents for the role responsibilities, the scoring rubric, the ledger,
  and the MCP servers and hooks (`plans/`).
- Plugin skeleton: manifest, marketplace entry, MCP server registration, the four
  role agents, the six skills, the planned hooks table, and the settings template.
- Ledger server skeleton (`mcp/`).
