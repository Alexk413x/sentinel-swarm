# Plan: deliver wake-ups through a channel when the session supports one

Status: built on 2026-09-27, steps 1 to 4. Step 5, the live run, is still open. The PRD
describes the built behavior: see "Wake-up delivery" in `knowledge/prd/05-sessions.md`.
Alex decided questions 1 and 2 on 2026-09-27: the default is `channel`, and it is built
now. The answers to questions 3 and 4 are **(proposed)** and listed in
`knowledge/prd/15-proposed.md`. The build differs from this plan in two places: the
Stop hook waits for confirmation instead of passing a young push, and `plugin.json`
has no `channels` entry (see "Channels" in `knowledge/prd/16-open-items.md`). The probe
results that shape it are in `plans/channels-and-http.md`.

## Goal

A wake-up reaches its target through a channel event when the target session supports
channels, and through `SendMessage` otherwise. One setting switches the behavior, and one
function makes the choice for every wake-up the swarm sends.

## How a wake-up works today

1. A ledger call that owes a wake-up, such as `handoff_submit` or `message_post`, records
   it with `_owe_wakeup` in `mcp/src/swarm_ledger/ledger.py`: a `wakeups` row with the
   sender, the target, the target's session name, the reason, and a pointer text.
2. `next_step(wakeup)` returns the call the sender must make: `SendMessage(to=<session
   name>, message=<pointer>)` when the target's session runs, or
   `agent_resume(target_name=<name>)` when it does not.
3. The sender makes that call. The post-tool hook, `handle_post_any` in
   `hooks/events.py`, marks the wake-up sent.
4. The Stop hook blocks the sender's turn while it owes an unsent wake-up.
5. The watchdog builds its own `SendMessage` suggestions (`_wake_call` in
   `watchdog.py`), and resumes a stopped Oracle with `sessions.resume`.

## The switch

A settings key in `.claude/sentinel-swarm.local.md`:

```yaml
wake_transport: sendmessage   # sendmessage | channel
```

- `sendmessage`, the default, keeps today's behavior exactly.
- `channel` delivers through a channel to every session that supports one, and falls
  back to `SendMessage` for any session that does not, or when a push is not confirmed.

## The one function

`route_wakeup(wakeup) -> Delivery`, in a new module `mcp/src/swarm_ledger/wake.py`,
receives everything both paths need: the wake-up row, the target's session id and
session name, the pointer, and the setting. It returns one of:

- `Delivery(pushed=True)`: the ledger sent the event through the target's channel. The
  sender has nothing to do.
- `Delivery(next="SendMessage(...)")`: today's instruction, returned to the sender.
- `Delivery(next="agent_resume(...)")`: the target's session is not running.

Every place that builds a wake instruction today calls it instead: `next_step`, the
watchdog's `_wake_call`, and `_wake_hint` in `hooks/events.py`. The watchdog's
Oracle resume stays as it is, because a channel reaches only a running session.

The decision:

1. The setting is `sendmessage`: return today's instruction.
2. The target's session is not running: return `agent_resume`.
3. The target's session supports channels (below): push the event and return `pushed`.
4. Otherwise: return the `SendMessage` instruction.

## Which sessions support channels

A session supports channels when both hold:

- **It was launched with the channel.** The launcher records it on the agent row, as a
  new column `channel` with `none`, `launched`, or `confirmed`. A launch can carry the
  channel only when Claude Code will register it: an interactive session with
  `--dangerously-load-development-channels server:swarm-events`, or any session with
  `--channels` for a plugin on the allowlist. Claude Code 2.1.283 discards the
  development flag in a `--bg` session, so today no role qualifies. An Oracle started
  in the user's own terminal does.
- **Its bridge is connected.** The bridge (below) registers with the ledger when it
  starts, and the ledger drops the registration when the bridge's connection closes.

The first push that is confirmed moves the row from `launched` to `confirmed`.

## The bridge: `swarm-events`

- A small stdio MCP server, one per session, run through the shim:
  `.sentinel-swarm/hook.py channel`. It declares
  `capabilities.experimental["claude/channel"]` and has no tools.
- On start it reads `CLAUDE_CODE_SESSION_ID`, opens a long-lived HTTP request to the
  ledger server, such as `GET /events?session=<id>`, and reconnects when that drops.
- For each event the ledger sends, it writes one `notifications/claude/channel` with
  `content` = the pointer and `meta` = `{"wakeup_id": ..., "reason": ...}`. Meta keys
  are identifiers only.
- Setup adds it to every role's MCP config. The launch passes it as `server:swarm-events`
  because roles run with `--strict-mcp-config`, which the `plugin:` form does not
  reach. When `CLAUDE_DEV_CHANNELS` is also set, both lists merge into one flag.
- `plugin.json` gains a `channels` entry bound to the server, so the plugin is ready
  for an allowlist listing.

## Confirmation and fallback

- A push is confirmed when the target's transcript records the event: a user turn whose
  origin is `channel` and whose text carries the wake-up's pointer. The ledger already
  reads transcripts for cost, so confirmation costs no model turn.
- A pushed wake-up records `pushed_at`, and `sent_at` once confirmed.
- The Stop hook treats a pushed wake-up as settled while it is younger than 30 seconds
  or confirmed. An unconfirmed push older than that blocks the sender again with the
  `SendMessage` instruction, so a lost event never strands a target.
- The watchdog reports a pushed wake-up that stays unconfirmed, as it reports a stuck
  agent today.

## Tests

- `route_wakeup` for each branch: the setting off, a stopped target, a supported target,
  an unsupported target.
- The bridge: it declares the capability, turns one ledger event into one notification,
  and reconnects after the ledger drops the connection.
- The Stop hook: a fresh push passes, an old unconfirmed push blocks, a confirmed push
  passes.
- Confirmation from a sample transcript line.
- A live probe: an interactive Oracle with `wake_transport: channel` receives a Lead's
  wake-up as a channel event, and the transcript confirms it.

## Build order

1. `wake.py` with `route_wakeup`, used by every caller, with only the `sendmessage`
   branch live. No behavior changes; the tests prove the single switch point.
2. The bridge, the `/events` route, and the `channel` column.
3. The `channel` branch, confirmation, and the Stop hook change.
4. The launch changes, the `channels` manifest entry, and the PRD updates.
5. A live run with the setting on and an interactive Oracle.

## Questions for Alex

1. Default: keep `sendmessage` as the default until a live run proves the channel path?
2. Roles stay `--bg`, so only an Oracle in your own terminal can use the channel today.
   Build it now for the Oracle and for a future allowlist listing, or wait?
3. Should the Oracle's `Monitor` listener also move to the channel when the Oracle
   supports one, or stay as it is?
4. The device-queue broker: a separate channel server, or a second event kind on
   `swarm-events`?
