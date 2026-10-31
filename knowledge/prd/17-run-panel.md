# Run panel

The run panel is a side pane that shows a swarm run as a tree of agents and the work
each one owns, with what waits on the user at the top. It reads the ledger. It writes
to the ledger only when the user answers in the panel: an answer to the Oracle's
question goes in as a directive reply, and an Allow or Deny answers a held permission
request.

## Opening it

- The plugin's mod registers two function tools in every session, both deferred:
  - `open_panel` (`mcp__sentinel-swarm__open_panel`) opens the pane. `repo` names the
    host repo; without it the pane keeps the repo it already reads, or the session
    folder. It opens in any folder, with or without a ledger or a live server.
  - `swarm_status` (`mcp__sentinel-swarm__swarm_status`) answers
    `{"running": boolean, "repo": string}`. `running` is true while the repo's ledger
    server answers `GET /health` with its own name and that repo. It reads
    `.sentinel-swarm/server.json` and makes one localhost request, with no Python.
- Another plugin may call both tools. The-index shows a swarm button on its band
  whenever `swarm_status` is listed: orange with no active run, green while
  `swarm_status` reports one. The button calls `open_panel`.
- The mod never opens the pane on its own, so swarm role sessions never draw it.

## What it reads

- While the pane is open, the mod refreshes it every 5 seconds by running
  `mcp/launch/ledger.py panel --repo <repo>` from the plugin folder on the base
  interpreter with `-I -S`. `panel.py` reads the ledger read-only and Claude Code's
  session files for each session's state, and prints one JSON document.
- The document lists every run, newest first. Each run carries its state, outcome,
  branch, elapsed time, agent counts, the agent tree (Oracle, Managers, Leads, Coders,
  and the Driver), and what waits on the user: the open `needs_user` directives, the
  watchdog's open `needs_user` and `run_stalled` findings, the open permission requests
  of its live agents that have not expired (`permissions`: the request id, the agent,
  the tool, the input summary, Claude Code's reason, and when it was asked and
  expires), and a paused run's reason (the reason of the run's latest move to `paused`).
- Each agent carries its session id, session name, and transcript path. A child that
  `agent_spawn` queued in `spawn_queue` has a brief but no session yet; it sits in the
  tree under its parent as a queued row, with its brief's task and the queue's reason.
- When the snapshot has a live run, the mod also runs `claude agents --json` through
  `$.process`. A session whose `status` or `state` is `waiting` or `blocked`, or that
  carries `waitingFor`, is waiting on a person. A `blocked` session with no `waitingFor`
  shows as "is blocked on you". The mod matches it to a live agent by session
  id, or by session name when the id changed. For each match it reads the agent's
  transcript through `$.fs` and takes the last tool use that has no tool result: the
  tool and its input (a shell command, a file path, or the input as JSON, cut at 400
  characters), or an `AskUserQuestion`'s questions and option labels. A transcript over
  the 4 MiB `$.fs` limit, or one that does not read, leaves the request text out.
- Each refresh also makes the `swarm_status` check, so the pane knows whether a run is
  active.

## With no active run

- When the repo's ledger server does not answer, a box above the runs says how to
  start one:
  - No ledger: the repo has had no run. Prepare it with `/sentinel-swarm:setup`, then
    start a run with `/sentinel-swarm:run` and a PRD.
  - The newest run has not ended: it stopped before it finished. Continue it with
    `/sentinel-swarm:resume`, or start a new run.
  - Otherwise: start a run with `/sentinel-swarm:run` and a PRD.
- The repo's past runs follow in their sections, so the panel is also the run history.

## Waiting on you

- A **Waiting on you** box with a red border sits under the controls whenever a live
  run waits on the user. It lists, one line each:
  - every role session that `claude agents --json` shows waiting, as "waits on a
    permission prompt" or "waits for your answer";
  - every held permission request, as "asks to run <tool> (permission request <id>)";
  - every open `needs_user` directive, as a question from the Oracle;
  - every open watchdog finding of kind `needs_user`, unless that agent's session wait
    or held request is already listed, and of kind `run_stalled`;
  - a paused run, with its reason.
