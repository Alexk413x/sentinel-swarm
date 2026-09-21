---
name: setup
description: Prepares a host repo for sentinel-swarm. Use for "set up sentinel-swarm here", "prepare this repo for the swarm", or "check if this repo is ready for sentinel-swarm".
---

# setup

Prepares a host repo so a sentinel-swarm run has what it needs before it starts.

## Purpose

Runs once per host repo (or again after the stack changes) to detect the project's
commands, confirm its dependencies, and create the local records the ledger needs.

## Planned steps

1. Detect the project's stack, and its test, build, and lint commands. Write them
   into `test_command`, `build_command`, and `lint_command` in
   `.claude/sentinel-swarm.local.md`, per
   `templates/sentinel-swarm.local.md.example`.
2. Check that the codebase-kg plugin is installed, since it is a required
   dependency of sentinel-swarm, per `plans/01-roles.md` "The code graph".
3. Check that a code graph already exists for the repo. When it does not, hand off
   to codebase-kg's own setup to build one. An empty repo starts with an empty
   graph.
4. Create the records folder the ledger uses for plans, briefs, reviews, scores,
   and evidence, per `plans/03-ledger.md` "Tracking and storage".
5. Add the records folder to `.git/info/exclude`, not the host repo's own
   `.gitignore`, so the swarm leaves no trace in the host repo's committed files.
6. Report what was detected, what was created, and anything the user needs to
   confirm or fill in by hand.

## Status

Not implemented. The swarm-ledger MCP server this skill depends on does not exist
yet, so this skill cannot write real records or detect commands today.
