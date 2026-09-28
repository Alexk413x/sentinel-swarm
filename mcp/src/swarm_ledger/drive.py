from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import agentfiles, notify
from .db import write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .settings import Settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
SEVERITIES = ("blocker", "major", "minor")
UNAVAILABLE_TAG = "[driver-unavailable]"

# Stop rules from knowledge/prd/02-run-lifecycle.md: a finding still present after this many fix
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


def _ordinal_list(ordinals: list[int]) -> str:
    return ", ".join(str(o) for o in ordinals)


def _finding_line(f: dict) -> str:
    return (
        f"- {f.get('title') or f['fingerprint']} ({f['fingerprint']}), "
        f"{f.get('severity') or 'unknown severity'}, area {f.get('area') or 'unknown'}"
    )


def compute_loop_status(requests: list[dict], findings: list[dict]) -> dict[str, Any]:
    """`requests`: rows with request_id, ordinal, state. `findings`: rows with request_id,
    fingerprint, area, and optionally title, severity, and evidence (a list of paths). Pure
    over plain dicts; the ledger wraps it with the database read."""
    ordinal_of = {r["request_id"]: r["ordinal"] for r in requests}
    done_ordinals = sorted(r["ordinal"] for r in requests if r["state"] == "done")
    finished = sorted(
        (r["ordinal"], r["state"]) for r in requests if r["state"] in ("done", "abandoned")
    )

    by_fingerprint: dict[str, list[int]] = {}
    findings_at: dict[int, list[dict]] = {}
    title_of: dict[str, str] = {}
    area_of: dict[str, str | None] = {}
    evidence_of: dict[str, list[str]] = {}
    for f in findings:
        ordinal = ordinal_of.get(f["request_id"])
        if ordinal is None:
            continue
        fp = f["fingerprint"]
        by_fingerprint.setdefault(fp, []).append(ordinal)
        findings_at.setdefault(ordinal, []).append(f)
        title_of[fp] = f.get("title") or fp
        area_of[fp] = f.get("area") or area_of.get(fp)
        for path in f.get("evidence") or []:
            if path not in evidence_of.setdefault(fp, []):
                evidence_of[fp].append(path)

    fingerprints = {fp: fingerprint_status(ords) for fp, ords in by_fingerprint.items()}

    stops: list[dict[str, Any]] = []

    def target(fp: str) -> dict[str, str | None]:
        return {"fingerprint": fp, "area": area_of.get(fp)}

    def stop(
        reason: str,
        summary: str,
        kind: str,
        targets: list[dict[str, str | None]],
        detail: list[str],
    ) -> None:
        stops.append(
            {
                "reason": reason,
                "summary": summary,
                "kind": kind,
                "targets": targets,
                "detail": detail,
            }
        )

    def evidence_lines(fp: str) -> list[str]:
        seen = sorted(set(fingerprints[fp]["occurrences"]))
        lines = [
            f"Finding: {title_of[fp]} ({fp}), area {area_of.get(fp) or 'unknown'}",
            f"Fix attempts: {fingerprints[fp]['attempts_in_a_row']} in a row, "
            f"{fingerprints[fp]['total_attempts']} in all",
            f"Seen in explorations: {_ordinal_list(seen)}",
        ]
        if evidence_of.get(fp):
            lines.append(f"Evidence: {', '.join(evidence_of[fp])}")
        return lines

    for fp, status in fingerprints.items():
        title = title_of[fp]
        in_a_row, total = status["attempts_in_a_row"], status["total_attempts"]
        seen = _ordinal_list(sorted(set(status["occurrences"])))
        summaries = []
        if in_a_row >= STOP_ATTEMPTS_IN_A_ROW:
            summaries.append(
                f"{in_a_row} attempts in a row with no progress on {title} [{fp}], "
                f"seen in explorations {seen}"
            )
        if total >= STOP_ATTEMPTS_TOTAL:
            summaries.append(
                f"{total} attempts in all with no fix for {title} [{fp}], "
                f"seen in explorations {seen}"
            )
        for reason, summary in zip(status["reasons"], summaries, strict=True):
            stop(f"finding {fp}: {reason}", summary, "finding", [target(fp)], evidence_lines(fp))
        if status["regressed"]:
            stop(
                f"finding {fp} is a regression: it was fixed, then came back",
                f"{title} came back after it was fixed",
                "pattern",
                [target(fp)],
                evidence_lines(fp),
            )

    wave_fixed_nothing: dict[int, bool] = {}
    for i in range(1, len(done_ordinals)):
        prior_ord, cur_ord = done_ordinals[i - 1], done_ordinals[i]
        prior_fps = {f["fingerprint"] for f in findings_at.get(prior_ord, [])}
        cur_fps = {f["fingerprint"] for f in findings_at.get(cur_ord, [])}
        wave_fixed_nothing[cur_ord] = bool(prior_fps) and not (prior_fps - cur_fps)

    latest_done = done_ordinals[-1] if done_ordinals else None
    left: list[dict] = []
    if latest_done is not None:
        for f in findings_at.get(latest_done, []):
            if all(f["fingerprint"] != kept["fingerprint"] for kept in left):
                left.append(f)

    if len(done_ordinals) >= STOP_WAVES_IN_A_ROW + 1:
        tail = done_ordinals[-STOP_WAVES_IN_A_ROW:]
        if all(wave_fixed_nothing.get(o, False) for o in tail):
            stalled = f"{STOP_WAVES_IN_A_ROW} explorations in a row fixed nothing"
            titles = "; ".join(f.get("title") or f["fingerprint"] for f in left)
            stop(
                stalled,
                f"the loop ended: {stalled}; {len(left)} left: {titles}",
                "stalled",
                [],
                [f"Left after exploration {latest_done}:", *(_finding_line(f) for f in left)],
            )

    fixes_causing_bugs: list[int] = []
    caused: list[dict] = []
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
            caused.extend(new_in_area)
    if fixes_causing_bugs:
        caused_fps = list(dict.fromkeys(f["fingerprint"] for f in caused))
        areas = sorted({f["area"] for f in caused if f.get("area")})
        stop(
            "the last fix(es) appear to have caused new findings in the files they touched "
            f"(exploration(s) {fixes_causing_bugs})",
            "the last fixes caused new bugs in the files they touched",
            "pattern",
            [target(fp) for fp in caused_fps],
            [f"Areas paused: {', '.join(areas) or 'unknown'}", *(_finding_line(f) for f in caused)],
        )

    window = done_ordinals[-_PING_PONG_WINDOW:]
    pairs = _ping_pong_pairs(by_fingerprint, window)
    if pairs:
        first, second = pairs[0]
        pair_fps = list(dict.fromkeys(fp for pair in pairs for fp in pair))
        stop(
            f"findings ping-pong between explorations: {pairs}",
            f"{title_of[first]} and {title_of[second]} keep coming back in turn",
            "pattern",
            [target(fp) for fp in pair_fps],
            [line for fp in pair_fps for line in evidence_lines(fp)],
        )

    reasons = [entry["reason"] for entry in stops]

    clean_latest = (
        bool(finished) and finished[-1][1] == "done" and not findings_at.get(finished[-1][0])
    )

    return {
        "fingerprints": fingerprints,
        "stopped": bool(reasons),
        "reasons": reasons,
        "stops": stops,
        "left": left,
        "clean_latest": clean_latest,
        "latest_done_ordinal": latest_done,
        "explorations_done": len(done_ordinals),
    }


