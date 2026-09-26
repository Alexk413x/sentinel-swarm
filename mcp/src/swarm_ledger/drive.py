from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import agentfiles
from .db import write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .settings import Settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
SEVERITIES = ("blocker", "major", "minor")

# Stop rules from plans/driver-agent.md: a finding still present after this many fix
# attempts in a row stops getting fixes; this many attempts in all is the hard cap; this
# many explorations in a row that fix nothing stops the loop.
STOP_ATTEMPTS_IN_A_ROW = 3
STOP_ATTEMPTS_TOTAL = 5
STOP_WAVES_IN_A_ROW = 3
_PING_PONG_WINDOW = 4


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


# -- Pure loop-status computation, over plain dicts so it is cheap to unit test -----------


def _streak(ordinals: list[int]) -> int:
    """The number of explorations in a row, ending at the last one, that carried this
    fingerprint."""
    streak = 1
    for i in range(len(ordinals) - 1, 0, -1):
        if ordinals[i] - ordinals[i - 1] == 1:
            streak += 1
        else:
            break
    return streak


def fingerprint_status(occurrences: list[int]) -> dict[str, Any]:
    occurrences = sorted(occurrences)
    streak = _streak(occurrences)
    attempts_in_a_row = streak - 1
    total_attempts = len(occurrences) - 1
    reasons: list[str] = []
    if attempts_in_a_row >= STOP_ATTEMPTS_IN_A_ROW:
        reasons.append(f"{attempts_in_a_row} fix attempts in a row have not fixed it")
    if total_attempts >= STOP_ATTEMPTS_TOTAL:
        reasons.append(f"{total_attempts} fix attempts in all have not fixed it")
    span = occurrences[-1] - occurrences[0] + 1 if occurrences else 0
    regressed = len(occurrences) >= 2 and span > len(occurrences)
    return {
        "occurrences": occurrences,
        "attempts_in_a_row": attempts_in_a_row,
        "total_attempts": total_attempts,
        "stop": bool(reasons),
        "reasons": reasons,
        "regressed": regressed,
    }


def _ping_pong_pairs(
    by_fingerprint: dict[str, list[int]], window: list[int]
) -> list[tuple[str, str]]:
    if len(window) < 2:
        return []
    window_set_by_fp = {fp: tuple(o in ords for o in window) for fp, ords in by_fingerprint.items()}
    fps = list(window_set_by_fp)
    pairs: list[tuple[str, str]] = []
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            a, b = window_set_by_fp[fps[i]], window_set_by_fp[fps[j]]
            if any(a) and any(b) and all(x != y for x, y in zip(a, b, strict=True)):
                pairs.append((fps[i], fps[j]))
    return pairs


def compute_loop_status(requests: list[dict], findings: list[dict]) -> dict[str, Any]:
    """`requests`: rows with request_id, ordinal, state. `findings`: rows with request_id,
    fingerprint, area. Pure over plain dicts; the ledger wraps it with the database read."""
    ordinal_of = {r["request_id"]: r["ordinal"] for r in requests}
    done_ordinals = sorted(r["ordinal"] for r in requests if r["state"] == "done")

    by_fingerprint: dict[str, list[int]] = {}
    findings_at: dict[int, list[dict]] = {}
    for f in findings:
        ordinal = ordinal_of.get(f["request_id"])
        if ordinal is None:
            continue
        by_fingerprint.setdefault(f["fingerprint"], []).append(ordinal)
        findings_at.setdefault(ordinal, []).append(f)

    fingerprints = {fp: fingerprint_status(ords) for fp, ords in by_fingerprint.items()}

    reasons: list[str] = []
    for fp, status in fingerprints.items():
        reasons += [f"finding {fp}: {reason}" for reason in status["reasons"]]
        if status["regressed"]:
            reasons.append(f"finding {fp} is a regression: it was fixed, then came back")

    wave_fixed_nothing: dict[int, bool] = {}
    for i in range(1, len(done_ordinals)):
        prior_ord, cur_ord = done_ordinals[i - 1], done_ordinals[i]
        prior_fps = {f["fingerprint"] for f in findings_at.get(prior_ord, [])}
        cur_fps = {f["fingerprint"] for f in findings_at.get(cur_ord, [])}
        wave_fixed_nothing[cur_ord] = bool(prior_fps) and not (prior_fps - cur_fps)
    if len(done_ordinals) >= STOP_WAVES_IN_A_ROW + 1:
        tail = done_ordinals[-STOP_WAVES_IN_A_ROW:]
        if all(wave_fixed_nothing.get(o, False) for o in tail):
            reasons.append(f"{STOP_WAVES_IN_A_ROW} explorations in a row fixed nothing")

    fixes_causing_bugs: list[int] = []
    for i in range(1, len(done_ordinals)):
        prior_ord, cur_ord = done_ordinals[i - 1], done_ordinals[i]
        prior_area = {f["fingerprint"]: f.get("area") for f in findings_at.get(prior_ord, [])}
        current = findings_at.get(cur_ord, [])
        current_fps = {f["fingerprint"] for f in current}
        fixed_areas = {area for fp, area in prior_area.items() if fp not in current_fps and area}
        if not fixed_areas:
            continue
        new_in_area = [
            f
            for f in current
            if f["fingerprint"] not in prior_area and f.get("area") in fixed_areas
        ]
        fixed_count = sum(1 for fp, area in prior_area.items() if fp not in current_fps and area)
        if len(new_in_area) >= fixed_count:
            fixes_causing_bugs.append(cur_ord)
    if fixes_causing_bugs:
        reasons.append(
            "the last fix(es) appear to have caused new findings in the files they touched "
            f"(exploration(s) {fixes_causing_bugs})"
        )

    window = done_ordinals[-_PING_PONG_WINDOW:]
    pairs = _ping_pong_pairs(by_fingerprint, window)
    if pairs:
        reasons.append(f"findings ping-pong between explorations: {pairs}")

    latest_done = done_ordinals[-1] if done_ordinals else None
    clean_latest = latest_done is not None and not findings_at.get(latest_done)

    return {
        "fingerprints": fingerprints,
        "stopped": bool(reasons),
        "reasons": reasons,
        "clean_latest": clean_latest,
        "latest_done_ordinal": latest_done,
        "explorations_done": len(done_ordinals),
    }


