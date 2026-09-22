# sentinel-swarm: ledger, agent registry, and lifecycle gates

Status on 2026-09-20: first draft. Bullets marked **(proposed)** are Claude's additions that Alex has not reviewed. Everything else comes from Alex.

## Decisions from Alex

- Every role records everything through one ledger MCP tool. Different tables in its database hold the different elements.
- Hooks call the ledger before each stage of an agent's lifecycle. They ensure that the agent completed its work correctly and got its sign-off.
- One table is the agent registry. It shows each agent's name, role, what it is doing, and all its settings. It tracks agents as they start and shut down, and which phase each one is in when it starts and when it ends.
- The registry tracks token usage for each agent individually, where the platform allows it.
- The plugin has settings for each role: the runtime (subagent or session) and the model.
- No role depends on how it was started. Every brief, review, change request, and score goes through the ledger in both runtimes.

## Tracking and storage

- The first version tracks work in the local ledger only.
- Tracking is a setting. `local` is its only value for now. Trackers such as GitHub issues and Jira can become values later.
- The ledger and its record files are tracking data. The host repo's own work, its code and tests, does not need them, so they are not committed to the host repo. Git ignores them.
- The plugin ignores them through `.git/info/exclude`, not through the host's `.gitignore`. The exclude file is local and uncommitted, so the plugin leaves no trace in the host repo, and it applies to every worktree of the repo. **(proposed)**
- The records live in one folder at the root of the main checkout, outside any worktree, so that every session and subagent reads the same ledger. **(proposed)**
- The final report lives in the same folder and is ignored too. The user copies it out when they want to keep it. **(proposed)**
- The folder is `.sentinel-swarm/` at the root of the main checkout, and the ledger is `.sentinel-swarm/ledger.db`. A worktree's `.git` file points to the main checkout, and the ledger resolves it. **(proposed)**
- Every connection opens with WAL, `synchronous=NORMAL`, a 5 second busy timeout, and foreign keys on. Writes run inside `BEGIN IMMEDIATE`. See check 5 in `04-mcp-and-hooks.md`. **(proposed)**
- Consequence: a run can resume on the same machine only. A fresh clone has no records. A later setting can allow committed records. **(proposed)**

## Per-role settings

| Role | `runtime` **(proposed default)** | `models` (approved list, Alex's defaults) |
|---|---|---|
| Oracle | session | fable, opus |
| Manager | subagent | opus |
| Lead | subagent | opus, sonnet |
| Coder | subagent | sonnet, haiku |

The first version works in one working tree, so only the Oracle runs as a session. See `01-roles.md` "Git workflow in the host repo".

The parent that spawns an agent picks the model from the child role's list, by the complexity of the task. The brief records the choice. The Oracle has no parent and runs on the first model in its list. **(Oracle rule proposed)**

No role has a time limit or a turn limit. The agent files do not set `maxTurns`. The watchdog, not a limit, catches an agent that is stuck or spinning.

Other candidates for per-role settings: effort, cache lifetime, and parallelism cap. **(proposed)**

## Agent registry table **(proposed columns)**

| Column | Source |
|---|---|
| `agent_id` | `agent_id` from hook input for a subagent. `session_id` for a session |
| `name`, `role` | The spawn call and `agent_type` from hook input |
| `runtime`, `model`, `effort`, settings snapshot | The plugin settings at spawn time |
| `parent_agent_id` | The spawning agent |
| `run_id`, `phase_id`, `module_id`, `file_id` | The brief |
| `state`, `current_activity` | Ledger calls and heartbeats |
| `phase_at_start`, `phase_at_end` | Ledger state at start and at end |
| `started_at`, `ended_at`, `end_reason` | `SubagentStart` and `SubagentStop` hooks, or `SessionStart` and `SessionEnd` |
| `last_heartbeat_at` | A `PostToolUse` hook |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Summed from the agent's transcript at stop |
| `context_pct_peak`, `context_pct_at_end` | The input size of the agent's latest request, divided by its model's context window. Computed from the transcript |
| `context_overflow_count` | The number of times the agent filled its context and compacted. Source to verify: a `PreCompact` hook, or compaction markers in the transcript |
| `tool_uses`, `duration_ms` | The transcript, or the completion notice |
| `transcript_path` | `agent_transcript_path` from `SubagentStop` |

## Run analytics

Alex wants this data kept so that later changes to the plugin can be measured: whether a run gets faster, more efficient, and more accurate.

Per agent, from Alex: start time, end time, tokens used, context percentage, and the number of times the agent overflowed its context.

Measures for each run, built from the ledger's tables: **(proposed)**

| Goal | Measures |
|---|---|
| Faster | Run duration, phase duration, time an agent spends blocked or waiting, time from hand-up to review |
| More efficient | Tokens for each role, file, and phase. Cache read share. Context overflows. Agents replaced. Estimated cost |
| More accurate | First-pass approval rate. Returns for each file. Issues that reach round 2 and round 3. Full-suite failures at join points. Score gaps between layers. Accepted and denied departures. Escalations to the user |

- Each run records the plugin version and a snapshot of its settings, so that two runs with different models, runtimes, or prompts can be compared. **(proposed)**
- Analytics rows are never deleted when a run finishes. **(proposed)**

## Other tables **(proposed)**

`runs`, `phases`, `modules`, `files` (ownership claims), `briefs`, `reviews`, `scores`, `issues` (attempts and escalation rounds), `change_requests`, `test_runs` (evidence), `guidelines`, `departures`, `agent_events` (every lifecycle transition, append-only).

## Lifecycle stages and gates **(proposed)**

| Stage | Gate: the ledger refuses the transition unless |
|---|---|
| Registered | A parent with authority created the agent, and a brief exists |
| Working | The agent acknowledged its brief, and its ownership claim is recorded |
| Self-reviewed | A passing test run and the agent's own scores are recorded. For a Coder, the code graph is current for its file |
| Handed up | Open issues and departures from the guidelines are listed |
| Approved | The parent's review and scores are recorded, and no issue is open |
| Released | The approval is recorded. Usage is written to the registry |

The gates live in the ledger's transition calls. A hook enforces that an agent cannot skip the calls.

## Platform facts

Read on 2026-09-20 from `code.claude.com/docs/en/hooks.md`, through a summarizing fetch.

- `SubagentStart` and `SubagentStop` hooks exist. Both receive `agent_id` and `agent_type`. `SubagentStop` also receives `agent_transcript_path` and `last_assistant_message`.
- Tool hooks that fire inside a subagent (`PreToolUse`, `PostToolUse`) receive `agent_id` and `agent_type`. For a plugin agent, `agent_type` is the plugin-scoped name. On the main thread, `agent_id` is absent, and `agent_type` is present only when the session uses `--agent`.
- Plugin hooks run inside subagents.
- No hook input field reports tokens, cost, model, or context size.
- Observed in this session: a subagent's completion notice to its parent carries total tokens, tool-use count, and duration.
- The fetch reported that `SubagentStop` cannot block a subagent from stopping. This conflicts with Claude's recollection of the hook. Verify with a prototype. The gate design above does not depend on it.

## To verify with a prototype

1. Whether `SubagentStop` can block.
2. The per-message `usage` fields in a subagent transcript, and whether nested subagents get their own transcripts.
3. Whether `SessionStart` and `SessionEnd` fire for agent view background sessions.
4. Whether a session started with `--agent` reports `agent_type` to every hook.
5. How to detect a context overflow in a subagent: whether `PreCompact` fires inside subagents, and what a compaction looks like in the transcript.