- An item that belongs to an agent names the agent and the first line of its request.
  Its details sit under the agent's row, in a red-bordered box (`borderColor="error"`):
  the tool and its input, or the question and its options, and where to answer.
  - A waiting session is answered in agent view (`claude agents`), on the session the
    box names. The panel cannot answer a prompt Claude Code already shows; it answers
    only the requests a role's own mod holds for it (below).
  - A held permission request shows the tool and its input summary, Claude Code's
    reason, and **Allow** and **Deny** buttons. A press runs `mcp/launch/ledger.py
    permission --repo <repo> --id <id> --allow` (or `--deny`), a toast says whether it
    went, and the panel refreshes. The box also says when the hold ends, after which
    the session shows Claude Code's own prompt in agent view. Every surface the mod API
    lists carries `Button`.
  - A `needs_user` directive sits under the Oracle's row with an answer field. Enter
    sends the text through `mcp/launch/ledger.py directive --source user_chat --sender
    "run panel" --reply-to <id>`, which resolves the directive, and a toast says whether
    it went. A surface with no `Input` element says to send the reply with the directive
    command from `/sentinel-swarm:run` instead.
- A stall or a pause belongs to no agent row, so the top box shows its detail and what
  to do: check the run with `/sentinel-swarm:status` and message the Oracle, or fix the
  cause and message the Oracle or run `/sentinel-swarm:resume`.
- The row of an agent with an item shows the warning glyph `⚠` and is never dimmed.

### Answering a permission prompt in the panel

A role session holds a call Claude Code would ask the person about, and the panel
answers it. The hold is a mod hook, since the plugin has no command hooks.

- **Which calls.** The mod's `tool.check` hook runs in role sessions only, the sessions
  the gates act in. It first reads Claude Code's own verdict with `next(e)`. It holds the
  call only when all of these hold:
  - the verdict is `ask` and carries no `plugin`, so it is Claude Code's own ask, not
    another plugin's;
  - `e.approver` is `person`, so a person would answer the ask;
  - `e.tool_use_id` is set, so it decides a real call, not another plugin's
    `$.tool.check` query;
  - the tool is not `AskUserQuestion` or `ExitPlanMode`, which require the person: a
    hook's allow does not dismiss their dialog;
  - `permission_hold_seconds` is above 0, and the session's ledger server answers.
- **The hold.** The hook posts `permission_request` with the tool, its input, and Claude
  Code's reason. The ledger records an `open` row in `permission_requests` with a
  one-line summary of the input (the file path, command, path, or URL, or the input as
  JSON, cut at 300 characters) and `expires_at`, `permission_hold_seconds` from now.
  It answers the request id, the hold, and the long poll's wait (25 seconds). The hook
  then posts `permission_wait` in a loop. Each post returns once the request is
  answered, after 25 seconds, or at `expires_at`.
- **The answer.** `allowed` returns `{ decision: 'allow', reason: 'Allowed by the user in
  the run panel' }`. `denied` returns `{ decision: 'deny', reason: 'The user denied this
  in the run panel' }`, with the note after a colon when one was given. The ledger
  records `answered_at` and `answered_by` ("run panel").
- **The fallback.** At `expires_at`, after one more poll than the hold needs, on any
  error, or when the ledger records no request, the hook marks the request `expired`
  (best effort) and returns Claude Code's verdict unchanged. The session then shows
  Claude Code's own prompt, which the user answers in agent view. When the person
  interrupts, `next.signal` aborts, and the hook marks the request `withdrawn` and
  returns the verdict. Its `.catch` returns `next(e)`, Claude Code's verdict, so a
  failure falls back to the ask and never to an allow. The hook never changes an
  `allow` or a `deny`.
