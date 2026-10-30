from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from . import graph
from .clock import parse_stamp, utcnow
from .db import dict_rows, ledger_path
from .host import LedgerHost
from .identity import ROLES, require_run, resolve
from .metrics import agent_cost
from .review import agent_name, agent_names, fix_briefs, latest_lead_review

_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

_AGENTS_WITH_ELAPSED = (
    "SELECT *, CAST((julianday(COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ', "
    "'now'))) - julianday(started_at)) * 86400000 AS INTEGER) AS elapsed_ms "
    "FROM agents WHERE run_id = ?"
)


def _run_cost(agents: list[dict]) -> float | None:
    costs = [agent_cost(a) for a in agents]
    return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.2f}"


def _duration(start: str | None, end: str | None) -> str:
    begin = parse_stamp(start)
    if begin is None:
        return "not started"
    finish = parse_stamp(end)
    seconds = int(((finish or utcnow()) - begin).total_seconds())
    text = _format_ms(seconds * 1000)
    return text if finish is not None else f"{text} so far"


def _format_ms(ms: int | None) -> str:
    seconds = int((ms or 0) / 1000)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m {rest % 60}s"


def _repo_line(raw: str | None) -> str:
    if not raw:
        return "(none)"
    check = json.loads(raw)
    tree = "clean" if check.get("clean") else f"dirty: {', '.join(check.get('dirty_paths') or [])}"
    parts = [
        f"on {check.get('branch')}",
        tree,
        f"base {check.get('base_branch')}",
        f"{check.get('ahead_of_base')} ahead, {check.get('behind_base')} behind",
    ]
    if check.get("advice"):
        parts.append(f"advice: {check['advice']}")
    return "; ".join(parts)


def _name_lookup(conn: sqlite3.Connection, run_id: int) -> Callable[[str | None], str]:
    names = agent_names(conn, run_id)

    def name_of(agent_id: str | None) -> str:
        if agent_id is not None and agent_id in names:
            return names[agent_id]
        return agent_name(conn, agent_id)

    return name_of


def _report_repo(run: sqlite3.Row) -> list[str]:
    return [
        "## Repo",
        f"- Branch: {run['branch'] or '(none)'}",
        f"- Last repo_check: {_repo_line(run['repo_check_json'])}",
        "",
    ]


def _report_phase_costs(phases: list[sqlite3.Row], agents: list[dict]) -> list[str]:
    phase_agents: dict[int, list[dict]] = {}
    oracle_agents: list[dict] = []
    for a in agents:
        if a["role"] == "oracle":
            oracle_agents.append(a)
        elif a["phase_id"] is not None:
            phase_agents.setdefault(a["phase_id"], []).append(a)
    lines = ["### Cost per phase"]
    lines.extend(
        f"- Phase {phase['name']}: {_money(_run_cost(phase_agents.get(phase['phase_id'], [])))}"
        for phase in phases
    )
    lines.append(f"- Oracle (run total): {_money(_run_cost(oracle_agents))}")
    lines.append("")
    return lines


def _report_agents(agents: list[dict]) -> list[str]:
    lines = ["## Agents"]
    lines.extend(
        f"- {a['name']} ({a['role']}, {a['model']}): "
        f"tokens in={a['input_tokens']} out={a['output_tokens']} "
        f"cache_read={a['cache_read_tokens']} cache_write={a['cache_write_tokens']}, "
        f"elapsed_ms={a['elapsed_ms']}, tool_uses={a['tool_uses']}, "
        f"context_overflow_count={a['context_overflow_count']}, "
        f"cost {_money(agent_cost(a))}"
        for a in agents
    )
    totals = {column: sum(a[column] or 0 for a in agents) for column in _TOKEN_COLUMNS}
    lines.append(
        f"- Run total: tokens in={totals['input_tokens']} out={totals['output_tokens']} "
        f"cache_read={totals['cache_read_tokens']} "
        f"cache_write={totals['cache_write_tokens']}, "
        f"cost {_money(_run_cost(agents))}"
    )
    lines.append(
        "- Costs are at list prices, from each response's usage in the agent's transcript."
    )
    return lines


