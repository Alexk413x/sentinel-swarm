# swarm-ledger

Local stdio MCP server for the sentinel-swarm plugin. It backs a SQLite
ledger of runs, phases, briefs, file claims, reviews, scores, and agent
activity.

The server is a skeleton today. It exposes one tool, `ledger_info`, which
reports its name, version, and status.

## Development

```
uv sync
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```
