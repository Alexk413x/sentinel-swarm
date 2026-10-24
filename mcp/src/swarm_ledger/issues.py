from __future__ import annotations

import sqlite3

from .db import dict_rows, row_dict, write_tx
from .host import LedgerHost
from .identity import Caller, LedgerError, parent_chain, resolve
from .review import ISSUE_OPEN_ROUND, escalation_target, latest_lead_review


class IssuesMixin(LedgerHost):
    def issue_open(self, caller: str, agent_id: str, file_id: int, title: str, body: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            round_ = ISSUE_OPEN_ROUND.get(c.role, 1)
            cur = conn.execute(
                "INSERT INTO issues (run_id, file_id, opened_by_agent_id, title, body, state, "
                "round) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (c.run_id, file_id, c.agent_id, title, body, round_),
            )
            issue_id = cur.lastrowid

        return row_dict(self.conn, "issues", "issue_id", issue_id)

    def issue_list(self, caller: str, agent_id: str, file_id: int | None = None) -> list[dict]:
        c = resolve(self.conn, caller, agent_id)
        if file_id is not None:
            rows = dict_rows(
                self.conn.execute(
                    "SELECT * FROM issues WHERE run_id = ? AND file_id = ? ORDER BY issue_id",
                    (c.run_id, file_id),
                )
            )
        else:
            rows = dict_rows(
                self.conn.execute(
                    "SELECT * FROM issues WHERE run_id = ? ORDER BY issue_id", (c.run_id,)
                )
            )
        if c.role != "lead":
            return rows
        return [row for row in rows if not self._hidden_from_lead(c, row)]

    def _lead_scored_current_handoff(self, file_id: int) -> bool:
        handoff = self.conn.execute(
            "SELECT created_at FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        since = handoff["created_at"] if handoff is not None else None
        return latest_lead_review(self.conn, file_id, since) is not None

    def _opened_by_self_review(self, row: dict) -> bool:
        if row["dimension"] is None or row["criterion"] is None:
            return False
        opener = self.conn.execute(
            "SELECT role FROM agents WHERE agent_id = ?", (row["opened_by_agent_id"],)
        ).fetchone()
        return opener is not None and opener["role"] == "coder"

    def _hidden_from_lead(self, c: Caller, row: dict) -> bool:
        # A Lead scores blind: a self-review issue is hidden from that Lead's own view of
        # its own module's file until the Lead records its own score for the handoff.
        if row["file_id"] is None:
            return False
        file_row = self.conn.execute(
            "SELECT module_id FROM files WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        if file_row is None or file_row["module_id"] != c.module_id:
            return False
        if self._lead_scored_current_handoff(row["file_id"]):
            return False
        return self._opened_by_self_review(row)

    def idea_record(
        self, caller: str, agent_id: str, issue_id: int, body: str, outcome: str
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            cur = conn.execute(
                "INSERT INTO ideas (issue_id, agent_id, body, outcome) VALUES (?, ?, ?, ?)",
                (issue_id, c.agent_id, body, outcome),
            )
            idea_id = cur.lastrowid

        return row_dict(self.conn, "ideas", "idea_id", idea_id)

    def _issue_chain(self, conn: sqlite3.Connection, issue: sqlite3.Row) -> list[sqlite3.Row]:
        anchor = None
        if issue["file_id"] is not None:
            owner = conn.execute(
                "SELECT owner_agent_id FROM files WHERE file_id = ?", (issue["file_id"],)
            ).fetchone()
            if owner is not None and owner["owner_agent_id"] is not None:
                anchor = conn.execute(
                    "SELECT * FROM agents WHERE run_id = ? AND (agent_id = ? OR name = ?) "
                    "ORDER BY ended_at IS NOT NULL LIMIT 1",
                    (issue["run_id"], owner["owner_agent_id"], owner["owner_agent_id"]),
                ).fetchone()
        if anchor is None:
            anchor = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (issue["opened_by_agent_id"],)
            ).fetchone()
        return list(parent_chain(conn, anchor["agent_id"])) if anchor is not None else []

    def issue_escalate(self, caller: str, agent_id: str, issue_id: int) -> dict:
        wakeup = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown issue_id {issue_id!r}")
            chain = self._issue_chain(conn, row)
            if c.agent_id not in {a["agent_id"] for a in chain}:
                raise LedgerError(
                    f"{c.name!r} may not escalate issue {issue_id!r}; only its owner and the "
                    f"owner's parent chain may: {[a['name'] for a in chain]}"
                )
            next_round = row["round"] + 1
            if next_round > self.settings.escalation.rounds:
                raise LedgerError(f"issue {issue_id!r} is already at the maximum escalation round")
            target = escalation_target(chain, next_round)
            conn.execute(
                "UPDATE issues SET round = ?, attempts = 0, escalated_to = ? WHERE issue_id = ?",
                (next_round, target["name"], issue_id),
            )
            pointer = (
                f"Issue {issue_id} is escalated to you for round {next_round}. "
                "Read it with issue_list."
            )
            conn.execute(
                "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                (row["run_id"], c.name, target["name"], pointer),
            )
            wakeup = self._owe_wakeup(conn, c, target["agent_id"], "issue_escalate", pointer)

        result = row_dict(self.conn, "issues", "issue_id", issue_id)
        result["next"] = self.next_step(wakeup)
        return result
