# sentinel-swarm: session handoff

Status on 2026-09-24: written by session `sentinel-swarm-a8`, updated by `sentinel-swarm-d3`, `sentinel-swarm-75`, and later sessions. Read `plans/CLAUDE.md` first, then this file.

## Where the work stands

- The design is drafted in `01-roles.md`, `02-rubric.md`, `03-ledger.md`, `04-mcp-and-hooks.md`, and `06-sessions-plan.md`. Items marked **(proposed)** are Claude's additions, and Alex has not reviewed them one by one.
- Every role runs as its own Claude Code session, decided by Alex on 2026-09-23 and built as `35905d7`. A parent starts its child with the ledger tool `agent_spawn`; no role has the `Agent` tool. One ledger server per host repo listens over HTTP, and every session connects to it. See `06-sessions-plan.md`.
- **The swarm runs.** On 2026-09-23 the background smoke run passed with four sessions, and the crash test passed: a stopped Coder session was resumed with `agent_resume` and the run finished with `success`. See `06-sessions-plan.md` "Build status on 2026-09-23".
- The repo is on `main`, pushed to the private remote `github.com/Alexk413x/sentinel-swarm`. CI has not run since 2026-09-22 05:36 because of a GitHub billing block, which Alex says resets on its own. Until then, the four local checks are the only checks: 477 tests, pyright, and ruff pass as of 2026-09-24.
- What exists: the `swarm-ledger` server (`mcp/src/swarm_ledger/`, layout in `mcp/ARCHITECTURE.md`), the hooks behind `python -m swarm_ledger.hooks`, the four role templates in `templates/agents/`, setup and the launcher, and the six skills. Run `uv run pytest`, `uv run pyright`, `uv run ruff check`, and `uv run ruff format --check` from `mcp/` before each commit.
- Built on 2026-09-24, unit-tested only, **not yet run live**:
  - **Watchdog** (`watchdog.py`, `watch.py`): a thread in the ledger server finds crashed, stuck, spinning, permission-blocked, near-full-context, and stalled sessions, and files each as a `watchdog` directive. The Oracle arms `Monitor` with `python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch`; a new directive prints a line, which wakes the Oracle (probed 2026-09-24: a Monitor line wakes an idle background session). The Oracle's Stop hook blocks until the watch is armed. `pre_monitor` allows only that command, which closes the shell hole that `Monitor` opened for the Oracle. Lifecycle: the thread lives and dies with the server; the server exits after `run_finish`, and after `idle_exit_minutes` (15) with no active run or a paused run and no session running. The listener exits when the run is not active, and a newer listener retires an older one. The server reuses its port from `.sentinel-swarm/server.port`, so resumed sessions keep their ledger URL.
  - **Change requests** (`agreements.py`): `cr_open`, `cr_accept`, `cr_complete` (needs test evidence), `cr_verify`, `cr_list`; gates on `handoff_submit`, `approve`, `phase_update(approved)`, and `run_finish`.
  - **Departures and shortfalls**: `departure_record`, `departure_decide`, `shortfall_record`; `handoff_submit`'s `departures` list becomes departure rows; `approve` refuses an undecided or denied departure; the report lists all three kinds.
  - **Repo tools** (`repo.py`): `repo_check` reports the branch, a clean tree, the base branch (`base_branch` setting, else `main`, else `master`), ahead and behind counts, and advice; `repo_branch_create` makes the run's branch only when the start is obvious. `agent_spawn` refuses a Manager until the run has a `repo_check`. `scripts/smoke.sh` now runs `git init -b main`.
  - **Review gates** (`oversight.py`): the Manager's `module_review` needs every file decided, a passing module or phase test run after the last decision, and a note for each score disagreement; `phase_update(handed_up)` needs one per module. The Oracle's `phase_review` needs a passing full test run after hand-up, a note for each file with a dimension below target, and a note for each accepted departure; `phase_update(approved)` needs it.
  - Setup adds a ledger hook that the template has and an existing role file lacks, so hosts set up earlier get `pre_monitor`.
  - The ledger server starts with a hidden console on Windows (`CREATE_NO_WINDOW`). With `DETACHED_PROCESS`, every console program it ran opened a terminal window that took focus.
- macOS: the code has POSIX branches, and CI covers macOS once billing resets, but no live run has happened on a Mac.
- Open: once, the ledger server exited about a minute after it started, with no error in its log. It did not happen again.

## Next steps, in order

