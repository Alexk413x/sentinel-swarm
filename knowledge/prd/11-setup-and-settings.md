# Setup, launch, and settings

## Install

1. Install sentinel-swarm at project scope in the host repo. `--plugin-dir` is not
   enough, because the launcher does not resolve its agents. codebase-kg must be
   installed too.
2. Trust the host folder once: run `claude` in it and accept the trust prompt.
3. Run setup, then the launcher.

## Setup

`python -m swarm_ledger.setup [--repo <root>] [--check-trust]`, or the
`/sentinel-swarm:setup` skill, which also creates the settings file, fills in the
commands from the detected stack, confirms codebase-kg, and builds the graph. Setup:

- Writes `.claude/agents/swarm-oracle.md`, `swarm-manager.md`, `swarm-lead.md`, and
  `swarm-coder.md` from `templates/agents/<role>.md`. For an existing file it keeps the
  user's frontmatter, adds any key the template has that the file lacks, adds every
  ledger hook the template has that the file lacks, and replaces the prompt body.
- Writes the shim to `.sentinel-swarm/hook.py`, overwriting local edits.
- Merges `{"worktree": {"bgIsolation": "none"}}` into `.claude/settings.local.json`.
- Adds `.sentinel-swarm/` and `.claude/agents/swarm-*.md` to `.git/info/exclude`.
- Reports whether the repo is trusted, from `~/.claude.json`, and prints the command
  that trusts it. `--check-trust` only checks.

The role files take effect in the next session. A session loads agent files once, at
startup.

## Launch

`python -m swarm_ledger.launch [--repo <root>] [--bg | --headless [--transcript <file>]] "<prompt>"`,
or the `/sentinel-swarm:run` skill.

- It runs setup when the shim or a role file is missing.
- `--bg` refuses an untrusted repo and prints the trust command.
- It starts the ledger server, then the Oracle with its agent file's model and the same
  flags as `agent_spawn`.
- Interactive by default. `--bg` starts a background session. `--headless` runs
  `claude -p` with a stream-json transcript and the prompt on stdin. Nobody answers
  questions in a headless run, so the Oracle records each assumption in the guidelines.

## Skills

| Skill | Purpose |
|---|---|
| `swarm-protocol` | Shared vocabulary, names, tool order, and rubric keys; loads when the swarm is mentioned |
| `run` | Starts a run with the launcher |
| `plan` | The Oracle's conventions for a phase graph |
| `status` | Reads `ledger.db` read-only and prints the run's state |
| `resume` | Continues a paused or crashed run, one session at a time |
| `setup` | Prepares a host repo |

## Settings file

`.claude/sentinel-swarm.local.md` in the host repo, YAML frontmatter. The template is
`templates/sentinel-swarm.local.md.example`. The file is not committed.

| Key | Default | Meaning |
|---|---|---|
| `tracking` | `local` | The only value |
| `runtime.<role>` | `session` | Recorded on each agent row; every role runs as a session |
| `models.<role>` | See "Models" in [01-roles.md](01-roles.md) | Approved models per role |
| `rubric.*` | See "Thresholds" in [03-scoring-and-review.md](03-scoring-and-review.md) | `target`, `floor`, `criterion_floor`, `disagreement_gap`, `plateau`, `regression_tolerance` |
| `escalation.rounds` | 3 | Escalation rounds |
| `escalation.attempts_per_round` | 3 | Non-improving attempts per round |
| `watchdog.*` | See "Watchdog" in [08-watchdog.md](08-watchdog.md) | Watchdog timing, thresholds, and server idle exit |
| `test_command` | empty | Must contain `{target}`, for example `python -m pytest -q -p no:cacheprovider {target}` |
| `build_command`, `lint_command` | empty | Optional; the Coder's shell gate allows them |
| `parallelism_cap` | empty | No limit when empty |
| `base_branch` | empty | `repo_check` tries `main`, then `master` |

`-p no:cacheprovider` keeps parallel pytest runs from contending on `.pytest_cache`.
