# Other platforms through ide-agent-tabs

Status: exploration, not a build plan. Nothing here is built. The PRD documents the Claude
Code design this plan starts from; the decision to start with a non-Claude Coder is in
`knowledge/prd/14-key-decisions.md` (2026-10-07).

## Goal

Decide whether sentinel can run some roles on agent CLIs other than Claude Code, through
ide-agent-tabs, such as Codex, Copilot CLI and Antigravity CLI, and what each guarantee costs
on the way.

## What ide-agent-tabs offers

- Agent profiles (`mcp/src/profiles.ts`): Claude Code, Codex, Codex (local), Antigravity CLI,
  Copilot CLI, Grok Build, Pi, Hermes, OpenCode, Qwen Code and Goose.
- Hook registration for Codex, Copilot, Antigravity, Grok, Hermes, Qwen and Goose
  (`hookConfig.ts`). The events differ per CLI (`messaging/hook.ts`, `HOOK_EVENTS`): Copilot has
  `preToolUse`, Grok and Qwen `PreToolUse`, Codex only `UserPromptSubmit`, `PermissionRequest`,
  `PostToolUse`, `Stop` and `Interrupt`.
- A machine-wide SQLite mailbox that every CLI's MCP server reads and writes (its PR #12),
  presence per session, a Stop-hook nudge to read mail before stopping, and a wake that types a
  line into the agent's tab. It wakes a session only in a tab it hosts, so it has nothing for a
  `--bg` session.
- Tab tools to open, resume and close an agent session in an IDE or terminal.
- A Claude Code mod that delivers mail inside a session it hosts: it polls its server every 2 s,
  claims unread mail with `take`, submits it with `$.prompt.submit`, then sends `ack`, or
  `release` on failure (`claude-plugin/hooks/register.tsx`, `docs/design.md`, "Inbound mail").
  It limits delivery to sessions in its own tabs (`IDE_AGENT_TABS_ID`), which is its own choice,
  not a platform limit.
- A verified path from a `--bg --strict-mcp-config` session to an HTTP MCP server
  (`docs/design.md`).

## Sorting sentinel's guarantees

**Enforced in the ledger, so platform-independent.** Every ledger gate holds for any MCP client:
`brief_ack` binding, `agent_spawn` needing a brief, the model list in `brief_create`,
`score_record` completeness and order, `handoff_submit` needing passing tests and a current
graph, `approve`, change requests, departures, deferrals, overrides, `issue_escalate`,
`phase_update`, `run_finish`, the `drive_*` gates, `message_post`'s recipient relation, the
role tool set (`identity.ROLE_TOOLS`, enforced in `pool.run_tool`), and `message_inbox`'s
exactly-once read and size caps.

**Two ledger gates lean on the mod today.**

- Identity. The mod's `pre_ledger` hook stamps `agent_id` through `updatedInput`. Without it, an
  agent can pass another agent's id. The ledger's bearer token is one per host repo
  (`http-token`), so it does not tell agents apart. Replacement: a token per agent, issued at
  spawn and sent as that session's bearer header, which the ledger maps to the `agent_id`. It
  also works for Claude roles.
- The mod check-in. `identity.require_mod_session` refuses `run_start` and `brief_ack` from a
  session with no `mod_sessions` row. A non-Claude session has no mod, so `brief_ack` would
  refuse it. The gate needs a replacement for such a session, such as the per-agent token.

**Dependent on the mod's hooks or agent frontmatter.**

| Guarantee | Claude Code mechanism | Candidate replacement for another CLI |
|---|---|---|
| Only the Coder writes, and only its own files | `pre_write` | The CLI's own pre-tool hook where it can deny (Copilot, Grok, Qwen; unverified); else the CLI's sandbox or writable-roots setting; plus a ledger check of `git status` against claims at `handoff_submit` (detects, does not prevent) |
| Shell limits | `pre_shell`, `post_shell` | The CLI's approval or sandbox mode; the same handoff-time diff check |
| No subagents | `pre_agent`, no `Agent` in `tools` | The CLI's config, if it has one; else accept and detect |
| Role tool allowlist | Frontmatter `tools` | MCP-only: `pool.run_tool` refuses out-of-role tools; give the CLI only the ledger and codebase-kg servers |
| Only run members get messages | `pre_send_message` | No `SendMessage` exists there. Route all messages through `message_post` or the ide-agent-tabs mailbox. |
| Start calls and the brief re-read | `session_start` hook calls `brief_ack`, `ledger_info`, `brief_get`, `guidelines_get` | The Coder makes the calls itself; the ledger gates still refuse any other call before `brief_ack` |
| Wake-up debt is paid | Mod `owed` and `wake_sent`, member Stop hook | The ledger wakes a non-Claude target itself through ide-agent-tabs; the watchdog reports a debt that stays unpaid |
| Inbox read inside a wake-up | Mod `session.receive` hook | The Coder calls `message_inbox` when woken |
| Read mail before stopping | Member Stop hook | ide-agent-tabs' Stop nudge already covers these CLIs |
| Coder does not stop without a handoff | Stop hook | The CLI's stop hook where it can block (ide-agent-tabs' `stopOutput`); else the watchdog |
| Heartbeat, activity, idle state | `post_activity`, Stop | ide-agent-tabs presence, which its hooks update for each CLI |
| Token and cost accounting | Transcript parsing, `PreCompact` | Per-CLI usage output, or "unknown" in the report |
| Session start, liveness, resume, stop | `claude --bg`, `claude agents --json`, `--resume`, `claude stop` | `open_tab`, presence PID, `resume_tab`, `close_tab`; these need an IDE or terminal host |
| Watchdog listener, `PushNotification` | `Monitor`, Oracle Stop hook | Keep the Oracle on Claude Code |

## Starting shape to test

Keep the Oracle, Managers and Leads on Claude Code. Allow a non-Claude CLI only as a Coder, in a
tab, with only the ledger and codebase-kg servers, and a handoff-time diff check in the ledger. A
Coder is the role with the narrowest tools, and every one of its results passes a Lead review.

Alex accepted this shape as the starting point on 2026-10-07, with one condition the shape must
meet before anything else: a non-Claude Coder communicates and works with its Lead, Manager and
Oracle as a Claude Coder does. Concretely, it must:

- receive and acknowledge its brief, and read its inbox, through the ledger;
- answer a Lead's question and ask one, and receive a Manager's or the Oracle's directive;
- submit its handoff and take a returned review back to work;
- be woken when a message arrives while it is idle.

Build in small, separately tested stages, in that order, and stop at the first stage a candidate
CLI cannot pass. Measure each stage with one Coder on the `hello` smoke run before any
multi-agent run. When a stage ships, update the matching `knowledge/prd/` document in the same
change.

## Open questions

1. Which CLIs' pre-tool hooks can deny a call, and do they see the file path for each write tool?
2. Can every candidate CLI send a static bearer header to an HTTP MCP server, or does each need a
   stdio relay?
3. Can ide-agent-tabs start an agent with no IDE or terminal host, as `claude --bg` does? If not,
   a non-Claude role needs an open IDE.
4. Should the ledger call ide-agent-tabs to wake a non-Claude target, or write to its mailbox
   directly? Direct writes would tie the ledger to that plugin's schema.
5. Does the per-agent token replace `pre_ledger` stamping for Claude roles too, so one identity
   path serves every platform?
6. How does the report count cost for a CLI that prints no usage?
7. Is a handoff-time diff check strong enough for a Coder whose writes no hook can stop?
8. What replaces the mod check-in for a session with no mod?
