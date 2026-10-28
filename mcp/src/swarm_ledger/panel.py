from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable

from .clock import parse_stamp, stamp, utcnow
from .db import ledger_path, open_readonly
from .lock import pid_alive
from .paths import config_dir

ROLE_ORDER = {"oracle": 0, "manager": 1, "lead": 2, "coder": 3, "driver": 4}
SUMMARY_CHARS = 90
OWN_COLUMN = {"manager": "phase_id", "lead": "module_id", "coder": "file_id"}
STATUS_STATES = {
    "done": ("approved", "superseded"),
    "active": ("working", "handed_up", "claimed", "returned"),
    "problem": ("incomplete", "released"),
}


def live_sessions(sessions_dir: Path | None = None) -> dict[str, dict]:
    folder = sessions_dir or config_dir() / "sessions"
    found: dict[str, dict] = {}
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return found
    for path in files:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str):
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            continue
        found[entry["sessionId"]] = entry
    return found


def _names(conn: sqlite3.Connection, table: str, run_id: int) -> dict[int | None, str]:
    if table == "phases":
        sql = "SELECT phase_id AS k, name AS v FROM phases WHERE run_id = ?"
    elif table == "modules":
        sql = (
            "SELECT m.module_id AS k, m.name AS v FROM modules m "
            "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ?"
        )
    else:
        sql = (
            "SELECT f.file_id AS k, f.path AS v FROM files f JOIN modules m "
            "ON m.module_id = f.module_id JOIN phases p ON p.phase_id = m.phase_id "
            "WHERE p.run_id = ?"
        )
    return {row["k"]: row["v"] for row in conn.execute(sql, (run_id,))}


def _counts(conn: sqlite3.Connection, sql: str, run_id: int) -> dict[str, int]:
    try:
        return {row[0]: row[1] for row in conn.execute(sql, (run_id,))}
    except sqlite3.Error:
        return {}


def _status(state: str) -> str:
    for status, states in STATUS_STATES.items():
        if state in states:
            return status
    return "waiting"


def _combine(statuses: list[str]) -> str:
    if "problem" in statuses:
        return "problem"
    if all(status == "done" for status in statuses):
        return "done"
    if all(status == "waiting" for status in statuses):
        return "waiting"
    return "active"


