# hooks

None of the hooks below are implemented. `hooks.json` registers no hooks yet. This
file records the plan from `plans/04-mcp-and-hooks.md`, so the eventual
implementation has one source to build from.

## Planned hooks

| # | Event | Matcher | What it does | Blocks |
|---|---|---|---|---|
| 1 | `SessionStart` | all | Registers a session-runtime agent. Reports an unfinished run. Checks that codebase-kg is installed, that a graph exists, and that the records folder is excluded from git | No |
| 2 | `PreToolUse` | `Agent` | Spawn gate. The caller's role may create this child role. The model is on the child role's approved list. A brief exists for the child. The parallelism cap has room. It records the parent for the registry | Yes |
| 3 | `SubagentStart` | the four roles | Writes the registry row: id, role, parent, model, phase at start, start time | No |
| 4 | `PreToolUse` | `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | Role gate: only a Coder writes. Ownership gate: the path is claimed by this agent, as its file or its test file. Protected paths: nobody hand-writes the records folder | Yes |
| 5 | `PreToolUse` | `Bash`, `PowerShell` | Shell gate for Coders: allows the commands in the project profile, such as test, build, lint, and read-only git. Denies the rest | Yes |
| 6 | `PreToolUse` | `mcp__.*swarm-ledger.*` | Stamps the caller's real identity onto the call. Checks that the role may use the tool. `override_grant` is for the Oracle only | Yes |
| 7 | `PostToolUse` | all | Heartbeat and current activity to the registry. After a Coder's edit, marks the file's test evidence and graph check as stale, so that a handoff needs fresh ones | No |
| 8 | `PostToolUse` | `Bash`, `PowerShell` | Compares the changed files with the caller's claims. A change outside the claim is recorded as a violation and reported to the Lead | No |
| 9 | `PreCompact` | all | Adds one to the agent's context overflow count | No |
| 10 | `SubagentStop` | the four roles | Closes the registry row: end time, tokens and context percentage from the transcript, phase at end. Flags an agent that stopped without a handoff, so that its parent hears about it | To verify |
| 11 | `Stop` | Oracle session | Keeps the Oracle going while the run has unlocked work or pending reviews, unless it waits on the user | Yes |
| 12 | `SessionEnd` | all | Closes the registry row of a session-runtime agent | No |

Source: `plans/04-mcp-and-hooks.md` "Hooks". Items marked **(proposed)** in that
document have not been reviewed by Alex; this table copies the plan as written and
does not resolve that status.