1. **Live smoke run with the new features.** Ran on 2026-09-24 with `--bg`: outcome `succeeded` in 7.5 minutes, with a `repo_check`, one Lead return and re-approval, a Manager `module_review`, an Oracle `phase_review`, a passing full test run, and `watch_heartbeat_at` set. No `swarm_ledger` process and no `server.json` remained. It found one bug, now fixed: `sessions.is_running` counted state `done` as dead, but a background session reports `done` when its turn ends while its process still runs. `agent_resume` then ran `claude --resume` on a live Coder, which started a second session under the same name, and `approve` stopped only the new one. The same rule could make the watchdog resume a live, idle Oracle. **Next:** run the smoke test again and confirm that a return wakes the Coder with `SendMessage` and leaves one session per role. The Oracle's session stays up after `run_finish` by design.
2. **Crash test that the watchdog catches alone.** During a `--bg` run, `claude stop` the Coder's session mid-file. Pass: within about a minute a `watchdog` directive of kind `crashed` appears, the Oracle wakes from its Monitor and calls `agent_resume`, and the run finishes, with no message from outside the run.
3. **Nothing left running.** After each run, check that no `swarm_ledger` process remains (PowerShell: `Get-CimInstance Win32_Process | ? CommandLine -match swarm_ledger`), that `.sentinel-swarm/server.json` is gone, and that the run's sessions are stopped in `claude agents`. Do not `claude rm` them; Alex reviews them in agent view.
4. **CI and macOS.** When the GitHub billing resets, confirm CI passes on Ubuntu, Windows, and macOS. Then one live smoke run on a Mac.
5. **Run the swarm on a real PRD** with two modules and several files, to exercise parallel Leads and Coders, release freeing slots, `return_work`, escalation, change requests, and messages between Coders. Draft a small PRD for Alex to review first. Expect about $10 to $20.
6. **Prototype checks still open.** Whether `PreCompact` fires in a long session.
7. **Open question** in `06-sessions-plan.md`: where the model settings live.
8. **Refresh the code graph.** `knowledge/code_graph.db` is stale for about 30 files after 2026-09-24. Run `/codebase-kg:refresh`.

## For Alex to review, all **(proposed)**

- The watchdog wakes the Oracle, and only the Oracle, with `claude --resume` when the Oracle's session is not running and reports are waiting. After 3 failed wakes it pauses the run. This sits next to your rule that the watchdog only reports.
- The server exits after 15 idle minutes on an abandoned or paused run. `idle_exit_minutes` sets it.
- Setup re-adds a ledger hook that the template has and a role file lacks, so a hook the user removed on purpose comes back.
- A change request goes to the lowest live owner: the file's Coder, else its Lead, else its Manager, else the Oracle.
- Only a role above the normal decider can deny a departure that was already accepted; a late denial on an approved file opens a deferral.
- The watchdog's context window defaults to 200,000 tokens. Sessions on a 1M model need `context_window: 1000000` in the settings, because the transcript does not say which window a session has.

## Earlier steps, done

1. **Code graph for this repo.** Done on 2026-09-21. The graph is at `knowledge/code_graph.db`, and `/codebase-kg:setup` installed `.githooks/`. Run `/codebase-kg:refresh` after changes to mapped files.
2. **Prototype checks.** Done on 2026-09-21. The results are in `04-mcp-and-hooks.md` "Prototype results on 2026-09-21".
   - Done: nesting depth (three layers, no spare), resume with `SendMessage` (context is kept), trust in a resume message, SQLite concurrent writers (WAL, busy timeout, `BEGIN IMMEDIATE`), and the cost of a Python hook (about 300 ms through Git Bash).
   - Done with the probe hooks, which are now deleted: `updatedInput` rewrites an MCP tool's input, `SubagentStop` can block, and hook input carries `agent_id` and `agent_type` at all three layers but never the parent. `SubagentStop` fires more than once for one agent, so it does not mean that the agent ended.
   - Open from the probe: whether `PreCompact` fires inside a subagent. It needs a subagent that fills its context, so run it during the first long swarm test.
   - Done: `kg_upsert_node` does not re-baseline a file's hash, and only `codebase-kg-build --rebaseline` does. Alex decided on 2026-09-21: `handoff_submit` checks anchors and coverage only, and the hash stays a review signal for the Lead and the git hooks. See the check 7 row of the results table.
   - Done: concurrent writers on the code graph file are unsafe, because `kg_upsert_node` copies, edits, and replaces the file without a lock, and changes are lost silently. Parallel per-file test runs are reliable. Hook 8 gets `agent_id` and `file_path` in one hook input.
   - Alex decided on 2026-09-21: a ledger tool `graph_upsert` does the lock and the upsert in one call. Fallback: Coders behind a ledger lock. See the check 5 row.
3. **Build the ledger, the hooks, the prompts, and the skills.** Done on 2026-09-22, in five delegated steps, each verified in the main session.
4. **Smoke test.** Passed on 2026-09-22 on the third run. Alex's checklist is in `04-mcp-and-hooks.md` "Build order and the smoke test", and the results follow it there.

## How Alex works

