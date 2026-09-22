# swarm-ledger

Local stdio MCP server for the sentinel-swarm plugin. It backs a SQLite
ledger of runs, phases, briefs, file claims, reviews, scores, and agent
activity.

The server is a skeleton today. It exposes one tool, `ledger_info`, which
reports its name, version, and status.

## The ledger database

`swarm_ledger.db.ledger_path(repo_root)` returns
`<main checkout>/.sentinel-swarm/ledger.db`. When `repo_root` is a git
worktree, it reads the worktree's `.git` file back to the main checkout, so
every worktree of a repo shares one ledger file. `ensure_git_exclude`
appends `.sentinel-swarm/` to the main checkout's `.git/info/exclude`
(never to `.gitignore`), so the ledger stays untracked in every worktree
without touching the host repo's own ignore rules.

`swarm_ledger.db.connect(path)` opens the database with:

- `PRAGMA journal_mode=WAL`, so readers and the one writer don't block each
  other.
- `PRAGMA busy_timeout=5000`, so a writer waits up to 5 seconds for a lock
  instead of failing immediately under contention.
- `isolation_level=None` (manual transactions) and `PRAGMA foreign_keys=ON`.

Use `swarm_ledger.db.write_tx(conn)` for every write. It issues
`BEGIN IMMEDIATE` (so the write lock is acquired up front, not on the first
write statement), commits on success, and rolls back on any exception.

`connect` also runs `migrate`, which applies `schema.sql` and is safe to
call from multiple processes against the same file.

## Development

```
uv sync
uv run pytest
uv run pyright
uv run ruff check
uv run ruff format --check
```