class ReportMixin(LedgerHost):
    def report_build(self, caller: str, agent_id: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        run_id = require_run(c, "oracle")
        return self.write_report(run_id)

    def write_report(self, run_id: int) -> dict:
        conn = self.conn
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        agents = dict_rows(conn.execute(_AGENTS_WITH_ELAPSED, (run_id,)))
        phases = conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall()
        name_of = _name_lookup(conn, run_id)
        sections = (
            self._report_outcome(run),
            _report_repo(run),
            self._report_phases(phases),
            self._report_reviews(run_id),
            self._report_returns(run_id, name_of),
            self._report_explorations(run_id),
            self._report_open_items(run_id),
            self._report_decided_deferrals(run_id, name_of),
            self._report_disputes(run_id, name_of),
            self._report_departures(run_id, name_of),
            self._report_shortfalls(run_id),
            self._report_change_requests(run_id, name_of),
            self._report_overrides(run_id),
            self._report_graph_gaps(run_id, name_of),
            self._report_stale_files(run_id),
            self._report_directives(run_id),
            self._report_notifications(run_id),
            self._report_final_test_run(run_id),
            self._report_stage_times(phases, agents),
            self._report_returns_per_file(run_id),
            _report_phase_costs(phases, agents),
            _report_agents(agents),
        )
        text = "\n".join(line for section in sections for line in section) + "\n"
        return self._save_report(run_id, text)

    def _save_report(self, run_id: int, text: str) -> dict:
        records_dir = ledger_path(self.repo_root).parent
        records_dir.mkdir(parents=True, exist_ok=True)
        per_run_path = records_dir / f"report-{run_id}.md"
        report_path = records_dir / "report.md"
        for path in (per_run_path, report_path):
            with path.open("w", encoding="utf-8", newline="\n") as out:
                out.write(text)
        return {"path": str(report_path), "per_run_path": str(per_run_path), "text": text}

    def _phase_work_start(self, phase: sqlite3.Row) -> str | None:
        row = self.conn.execute(
            "SELECT MIN(started_at) AS at FROM agents WHERE phase_id = ? AND role = 'manager'",
            (phase["phase_id"],),
        ).fetchone()
        return row["at"] if row is not None and row["at"] else phase["started_at"]

    def _report_outcome(self, run: sqlite3.Row) -> list[str]:
        lines = [
            "# Run report",
            "",
            f"Outcome: {run['outcome'] or run['state']}",
            f"Duration: {_duration(run['started_at'], run['ended_at'])}",
        ]
        if run["state"] == "paused":
            lines.append(f"Paused: {self.pause_reason(run['run_id'])}")
        lines.append("")
        return lines

    def _report_phases(self, phases: list[sqlite3.Row]) -> list[str]:
        conn = self.conn
        lines: list[str] = []
        for phase in phases:
            lines.append(
                f"## Phase: {phase['name']} ({phase['state']}, "
                f"{_duration(self._phase_work_start(phase), phase['ended_at'])})"
            )
            for module in conn.execute(
                "SELECT * FROM modules WHERE phase_id = ? ORDER BY module_id",
                (phase["phase_id"],),
            ).fetchall():
                lines.append(f"### Module: {module['name']} ({module['state']})")
                lines.extend(
                    self._report_file_line(file_row)
                    for file_row in conn.execute(
                        "SELECT * FROM files WHERE module_id = ? ORDER BY file_id",
                        (module["module_id"],),
                    ).fetchall()
                )
            lines.append("")
        return lines

    def _report_file_line(self, file_row: sqlite3.Row) -> str:
        lead_review = latest_lead_review(self.conn, file_row["file_id"])
        scores = self._scores_for(lead_review["review_id"]) if lead_review is not None else {}
        floor_note = ""
        approved_handoff = self.conn.execute(
            "SELECT floor_pass_json FROM handoffs WHERE file_id = ? "
            "AND state = 'approved' ORDER BY handoff_id DESC LIMIT 1",
            (file_row["file_id"],),
        ).fetchone()
        if approved_handoff is not None and approved_handoff["floor_pass_json"]:
            dims = ", ".join(json.loads(approved_handoff["floor_pass_json"]))
            floor_note = f" -- floor pass: {dims} below target but at or above the floor"
        return f"- {file_row['path']} -- {file_row['state']} -- {scores}{floor_note}"

    def _report_reviews(self, run_id: int) -> list[str]:
        lines = ["## Manager and Oracle reviews"]
        for r in dict_rows(
            self.conn.execute(
                "SELECT r.* FROM reviews r JOIN phases p ON p.phase_id = r.phase_id "
                "WHERE p.run_id = ? AND r.kind IN ('manager', 'oracle') ORDER BY r.review_id",
                (run_id,),
            )
        ):
            scope = (
                f"module {r['module_id']}" if r["kind"] == "manager" else f"phase {r['phase_id']}"
            )
            lines.append(f"- [{r['kind']}] {scope} ({r['outcome']}): {r['notes']}")
            review_scores = json.loads(r["details_json"] or "{}").get("review_scores") or []
            if review_scores:
                parts = ", ".join(
                    f"{s['dimension']}={s['value']}"
                    + (f" ({s['reason']})" if s.get("reason") else "")
                    for s in review_scores
                )
                lines.append(f"  - Scores: {parts}")
        lines.append("")
        return lines

    def _report_returns(self, run_id: int, name_of: Callable[[str | None], str]) -> list[str]:
        lines = ["## Returns and fix attempts"]
        for a in dict_rows(
            self.conn.execute(
                "SELECT a.*, f.path, h.decided_by FROM attempts a "
                "JOIN files f ON f.file_id = a.file_id "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id "
                "LEFT JOIN handoffs h ON h.handoff_id = a.handoff_id "
                "WHERE p.run_id = ? ORDER BY a.attempt_id",
                (run_id,),
            )
        ):
            targeted = ", ".join(json.loads(a["targeted_json"] or "[]")) or "none named"
            lines.append(
                f"- {a['path']}, fix round {a['round']}, returned by "
                f"{name_of(a['decided_by'])} -- targeted: {targeted} -- "
                f"outcome: {a['outcome'] or 'pending'}"
            )
            lines.extend(f"  - {issue}" for issue in json.loads(a["issue_ids_json"] or "[]"))
        lines.append("")
        return lines

    def _report_explorations(self, run_id: int) -> list[str]:
        lines = ["## Explorations"]
        drive_requests = dict_rows(
            self.conn.execute(
                "SELECT * FROM drive_requests WHERE run_id = ? ORDER BY ordinal", (run_id,)
            )
        )
        if not drive_requests:
            lines.append("- No exploration ran.")
        for req in drive_requests:
            lines.extend(self._report_exploration(run_id, req))
        lines.extend(self._report_fixes(run_id))
        lines.extend(self._report_stop_rules(run_id))
        lines.append("")
        return lines

    def _report_exploration(self, run_id: int, req: dict) -> list[str]:
        lines = [
            f"### Exploration {req['ordinal']} (request {req['request_id']}, "
            f"{req['state']}): {req['focus']}"
        ]
        findings = dict_rows(
            self.conn.execute(
                "SELECT * FROM drive_findings WHERE request_id = ? ORDER BY finding_id",
                (req["request_id"],),
            )
        )
        if not findings:
            lines.append("- No finding.")
            return lines
        later_fingerprints = {
            row["fingerprint"]
            for row in self.conn.execute(
                "SELECT DISTINCT df.fingerprint FROM drive_findings df "
                "JOIN drive_requests dr ON dr.request_id = df.request_id "
                "WHERE dr.run_id = ? AND dr.ordinal > ?",
                (run_id, req["ordinal"]),
            )
        }
        for f in findings:
            fix = (
                "still open in a later exploration"
                if f["fingerprint"] in later_fingerprints
                else "not seen again"
            )
            lines.append(
                f"- [{f['severity']}] finding {f['finding_id']}: {f['title']} "
                f"({f['fingerprint']}), area {f['area'] or 'unknown'} -- {fix}"
            )
        return lines

    def _report_fixes(self, run_id: int) -> list[str]:
        fixes = fix_briefs(self.conn, run_id)
        if not fixes:
            return []
        lines = ["### Fixes"]
        for fix in fixes:
            named = "; ".join(f"finding {f['finding_id']} {f['title']}" for f in fix["findings"])
            lines.append(f"- {fix['child_name']} ({fix['child_role']}): {named}")
        return lines

    def _report_stop_rules(self, run_id: int) -> list[str]:
        stop_directives = dict_rows(
            self.conn.execute(
                "SELECT * FROM directives WHERE run_id = ? AND source = 'driver' "
                "AND substr(body, 1, 13) = '[driver-stop]' ORDER BY directive_id",
                (run_id,),
            )
        )
        if not stop_directives:
            return []
        lines = ["### Stop rules"]
        for d in stop_directives:
            head, *detail = d["body"].removeprefix("[driver-stop] ").splitlines()
            lines.append(f"- {head} -> {d['outcome'] or d['state']}")
            lines.extend(f"  - {line.removeprefix('- ')}" for line in detail)
        return lines

    def _report_open_items(self, run_id: int) -> list[str]:
        conn = self.conn
        lines = ["## Open items"]
        lines.extend(
            f"- Deferral #{d['deferral_id']}: {d['reason']}"
            for d in dict_rows(
                conn.execute(
                    "SELECT * FROM deferrals WHERE run_id = ? AND state = 'open'", (run_id,)
                )
            )
        )
        issues = dict_rows(conn.execute("SELECT * FROM issues WHERE run_id = ?", (run_id,)))
        for round_no in sorted({i["round"] for i in issues}):
            lines.append(f"- Round {round_no}:")
            for issue in (i for i in issues if i["round"] == round_no):
                detail = f" -- {issue['body']}" if issue["body"] else ""
                lines.append(
                    f"  - Issue #{issue['issue_id']} ({issue['state']}, "
                    f"{issue['attempts']} attempts): {issue['title']}{detail}"
                )
                if issue["resolution"]:
                    lines.append(f"    - Resolution: {issue['resolution']}")
        lines.append("")
        return lines

    def _report_decided_deferrals(
        self, run_id: int, name_of: Callable[[str | None], str]
    ) -> list[str]:
        lines = ["## Decided deferrals"]
        for d in dict_rows(
            self.conn.execute(
                "SELECT d.*, f.path FROM deferrals d LEFT JOIN files f ON f.file_id = d.file_id "
                "WHERE d.run_id = ? AND d.state IN ('agreed', 'denied') ORDER BY d.deferral_id",
                (run_id,),
            )
        ):
            where = f" on {d['path']}" if d["path"] else ""
            lines.append(
                f"- Deferral #{d['deferral_id']}{where}: {d['reason']} -- proposed by "
                f"{name_of(d['proposed_by'])}, decided by "
                f"{name_of(d['decided_by'])} ({d['state']}): {d['decision_reason']}"
            )
        lines.append("")
        return lines

    def _report_disputes(self, run_id: int, name_of: Callable[[str | None], str]) -> list[str]:
        lines = ["## Disputes"]
        for d in dict_rows(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE run_id = ? AND arbiter_agent_id IS NOT NULL "
                "ORDER BY deferral_id",
                (run_id,),
            )
        ):
            parties = ", ".join(json.loads(d["parties_json"] or "[]"))
            outcome = (
                f"{d['state']} by {name_of(d['decided_by'])}: {d['decision_reason']}"
                if d["state"] != "open"
                else "open"
            )
            lines.append(
                f"- Dispute #{d['deferral_id']} ({d['kind']}) between "
                f"{name_of(d['proposed_by'])} and {parties}, arbiter "
                f"{name_of(d['arbiter_agent_id'])}: {d['reason']} -> {outcome}"
            )
        lines.append("")
        return lines

    def _report_departures(self, run_id: int, name_of: Callable[[str | None], str]) -> list[str]:
        lines = ["## Departures"]
        for dep in dict_rows(
            self.conn.execute(
                "SELECT d.*, f.path FROM departures d LEFT JOIN files f ON f.file_id = d.file_id "
                "WHERE d.run_id = ? AND d.kind = 'departure' ORDER BY d.departure_id",
                (run_id,),
            )
        ):
            where = f" on {dep['path']}" if dep["path"] else ""
            lines.append(
                f"- Departure #{dep['departure_id']}{where}, recorded by "
                f"{name_of(dep['agent_id'])}: {dep['body']}"
            )
            lines.extend(self._report_departure_decisions(dep["departure_id"], name_of))
            final = dep["state"]
            if dep["state"] == "reworked" and dep["reworked_by_handoff_id"] is not None:
                final = f"reworked, approved in handoff {dep['reworked_by_handoff_id']}"
            elif dep["state"] == "superseded":
                final = f"superseded: {dep['decision_reason']}"
            elif dep["level"]:
                final = f"{dep['state']}, waits on the {dep['level']}"
            lines.append(f"  - Final state: {final}")
        lines.append("")
        return lines

    def _report_departure_decisions(
        self, departure_id: int, name_of: Callable[[str | None], str]
    ) -> list[str]:
        lines = []
        for decision in dict_rows(
            self.conn.execute(
                "SELECT * FROM departure_decisions WHERE departure_id = ? ORDER BY decision_id",
                (departure_id,),
            )
        ):
            verb = "agreed" if decision["decision"] == "agree" else "pushed back"
            solution = f" -- solution: {decision['solution']}" if decision["solution"] else ""
            lines.append(
                f"  - {decision['role']} {name_of(decision['agent_id'])} {verb}: "
                f"{decision['reason']}{solution}"
            )
        return lines

    def _report_shortfalls(self, run_id: int) -> list[str]:
        lines = ["## Shortfalls"]
        lines.extend(
            f"- Shortfall #{sf['departure_id']}: {sf['body']}"
            for sf in dict_rows(
                self.conn.execute(
                    "SELECT * FROM departures WHERE run_id = ? AND kind = 'shortfall' "
                    "ORDER BY departure_id",
                    (run_id,),
                )
            )
        )
        lines.append("")
        return lines

    def _report_change_requests(
        self, run_id: int, name_of: Callable[[str | None], str]
    ) -> list[str]:
        lines = ["## Change requests"]
        for cr in dict_rows(
            self.conn.execute(
                "SELECT * FROM change_requests WHERE run_id = ? ORDER BY cr_id", (run_id,)
            )
        ):
            lines.append(
                f"- CR #{cr['cr_id']} for {cr['path']} ({cr['state']}), from "
                f"{name_of(cr['from_agent_id'])} to "
                f"{name_of(cr['to_agent_id'])}: {cr['body']}"
            )
            if cr["decision_reason"]:
                lines.append(f"  - Decision: {cr['decision_reason']}")
            if cr["completion_notes"]:
                lines.append(f"  - Work done: {cr['completion_notes']}")
            if cr["evidence_test_run_id"] is not None:
                lines.extend(self._report_evidence(cr["evidence_test_run_id"]))
            if cr["verify_notes"]:
                lines.append(f"  - Verification: {cr['verify_notes']}")
        lines.append("")
        return lines

    def _report_evidence(self, test_run_id: int) -> list[str]:
        test_run = self.conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ?", (test_run_id,)
        ).fetchone()
        if test_run is None:
            return []
        return [
            f"  - Evidence: test run {test_run['test_run_id']} ({test_run['scope']}), "
            f"{test_run['passed']} passed, {test_run['failed']} failed, "
            f"exit {test_run['exit_code']}"
        ]

    def _report_overrides(self, run_id: int) -> list[str]:
        lines = ["## Overrides"]
        lines.extend(
            f"- {o['rule']} for {o['target_agent_name']} on {o['target']}: {o['reason']}"
            for o in dict_rows(
                self.conn.execute("SELECT * FROM overrides WHERE run_id = ?", (run_id,))
            )
        )
        lines.append("")
        return lines

    def _report_graph_gaps(self, run_id: int, name_of: Callable[[str | None], str]) -> list[str]:
        gaps = dict_rows(
            self.conn.execute(
                "SELECT * FROM graph_gaps WHERE run_id = ? ORDER BY gap_id", (run_id,)
            )
        )
        anchored = graph.anchored_paths(
            self.repo_root, sorted({g["path"] for g in gaps if g["path"]})
        )
        lines = ["## Graph gaps"]
        for gap in gaps:
            found = json.loads(gap["results_json"] or "[]")
            scope = f" in {gap['path']}" if gap["path"] else ""
            mapped = " (the graph anchors this file)" if gap["path"] in anchored else ""
            lines.append(
                f"- {name_of(gap['agent_id'])} ran {gap['tool']} "
                f"{gap['pattern']!r}{scope}{mapped}: {len(found)} path(s)"
                + (f", {', '.join(found[:5])}" if found else "")
            )
        lines.append("")
        return lines

    def _report_stale_files(self, run_id: int) -> list[str]:
        claimed = {
            path
            for row in self.conn.execute(
                "SELECT f.path, f.test_path FROM files f JOIN modules m USING (module_id) "
                "JOIN phases p USING (phase_id) WHERE p.run_id = ?",
                (run_id,),
            )
            for path in (row["path"], row["test_path"])
            if path
        }
        lines = ["## Stale mapped files"]
        for path, node_ids in graph.stale_files(self.repo_root).items():
            owner = " (a file of this run)" if path in claimed else ""
            lines.append(
                f"- {path}{owner}: changed since its baseline; nodes {', '.join(node_ids)}"
            )
        lines.append("")
        return lines

    def _report_directives(self, run_id: int) -> list[str]:
        lines = ["## Directives"]
        for d in dict_rows(
            self.conn.execute("SELECT * FROM directives WHERE run_id = ?", (run_id,))
        ):
            head, *rest = d["body"].splitlines() or [""]
            lines.append(f"- [{d['source']}] {head} -> {d['outcome'] or d['state']}")
            if d["source"] != "driver":
                lines.extend(f"  {line}" for line in rest)
        lines.append("")
        return lines

    def _report_notifications(self, run_id: int) -> list[str]:
        conn = self.conn
        lines = ["## Notifications to the user"]
        for d in dict_rows(
            conn.execute(
                "SELECT * FROM directives WHERE run_id = ? AND question IS NOT NULL "
                "ORDER BY directive_id",
                (run_id,),
            )
        ):
            lines.append(f"- Directive #{d['directive_id']} ({d['source']}): {d['question']}")
            lines.extend(
                f"  - Reply from {reply['sender_name'] or reply['source']}: {reply['body']}"
                for reply in dict_rows(
                    conn.execute(
                        "SELECT * FROM directives WHERE reply_to = ? ORDER BY directive_id",
                        (d["directive_id"],),
                    )
                )
            )
        lines.extend(
            f"- [{n['kind']}] {n['message']} ({n['created_at']})"
            for n in dict_rows(
                conn.execute(
                    "SELECT * FROM notifications WHERE run_id = ? ORDER BY notification_id",
                    (run_id,),
                )
            )
        )
        lines.append("")
        return lines

    def _report_final_test_run(self, run_id: int) -> list[str]:
        final_run = self.conn.execute(
            "SELECT * FROM test_runs WHERE run_id = ? AND scope = 'full' AND state != 'running' "
            "ORDER BY test_run_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if final_run is None:
            summary = "- No full-suite test run is recorded."
        else:
            summary = (
                f"- {final_run['passed']} passed, {final_run['failed']} failed, "
                f"{final_run['skipped']} skipped, exit {final_run['exit_code']}"
            )
        return ["## Final test run", summary, ""]

    def _report_stage_times(self, phases: list[sqlite3.Row], agents: list[dict]) -> list[str]:
        lines = ["## Measures", "### Time per stage"]
        lines.extend(
            f"- Phase {phase['name']} working time: "
            f"{_duration(self._phase_work_start(phase), phase['ended_at'])}"
            for phase in phases
        )
        role_ms: dict[str, int] = dict.fromkeys(ROLES, 0)
        for a in agents:
            if a["role"] in role_ms:
                role_ms[a["role"]] += a["elapsed_ms"] or 0
        lines.extend(f"- {role} agent time total: {_format_ms(ms)}" for role, ms in role_ms.items())
        lines.append("")
        return lines

    def _report_returns_per_file(self, run_id: int) -> list[str]:
        returns = dict_rows(
            self.conn.execute(
                "SELECT f.path, COUNT(*) AS n FROM attempts a "
                "JOIN files f ON f.file_id = a.file_id "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id "
                "WHERE p.run_id = ? GROUP BY f.path ORDER BY f.path",
                (run_id,),
            )
        )
        lines = ["### Returns per file"]
        if not returns:
            lines.append("- No file was returned.")
        lines.extend(f"- {r['path']}: {r['n']} return(s)" for r in returns)
        lines.append("")
        return lines
