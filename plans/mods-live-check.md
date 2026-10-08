# Mods in role sessions: live check (step 0)

Step 0 of `plans/messaging-and-tooling.md`, run on 2026-10-08 on Windows 11 (marbles-xps), Claude
Code 2.1.294. The model was `claude-haiku-5-5` (`--model haiku`). Not yet copied into
`13-platform-facts.md`.

## Method

- A throwaway plugin, `mod-probe`, in `%TEMP%\claude\mod-probe\`: `plugin.json`, `hooks/hooks.json`
  naming `./register.ts`, and the module. `claude plugin validate` passed it.
- The module wrote one JSON file per event to `%TEMP%\claude\mod-probe-log\ev\`, with the session id,
  the cached `agent_type` and the time. It hooked `session.start`, `classic.SessionStart` (adds
  context), `turn.start`, `classic.Stop` (blocks the first stop), `tool.call` on `Write` (denies
  `deny-me`, throws on `throw-me` with a `.catch` deny), `classic.PreToolUse` (adds `agent_id` through
  `updatedInput`), `tool.call` on an MCP tool (adds `agent_id` through `next`), `session.send` and
  `session.receive`.
- A 2 s `$.clock.every` timer read `control.json` and ran commands addressed to its session:
  `$.session.send`, `$.prompt.submit`, `$.http.fetch`, and the timing bench. A 30 s timer logged ticks.
- The host was a scratch git repo, `%TEMP%\claude\mod-probe-host\`, trusted by a `hasTrustDialogAccepted`
  entry in `~/.claude.json`. Its `.claude/agents/swarm-coder.md` had `model: haiku`,
  `permissionMode: acceptEdits` and `tools: Read, Write, SendMessage, mcp__probe`.
- A loopback stub HTTP server on `127.0.0.1:47613` answered `/health` and `/hook/pre_write`. A stdio
  stub MCP server, `probe`, had three echo tools: `echo_open` (schema lists only `text`),
  `echo_strict` (same, with `additionalProperties: false`) and `echo_rewrite`.
- Sessions started from a hidden console with the options `agent_spawn` uses:
  `claude "<prompt>" --bg --name <n> --agent swarm-coder --model haiku --permission-mode acceptEdits
  --strict-mcp-config --mcp-config mcp.json --allowedTools "Read,Write,SendMessage,mcp__probe"`.

| Session | How it started | Mod loaded from |
|---|---|---|
| probe-b `db715681` | `--bg`, idle target | `--plugin-dir` |
| probe-a `34a088be` | `--bg`, ran the gate steps, sender | `--plugin-dir` |
| probe-c-nomod `1dac060f` | `--bg`, memory baseline | none |
| probe-d-project `407967a3` | `--bg` | project-scope install (`claude plugin install --scope project` from a directory marketplace) |
| probe-e-interactive `b1855c99` | interactive, in a hidden console | project-scope install |
| `7a605078` | `claude -p` in an untrusted folder | `--plugin-dir` |
| probe-untrusted | `--bg` in an untrusted folder | refused before start |

## Results

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Module loads in a `--bg --agent swarm-coder --strict-mcp-config` session in a trusted folder | PASS | `session.start` and `classic.SessionStart` logged for probe-b, probe-a (`--plugin-dir`) and probe-d (project-scope install). No such events for probe-c-nomod. |
| 1 | Does not load in an untrusted folder | PASS for `--bg`, with a caveat | `--bg` refuses to start: "Workspace not trusted. Run `claude` in ...mod-probe-untrusted once". Caveat: `claude -p --plugin-dir` in the same untrusted folder ran the mod (session.start, Stop block; the reply was "continued"). |
| 2 | `session.start` fires; `classic.SessionStart` carries `agent_type: swarm-coder` | PASS | `"agent_type":"swarm-coder"` in every role session's SessionStart input, project-scope included. `session.start` input in a `--bg` session: `{"surface":"terminal","isInteractive":true}`. |
| 3 | `tool.call` `{ deny }` stops a `Write` | PASS | Tool result: `PROBE-DENY: this unit refuses deny-me files.` |
| 3 | A `.catch` deny stops a `Write` when the hook throws | PASS | Tool result: `PROBE-CATCH-DENY: the guard failed.` |
| 3 | `classic.Stop` `block` re-prompts | PASS | Transcript: user row `Stop hook feedback: PROBE-STOP-BLOCK: ...`, then assistant `continued`. Second Stop input has `stop_hook_active: true`. Passed in probe-a, probe-b, probe-d, probe-e and `-p`. |
| 3 | `classic.PreToolUse` `updatedInput` adds `agent_id` to an MCP tool whose schema does not list it | PASS | `echo_open` received `{"agent_id": "probe-agent-123", "text": "a"}`. `echo_strict` (`additionalProperties: false`) received the same: the engine did not reject the extra key. |
| 3 | `tool.call` `next({ ...e, agent_id })` does the same | PASS | `echo_rewrite` received `{"agent_id": "probe-agent-456", "text": "c"}`. |
| 3 | `additionalContext` from `classic.SessionStart` | PASS | Transcript attachment `hook_additional_context`: `PROBE-CONTEXT: the probe word is heliotrope.`; the model answered `heliotrope`. |
| 4 | `$.http.fetch` reaches a loopback server | PASS | `GET http://127.0.0.1:47613/health` from probe-a: `status 200`, 5.5 ms. Server log user agent: `Bun/1.4.3`. The real ledger was not running; a stub stood in. |
| 5 | `$.session.send({ to: { sessionId } })` from a `--bg` session wakes an idle `--bg` session | PASS | Result `{"isDelivered":true}` after 2.9 s. probe-b logged `session.receive` at 16:19:55.016, `turn.start` at 16:20:04.8, and replied `pong`. Its transcript holds the text inside `<cross-session-message ... from-name="probe-a" from-plugin="mod-probe">`. |
| 5 | Same, to an idle interactive session by session id | FAIL (as tested) | `{"isDelivered":false,"reason":"no live session on this machine has id b1855c99-..."}`. probe-e had no `~/.claude/sessions/<pid>.json` (only a `.key` file) and was missing from `claude agents --json`. |
| 5 | Same, to that interactive session by name (`to: "probe-e-interactive"`) | PASS | `isDelivered: true` after 3.6 s. probe-e logged `session.receive` (sender `bridge:session_01WD...`), took a turn, and its Stop input shows `last_assistant_message: "pong"`. |
| 6 | `$.clock.every` keeps firing in an idle `--bg` session for 10 minutes | PASS | probe-b went idle at 16:20:09. Its 30 s ticks ran to the 40-tick cap at 16:34:19 (14 min idle). Its 2 s control timer still ran at 16:38. |
| 6 | `$.prompt.submit` from that timer starts a turn | PASS | At 16:38, 18 min idle: submit resolved in 9.9 s, `turn.start` logged, the reply was `awake`. Also passed in probe-a (6.4 s). |
| 7 | Time and memory per hook | PASS, with limits | See "Timings". |

