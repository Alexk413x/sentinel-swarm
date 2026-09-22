from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ROLES = ("oracle", "manager", "lead", "coder")

_CHILD_ROLE = {
    "oracle": "manager",
    "manager": "lead",
    "lead": "coder",
}


class LedgerError(Exception):
    pass


@dataclass
class Caller:
    agent_id: str
    name: str
    role: str
    run_id: int | None
    phase_id: int | None
    module_id: int | None
    file_id: int | None
    parent_agent_id: str | None


def child_role_of(role: str) -> str | None:
    return _CHILD_ROLE.get(role)


def resolve(conn: sqlite3.Connection, caller: str, agent_id: str | None) -> Caller:
    if not agent_id:
        raise LedgerError("agent_id is required")
    row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row is None:
        raise LedgerError(f"no agent is registered for agent_id {agent_id!r}")
    if row["ended_at"] is not None:
        raise LedgerError(f"agent {agent_id!r} has already ended")
    if row["name"] != caller:
        raise LedgerError(f"caller {caller!r} does not match agent_id {agent_id!r}")
    return Caller(
        agent_id=row["agent_id"],
        name=row["name"],
        role=row["role"],
        run_id=row["run_id"],
        phase_id=row["phase_id"],
        module_id=row["module_id"],
        file_id=row["file_id"],
        parent_agent_id=row["parent_agent_id"],
    )


def require_role(c: Caller, *roles: str) -> None:
    if c.role not in roles:
        raise LedgerError(f"role {c.role!r} may not call this; requires one of {roles}")
