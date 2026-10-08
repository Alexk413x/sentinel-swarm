---
name: run
description: How to start a sentinel-swarm run that takes a PRD to built, tested, reviewed code. Use for "run the swarm", "start sentinel-swarm on this PRD", "build this PRD with the swarm", or "take this PRD to done".
disable-model-invocation: true
---

# run

Starts a full sentinel-swarm run: a PRD in, built and reviewed code out.

## Before you start

1. sentinel-swarm is installed at project scope in the host repo. codebase-kg is
   installed and enabled; sentinel-swarm declares it as a required dependency.
2. `/sentinel-swarm:setup` has run in this repo. It writes the role files in
   `.claude/agents/`, the hook shim, and the settings the sessions need.
3. The host repo is trusted. A background session refuses to start in an untrusted
   folder, and Claude Code runs the role files' hooks only in a trusted folder.
   `setup` reports this and prints the command to fix it.
4. `.claude/sentinel-swarm.local.md` exists and its `test_command` is filled in. The
   command must contain `{target}`, for example
   `python -m pytest -q -p no:cacheprovider {target}`. Every test run in the swarm
   uses it, so a missing or wrong value blocks every handoff.
5. The host repo has a code graph at `knowledge/code_graph.db`.
6. The working tree is on the branch the run should build on, and it is up to date
   with `main`. The whole swarm works in one working tree and commits nothing.

## Start the run

Run the launcher from the host repo root. It starts the repo's ledger server, then
starts the Oracle as a background session named `<repo>-oracle-<MMDD-HHMMSS>`:

```bash
python="$(command -v py >/dev/null 2>&1 && echo 'py -3' || { command -v python3 >/dev/null 2>&1 && echo python3; } || echo python)"
$python -I -S "${CLAUDE_PLUGIN_ROOT}/mcp/launch/ledger.py" launch --bg "<the PRD text>"
```

When the PRD is long, save it to a file in the repo and pass a prompt that names the
file, for example `"Build the PRD in docs/prd.md."`.

The launcher writes the role files first when any is missing, and it refuses to start
a background or headless run in an untrusted repo. When it refuses, show the user the command it
prints and stop.

Two other modes, for a user who runs the launcher from a terminal:

- No flag: an interactive Oracle session in the terminal.
- `--headless --transcript <file>`: `claude -p`, with the stream-json transcript
  written to the file. Nobody answers questions in a headless run, so the Oracle
  makes reasonable assumptions and records each one in the run's guidelines.

## Watch the run

Every role is its own session with its own row in agent view: the Oracle in cyan,
each Manager in green, each Lead in purple, and each Coder in orange. The user can
open any row, watch it, and type into it. `claude agents` lists the same sessions.

`/sentinel-swarm:status` reads the run's state from the ledger.

## Steer a live run

An ordinary session sends the Oracle a directive with the directive command, from the
host repo root:

```bash
python="$(command -v py >/dev/null 2>&1 && echo 'py -3' || { command -v python3 >/dev/null 2>&1 && echo python3; } || echo python)"
$python -I -S "${CLAUDE_PLUGIN_ROOT}/mcp/launch/ledger.py" directive --source skill --sender "<your name>" "<the directive>"
```

Use `--source outside_session` when the text comes from the user outside this skill,
and `--reply-to <directive id>` to answer a question the Oracle asked. It prints the
recorded directive as JSON. A directive steers the plan, a future phase, or the
guidelines. It does not interrupt the agents; the Oracle applies it at its next safe
point.

## What to expect

1. The Oracle reads the PRD, sets the project guidelines, and plans the work as a
   dependency graph of phases.
2. It starts one Manager session per unlocked phase. Each Manager starts one Lead
   session per module, and each Lead starts one Coder session per file.
3. Every file gets two reviews: the Coder's self review and the Lead's blind review.
   Passing tests and a current code graph gate every handoff.
4. The Oracle runs the full test suite at each join point and again at the end, then
   writes the final report.

Phases that do not depend on each other run at the same time, so several Coders write
different files at once. Release stops a finished session. Nothing is committed: the
user commits at the end.

## Where the records are

`.sentinel-swarm/` at the root of the working tree:

- `ledger.db` holds every brief, claim, score, review, test run, issue, and decision.
- `versions/` holds a saved copy of each file at each handoff.
- `report.md` is the Oracle's final report, written by `report_build`.
- `server.json` holds the URL of the repo's ledger server while a run is live.
- `hook.py` is the hook shim.

The folder is excluded through `.git/info/exclude`, so it never reaches the host
repo's history.