## Timings (check 7)

50 calls each. The machine ran several other Claude sessions throughout.

| Path | Median | p90 | Min / max | Memory |
|---|---|---|---|---|
| Mod: `$.http.fetch` POST `/hook/pre_write` to the loopback stub | 6.3 ms | 26.1 ms | 3.6 / 64.0 ms | No separate process. The hooks worker runs inside the session's `claude.exe` (no child process). |
| `python .sentinel-swarm\hook.py hook pre_write` (the real shim, fast path to the same stub) | 383 ms | 488 ms | 274 / 1098 ms | 18.8 MB peak working set per call |
| `bash -c "python3 ... \|\| python ..."` (the template's command form) | 766 ms | 1286 ms | 507 / 1857 ms | bash's own peak 7.5 MB, plus the python child |

- Session memory, idle, after one turn: 397 MB and 406 MB working set with the mod, 405 MB without
  it. The difference is inside the noise; the mod's memory does not show.
- The plan's figure for the shim was 235 ms median. This run measured 383 ms on a busier machine.
- I could not isolate the engine's dispatch cost into the hook. `$.tool.call({ tool: "Read" })` from
  the timer took 1.8 s median without the hook's fetch and 1.5 s with it. That path also runs this
  machine's user-scope command hooks on every call (the transcript shows `PostToolUse` command
  hooks), so it measures them, not the mod.
