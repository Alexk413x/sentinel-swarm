---
name: status
description: Prints a sentinel-swarm run's phases, files, agents, locks, and open issues by reading .sentinel-swarm/ledger.db read-only. Use for "swarm status", "what is the swarm doing", "show the run tree", or "what is still open on this run".
---

# status

Reports a run's state from the ledger database without changing anything.

## Why it reads the file directly

Every ledger tool resolves its caller against the agent registry, so a session that
is not part of the run cannot call `status_tree`. This skill reads
`.sentinel-swarm/ledger.db` instead, read-only, with SELECT statements only. It works
while a run is live and after it has finished.

## Run it

From the root of the working tree the swarm is building in:

```bash
python - <<'PY'
import sqlite3, pathlib, sys
db = pathlib.Path(".sentinel-swarm/ledger.db")
if not db.is_file():
    sys.exit("no ledger at .sentinel-swarm/ledger.db")
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
q = lambda sql, *a: conn.execute(sql, a).fetchall()

run = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
if run is None:
    sys.exit("the ledger has no run")
print(f"run {run['run_id']}: {run['state']} outcome={run['outcome']} started={run['started_at']}")

print("\nphases")
for p in q("SELECT * FROM phases WHERE run_id=? ORDER BY ordinal", run["run_id"]):
    deps = [str(d["depends_on_phase_id"]) for d in
            q("SELECT depends_on_phase_id FROM phase_deps WHERE phase_id=?", p["phase_id"])]
    print(f"  [{p['phase_id']}] {p['name']:<24} {p['state']:<10} deps={','.join(deps) or '-'}")

print("\nfiles")
for f in q("""SELECT f.*, m.name AS module FROM files f
              JOIN modules m ON m.module_id=f.module_id
              JOIN phases ph ON ph.phase_id=m.phase_id
              WHERE ph.run_id=? ORDER BY f.file_id""", run["run_id"]):
    lock = "released" if f["released_at"] else f"locked by {f['owner_agent_id']}"
    print(f"  [{f['file_id']}] {f['path']:<40} {f['state']:<12} {f['module']:<16} {lock}")

print("\nagents")
for a in q("SELECT * FROM agents WHERE run_id=? ORDER BY started_at", run["run_id"]):
    live = f"ended {a['ended_at']}" if a["ended_at"] else "live"
    print(f"  {a['name']:<28} {a['role']:<8} {a['state']:<10} {live:<28} "
          f"model={a['model']} heartbeat={a['last_heartbeat_at']} doing={a['current_activity']}")

print("\nopen issues")
for i in q("SELECT * FROM issues WHERE run_id=? AND state='open' ORDER BY issue_id", run["run_id"]):
    print(f"  [{i['issue_id']}] round {i['round']} attempts {i['attempts']} file={i['file_id']} {i['title']}")

print("\nopen deferrals")
for d in q("SELECT * FROM deferrals WHERE run_id=? AND state='open' ORDER BY deferral_id", run["run_id"]):
    print(f"  [{d['deferral_id']}] file={d['file_id']} {d['reason']}")
PY
```

Use `python3` in place of `python` on a host where only that name exists.

## Recent activity

```bash
python - <<'PY'
import sqlite3
conn = sqlite3.connect("file:.sentinel-swarm/ledger.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
for e in conn.execute("SELECT * FROM agent_events ORDER BY event_id DESC LIMIT 30"):
    print(f"{e['at']} {e['agent_id'][:12]:<12} {e['from_state']} -> {e['to_state']} ({e['reason']})")
PY
```

## Latest scores for one file

```bash
python - <<'PY'
import sqlite3
FILE_ID = 1
conn = sqlite3.connect("file:.sentinel-swarm/ledger.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
for r in conn.execute(
    "SELECT * FROM reviews WHERE file_id=? ORDER BY review_id DESC LIMIT 2", (FILE_ID,)):
    print(f"review {r['review_id']} kind={r['kind']} outcome={r['outcome']} at {r['created_at']}")
    for s in conn.execute(
        "SELECT dimension, AVG(value)*10 AS score FROM scores WHERE review_id=? "
        "GROUP BY dimension ORDER BY dimension", (r["review_id"],)):
        print(f"    {s['dimension']:<20} {s['score']:.1f}")
PY
```

## Reading the output

- A file whose `state` is `working` and whose lock names a Coder is being written
  now. `handed_up` means it waits on its Lead. `approved` means the claim is
  released.
- An agent whose `last_heartbeat_at` has not moved is what the watchdog reports to
  the Oracle.
- An issue at round 3 is with the Oracle.

Report what you read as plain text. Do not write to the database, and do not report
test results from anywhere but the `test_runs` table.

## When the read fails

`unable to open database file` on a read-only open means the running server holds the
write-ahead log and its shared-memory file is not readable. Retry the same connect
without `?mode=ro`, and still run SELECT statements only.
