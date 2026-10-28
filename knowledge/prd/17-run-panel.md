# Run panel

The run panel is a side pane that shows a swarm run as a tree of agents and the work
each one owns. It is read-only: it reads the ledger and changes nothing.

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
  branch, elapsed time, agent counts, and the agent tree: Oracle, Managers, Leads,
  Coders, and the Driver.
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

## The tree

- Each run sits in its own bordered section. Clicking the header line collapses or
  expands it. The newest run starts expanded.
- Each agent row shows a status glyph, its name in its role colour, its unread and owed
  counts, and its own task at the right end of the line. Hovering the name shows the
  model and effort.
- An agent with children or tasks shows its count after its name, and clicking the name
  collapses or expands it. The Oracle is not collapsible.
- Tree lines take the colour of the parent they come from. Oracle and Manager lines
  reach the bottom of their expanded sections.
- Ended agents are hidden while a run is live; **show ended** shows them.
- Controls: show or hide ended, collapse all, expand all, refresh, and the time of the
  last read. A grey legend box sits under them.

## Tasks

- Each role's own task is the item it owns: a Manager's phase, a Lead's module, a
  Coder's file. The Oracle's own task is **all phases**, the whole run.
- Work that no agent owns yet appears under the agent it belongs to, in ledger order.
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