def loop_status(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    requests = _rows(
        conn.execute(
            "SELECT request_id, ordinal, state FROM drive_requests WHERE run_id = ?", (run_id,)
        )
    )
    findings = _rows(
        conn.execute(
            "SELECT request_id, fingerprint, area, severity, title, evidence_json "
            "FROM drive_findings WHERE run_id = ? ORDER BY finding_id",
            (run_id,),
        )
    )
    for f in findings:
        f["evidence"] = json.loads(f.pop("evidence_json") or "[]")
    return compute_loop_status(requests, findings)


def open_findings(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    ordinal_of: dict[int, int] = {}
    done: list[int] = []
    for r in conn.execute(
        "SELECT request_id, ordinal, state FROM drive_requests WHERE run_id = ?", (run_id,)
    ):
        ordinal_of[r["request_id"]] = r["ordinal"]
        if r["state"] == "done":
            done.append(r["ordinal"])
    latest: dict[str, dict] = {}
    for f in _rows(
        conn.execute(
            "SELECT finding_id, request_id, fingerprint, title, severity, area "
            "FROM drive_findings WHERE run_id = ? ORDER BY finding_id",
            (run_id,),
        )
    ):
        latest[f["fingerprint"]] = f
    stopped = {
        row["fingerprint"]
        for row in conn.execute(
            "SELECT fingerprint FROM drive_stops WHERE run_id = ? AND kind = 'finding'",
            (run_id,),
        )
    }
    live = loop_status(conn, run_id)["fingerprints"]
    return [
        f
        for fp, f in sorted(latest.items(), key=lambda item: item[1]["finding_id"])
        if fp not in stopped
        and not live.get(fp, {}).get("stop")
        and not any(o > ordinal_of[f["request_id"]] for o in done)
    ]


def findings_named(conn: sqlite3.Connection, finding_ids: list[int]) -> list[dict]:
    if not finding_ids:
        return []
    marks = ", ".join("?" for _ in finding_ids)
    rows = {
        row["finding_id"]: dict(row)
        for row in conn.execute(
            "SELECT finding_id, fingerprint, title, severity, area FROM drive_findings "
            f"WHERE finding_id IN ({marks})",
            finding_ids,
        )
    }
    return [rows[i] for i in finding_ids if i in rows]


def _severities_help() -> str:
    return f"severity must be one of {SEVERITIES}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _done_pointer(
    request: dict, status: dict, flagged: list[dict[str, Any]], blocked: str, findings: int
) -> str:
    outcome: list[str] = []
    if blocked:
        outcome.append(f"blocked: {blocked}")
    if flagged:
        first = sorted(flagged, key=lambda entry: entry["kind"] != "stalled")[0]
        more = f" (+{len(flagged) - 1} more)" if len(flagged) > 1 else ""
        outcome.append(f"stopped by a stop rule: {first['reason']}{more}")
    if not outcome:
        outcome.append(
            "clean, no findings" if status["clean_latest"] else f"done, {findings} finding(s)"
        )
    return _one_line(
        f"Driver exploration {request['ordinal']} (request {request['request_id']}) ended: "
        f"{'; '.join(outcome)}. Its result is in the ledger."
    )


_SETTLED_DRIVER = (
    "SELECT 1 FROM agents a WHERE a.agent_id = ? AND a.role = 'driver' "
    "AND a.ended_at IS NULL "
    "AND EXISTS (SELECT 1 FROM drive_requests r WHERE r.agent_id = a.agent_id "
    "AND r.state != 'open') "
    "AND NOT EXISTS (SELECT 1 FROM drive_requests r WHERE r.agent_id = a.agent_id "
    "AND r.state = 'open') "
    "AND NOT EXISTS (SELECT 1 FROM wakeups w JOIN agents t ON t.agent_id = w.to_agent_id "
    "WHERE w.from_agent_id = a.agent_id AND w.sent_at IS NULL AND t.ended_at IS NULL)"
)


class DriveMixin:
    """The Driver's request queue and its stop-rule loop status. See
    "Explorations" in knowledge/prd/02-run-lifecycle.md for the decisions this implements.

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
        finding_ids: list[int] | None = None,
    ) -> dict: ...

    def agent_spawn(self, caller: str, agent_id: str, child_name: str) -> dict: ...

    def _drive_loop_status(self, run_id: int) -> dict[str, Any]:
        return loop_status(self.conn, run_id)

    def _oracle_agent_id(self, conn: sqlite3.Connection, run_id: int) -> str | None:
        row = conn.execute(
            "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' "
            "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["agent_id"] if row is not None else None

    def release_closed_driver(self, agent_id: str) -> bool:
        with self._release_tx() as conn:
            settled = conn.execute(_SETTLED_DRIVER, (agent_id,)).fetchone()
            if settled is None:
                return False
            self._release_agent(conn, agent_id, "exploration closed; final wake-up sent")
        return True

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

    def _flag_stop_rules(
        self, conn: sqlite3.Connection, run_id: int, status: dict
    ) -> list[dict[str, Any]]:
        flagged: list[dict[str, Any]] = []
        for entry in status["stops"]:
            head = f"[driver-stop] {entry['reason']}"
            already = conn.execute(
                "SELECT 1 FROM drive_stops WHERE run_id = ? AND reason = ? "
                "UNION SELECT 1 FROM directives WHERE run_id = ? AND source = 'driver' "
                "AND body = ?",
                (run_id, entry["reason"], run_id, head),
            ).fetchone()
            if already is not None:
                continue
            body = "\n".join([head, *entry["detail"]])
            cur = conn.execute(
                "INSERT INTO directives (run_id, source, sender_name, body) "
                "VALUES (?, 'driver', 'driver', ?)",
                (run_id, body),
            )
            targets = entry["targets"] or [{"fingerprint": None, "area": None}]
            conn.executemany(
                "INSERT INTO drive_stops (run_id, directive_id, kind, reason, fingerprint, area) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        cur.lastrowid,
                        entry["kind"],
                        entry["reason"],
                        target["fingerprint"],
                        target["area"],
                    )
                    for target in targets
                ],
            )
            flagged.append(entry)
        return flagged

    def _drive_notice(
        self,
        conn: sqlite3.Connection,
        request: dict,
        status: dict,
        flagged: list[dict[str, Any]],
        blocked: str,
    ) -> dict | None:
        ordinal = request["ordinal"]
        flagged = sorted(flagged, key=lambda entry: entry["kind"] != "stalled")
        if blocked:
            kind = "error"
            message = f"Driver blocked: exploration {ordinal} cannot continue: {blocked}"
        elif flagged:
            more = f" (+{len(flagged) - 1} more)" if len(flagged) > 1 else ""
            verb = "Driver loop ended" if flagged[0]["kind"] == "stalled" else "Driver stopped"
            summary = flagged[0]["summary"].removeprefix("the loop ended: ")
            head = notify.one_line(f"{verb}: {summary}", notify.MAX_CHARS - len(more))
            kind, message = "warning", head + more
        elif status["clean_latest"]:
            fixed = len(status["fingerprints"])
            kind, message = "done", f"Driver done: exploration {ordinal} clean, 0 open bugs"
            if fixed:
                message += f", {fixed} fixed this run"
        else:
            return None
        return notify.record(
            conn,
            request["run_id"],
            kind=kind,
            event_key=f"drive_done:{request['request_id']}",
            message=message,
            channels=self.settings.notify,
        )

    def _unavailable_directive(self, conn: sqlite3.Connection, run_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM directives WHERE run_id = ? AND source = 'driver' "
            "AND substr(body, 1, ?) = ? ORDER BY directive_id DESC LIMIT 1",
            (run_id, len(UNAVAILABLE_TAG), UNAVAILABLE_TAG),
        ).fetchone()
        return dict(row) if row is not None else None

    def _explorations_skipped(self, conn: sqlite3.Connection, run_id: int) -> dict | None:
        directive = self._unavailable_directive(conn, run_id)
        if directive is None or directive["state"] == "open" or directive["outcome"] != "declined":
            return None
        return directive

    def _block_run_finish_for_driver(self, run_id: int) -> None:
        if not agentfiles.driver_available(self.repo_root):
            return
        if self._explorations_skipped(self.conn, run_id) is not None:
            return
        status = self._drive_loop_status(run_id)
        if status["stopped"] or status["clean_latest"]:
            return
        if status["explorations_done"] == 0:
            raise LedgerError(
                "the host has a Driver available and no exploration has run yet; call "
                "drive_request before run_finish, or drive_unavailable when the Driver's "
                "servers fail to load"
            )
        raise LedgerError(
            "the host has a Driver available and no exploration has ended clean since the "
            "last fix; call drive_request again once every fix has finished"
        )

    def _findings_of(
        self, conn: sqlite3.Connection, run_id: int, finding_ids: list[int]
    ) -> list[sqlite3.Row]:
        rows = []
        for finding_id in finding_ids:
            row = conn.execute(
                "SELECT * FROM drive_findings WHERE finding_id = ? AND run_id = ?",
                (finding_id, run_id),
            ).fetchone()
            if row is None:
                raise LedgerError(f"finding {finding_id!r} is not a Driver finding of this run")
            rows.append(row)
        return rows

    def _require_finding_ids(self, conn: sqlite3.Connection, run_id: int) -> None:
        open_rows = open_findings(conn, run_id)
        if not open_rows:
            return
        listed = "; ".join(f"{f['finding_id']} {f['title']}" for f in open_rows)
        raise LedgerError(
            f"{len(open_rows)} Driver finding(s) are open: {listed}. Pass finding_ids=[...] "
            "with the ids this brief fixes, or finding_ids=[] when it fixes none"
        )

    def _block_fix_for_stops(
        self, conn: sqlite3.Connection, run_id: int, finding_ids: list[int]
    ) -> None:
        if not finding_ids:
            return
        live = self._drive_loop_status(run_id)["fingerprints"]
        for finding in self._findings_of(conn, run_id, finding_ids):
            fp, area = finding["fingerprint"], finding["area"]
            stopped = conn.execute(
                "SELECT directive_id, reason FROM drive_stops WHERE run_id = ? "
                "AND kind = 'finding' AND fingerprint = ? ORDER BY stop_id LIMIT 1",
                (run_id, fp),
            ).fetchone()
            if stopped is not None:
                raise LedgerError(
                    f"finding {finding['finding_id']} ({fp}) hit a stop rule and gets no more "
                    f"fixes: {stopped['reason']}. Its evidence is in directive "
                    f"{stopped['directive_id']} and in the user's notification"
                )
            if live.get(fp, {}).get("stop"):
                raise LedgerError(
                    f"finding {finding['finding_id']} ({fp}) reached a stop rule and gets no "
                    f"more fixes: {'; '.join(live[fp]['reasons'])}"
                )
            paused = conn.execute(
                "SELECT s.directive_id, s.reason FROM drive_stops s "
                "JOIN directives d ON d.directive_id = s.directive_id "
                "WHERE s.run_id = ? AND s.kind = 'pattern' AND d.state = 'open' "
                "AND (s.fingerprint = ? OR (s.area IS NOT NULL AND s.area = ?)) "
                "ORDER BY s.stop_id LIMIT 1",
                (run_id, fp, area),
            ).fetchone()
            if paused is not None:
                raise LedgerError(
                    f"fixes in area {area or 'unknown'} are paused: {paused['reason']}. "
                    f"Resolve directive {paused['directive_id']} first"
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
        unavailable = self._unavailable_directive(self.conn, c.run_id)
        if unavailable is not None and unavailable["state"] == "open":
            raise LedgerError(
                f"the Driver is unavailable: directive {unavailable['directive_id']} is open "
                f"({unavailable['body'].removeprefix(UNAVAILABLE_TAG).strip()}). Fix the cause "
                "and resolve it applied, or resolve it declined once the user decides to go "
                "without the Driver"
            )
        skipped = self._explorations_skipped(self.conn, c.run_id)
        if skipped is not None:
            raise LedgerError(
                f"the user decided to go without the Driver (directive "
                f"{skipped['directive_id']} was declined); skip exploration for this run"
            )
        open_row = self.conn.execute(
            "SELECT request_id FROM drive_requests WHERE run_id = ? AND state = 'open'",
            (c.run_id,),
        ).fetchone()
        if open_row is not None:
            raise LedgerError(
                f"exploration {open_row['request_id']} is still open; its Driver ends it with "
                "drive_done, or agent_release on a stuck Driver abandons it"
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

        with self._release_tx() as conn:
            for row in conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'driver' "
                "AND ended_at IS NULL",
                (c.run_id,),
            ).fetchall():
                self._release_agent(conn, row["agent_id"], "drive_request: its exploration ended")

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
        self.brief_create(caller, agent_id, child_name, "driver", model, body, finding_ids=[])
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

    def drive_done(
        self, caller: str, agent_id: str, request_id: int, blocked: str | None = None
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "driver")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        request = self._own_open_request(c, request_id)
        blocked = (blocked or "").strip()
        recorded = self.conn.execute(
            "SELECT 1 FROM drive_findings WHERE request_id = ?", (request_id,)
        ).fetchone()
        if blocked and recorded is None:
            raise LedgerError(
                "record what stopped the exploration with drive_issue first, so it never "
                "counts as clean; then call drive_done with blocked again"
            )

        with self._release_tx() as conn:
            conn.execute(
                f"UPDATE drive_requests SET state = 'done', done_at = {_NOW} WHERE request_id = ?",
                (request_id,),
            )
            # Computed after the state update, in the same transaction, so this exploration
            # counts as done for the wave-stall and fixes-causing-bugs checks.
            status = self._drive_loop_status(c.run_id)
            flagged = self._flag_stop_rules(conn, c.run_id, status)
            notice = self._drive_notice(conn, request, status, flagged, blocked)
            findings = conn.execute(
                "SELECT COUNT(*) AS n FROM drive_findings WHERE request_id = ?", (request_id,)
            ).fetchone()["n"]
            wakeup = self._owe_wakeup(
                conn,
                c,
                self._oracle_agent_id(conn, c.run_id),
                "drive_done",
                _done_pointer(request, status, flagged, blocked, findings),
            )
            # The Driver is released once this wake-up is sent (release_closed_driver);
            # releasing it here would stop its session before it can send it.
            if wakeup is None:
                self._release_agent(conn, c.agent_id, "drive_done")
        notify.deliver(notice, self.settings.notify)

        result = dict(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        )
        result["loop_status"] = status
        result["notification"] = notice
        result["next"] = self.next_step(wakeup)
        return result

    def drive_unavailable(self, caller: str, agent_id: str, reason: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle", "driver")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        reason = reason.strip()
        if not reason:
            raise LedgerError("drive_unavailable needs a reason: what failed to load")
        open_rows = _rows(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE run_id = ? AND state = 'open'", (c.run_id,)
            )
        )
        if c.role == "driver" and not any(r["agent_id"] == c.agent_id for r in open_rows):
            raise LedgerError(f"{caller!r} has no open exploration")

        with self._release_tx() as conn:
            directive = self._unavailable_directive(conn, c.run_id)
            if directive is None or directive["state"] != "open":
                cur = conn.execute(
                    "INSERT INTO directives (run_id, source, sender_name, body) "
                    "VALUES (?, 'driver', ?, ?)",
                    (c.run_id, c.name, f"{UNAVAILABLE_TAG} {reason}"),
                )
                directive_id = cur.lastrowid
            else:
                directive_id = directive["directive_id"]
            wakeup = None
            if c.role == "driver":
                wakeup = self._owe_wakeup(
                    conn,
                    c,
                    self._oracle_agent_id(conn, c.run_id),
                    "drive_unavailable",
                    _one_line(
                        f"Driver unavailable: {reason}. Directive {directive_id} waits in "
                        "the ledger, and the exploration is abandoned."
                    ),
                )
            for request in open_rows:
                conn.execute(
                    f"UPDATE drive_requests SET state = 'abandoned', done_at = {_NOW} "
                    "WHERE request_id = ?",
                    (request["request_id"],),
                )
                if request["agent_id"] is None:
                    continue
                if wakeup is not None and request["agent_id"] == c.agent_id:
                    continue
                self._release_agent(conn, request["agent_id"], "drive_unavailable")
            notice = notify.record(
                conn,
                c.run_id,
                kind="error",
                event_key=f"drive_unavailable:{directive_id}",
                message=f"Driver unavailable: {reason}. Explorations wait until it is fixed "
                "or you decide to go without the Driver",
                channels=self.settings.notify,
            )
        notify.deliver(notice, self.settings.notify)

        result = dict(
            self.conn.execute(
                "SELECT * FROM directives WHERE directive_id = ?", (directive_id,)
            ).fetchone()
        )
        result["abandoned"] = [r["request_id"] for r in open_rows]
        result["notification"] = notice
        result["next"] = self.next_step(wakeup)
        return result
