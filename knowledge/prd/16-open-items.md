# Open items

- **CI.** Confirm CI passes on Ubuntu, Windows, and macOS once the GitHub billing block
  resets. Until then, the four local checks are the only checks.
- **macOS.** The code has POSIX branches, but no live run has happened on a Mac. Run one
  smoke test there.
- **Watchdog rescue of a dropped API call.** The idle-stall check is built to catch a
  run whose Oracle API call dropped mid-response. It has not caught one in a live run.
- **Start calls in the hook, live.** Built and unit-tested; no live run yet. A
  `textstats` smoke run should show each child's first turn reading its start calls
  from the `session_start` context, no child calling `brief_ack` itself, and each
  Coder loading `score_record` and `handoff_submit` once, before its first handoff.
- **PreCompact.** Whether `PreCompact` fires in a long session. It needs a session that
  fills its context.
- **Ledger server early exit.** Once, the server exited about a minute after it started,
  with no error in its log. It has not recurred.
- **Plugin relays in a live run.** No live run has confirmed that role sessions reach a
  plugin's shared server through the shim's stdio entry. A live run on a host with a11y
  0.8.2 or later should confirm that every role reaches the shared a11y servers through
  the a11y relay, and that no role starts a private a11y server while they answer. The
  plan is `plans/a11y-relay-smoke-test.md`.
- **The Driver, live.** Built and unit-tested, never run: no host here has cartographer and a driver plugin installed yet. The first live run needs an Android app host, and must confirm the shim starts cartographer's and the driver plugins' servers.
- **Driver notifications, live.** Built and unit-tested; no test shows a real
  notification. On Windows, a dry run loaded the WinRT toast types, parsed the toast
  XML, and created the notifier, but no toast has been seen on screen. The macOS and
  Linux commands have never run. A live Driver run should confirm the toast, and that
  the Oracle's `PushNotification` call clears its Stop hook block.
- **Channels.** Wake-up delivery through the `swarm-events` channel is built and
  unit-tested, and has never run live. Only an interactive Oracle started by the launcher
  carries the channel: Claude Code reads the development flag only in interactive
  sessions, and sentinel-swarm is not on the allowlist, so every `--bg` role keeps
  `SendMessage`. Still open:
  - A live run with `wake_transport: channel` and an interactive Oracle: a Lead's or
    Manager's wake-up must arrive as a channel event, and the ledger must confirm it
    from the Oracle's transcript. The transcript shape of a channel turn is not verified
    yet; `wake.transcript_confirms` accepts origin kind `channel`, a `turnOrigin` or
    `promptSource` of `channel`, or the `swarm-events` tag.
  - The plugin manifest's `channels` entry. The manifest shape is
    `"channels": [{"server": "swarm-events"}]`, and `server` must name a server in the
    plugin's own `mcpServers`. That server would then start in every session that has
    the plugin enabled, not only in swarm sessions, so it waits for Alex's decision. It
    matters only for an allowlist listing or a `plugin:` development channel.
  - The device-queue broker, as a second event kind on `swarm-events`. Roles stay `--bg`
    sessions, so it still needs another path to a waiting role.
  - An event pushed while the Oracle is busy. The channels docs say such events queue and
    arrive together on the next turn. No probe has checked it.
  - An interactive Oracle sent to the background with `/bg` or `←`. Per the agent-view
    docs, it resumes in a fresh process, and the supervisor stops a background process
    after about an hour idle unless it is pinned with `Ctrl+T`. Either would restart or
    end the channel bridge. Not probed.
  - Retest the `--bg` case when the development flag works in `--bg` sessions or a user
    setting can allowlist a plugin. The retest repeats the 2026-09-25 probe and checks
    the debug log for `Channel notifications skipped`. See the channel facts in
    [13-platform-facts.md](13-platform-facts.md).
- **`pre_skill`, live.** Built and unit-tested from the hooks reference and the `Skill`
  tool's input schema. No live Driver session has shown a `PreToolUse` hook firing on
  `Skill` with the skill name in `tool_input.skill`.
- **A web build that must be served.** `profile_set` refuses a build command that
  serves, and the Driver's shell runs only the build command, so nothing serves a built
  web app over HTTP. web-driver navigates to any URL the Driver passes, so a dev server
  someone left running passes every check. See "Explorations" in
  [02-run-lifecycle.md](02-run-lifecycle.md).
- **The Oracle's wake of a waiting agent.** `pre_send_message` lets the Oracle wake any
  live agent of its run that is not working, because its Stop hook names such agents.
  The text of that `SendMessage` is not checked, so it can carry direction past a
  Manager.

## Needs implementation

Each item below is marked **(needs implementation)** in its document.

- **The device-queue broker** ([05-sessions.md](05-sessions.md)). The event `kind` and
  the `ping` on `swarm-events` are built; the broker is not.
