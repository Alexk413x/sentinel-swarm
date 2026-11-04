# Copilot companion

This companion reads Sentinel Swarm records from Copilot without changing the
Claude Code implementation. It does not implement native Copilot workers or a
Claude-worker bridge. No Copilot UI port is included.

Use only `skills/sentinel-swarm/SKILL.md` from this folder as the Copilot skill.
Copy that directory to your host repo's `.github/skills/sentinel-swarm/` if you
want a project-local skill. The skill needs the resolved Sentinel Swarm installation
or checkout path and Python 3.9 or newer. This folder is not a standalone plugin
package. Do not install the repository's Claude manifest or top-level Claude
skills as evidence that Copilot swarm workers are supported.

From the host repo in PowerShell:

```powershell
py -3 -I -S '<sentinel-swarm root>\mcp\launch\ledger.py' copilot check
py -3 -I -S '<sentinel-swarm root>\mcp\launch\ledger.py' copilot status --repo '<host repo>'
```

Replace the placeholders with actual absolute paths. On macOS, use `python3` and
POSIX paths. Both commands use the standard-library ledger launcher; neither
needs an SDK dependency or starts a paid session.

| Action | Behavior |
|---|---|
| `check`, `setup`, `run`, `resume` | Exit 1 with JSON that explains unsupported native workers. Write nothing and start nothing. |
| `status [--run <id>]` | Read the Claude ledger with SQLite `mode=ro`. Print the structured run snapshot. Exit 1 with JSON `error` if reading fails. |

Claude's setup, worker launcher, role templates, hooks, and skills remain
unchanged. The separate entry point never invokes them. A status read does not
grant worker identity or enforce gates in Copilot.

The official Copilot SDK provides candidate
[hooks](https://github.com/github/copilot-sdk/blob/main/docs/hooks/hooks-overview.md)
and [session APIs](https://github.com/github/copilot-sdk/blob/main/docs/session-management.md)
for a native host. Sentinel still needs trusted identity, tool-schema, lifecycle,
permission, and fail-closed enforcement adapters before native workers are supported.
Session management is experimental and opt-in. API documentation alone does not
verify that every Sentinel gate works. A Claude-worker bridge would instead require
Claude installation and authentication and an explicit user choice.
