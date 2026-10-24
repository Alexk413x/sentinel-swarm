from __future__ import annotations

import secrets
import sqlite3

from .db import NOW, dict_rows, in_clause, row_dict, write_tx
from .host import LedgerHost
from .identity import Caller, LedgerError, require_role, resolve

_DIRECTIVE_SOURCES = ("user_chat", "outside_session", "skill", "watchdog", "driver")
_DIRECTIVE_OUTCOMES = ("applied", "scheduled", "declined", "needs_user")
MESSAGE_BODY_MAX = 32_000
INBOX_BODY_BUDGET = 40_000
INBOX_CLAIM_SECONDS = 120
_MESSAGE_COLUMNS = "message_id, run_id, from_name, to_name, body, read_at, created_at"
_UNCLAIMED = (
    f"(claim_id IS NULL OR claimed_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',"
    f"'-{INBOX_CLAIM_SECONDS} seconds'))"
)


class MessagesMixin(LedgerHost):
    def message_post(self, caller: str, agent_id: str, to_name: str, body: str) -> dict:
        if len(body) > MESSAGE_BODY_MAX:
            raise LedgerError(
                f"the body has {len(body)} characters; a message takes at most "
                f"{MESSAGE_BODY_MAX}. Store the detail in its ledger record and point at it"
            )
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            names = {
                row["name"]
                for row in conn.execute("SELECT name FROM agents WHERE run_id = ?", (c.run_id,))
            }
            if to_name not in names:
                raise LedgerError(
                    f"no agent named {to_name!r} in this run; registered names: {sorted(names)}"
                )
            allowed = {row["name"] for row in self.message_peers(conn, c)}
            if to_name not in allowed:
                raise LedgerError(
                    f"{c.name!r} posts only to its parent, its children, and its siblings: "
                    f"{sorted(allowed)}. Reach {to_name!r} through that chain; a change to a "
                    "file goes through cr_open"
                )
            cur = conn.execute(
                "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                (c.run_id, c.name, to_name, body),
            )
            message_id = cur.lastrowid
            recipient = conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND name = ? AND ended_at IS NULL",
                (c.run_id, to_name),
            ).fetchone()
            wakeup = self._owe_wakeup(
                conn,
                c,
                recipient["agent_id"] if recipient is not None else None,
                "message_post",
                f"Message {message_id} from {c.name} is waiting in the ledger; "
                "read it with message_inbox.",
            )

        message = row_dict(self.conn, "messages", "message_id", message_id)
        message["next"] = self.next_step(wakeup)
        return message

    def message_peers(self, conn: sqlite3.Connection, c: Caller) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND agent_id != ? AND (agent_id = ? "
            "OR parent_agent_id = ? OR (? IS NOT NULL AND parent_agent_id = ?))",
            (
                c.run_id,
                c.agent_id,
                c.parent_agent_id,
                c.agent_id,
                c.parent_agent_id,
                c.parent_agent_id,
            ),
        ).fetchall()

    @staticmethod
    def _inbox_batch(
        conn: sqlite3.Connection, run_id: int | None, name: str
    ) -> tuple[list[int], int]:
        rows = conn.execute(
            "SELECT message_id, LENGTH(body) AS size FROM messages "
            f"WHERE run_id = ? AND to_name = ? AND read_at IS NULL AND {_UNCLAIMED} "
            "ORDER BY message_id",
            (run_id, name),
        ).fetchall()
        ids: list[int] = []
        used = 0
        for row in rows:
            # The first message always goes out, so one that the ledger wrote over the
            # budget cannot block the inbox.
            if ids and used + row["size"] > INBOX_BODY_BUDGET:
                break
            ids.append(row["message_id"])
            used += row["size"]
        return ids, len(rows) - len(ids)

    @staticmethod
    def _messages(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        return dict_rows(
            conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE message_id IN ({in_clause(ids)}) "
                "ORDER BY message_id",
                ids,
            )
        )

    def message_inbox(self, caller: str, agent_id: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            ids, remaining = self._inbox_batch(conn, c.run_id, c.name)
            if ids:
                conn.execute(
                    f"UPDATE messages SET read_at = {NOW} WHERE message_id IN ({in_clause(ids)})",
                    ids,
                )
            messages = self._messages(conn, ids)

        return {"messages": messages, "remaining": remaining}

    def inbox_take(self, c: Caller) -> dict:
        with write_tx(self.conn) as conn:
            ids, remaining = self._inbox_batch(conn, c.run_id, c.name)
            if not ids:
                return {"claim": None, "messages": [], "remaining": remaining}
            claim = secrets.token_hex(8)
            conn.execute(
                f"UPDATE messages SET claim_id = ?, claimed_at = {NOW} "
                f"WHERE message_id IN ({in_clause(ids)})",
                (claim, *ids),
            )
            messages = self._messages(conn, ids)
        for message in messages:
            del message["read_at"]
        return {"claim": claim, "messages": messages, "remaining": remaining}

    def inbox_settle(self, c: Caller, claim: str, *, read: bool) -> int:
        with write_tx(self.conn) as conn:
            change = f"read_at = {NOW}" if read else "claim_id = NULL, claimed_at = NULL"
            cur = conn.execute(
                f"UPDATE messages SET {change} WHERE claim_id = ? AND run_id = ? "
                "AND to_name = ? AND read_at IS NULL",
                (claim, c.run_id, c.name),
            )
        return cur.rowcount

    def directive_submit(
        self, source: str, sender_name: str | None, body: str, reply_to: int | None = None
    ) -> dict:
        if source not in _DIRECTIVE_SOURCES:
            raise LedgerError(
                f"unknown directive source {source!r}; use one of {sorted(_DIRECTIVE_SOURCES)}. "
                "An agent that needs the Oracle uses message_post"
            )
        with write_tx(self.conn) as conn:
            run = self._active_run(conn)
            if reply_to is not None:
                parent = conn.execute(
                    "SELECT 1 FROM directives WHERE directive_id = ? AND run_id = ?",
                    (reply_to, run["run_id"]),
                ).fetchone()
                if parent is None:
                    raise LedgerError(f"reply_to {reply_to!r} names no directive of this run")
                # Resolves any open directive it answers, not only a needs_user one: the
                # outcome column is left as-is, so a needs_user question stays on the row.
                conn.execute(
                    f"UPDATE directives SET state = 'resolved', resolved_at = {NOW} "
                    "WHERE directive_id = ? AND state = 'open'",
                    (reply_to,),
                )
            cur = conn.execute(
                "INSERT INTO directives (run_id, source, sender_name, body, reply_to) "
                "VALUES (?, ?, ?, ?, ?)",
                (run["run_id"], source, sender_name, body, reply_to),
            )
            directive_id = cur.lastrowid

        return row_dict(self.conn, "directives", "directive_id", directive_id)

    def directive_inbox(self, caller: str, agent_id: str) -> list[dict]:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            conn.execute(
                f"UPDATE directives SET notified_at = {NOW} WHERE run_id = ? "
                "AND state = 'open' AND notified_at IS NULL",
                (c.run_id,),
            )
            return dict_rows(
                conn.execute(
                    "SELECT * FROM directives WHERE run_id = ? AND state = 'open' "
                    "ORDER BY directive_id",
                    (c.run_id,),
                )
            )

    def watch_armed(self, timeout_ms: int | None) -> dict:
        expires = (
            f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+{timeout_ms / 1000:.3f} seconds')"
            if timeout_ms is not None and timeout_ms > 0
            else "NULL"
        )
        with write_tx(self.conn) as conn:
            run = self._active_run(conn)
            conn.execute(
                f"UPDATE runs SET watch_heartbeat_at = {NOW}, watch_expires_at = {expires} "
                "WHERE run_id = ?",
                (run["run_id"],),
            )
        return row_dict(self.conn, "runs", "run_id", run["run_id"])

    def directive_resolve(
        self, caller: str, agent_id: str, directive_id: int, outcome: str, resolution: str
    ) -> dict:
        if outcome not in _DIRECTIVE_OUTCOMES:
            raise LedgerError(
                f"unknown directive outcome {outcome!r}; use one of {list(_DIRECTIVE_OUTCOMES)}"
            )
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            if (
                conn.execute(
                    "SELECT 1 FROM directives WHERE directive_id = ?", (directive_id,)
                ).fetchone()
                is None
            ):
                raise LedgerError(f"unknown directive_id {directive_id!r}")
            if outcome == "needs_user":
                conn.execute(
                    "UPDATE directives SET state = 'open', outcome = ?, resolution = ?, "
                    "resolved_at = NULL, question = ? WHERE directive_id = ?",
                    (outcome, resolution, resolution, directive_id),
                )
            else:
                conn.execute(
                    f"UPDATE directives SET state = 'resolved', outcome = ?, resolution = ?, "
                    f"resolved_at = {NOW} WHERE directive_id = ?",
                    (outcome, resolution, directive_id),
                )

        return row_dict(self.conn, "directives", "directive_id", directive_id)
