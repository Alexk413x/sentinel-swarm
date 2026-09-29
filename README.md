# sentinel-swarm

A Claude Code plugin that runs an agent swarm, Oracle, Manager, Lead, and Coder, plus a
Driver that tests the app when the host has cartographer and a driver plugin, to take a
PRD to built, tested, reviewed code. Enforced reviews, a shared
ledger, and a scoring rubric hold every role accountable, and every hand-off
requires recorded evidence, not a claim.

## Status

Pre-release. A small run works from start to finish: the hello-world smoke test
passes with every role as its own background session, and a stopped session resumes
and finishes its work. `knowledge/prd/16-open-items.md` lists what is not built yet and
what comes next.

## Roles

| Role | Color | Default model | Created by |
|---|---|---|---|
| Oracle | cyan | opus | The user |
| Manager | green | opus | Oracle |
| Lead | purple | sonnet | Manager |
| Coder | orange | sonnet | Lead |

Each parent-child pair follows the same cycle: brief, work, self-review, hand up,
review, then approve, return, or accept as incomplete. Only the Coder writes
project files. See `knowledge/prd/01-roles.md` for what each role owns and must not do.

Every role runs as its own Claude Code session, listed in agent view. A parent starts
its child with the ledger tool `agent_spawn`; no role has the `Agent` tool.

## Running a swarm

1. Install the plugin at project scope in the host repo.
2. Trust the host folder once: run `claude` in it and accept the trust prompt.
3. Run `python -m swarm_ledger.setup` (or `/sentinel-swarm:setup`). It writes the
   role files to `.claude/agents/swarm-<role>.md`, the hook shim to
   `.sentinel-swarm/hook.py`, and the git excludes.
4. Run `python -m swarm_ledger.launch "<prompt>"` (or `/sentinel-swarm:run`). It
   starts the repo's ledger server and the Oracle session. Add `--bg` for a
   background session or `--headless` for `claude -p`.

`scripts/smoke.sh` does all of this for a scratch host repo in `runs/hello/`.

## Required dependency

sentinel-swarm requires the `codebase-kg` plugin (`>=0.8.2`). Every role queries the
code graph before it plans or writes, and the Coder updates it for its own file.
`accessibility-tools` and `cartographer` are optional dependencies.

## Repo layout

```
.claude-plugin/   plugin.json and marketplace.json
skills/           swarm-protocol, run, plan, status, resume, setup
hooks/            hooks.json, which carries no hooks, and the per-role hook table
templates/        agents/<role>.md role templates, hook_shim.py, and the settings example
mcp/              the swarm-ledger Python server (separate project, see below)
scripts/          smoke.sh, the end-to-end smoke test
knowledge/        code_graph.db, and prd/, the source of truth for how the swarm works
plans/            plans for work not built yet
```

The plugin ships no `agents/` folder. Setup writes the four role files into the host
repo, where their `hooks` frontmatter applies.

## Dev commands (mcp/)

The ledger server is a separate Python project under `mcp/`.

```
cd mcp
uv sync
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```

## Design documents

Read `knowledge/prd/README.md`, which lists one document per subject. Anything marked
**(proposed)** has not been reviewed and is not final. `plans/` holds plans for work not
built yet.
