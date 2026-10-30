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

When the user set the plugin's `test_command` option (`${user_config.test_command}`),
use that value verbatim for `test_command`. Otherwise detect the stack and write the
three commands into the frontmatter. `test_command` must contain `{target}`; the
ledger substitutes the file, module, or phase being tested. A full run substitutes
`full_target`, and refuses while it is empty: set it to the folder that holds the
tests, for pytest the `testpaths` folder such as `tests`. Run from the repo root
without a path, pytest walks the whole tree and imports every `test_*.py` it finds.

| Stack | `test_command` |
|---|---|
| Python with pytest | `python -m pytest -q -p no:cacheprovider {target}` |
| Node with vitest | `npx vitest run {target}` |
| Node with jest | `npx jest {target}` |
| Go | `go test {target}` |

`-p no:cacheprovider` matters: several Coders run pytest at the same time in one
working tree, and they would otherwise contend on `.pytest_cache`.

Then fill in the test settings:

- `test_timeout_s` (default 600) bounds one test run's process. A suite that takes
  longer than half of it needs sharding: put `-n auto` (pytest-xdist) in the full
  run's command, `test_command_by_scope.full`, or raise the cap.
- `test_command_by_scope` takes a command per scope (`file`, `module`, `phase`,
  `full`). Use it for what only one scope needs, such as an environment variable that
  turns on a slow benchmark: put it in `test_command`, and every handoff runs it.
- `allowed_skips` names the skips the environment causes on purpose, by text in the
  skip's location or reason. Every other skip fails its run and blocks every handoff
  and phase review. For pytest, add `-rs` to the command so the ledger can read the
  reasons.
- `slow_tests` names pytest node ids or folders to leave out of every run unless a
  brief asks for them (`tests_run(include_slow=true)`).

Fill `build_command` and `lint_command` the same way, from what the repo actually
uses, for example `npm run build` and `npx eslint .`, or `uv run ruff check`. Both
are optional; the shell gate allows them for Coders when they are set.

Leave `models`, `rubric`, `escalation`, and `runtime` at their defaults unless the
user asks for a change.

## 3. Confirm codebase-kg

codebase-kg is a required dependency. Check that the plugin is installed and its MCP
server is loaded. A session loads plugin tools when it starts, and you cannot run
`/reload-plugins` yourself: after a fresh install, tell the user to start a new session,
or to run `/reload-plugins` in this one, before the server and the swarm's tools appear.

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
python="$(command -v py >/dev/null 2>&1 && echo 'py -3' || { command -v python3 >/dev/null 2>&1 && echo python3; } || echo python)"
$python -I -S "${CLAUDE_PLUGIN_ROOT}/mcp/launch/ledger.py" setup
```

The first line picks a Python 3.9 or newer interpreter: `py -3`, else `python3`, else
`python`. The ledger imports only the standard library, so it needs no venv and no install
step.

It is safe to run again, and it prints what it changed and what it found:

- The test command, tried through the ledger's own runner (`cmd.exe` on Windows, so a
  path only a POSIX shell accepts fails here first). For pytest it dry-runs the full
  command with `--collect-only` and reports the collected count or the collection
  error. It warns about an empty `full_target`, and about skips no `allowed_skips`
  entry can match.
- With `--time-full-run`, it runs the full suite once and warns when the run takes
  more than half of `test_timeout_s`. Offer the user this run: it takes as long as the
  suite does.

- `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and
  `swarm-coder.md`, from the plugin's templates. Each file sets its role's model,
  color, tools, and MCP servers. It sets no permission mode: each role starts in your
  own `permissions.defaultMode` unless `permission_mode.<role>` in
  `.claude/sentinel-swarm.local.md` names one, and setup prints the mode each role
  starts in. Setup adds no MCP server
  beyond the template's. A session gets the a11y servers when the host has a11y
  installed.
- When a role file already exists, it keeps the user's frontmatter values, adds any
  key the template has that the file lacks, and replaces the prompt body. Run it
  again after a plugin upgrade.
- `.sentinel-swarm/hook.py`, the shim every role's hooks call.
- `worktree.bgIsolation: "none"` in `.claude/settings.local.json`, so every session
  works in the one shared working tree.
- `.sentinel-swarm/`, `.claude/agents/swarm-*.md`, `.claude/codebase-kg.local.md`, and
  `.claude/sentinel-swarm.local.md` in `.git/info/exclude`. Do not
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
a test command you could not infer, every warning the test check printed, a missing
dependency, a repo with no git checkout, or a repo that is not trusted yet.