def _task_lists(
    conn: sqlite3.Connection, run: sqlite3.Row, agents: list[dict]
) -> Callable[[dict], tuple[dict | None, list[dict]]]:
    run_id = run["run_id"]
    phases = conn.execute(
        "SELECT phase_id, name, state FROM phases WHERE run_id = ? ORDER BY ordinal, phase_id",
        (run_id,),
    ).fetchall()
    modules = conn.execute(
        "SELECT m.module_id, m.phase_id, m.name, m.state FROM modules m "
        "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ? ORDER BY m.module_id",
        (run_id,),
    ).fetchall()
    files = conn.execute(
        "SELECT f.file_id, f.module_id, f.path, f.state FROM files f "
        "JOIN modules m ON m.module_id = f.module_id JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? ORDER BY f.file_id",
        (run_id,),
    ).fetchall()
    owners: dict[tuple[str, int], list[str]] = {}
    for agent in sorted(agents, key=lambda a: a["started_at"] or ""):
        column = OWN_COLUMN.get(agent["role"])
        if column and agent.get(column) is not None:
            owners.setdefault((column, agent[column]), []).append(agent["name"])
    briefs = _latest_by_subject(
        conn,
        "SELECT phase_id, module_id, file_id, child_role, body FROM briefs "
        "WHERE run_id = ? ORDER BY brief_id",
        run_id,
    )
    reviews = _latest_by_subject(
        conn,
        "SELECT r.phase_id, r.module_id, r.file_id, r.kind, r.outcome, r.notes FROM reviews r "
        "JOIN agents a ON a.agent_id = r.reviewer_agent_id "
        "WHERE a.run_id = ? AND r.outcome IS NOT NULL ORDER BY r.review_id",
        run_id,
    )

    file_status = {f["file_id"]: _status(f["state"]) for f in files}
    module_status = {
        m["module_id"]: _combine(
            [_status(m["state"])]
            + [
                file_status[f["file_id"]]
                for f in files
                if f["module_id"] == m["module_id"] and f["state"] != "superseded"
            ]
        )
        for m in modules
    }
    phase_status = {
        p["phase_id"]: _combine(
            [_status(p["state"])]
            + [module_status[m["module_id"]] for m in modules if m["phase_id"] == p["phase_id"]]
        )
        for p in phases
    }
    status_of = {"phase_id": phase_status, "module_id": module_status, "file_id": file_status}

    def item(column: str, key: int, name: str, state: str) -> dict:
        brief = briefs.get((column, key))
        review = reviews.get((column, key))
        body = brief["body"] if brief is not None else None
        return {
            "id": f"{column}:{key}",
            "name": name,
            "state": state,
            "status": status_of[column][key],
            "owners": owners.get((column, key), []),
            "summary": _summary(body),
            "brief": body,
            "review": None
            if review is None
            else {"kind": review["kind"], "outcome": review["outcome"], "notes": review["notes"]},
        }

    by_subject = {
        **{
            ("phase_id", p["phase_id"]): item("phase_id", p["phase_id"], p["name"], p["state"])
            for p in phases
        },
        **{
            ("module_id", m["module_id"]): item("module_id", m["module_id"], m["name"], m["state"])
            for m in modules
        },
        **{
            ("file_id", f["file_id"]): item("file_id", f["file_id"], f["path"], f["state"])
            for f in files
        },
    }
    under = {
        "oracle": [("phase_id", p["phase_id"]) for p in phases],
        "manager": [("module_id", m["module_id"], m["phase_id"]) for m in modules],
        "lead": [
            ("file_id", f["file_id"], f["module_id"]) for f in files if f["state"] != "superseded"
        ],
    }
    scope = {"manager": "phase_id", "lead": "module_id"}
    whole_run = {
        "id": f"run:{run['run_id']}",
        "name": "all phases",
        "state": run["state"],
        "status": _combine(list(phase_status.values())) if phase_status else "waiting",
        "owners": [],
        "summary": None,
        "brief": run["outcome"],
        "review": None,
    }

    def tasks(agent: dict) -> tuple[dict | None, list[dict]]:
        role = agent["role"]
        column = OWN_COLUMN.get(role)
        own = by_subject.get((column, agent.get(column))) if column else None
        if role == "oracle":
            own = whole_run
        waiting = [
            by_subject[(entry[0], entry[1])]
            for entry in under.get(role, [])
            if (len(entry) == 2 or entry[2] == agent.get(scope[role]))
            and not owners.get((entry[0], entry[1]))
        ]
        return own, waiting

    return tasks


def _latest_by_subject(
    conn: sqlite3.Connection, sql: str, run_id: int
) -> dict[tuple[str, int], sqlite3.Row]:
    latest: dict[tuple[str, int], sqlite3.Row] = {}
    try:
        rows = conn.execute(sql, (run_id,)).fetchall()
    except sqlite3.Error:
        return latest
    for row in rows:
        for column in ("file_id", "module_id", "phase_id"):
            if row[column] is not None:
                latest[(column, row[column])] = row
                break
    return latest


def _summary(body: str | None) -> str | None:
    if not body:
        return None
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        text = text.removeprefix("Goal:").strip()
        sentence = text.split(". ", 1)[0].rstrip(".")
        return sentence if len(sentence) <= SUMMARY_CHARS else sentence[: SUMMARY_CHARS - 1] + "…"
    return None


def _session_state(agent: dict, sessions: dict[str, dict], by_name: dict[str, dict]) -> str | None:
    if agent["ended_at"] is not None:
        return None
    entry = sessions.get(agent["agent_id"]) or by_name.get(agent.get("session_name") or "")
    if entry is None:
        return "gone"
    status = str(entry.get("status") or "").lower()
    return status if status in ("busy", "idle") else "running"


def snapshot(
    conn: sqlite3.Connection, sessions: dict[str, dict], run_id: int | None = None
) -> dict:
    rows = conn.execute(
        "SELECT * FROM runs WHERE ? IS NULL OR run_id = ? ORDER BY run_id DESC",
        (run_id, run_id),
    ).fetchall()
    return {"runs": [_run_snapshot(conn, sessions, run) for run in rows]}


