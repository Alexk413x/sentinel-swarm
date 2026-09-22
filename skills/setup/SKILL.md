---
name: setup
description: Prepares a host repo for sentinel-swarm: writes .claude/sentinel-swarm.local.md, detects the test, build, and lint commands, and builds the code graph. Use for "set up sentinel-swarm here", "prepare this repo for the swarm", or "check if this repo is ready for sentinel-swarm".
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

## 5. Confirm the records folder is excluded

The ledger creates `.sentinel-swarm/` at the root of the main checkout and adds it to
`.git/info/exclude` on first use. Confirm the repo is a git checkout so that this
works, and confirm that nothing in the host repo's `.gitignore` or CI expects the
folder. Do not add it to the host repo's committed `.gitignore`.

## 6. Report

Say what was detected, what was written, and anything the user must confirm by hand:
a test command you could not infer, a missing dependency, or a repo with no git
checkout.
