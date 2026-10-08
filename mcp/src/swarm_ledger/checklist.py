from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import ledger_path
from .identity import LedgerError
from .metrics import run_metrics
from .serve import is_answering, read_server_info
from .sessions import claude_binary, is_running, list_sessions


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""
    warning: bool = False


def _connect(db_path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1")
    except sqlite3.OperationalError:
        # The running server holds the WAL and its shared-memory file, which a read-only
        # open cannot see on every platform. A plain connect still issues SELECTs only.
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()


# The Oracle writes the outcome as free text; runs have ended "success", "succeeded",
# and "completed".
_SUCCESS_WORDS = ("success", "succeeded", "complete")


def _outcome_check(run: sqlite3.Row) -> Check:
    outcome = (run["outcome"] or "").strip().lower()
    ok = run["state"] == "finished" and outcome.startswith(_SUCCESS_WORDS)
    return Check(
        "the run finished with a success-like outcome",
        ok,
        f"state={run['state']!r} outcome={run['outcome']!r}",
    )


def _phases_check(conn: sqlite3.Connection, run_id: int) -> Check:
    rows = conn.execute("SELECT name, state FROM phases WHERE run_id = ?", (run_id,)).fetchall()
    bad = [f"{r['name']} ({r['state']})" for r in rows if r["state"] != "approved"]
    detail = "; ".join(bad) if bad else f"{len(rows)} phase(s) approved"
    return Check("every phase is approved", not bad, detail)


def _files_check(conn: sqlite3.Connection, run_id: int) -> Check:
    rows = conn.execute(
        "SELECT f.path, f.state FROM files f "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND f.state != 'superseded'",
        (run_id,),
    ).fetchall()
    bad = [f"{r['path']} ({r['state']})" for r in rows if r["state"] != "approved"]
    detail = "; ".join(bad) if bad else f"{len(rows)} file(s) approved"
    return Check("every file is approved (superseded rows ignored)", not bad, detail)


def _agents_check(conn: sqlite3.Connection, run_id: int) -> Check:
    rows = conn.execute("SELECT name, state FROM agents WHERE run_id = ?", (run_id,)).fetchall()
    bad = [f"{r['name']} ({r['state']})" for r in rows if r["state"] != "released"]
    detail = "; ".join(bad) if bad else f"{len(rows)} agent(s) released"
    return Check("every agent is released", not bad, detail)


def _full_test_check(conn: sqlite3.Connection, run_id: int) -> Check:
    row = conn.execute(
        "SELECT * FROM test_runs WHERE run_id = ? AND scope = 'full' "
        "ORDER BY test_run_id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return Check("the last full test run passed", False, "no full-scope test run recorded")
    detail = f"exit_code={row['exit_code']} passed={row['passed']} failed={row['failed']}"
    return Check("the last full test run passed", row["exit_code"] == 0, detail)


def _directives_check(conn: sqlite3.Connection, run_id: int) -> Check:
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM directives WHERE run_id = ? AND state = 'open'", (run_id,)
    ).fetchone()["n"]
    return Check("no open directive", n == 0, f"{n} open directive(s)")


def _watchdog_check(conn: sqlite3.Connection, run_id: int) -> Check:
    live = conn.execute(
        "SELECT kind, detail FROM watchdog_findings WHERE run_id = ? AND cleared_at IS NULL",
        (run_id,),
    ).fetchall()
    if live:
        detail = "; ".join(f"{r['kind']}: {r['detail']}" for r in live)
        return Check("no live watchdog findings", False, detail)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM watchdog_findings WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]
    if total:
        return Check(
            "no live watchdog findings",
            True,
            f"{total} finding(s) reported and cleared during the run",
            warning=True,
        )
    return Check("no live watchdog findings", True, "none reported")


def _report_check(db_path: Path) -> Check:
    path = db_path.parent / "report.md"
    return Check("a report file exists", path.is_file(), str(path))


def _sessions_and_server_checks(
    conn: sqlite3.Connection, run_id: int, repo_root: Path
) -> list[Check]:
    if shutil.which(claude_binary()) is None:
        note = "claude is not on PATH: skipped"
        return [
            Check("no swarm session of the run is still running", True, note, warning=True),
            Check("no ledger server process is left", True, note, warning=True),
        ]

    names = {
        row["session_name"]
        for row in conn.execute(
            "SELECT session_name FROM agents WHERE run_id = ? AND session_name IS NOT NULL",
            (run_id,),
        ).fetchall()
    }
    try:
        listing = list_sessions()
    except LedgerError as exc:
        session_check = Check(
            "no swarm session of the run is still running", False, f"could not list sessions: {exc}"
        )
    else:
        running = sorted(
            name
            for name in names
            if any(str(s.get("name")) == name and is_running(s) for s in listing)
        )
        detail = ", ".join(running) if running else "none running"
        session_check = Check("no swarm session of the run is still running", not running, detail)
    checks = [session_check]

    info = read_server_info(repo_root)
    server_up = info is not None and is_answering(info, repo_root)
    checks.append(
        Check(
            "no ledger server process is left",
            not server_up,
            "still answering" if server_up else "not running",
        )
    )
    return checks


def run_checklist(repo_root: Path) -> list[Check]:
    return evaluate(repo_root, with_metrics=False)[0]


def evaluate(
    repo_root: Path, with_metrics: bool = True
) -> tuple[list[Check], dict[str, Any] | None]:
    db_path = ledger_path(repo_root)
    if not db_path.is_file():
        return [Check("a run ledger exists", False, str(db_path))], None
    conn = _connect(db_path)
    try:
        run = _latest_run(conn)
        if run is None:
            return [Check("a run ledger exists", False, "the ledger has no run")], None
        run_id = run["run_id"]
        checks = [
            _outcome_check(run),
            _phases_check(conn, run_id),
            _files_check(conn, run_id),
            _agents_check(conn, run_id),
            _full_test_check(conn, run_id),
            _directives_check(conn, run_id),
            _watchdog_check(conn, run_id),
            _report_check(db_path),
            *_sessions_and_server_checks(conn, run_id, repo_root),
        ]
        metrics = run_metrics(conn, run, repo_root) if with_metrics else None
    finally:
        conn.close()
    return checks, metrics


def _status(check: Check) -> str:
    return "fail" if not check.passed else "warn" if check.warning else "pass"


def document(repo_root: Path) -> dict[str, Any]:
    checks, metrics = evaluate(repo_root)
    return {
        "schema": 1,
        "repo": str(repo_root),
        "passed": all(check.passed for check in checks),
        "checks": [
            {"name": check.name, "status": _status(check), "detail": check.detail}
            for check in checks
        ],
        "metrics": metrics,
    }


def _line(check: Check) -> str:
    label = _status(check).upper()
    return f"{label}  {check.name}: {check.detail}" if check.detail else f"{label}  {check.name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py checklist")
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument(
        "--json", action="store_true", help="print the checks and the run's metrics as JSON"
    )
    args = parser.parse_args(argv)
    repo_root = (args.repo or Path.cwd()).resolve()
    if args.json:
        doc = document(repo_root)
        print(json.dumps(doc, indent=2))
        return 0 if doc["passed"] else 1
    checks = run_checklist(repo_root)
    for check in checks:
        print(_line(check))
    return 1 if any(not check.passed for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
