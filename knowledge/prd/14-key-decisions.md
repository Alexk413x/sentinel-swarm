
- 2026-11-13: A wake-up send that outlives the mod's 15-second wait stays in flight: the mod does not send it again, and posts `wake_sent` when it lands. A live run on a machine with a slow hook from another plugin showed delivered wake-ups recorded as unsent and re-sent by the model.
- 2026-11-14: A session waits on the user only with a pending tool request in its transcript or a call held for the run panel. A `waiting` or `blocked` status alone is a work blocker, routed to the parent as `blocked`. A live run raised about 30 false `needs_user` popups from bypass-mode sessions that had ended their turns blocked.
- 2026-11-14: A listing entry is a running session only with a `busy`, `idle`, or `waiting` status, or a pid whose process started with the session. A reboot left a stale Oracle entry that made `run_start` refuse the resume.
- 2026-11-14: A live Coder whose file a later claim superseded raises only a `superseded` finding asking its Lead to release it.
- 2026-11-14: Setup warns about a shell operator in `test_command`, `test_command_by_scope`, `build_command`, or `lint_command`, since the Coder shell gate denies every such command.