def _run_snapshot(conn: sqlite3.Connection, sessions: dict[str, dict], run: sqlite3.Row) -> dict:
    run_id = run["run_id"]
    started = parse_stamp(run["started_at"])
    ended = parse_stamp(run["ended_at"]) if run["ended_at"] else None
    elapsed = int(((ended or utcnow()) - started).total_seconds()) if started else None

    phases = _names(conn, "phases", run_id)
    modules = _names(conn, "modules", run_id)
    files = _names(conn, "files", run_id)
    owed = _counts(
        conn,
        "SELECT to_agent_id, COUNT(*) FROM wakeups WHERE run_id = ? AND sent_at IS NULL "
        "GROUP BY to_agent_id",
        run_id,
    )
    unread = _counts(
        conn,
        "SELECT to_name, COUNT(*) FROM messages WHERE run_id = ? AND read_at IS NULL "
        "GROUP BY to_name",
        run_id,
    )
    by_name = {
        str(entry["name"]): entry
        for entry in sessions.values()
        if isinstance(entry.get("name"), str)
    }

    nodes: dict[str, dict] = {}
    rows = [dict(row) for row in conn.execute("SELECT * FROM agents WHERE run_id = ?", (run_id,))]
    tasks = _task_lists(conn, run, rows)
    for agent in rows:
        own_task, waiting = tasks(agent)
        nodes[agent["agent_id"]] = {
            "name": agent["name"],
            "role": agent["role"],
            "model": agent.get("model"),
            "effort": agent.get("effort"),
            "state": agent["state"],
            "session": _session_state(agent, sessions, by_name),
            "parked": agent.get("parked_at") is not None,
            "park_reason": agent.get("park_reason"),
            "ended_at": agent["ended_at"],
            "end_reason": agent.get("end_reason"),
            "phase": phases.get(agent.get("phase_id")),
            "module": modules.get(agent.get("module_id")),
            "file": files.get(agent.get("file_id")),
            "activity": agent.get("current_activity"),
            "heartbeat_at": agent.get("last_heartbeat_at"),
            "started_at": agent["started_at"],
            "owed": owed.get(agent["agent_id"], 0),
            "unread": unread.get(agent["name"], 0),
            "task": own_task,
            "tasks": waiting,
            "children": [],
        }

    roots: list[dict] = []
    for agent in rows:
        node = nodes[agent["agent_id"]]
        parent = nodes.get(agent.get("parent_agent_id") or "")
        (parent["children"] if parent is not None else roots).append(node)

    def order(node: dict) -> tuple:
        return (ROLE_ORDER.get(node["role"], 9), node["started_at"] or "")

    def sort(level: list[dict]) -> None:
        level.sort(key=order)
        for node in level:
            sort(node["children"])

    sort(roots)
    live = [n for n in nodes.values() if n["ended_at"] is None]
    return {
        "run": {
            "run_id": run_id,
            "state": run["state"],
            "outcome": run["outcome"],
            "branch": run["branch"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "elapsed_s": elapsed,
        },
        "agents": roots,
        "counts": {
            "live": len(live),
            "working": sum(1 for n in live if n["state"] == "working"),
            "parked": sum(1 for n in live if n["parked"]),
            "gone": sum(1 for n in live if n["session"] == "gone" and not n["parked"]),
            "ended": len(nodes) - len(live),
        },
    }


def document(repo_root: Path, run_id: int | None = None, sessions_dir: Path | None = None) -> dict:
    path = ledger_path(repo_root)
    base = {"repo": str(repo_root), "generated_at": stamp(utcnow())}
    if not path.is_file():
        return {**base, "runs": [], "error": "no ledger"}
    sessions = live_sessions(sessions_dir)
    error = "the ledger did not open"
    for opener in (open_readonly, _open_plain):
        try:
            conn = opener(path)
        except sqlite3.Error as exc:
            error = str(exc)
            continue
        try:
            return {**base, **snapshot(conn, sessions, run_id)}
        except sqlite3.Error as exc:
            error = str(exc)
        finally:
            conn.close()
    return {**base, "runs": [], "error": error}


def _open_plain(path: Path) -> sqlite3.Connection:
    # A read-only open fails while the server holds the WAL and its shared-memory file is not
    # readable; a plain open still only runs SELECTs here.
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py panel")
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--run", type=int, default=None, help="run id; default: every run")
    args = parser.parse_args(argv)
    doc = document((args.repo or Path.cwd()).resolve(), args.run)
    sys.stdout.write(json.dumps(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
