from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ROLES = ("oracle", "manager", "lead", "coder", "driver")

# The Oracle starts a Driver alongside its Managers; every other role still starts
# only the one role below it.
_CHILD_ROLES: dict[str, tuple[str, ...]] = {
    "oracle": ("manager", "driver"),
    "manager": ("lead",),
    "lead": ("coder",),
}

_CHANGE_REQUEST_TOOLS = frozenset({"cr_open", "cr_accept", "cr_complete", "cr_verify", "cr_list"})

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "oracle": frozenset(
        {
            "ledger_info",
            "run_start",
            "run_status",
            "run_pause",
            "phase_resume",
            "run_finish",
            "profile_set",
            "repo_check",
            "repo_branch_create",
            "guidelines_set",
            "guidelines_get",
            "phase_add",
            "phase_update",
            "plan_unlocked",
            "brief_create",
            "brief_get",
            "agent_spawn",
            "agent_resume",
            "agent_release",
            "message_inbox",
            "directive_submit",
            "directive_inbox",
            "directive_resolve",
            "override_grant",
            "issue_open",
            "issue_list",
            "issue_close",
            "idea_record",
            "events",
            "tests_run",
            "phase_review",
            "agreement_decide",
            "departure_record",
            "departure_decide",
            "shortfall_record",
            "status_tree",
            "report_build",
            "analytics_query",
            "drive_request",
            "drive_unavailable",
            *_CHANGE_REQUEST_TOOLS,
        }
    ),
    "manager": frozenset(
        {
            "ledger_info",
            "run_status",
            "guidelines_get",
            "phase_update",
            "module_add",
            "brief_create",
            "brief_get",
            "brief_ack",
            "agent_spawn",
            "agent_resume",
            "agent_release",
            "who_owns",
            "message_post",
            "message_inbox",
            "issue_open",
            "issue_list",
            "issue_close",
            "idea_record",
            "issue_escalate",
            "tests_run",
            "module_review",
            "deferral_propose",
            "agreement_decide",
            "departure_record",
            "departure_decide",
            "shortfall_record",
            "status_tree",
            *_CHANGE_REQUEST_TOOLS,
        }
    ),
    "lead": frozenset(
        {
            "ledger_info",
            "run_status",
            "guidelines_get",
            "brief_create",
            "brief_get",
            "brief_ack",
            "agent_spawn",
            "agent_resume",
            "agent_release",
            "claim_file",
            "release_file",
            "message_post",
            "message_inbox",
            "issue_list",
            "issue_close",
            "tests_run",
            "score_record",
            "review_compare",
            "approve",
            "return_work",
            "attempt_record",
            "accept_incomplete",
            "deferral_propose",
            "agreement_decide",
            "departure_record",
            "departure_decide",
            "shortfall_record",
            "status_tree",
            *_CHANGE_REQUEST_TOOLS,
        }
    ),
    "coder": frozenset(
        {
            "ledger_info",
            "brief_get",
            "brief_ack",
            "guidelines_get",
            "who_owns",
            "agent_resume",
            "message_inbox",
            "tests_run",
            "graph_upsert",
            "score_record",
            "handoff_submit",
            "version_restore",
            "issue_open",
            "issue_list",
            "idea_record",
            "deferral_propose",
            "departure_record",
            "shortfall_record",
            *_CHANGE_REQUEST_TOOLS,
        }
    ),
    "driver": frozenset(
        {
            "ledger_info",
            "brief_get",
            "brief_ack",
            "guidelines_get",
            "agent_resume",
            "message_inbox",
            "drive_issue",
            "drive_checkin",
            "drive_done",
            "drive_unavailable",
        }
    ),
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


def child_roles_of(role: str) -> tuple[str, ...]:
    return _CHILD_ROLES.get(role, ())


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


def require_role_tool(conn: sqlite3.Connection, tool: str, agent_id: str | None) -> None:
    if not agent_id:
        return
    row = conn.execute(
        "SELECT role FROM agents WHERE agent_id = ? AND ended_at IS NULL", (agent_id,)
    ).fetchone()
    if row is None or tool in ROLE_TOOLS.get(row["role"], frozenset()):
        return
    raise LedgerError(f"the {row['role']} role may not call {tool}")
