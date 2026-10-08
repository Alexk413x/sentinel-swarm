# sentinel-swarm

A Claude Code plugin that runs an agent swarm to take a PRD to built, tested, reviewed
code. An Oracle plans the run, Managers own phases, Leads own modules, and Coders write
one file each. A Driver tests the app between waves when the host has cartographer and a
driver plugin. Enforced reviews, a shared ledger, and a scoring rubric hold every role
accountable, and every hand-off requires recorded evidence, not a claim.

## Install

```sh
claude plugin marketplace add Alexk413x/marketplace
claude plugin install codebase-kg@alexk413x --scope project
claude plugin install sentinel-swarm@alexk413x --scope project
```

Run the install commands from the repo the swarm builds in. sentinel-swarm depends on
[codebase-kg](https://github.com/Alexk413x/codebase-kg), and Claude Code refuses to load
sentinel-swarm in a project where codebase-kg is not installed. Every role queries the
code graph before it plans or writes, and each Coder updates the graph for its own file.

> **Pre-1.0.** A small run works from start to finish: the hello-world smoke test passes
> with every role as its own background session, and a stopped session resumes and
> finishes its work. The hook transport runs on function hooks, an early-access Claude
> Code API that changes between releases.

## Requirements

- Claude Code 2.1.294 or later: the CLI, the desktop app, or an IDE extension, on
  Windows or macOS. claude.ai chat and Cowork cannot run a swarm, because every role is a
  local background session with its own hooks.
- Python 3.9 or newer on `PATH` as `py`, `python3`, or `python`. The ledger imports only
  the standard library, so it needs no venv. On a Python older than codebase-kg's floor
  (3.10), graph writes run codebase-kg through `uv`, so install `uv` too.
- git.
- A trusted host folder: run `claude` in it once and accept the trust prompt.
  `claude --bg` refuses an untrusted folder.
- Optional: cartographer and a driver plugin (android-driver, ios-driver, or
  web-driver) for the Driver. Without them the run finishes on unit tests alone.

## Run a swarm

1. Run `/sentinel-swarm:setup` in the host repo. It writes the role files to
   `.claude/agents/swarm-<role>.md`, the hook shim to `.sentinel-swarm/hook.py`, the
   settings file, and the git excludes, and it builds the code graph when there is none.
2. Run `/sentinel-swarm:run` with the PRD. It starts the repo's ledger server and the
   Oracle as a background session.
3. Watch the run in agent view: every role is its own session. `/sentinel-swarm:status`
   reads the run's state, and `/sentinel-swarm:resume` picks up a stopped run.

The run and resume skills only run when you ask for them. The swarm works in your current
working tree and commits nothing.

## Roles

| Role | Color | Default model | Created by |
|---|---|---|---|
| Oracle | cyan | opus | The user |
| Manager | green | opus | Oracle |
| Lead | purple | sonnet | Manager |
| Coder | orange | sonnet | Lead |
| Driver | yellow | sonnet | Oracle |

Each parent-child pair follows the same cycle: brief, work, self-review, hand up,
review, then approve, return, or accept as incomplete. Only the Coder writes project
files. A parent starts its child with the ledger tool `agent_spawn`; no role has the
`Agent` tool except the Driver, which may run only cartographer's agents.

## What it runs, reads and writes

- **Runs:** `claude --bg` sessions for every role; one ledger server per host repo, on a
  loopback port with a bearer token; the host's test, build, and lint commands; `git`; and
  codebase-kg's tools.
- **Reads:** the host repo, `.claude/sentinel-swarm.local.md`, and Claude Code's plugin
  registry, to find the installed plugins.
- **Writes:** the role files in `.claude/agents/`, the run's records in `.sentinel-swarm/`
  (the ledger database, the server's port and token, and the report), and the project
  files the Coders write. It shows desktop notifications unless `notify` turns them off.

## Develop

The ledger is a Python project under `mcp/`. Run every check before you commit:

```sh
cd mcp
uv sync
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
cd ..
claude plugin test .
claude plugin validate --strict .claude-plugin/plugin.json
```

`scripts/smoke.sh` runs a hello-world swarm in a scratch host repo under `runs/hello/`.
`knowledge/prd/README.md` indexes the specification, one document per subject; the code
must match it. Run `sh .githooks/install.sh` once per clone.

## Licence

Free to use, including at work, and free to fork and share. You may not sell it, a fork
of it, or paid setup or hosting of it, and you may not use it for fraud or scams. See
[LICENSE](LICENSE) for the full terms.
