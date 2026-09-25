---
name: setup
description: Prepares a host repo for sentinel-swarm. It writes .claude/sentinel-swarm.local.md, detects the test, build, and lint commands, builds the code graph, writes the role files in .claude/agents/, and checks that the repo is trusted. Use for "set up sentinel-swarm here", "prepare this repo for the swarm", or "check if this repo is ready for sentinel-swarm".
---

# setup

Prepares a host repo so a sentinel-swarm run has what it needs. Run it once per
repo, and again when the stack changes.

## 1. Create the settings file

Copy the plugin's template to `.claude/sentinel-swarm.local.md` in the host repo:

```
${CLAUDE_PLUGIN_ROOT}/templates/sentinel-swarm.local.md.example
```

Keep every key. The ledger reads this frontmatter, and a renamed key is ignored. When
the file already exists, edit it in place instead of overwriting the user's values.

## 2. Fill in the commands

Detect the stack and write the three commands into the frontmatter. `test_command`
must contain `{target}`; the ledger substitutes the file, module, or phase being
tested, and drops the placeholder for a full run.

| Stack | `test_command` |
|---|---|
| Python with pytest | `python -m pytest -q -p no:cacheprovider {target}` |
| Node with vitest | `npx vitest run {target}` |
| Node with jest | `npx jest {target}` |
| Go | `go test {target}` |

`-p no:cacheprovider` matters: several Coders run pytest at the same time in one
working tree, and they would otherwise contend on `.pytest_cache`.

Fill `build_command` and `lint_command` the same way, from what the repo actually
uses, for example `npm run build` and `npx eslint .`, or `uv run ruff check`. Both
are optional; the shell gate allows them for Coders when they are set.

Leave `models`, `rubric`, `escalation`, and `runtime` at their defaults unless the
user asks for a change.

## 3. Confirm codebase-kg

codebase-kg is a required dependency. Check that the plugin is installed and its MCP
server is loaded. A fresh install sometimes needs `/reload-plugins` before the server
appears.

## 4. Build the code graph

Check for `knowledge/code_graph.db` in the host repo. When it is missing, run
`/codebase-kg:build`. An empty repo starts with an empty graph, which is fine: the
swarm's Coders add nodes for the files they write.

When a graph exists but is stale, run `/codebase-kg:validate` and report what it
finds. `handoff_submit` checks that each Coder's file has a node whose anchors
resolve, so a graph that does not cover the repo's existing code slows the first
phase down.

## 5. Write the role files

Run from the host repo root:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/mcp" --frozen --no-dev python -m swarm_ledger.setup
```

It is safe to run again, and it prints what it changed:

- `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and
  `swarm-coder.md`, from the plugin's templates. Each file sets its role's model,
  color, tools, permission mode, MCP servers, and hooks. Setup adds no MCP server
  beyond the template's. A session gets the a11y servers when the host has a11y
  installed.
- When a role file already exists, it keeps the user's frontmatter values, adds any
  key the template has that the file lacks, and replaces the prompt body. Run it
  again after a plugin upgrade.
- `.sentinel-swarm/hook.py`, the shim every role's hooks call.
- `worktree.bgIsolation: "none"` in `.claude/settings.local.json`, so every session
  works in the one shared working tree.
- `.sentinel-swarm/` and `.claude/agents/swarm-*.md` in `.git/info/exclude`. Do not
  add them to the host repo's committed `.gitignore`. A user who wants to share the
  role files commits them on purpose.

The role files are the user's to edit. Tell the user so: each role's model, tools,
and hooks live in its own file.

## 6. Trust the repo

The last line of the output says whether the repo is trusted. A background session
refuses to start in an untrusted folder, and Claude Code runs the role files' hooks
only in a trusted folder. When the repo is not trusted, show the user the command the
output prints. They run it once in a terminal, accept the trust prompt, and exit.

## 7. Report

Say what was detected, what was written, and anything the user must confirm by hand:
a test command you could not infer, a missing dependency, a repo with no git
checkout, or a repo that is not trusted yet.
