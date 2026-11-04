---
name: sentinel-swarm
description: Copilot companion for Sentinel Swarm. Use for swarm status, setup, run, or resume requests in Copilot. Reads Claude run records and reports unsupported native workers without starting a swarm.
---

# Sentinel Swarm in Copilot

Use the separate Copilot entry point. Do not follow the Claude-only skills in the
repository's top-level `skills/` folder as Copilot worker instructions.

## Setup, run, and resume

Native Copilot workers are unsupported. Stop before changing host files, starting
a ledger server, or launching workers. An installed skill does not load the
`claude-code` enforcement mod. Do not copy Claude role templates to Copilot agent
folders, substitute `copilot` for `claude`, or bypass the mod.

To print the capability check, resolve the Sentinel Swarm installation or checkout
root, then run its `mcp/launch/ledger.py copilot check` entry point on Python 3.9 or
newer with `-I -S`. The JSON reports `native_workers: false` and an `error`, and
the command exits 1. This is an unsupported capability, not a missing dependency
that you can fix by running Claude setup.

A Claude-worker bridge requires an explicit user choice and remains a Claude Code
run. Do not infer this choice from "run the swarm" in Copilot. Native workers versus
a bridge is a product decision; this companion implements neither.

## Status

Resolve the actual Sentinel Swarm installation or checkout root. Do not assume
`CLAUDE_PLUGIN_ROOT` exists in Copilot or points at this plugin. From the host repo,
run the separate entry point. For PowerShell:

```powershell
py -3 -I -S '<sentinel-swarm root>\mcp\launch\ledger.py' copilot status --repo '<host repo>'
```

For a POSIX shell:

```sh
python3 -I -S '<sentinel-swarm root>/mcp/launch/ledger.py' copilot status --repo '<host repo>'
```

Replace the placeholders with resolved absolute paths. If the named interpreter
is unavailable, use `python` or `python3` on Python 3.9 or newer. Do not run setup
to locate or create a missing ledger.

The command reads SQLite with `mode=ro` and never retries a writable open. It prints
the run snapshot as JSON. It exits 1 with an `error` for a missing, unreadable, or
incompatible ledger. Report that error; do not claim the run finished. Empty `runs`
without an error means no recorded run matches the request. Use `--run <id>` to
select a run. Sessions and liveness refer to Claude Code, not Copilot sessions.

Report the records without editing them. Do not start a server, resume workers,
answer permissions, or send directives for a status request.