- A mod hook that posts to the ledger costs the server's own time plus about 6 ms. The shim costs
  the server's time plus about 380 to 770 ms.

## What this means for the plan

| Step | Needs | Verdict |
|---|---|---|
| 2. Mod as the hook transport | Checks 1 to 4, 7 | GO. Deny, `.catch` deny, Stop block, `additionalContext`, `updatedInput` and loopback fetch all work in a `--bg --agent --strict-mcp-config` session, from a project-scope install. |
| 3. Sender's mod pays owed wake-ups with `$.session.send` | Check 5 | GO for `--bg` targets: every member role. NO-GO by session id for an interactive Oracle until a retest in a normal terminal passes; keep `SendMessage` (or send by name) as the Oracle path until then. |
| 4, recipient form (`session.receive`) | Check 5 | GO. `session.receive` fires in the target before the turn, with the full text and `origin.plugin`. Rewriting `text` was not tested. |
| 4, timer form (`$.prompt.submit`) | Check 6 | GO. Timers survive 18 minutes idle in `--bg`, and a submit from a timer starts a turn. |

## Surprises

- The plugin's own `session.send` hook never fired for its own `$.session.send` calls (0 events).
  Step 3 item 4 assumes the hook sees "the model's and its own" sends. The mod must run the relation
  check in code before it calls `$.session.send`.
- `additionalProperties: false` did not stop an added `agent_id`. The engine does not enforce that
  keyword on a rewritten MCP tool input. The ledger tools receive the key either way.
- `claude -p --plugin-dir` loads and runs the mod in an untrusted folder. Role sessions are `--bg`,
  which refuses an untrusted folder, so this does not reach them. A project-scope install in an
  untrusted folder was not tested.
- The interactive session in a hidden console never wrote a transcript file or a
  `~/.claude/sessions/<pid>.json`, though its hooks reported a `transcript_path`. Name delivery went
  through a `bridge:session_...` address. I cannot tell whether a hidden console causes this. Retest
  the Oracle case in a visible terminal before building step 3's Oracle path.
- `$.session.send` resolved in 2.9 to 3.6 s, and `$.prompt.submit` in 6 to 10 s. Neither belongs on
  a hook's critical path; run them after `next(e)` or from a timer.
- In a `--bg` session, `session.start` reports `isInteractive: true` and `surface: "terminal"`. In
  `-p` and interactive sessions, `classic.SessionStart` fired before `session.start`.
- Unrelated to mods: a `--bg` session's `Write` into the shared checkout fails with "This background
  session hasn't isolated its changes yet. Call EnterWorktree first ...". The message names
  `"worktree": {"bgIsolation": "none"}` in `.claude/settings.json` as the opt-out. Check how the Coder
  role handles this on 2.1.294.

## Not tested

- The real swarm-ledger server: `/health` and `/hook/pre_write` were a stub.
- A `session.receive` rewrite of `text`, and `session.send` refusal from a hook.
- `$.session.send` to an interactive session started in a visible terminal.
- A project-scope install in an untrusted folder.

## Spend and cleanup

- Model usage across the five saved transcripts: 40,246 cache-write, 174,534 cache-read, 50 input and
  2,529 output tokens. That is about $0.08 at Haiku 4.5 list rates. The interactive session saved no
  transcript; its three short turns add a few cents.
- I ran `claude stop` on the four `--bg` sessions and ended the interactive session's process. No
  session was removed.
- I uninstalled the project-scope plugin, removed the `mod-probe-mkt` marketplace, and removed the
  host's trust entry from `~/.claude.json`. Claude Code keeps a `mod-probe@inline` usage counter in
  `~/.claude.json`.
- The scratch files stay in `%TEMP%\claude\mod-probe*` for review.
