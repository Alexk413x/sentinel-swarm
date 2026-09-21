# sentinel-swarm

A Claude Code plugin that runs a four-role agent swarm, Oracle, Manager, Lead, and
Coder, to take a PRD to built, tested, reviewed code. Enforced reviews, a shared
ledger, and a scoring rubric hold every role accountable, and every hand-off
requires recorded evidence, not a claim.

## Status

Design complete in `plans/`, skeleton only. Nothing runs yet: the ledger MCP server
has no working tools, and no hook in `hooks/hooks.json` is wired up. The plugin
surface exists so the implementation has a fixed shape to build into.

## Roles

| Role | Color | Default model | Created by |
|---|---|---|---|
| Oracle | cyan | fable | The user |
| Manager | green | opus | Oracle |
| Lead | purple | sonnet | Manager |
| Coder | orange | sonnet | Lead |

Each parent-child pair follows the same cycle: brief, work, self-review, hand up,
review, then approve, return, or accept as incomplete. Only the Coder writes
project files. See `plans/01-roles.md` for what each role owns and must not do.

## Required dependency

sentinel-swarm requires the `codebase-kg` plugin (`>=0.7.0`). Every role queries the
code graph before it plans or writes, and the Coder updates it for its own file.
`accessibility-tools` and `cartographer` are optional dependencies.

## Repo layout

```
.claude-plugin/   plugin.json and marketplace.json
.mcp.json         the swarm-ledger MCP server entry
agents/           oracle.md, manager.md, lead.md, coder.md
skills/           swarm-protocol, run, plan, status, resume, setup
hooks/            hooks.json (empty today) and the planned hook table
templates/        sentinel-swarm.local.md.example, the per-project settings file
mcp/              the swarm-ledger Python server (separate project, see below)
plans/            design documents; the source of truth for every role
```

## Dev commands (mcp/)

The ledger server is a separate Python project under `mcp/`.

```
cd mcp
uv sync
uv run pytest
uv run pyright
uv run ruff check
```

## Design documents

Read `plans/CLAUDE.md` first, then `plans/01-roles.md`, `plans/02-rubric.md`,
`plans/03-ledger.md`, and `plans/04-mcp-and-hooks.md`. Anything marked
**(proposed)** in those files has not been reviewed and is not final.