def _severities_help() -> str:
    return f"severity must be one of {SEVERITIES}"


class DriveMixin:
    """The Driver's request queue and its stop-rule loop status. See
    plans/driver-agent.md for the decisions this implements.

    Split out of `ledger.py`; every method here still assumes it is mixed into
    `Ledger` and relies on `self.conn`, `self.settings`, `self.repo_root`,
    `self._release_tx`, `self._release_agent`, `self._owe_wakeup`, `self.next_step`,
    `self.brief_create`, and `self.agent_spawn`.
    """

    # Declared, not assigned: Ledger.__init__ sets these. The declarations let
    # pyright check this file as if it were part of Ledger, which it always is.
    conn: sqlite3.Connection
    settings: Settings
    repo_root: Path

    def _release_tx(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def _release_agent(
        self, conn: sqlite3.Connection, target_agent_id: str, reason: str
    ) -> None: ...

    def _owe_wakeup(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        to_agent_id: str | None,
        reason: str,
        pointer: str,
    ) -> dict | None: ...

    def next_step(self, wakeup: dict | None) -> str | None: ...

    def brief_create(
        self,
        caller: str,
        agent_id: str,
        child_name: str,
        child_role: str,
        model: str,
        body: str,
        phase_id: int | None = None,
        module_id: int | None = None,
        file_id: int | None = None,
    ) -> dict: ...

    def agent_spawn(self, caller: str, agent_id: str, child_name: str) -> dict: ...

    def _drive_loop_status(self, run_id: int) -> dict[str, Any]:
        requests = _rows(
            self.conn.execute(
                "SELECT request_id, ordinal, state FROM drive_requests WHERE run_id = ?",
                (run_id,),
            )
        )
        findings = _rows(
            self.conn.execute(
                "SELECT request_id, fingerprint, area, severity FROM drive_findings "
                "WHERE run_id = ?",
                (run_id,),
            )
        )
        return compute_loop_status(requests, findings)

    def _oracle_agent_id(self, conn: sqlite3.Connection, run_id: int) -> str | None:
        row = conn.execute(
            "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' "
            "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["agent_id"] if row is not None else None

    def _own_open_request(self, c: Caller, request_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM drive_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown request_id {request_id!r}")
        if row["agent_id"] != c.agent_id:
            raise LedgerError(f"{c.name!r} does not own exploration {request_id!r}")
        if row["state"] != "open":
            raise LedgerError(f"exploration {request_id!r} is already {row['state']!r}")
        return dict(row)

    def _flag_stop_rules(self, conn: sqlite3.Connection, run_id: int, status: dict) -> None:
        for reason in status["reasons"]:
            body = f"[driver-stop] {reason}"
            already = conn.execute(
                "SELECT 1 FROM directives WHERE run_id = ? AND source = 'driver' AND body = ?",
                (run_id, body),
            ).fetchone()
            if already is not None:
                continue
            conn.execute(
                "INSERT INTO directives (run_id, source, sender_name, body) "
                "VALUES (?, 'driver', 'driver', ?)",
                (run_id, body),
            )

    def _block_run_finish_for_driver(self, run_id: int) -> None:
        if not agentfiles.driver_available(self.repo_root):
            return
        status = self._drive_loop_status(run_id)
        if status["stopped"] or status["clean_latest"]:
            return
        if status["explorations_done"] == 0:
            raise LedgerError(
                "the host has a Driver available and no exploration has run yet; call "
                "drive_request before run_finish"
            )
        raise LedgerError(
            "the host has a Driver available and no exploration has ended clean since the "
            "last fix; call drive_request again once every fix has finished"
        )

    # -- Tools ----------------------------------------------------------------------

    def drive_request(self, caller: str, agent_id: str, focus: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        if not focus.strip():
            raise LedgerError("drive_request needs a focus list")
        if not agentfiles.driver_available(self.repo_root):
            raise LedgerError(
                "no Driver is available: install cartographer@cartographer and at least "
                "one driver plugin (android-driver, ios-driver, or web-driver) from "
                "accessibility-tools; skip exploration for this run"
            )
        open_row = self.conn.execute(
            "SELECT request_id FROM drive_requests WHERE run_id = ? AND state = 'open'",
            (c.run_id,),
        ).fetchone()
        if open_row is not None:
            raise LedgerError(
                f"exploration {open_row['request_id']} is still open; call drive_done first"
            )
        fixing = _rows(
            self.conn.execute(
                "SELECT name FROM agents WHERE run_id = ? "
                "AND role IN ('manager', 'lead', 'coder') AND ended_at IS NULL",
                (c.run_id,),
            )
        )
        if fixing:
            names = [row["name"] for row in fixing]
            raise LedgerError(
                f"a fix is still running ({names}); drive_request waits until every fix "
                "has finished"
            )

        status = self._drive_loop_status(c.run_id)

        with write_tx(self.conn) as conn:
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM drive_requests WHERE run_id = ?",
                (c.run_id,),
            ).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO drive_requests (run_id, ordinal, opened_by, focus, state) "
                "VALUES (?, ?, ?, ?, 'open')",
                (c.run_id, ordinal, c.agent_id, focus),
            )
            request_id = cur.lastrowid

        child_name = f"driver-e{ordinal}"
        model = (self.settings.models.get("driver") or ["sonnet"])[0]
        body = (
            f"Exploration request {request_id}. Focus:\n{focus}\n\n"
            f"Call drive_issue(request_id={request_id}, finding=...) for each finding, "
            f"drive_checkin(request_id={request_id}, ...) every 30 minutes, and "
            f"drive_done(request_id={request_id}) when the exploration ends."
        )
        self.brief_create(caller, agent_id, child_name, "driver", model, body)
        spawned = self.agent_spawn(caller, agent_id, child_name)

        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE drive_requests SET agent_id = ? WHERE request_id = ?",
                (spawned["agent_id"], request_id),
            )

        result = dict(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        result["driver"] = spawned
        result["loop_status"] = status
        return result

    def drive_issue(
        self, caller: str, agent_id: str, request_id: int, finding: dict[str, Any]
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "driver")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        self._own_open_request(c, request_id)

        fingerprint = str(finding.get("fingerprint") or "").strip()
        title = str(finding.get("title") or "").strip()
        severity = finding.get("severity")
        if not fingerprint or not title:
            raise LedgerError("a finding needs a fingerprint and a title")
        if severity not in SEVERITIES:
            raise LedgerError(_severities_help())

        wakeup = None
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO drive_findings (request_id, run_id, fingerprint, title, steps, "
                "expected, actual, severity, area, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    c.run_id,
                    fingerprint,
                    title,
                    finding.get("steps"),
                    finding.get("expected"),
                    finding.get("actual"),
                    severity,
                    finding.get("area"),
                    json.dumps(finding.get("evidence") or []),
                ),
            )
            finding_id = cur.lastrowid
            oracle_id = self._oracle_agent_id(conn, c.run_id)
            wakeup = self._owe_wakeup(
                conn,
                c,
                oracle_id,
                "drive_issue",
                f"Driver finding {finding_id} ({fingerprint}, {severity}) is waiting in the "
                f"ledger for exploration {request_id}.",
            )

        result = dict(
            self.conn.execute(
                "SELECT * FROM drive_findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        result["loop_status"] = self._drive_loop_status(c.run_id)
        return result

    def drive_checkin(
        self,
        caller: str,
        agent_id: str,
        request_id: int,
        covered: str,
        steps: str,
        notes: str,
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "driver")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        self._own_open_request(c, request_id)

        wakeup = None
        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE drive_requests SET last_checkin_at = {_NOW}, last_covered = ?, "
                "last_steps = ?, last_notes = ? WHERE request_id = ?",
                (covered, steps, notes, request_id),
            )
            oracle_id = self._oracle_agent_id(conn, c.run_id)
            wakeup = self._owe_wakeup(
                conn,
                c,
                oracle_id,
                "drive_checkin",
                f"Driver check-in for exploration {request_id} is waiting in the ledger.",
            )

        result = dict(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        return result

    def drive_done(self, caller: str, agent_id: str, request_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "driver")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        self._own_open_request(c, request_id)

        with self._release_tx() as conn:
            conn.execute(
                f"UPDATE drive_requests SET state = 'done', done_at = {_NOW} WHERE request_id = ?",
                (request_id,),
            )
            # Computed after the state update, in the same transaction, so this exploration
            # counts as done for the wave-stall and fixes-causing-bugs checks.
            status = self._drive_loop_status(c.run_id)
            self._flag_stop_rules(conn, c.run_id, status)
            self._release_agent(conn, c.agent_id, "drive_done")

        result = dict(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        result["loop_status"] = status
        return result