- Alex writes loosely and expects you to interpret intent. Restate your reading before you build on it.
- Alex declined multiple-choice prompts. Ask one question at a time, with a concrete example, two readings in a table, and a recommendation.
- A draft to react to works better than an abstract question.
- Alex wants responsibilities settled before mechanics.
- Record each decision in the plans as soon as Alex makes it. Mark your own additions **(proposed)**.
- Delegate multi-file work to Sonnet subagents with precise briefs, then run the checks yourself. A subagent's report is not proof.
- Commit with neutral messages. Pushing to the private remote is approved for this repo.

## Decisions worth knowing before you read the plans

- Four roles: Oracle (cyan), Manager (green), Lead (purple), Coder (orange). One Manager per phase, one Lead per module, one Coder per file. A hello-world run still creates one of each.
- Only the Coder writes project files. Oracle, Manager, and Lead have no shell and no write tools. They work through MCP tools only.
- The first version works in one working tree, on the checked-out branch. The swarm never commits or pushes. Every role runs as its own session (decided 2026-09-23; this replaced nested subagents).
- The ledger MCP server holds the state and the gates. Hooks make the ledger unavoidable. Enforcement is a hard block, and only the Oracle overrides.
- The hard check at handoff: the ledger runs the file's tests itself, and the code graph must be current for the file.
- Scoring: each criterion rated 1 to 10, each dimension computed out of 100, no combined score and no weights. Target 90, floor 70.
- Escalation and diminishing returns are one mechanism: 3 rounds, and a round ends after 3 attempts that did not improve the scores.
- codebase-kg is required. Every agent searches the graph first, then falls back to Grep or Glob.
- Tracking is local only. The records folder is git-ignored through `.git/info/exclude`.
- No role has a time limit or a turn limit. The watchdog reports stuck or spinning agents to the Oracle.

## Decisions made on 2026-09-21

All are recorded in `04-mcp-and-hooks.md`.

- The ledger identity is the agent's name, not its `agent_id`. The name is set at start, does not change, and is the message address. How hook 6 binds the name to the `agent_id` is a **(proposed)** item.
- User directives: a ledger channel that steers the Oracle from any source, without interrupting the agents. It also carries escalation replies and watchdog reports. Every directive carries full authority, whatever its source.
- Build order: every piece is tested on its own first. The smoke test is one run with the prompt "Create a hello world file."
- The code graph hash is a review signal, not a gate. `handoff_submit` checks anchors and coverage only.
- `graph_upsert` is a ledger tool that takes the lock and applies the upsert in one call.
- No telephone game: every plan and task is a ledger record, and an agent re-reads its brief instead of trusting its memory. See `04-mcp-and-hooks.md` "No telephone game".

## Working notes for the smoke test

- Run `bash scripts/smoke.sh [--headless | --bg] ["<prompt>"]` from this repo. It empties `runs/hello/`, rebuilds the host repo in `runs/hello/host/`, installs a clean copy of the plugin at project scope, runs setup, and starts the Oracle through the installed copy's launcher. Without a flag the Oracle opens as an interactive session. `--bg` starts it in the background; `bash scripts/smoke.sh --results` then prints the results. `--headless` runs `claude -p` and writes the transcript to `runs/hello/transcript.jsonl`. From PowerShell, use Git Bash explicitly: `& "C:\Program Files\Git\bin\bash.exe" scripts/smoke.sh`, because `bash` on Windows can resolve to WSL. `runs/` is git-ignored. The host repo has a README, a `pyproject.toml` that sets `testpaths` and `pythonpath`, `.claude/sentinel-swarm.local.md` from the template with `test_command: python -m pytest -q -p no:cacheprovider {target}`, and a one-node graph. Set `KG_PLUGIN_DIR` to pick a codebase-kg version other than the newest in the plugin cache.
- A headless run needs an explicit `--allowedTools` list. Do not use `--dangerously-skip-permissions`; the session's permission classifier denies it.
- Judge a run from `.sentinel-swarm/ledger.db`, not from the transcript. The queries used are in the results table's rows.

## Working notes for this repo

- `plans/*.md` are mapped in the code graph. After a plan edit, rebuild or refresh the graph before the commit, or the `pre-push` hook reports drift. The graph's authoring JSON is not kept. Use `/codebase-kg:refresh`. For a plan edit that leaves the node descriptions true, export the graph, then build the export with `--rebaseline`, and only when `kg_validate` lists no stale file that you did not re-read.
- If the codebase-kg tools are missing in a new session, run `/reload-plugins`.
- The builder CLI in this repo: `uvx --quiet --from "<codebase-kg plugin cache>/mcp" codebase-kg-build <json> -o knowledge/code_graph.db`.

## Housekeeping

- The misspelled folder `C:\Users\Alexk\Projects\Plugins\sentinal-swarm` is empty. Delete it once no VS Code window holds it open. Ask Alex first.
- `plans/notes-prompt-cache-costs.md` holds cost notes that Alex wants raised when swarm testing starts.