- **The hook's budget.** A mod hook's budget is 10 seconds of its own code
  (`HookBudget.ms`). The time it awaits `$.http.fetch` does not count, so the hold can
  last minutes. Past the budget its `.catch` runs with a 1-second grace (`catchMs`).
  After `next.signal` aborts, a hook has 5 seconds (`lingerMs`); the hook stops waiting
  on the poll at once.
- **The routes.** `permission_request`, `permission_wait`, and `permission_close` are
  hook events on the token-authenticated `POST /hook/<event>` route. Each answers
  nothing for a session that is not a live agent of the live run, and
  `permission_wait` and `permission_close` act only on the caller's own request.
  `permission_wait` runs on the server's request thread, never on a hook worker, so a
  held session does not hold up other sessions' gates. See "The hook route" in
  [06-ledger-server.md](06-ledger-server.md).
- **Answering.** `mcp/launch/ledger.py permission --repo <repo> --id <id> (--allow |
  --deny) [--note <text>]` answers an open request. It refuses a request that is not
  open. It marks an open request past `expires_at` `expired` and refuses it, saying to
  answer the prompt in agent view.
- **The watchdog.** While the hook holds a call, Claude Code shows the session busy.
  The watchdog reads open, unexpired requests and treats the session as waiting on the
  user: it files `needs_user` with the tool and input, which notifies the user and is
  reminded every `remind_minutes`, and it does not report the session `stuck` or
  `crashed` or count it as working for `run_stalled`. See [08-watchdog.md](08-watchdog.md).

## The tree

- Each run sits in its own bordered section. Clicking the header line collapses or
  expands it. The newest run starts expanded.
- Each agent row shows a status glyph, its name in its role colour, its unread and owed
  counts, and its own task at the right end of the line. Hovering the name shows the
  model and effort, or a queued row's queue reason.
- An agent with children or tasks shows its count after its name, and clicking the name
  collapses or expands it. The Oracle is not collapsible.
- Tree lines take the colour of the parent they come from. Oracle and Manager lines
  reach the bottom of their expanded sections.
- A row is dimmed when its agent has ended or has not started:
  - it has ended, or its state is `released` or `parked`;
  - the ledger parked it (`parked_at` set), for example while it waits for memory;
  - its session is gone;
  - its own task is `approved`;
  - it is a Manager whose phase is still `planned`;
  - it is a queued child with no session yet.
  A row that works keeps full colour, and so does a row that waits on the user.
- Ended agents are hidden while a run is live; **show ended** shows them.
- Controls: show or hide ended, collapse all, expand all, refresh, and the time of the
  last read. A grey legend box sits under the Waiting on you box. Its lines name the
  agent glyphs, including `⚠` for "waiting on you", the work glyphs, and "dimmed: ended
  or not started".
- The run header counts live, working, parked, gone, queued, and ended agents.

## Tasks

- Each role's own task is the item it owns: a Manager's phase, a Lead's module, a
  Coder's file. The Oracle's own task is **all phases**, the whole run. Its detail shows
  the run's PRD until the run has an outcome, then the outcome.
- A file task shows its file name only. Its detail starts with the full repo path.
- The task sits at the end of the row and gives way first: it shrinks before the agent's
  name, and a name too long for the room truncates from the left with a leading `…`
  (`wrap="truncate-start"`), so the end of the file name stays visible.
- Work that no agent owns yet appears under the agent it belongs to, in ledger order.
  Work a queued child will own sits on that child's row.
- Clicking a task opens its brief below it, with simple Markdown (headings, lists,
  bold, italic, code), and its latest review. One task is open at a time. Clicking the
  open text, another task, or any control closes it.
- Each task's status glyph:
  - `!` red, **problem**: the item, or anything below it, is incomplete or was released
    without finishing.
  - `✓` green, **done**: the item is approved and everything below it is done.
  - `○` grey, **waiting**: nothing has started.
  - `●` yellow, **in progress**: anything else.
- Status rolls up: file, then module, then phase, then the run. A superseded file
  version counts as done and stops counting once a newer version exists, so a fix by a
  new or resumed agent clears a problem all the way up. A module approved with an
  accepted incomplete file stays a problem until the file is fixed.
