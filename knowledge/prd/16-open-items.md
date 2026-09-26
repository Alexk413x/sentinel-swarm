# Open items

- **CI.** Confirm CI passes on Ubuntu, Windows, and macOS once the GitHub billing block
  resets. Until then, the four local checks are the only checks.
- **macOS.** The code has POSIX branches, but no live run has happened on a Mac. Run one
  smoke test there.
- **Watchdog rescue of a dropped API call.** The idle-stall check is built to catch a
  run whose Oracle API call dropped mid-response. It has not caught one in a live run.
- **PreCompact.** Whether `PreCompact` fires in a long session. It needs a session that
  fills its context.
- **Ledger server early exit.** Once, the server exited about a minute after it started,
  with no error in its log. It has not recurred.
- **Shared HTTP servers in a live run.** No real session has connected to a shared
  codebase-kg or a11y server yet. See `plans/channels-and-http.md`.
- **Claude Code across a shared server restart.** Unknown for a real session: which
  protocol version Claude Code negotiates, when it notices a dropped HTTP server
  (a failed tool call, or a lost stream), and whether a failed call starts its
  reconnect. The server side is proven: a stateless server accepts a client from before
  its restart. Probe it by killing a shared server under a live background session and
  calling one of its tools after the supervisor restarts it.
- **a11y sharing.** The ledger shares the a11y servers because a11y has no relay of its own yet. When a11y ships one, as codebase-kg 0.8.0 did, remove a11y from `shared.planned_servers` and let a11y share itself.
- **The Driver role.** Planned, not built. See `plans/driver-agent.md`.
- **Directive source names.** The directive sources `user-chat` and `outside-session`
  use hyphens, against the snake_case rule for ledger enum values. Renaming them
  changes the value that `directive_submit` accepts.
