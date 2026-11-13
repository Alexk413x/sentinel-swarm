
- 2026-11-13: A wake-up send that outlives the mod's 15-second wait stays in flight: the mod does not send it again, and posts `wake_sent` when it lands. A live run on a machine with a slow hook from another plugin showed delivered wake-ups recorded as unsent and re-sent by the model.
