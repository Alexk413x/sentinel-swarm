from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_sessions import Ctx, FakeClaude, _bootstrap, _row, _spawn_coder, host

from swarm_ledger import rekey, sessions
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events, run_event
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

__all__ = ["host"]

AGENT_ID_COLUMNS = [
    ("agent_events", "agent_id"),
    ("agents", "agent_id"),
    ("agents", "parent_agent_id"),
    ("briefs", "acked_by_agent_id"),
    ("briefs", "parent_agent_id"),
    ("change_requests", "from_agent_id"),
    ("change_requests", "to_agent_id"),
    ("deferrals", "arbiter_agent_id"),
    ("deferrals", "decided_by"),
    ("deferrals", "proposed_by"),
    ("departure_decisions", "agent_id"),
    ("departures", "agent_id"),
    ("departures", "decided_by"),
    ("drive_requests", "agent_id"),
    ("drive_requests", "opened_by"),
    ("file_waits", "agent_id"),
    ("files", "owner_agent_id"),
    ("graph_gaps", "agent_id"),
    ("handoffs", "agent_id"),
    ("handoffs", "decided_by"),
    ("ideas", "agent_id"),
    ("issues", "closed_by_agent_id"),
    ("issues", "opened_by_agent_id"),
    ("mod_sessions", "session_id"),
    ("released_processes", "agent_id"),
    ("reviews", "reviewer_agent_id"),
    ("reviews", "subject_agent_id"),
    ("spawn_queue", "agent_id"),
    ("spawn_queue", "parent_agent_id"),
    ("test_runs", "agent_id"),
    ("versions", "agent_id"),
    ("wakeups", "from_agent_id"),
    ("wakeups", "to_agent_id"),
    ("watchdog_findings", "agent_id"),
]


class MovingClaude(FakeClaude):
    """A CLI whose `claude --resume <id> --bg` comes back under a new session id."""

    def __init__(self) -> None:
        super().__init__()
        self.visible = True
        self.resumed: list[str] = []

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        if args[0] != "--resume":
            return super().__call__(args, cwd)
        self.calls.append((list(args), cwd))
        name = args[args.index("--name") + 1]
        new_id = f"{len(self.resumed) + 1:x}e5e5e5e-1111-2222-3333-444444444444"
        self.resumed.append(new_id)
        if self.visible:
            self.add(new_id, name, bg_id=new_id[:8])
        return f"backgrounded · {new_id[:8]} · {name}\n"


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> MovingClaude:
    fake = MovingClaude()
    monkeypatch.setattr(sessions, "_run", fake)
    return fake


def _all_values(ledger: Ledger, value: str) -> list[tuple[str, str]]:
    found = []
    for table in ledger.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        name = table["name"]
        for column in ledger.conn.execute(f"PRAGMA table_info({name})"):
            hit = ledger.conn.execute(
                f"SELECT 1 FROM {name} WHERE {column['name']} = ?", (value,)
            ).fetchone()
            if hit is not None:
                found.append((name, column["name"]))
    return found


def _stopped_manager(ledger: Ledger, claude: MovingClaude) -> Ctx:
    ctx = _bootstrap(ledger, claude)
    ledger.mod_session(ctx.manager[1])
    claude.stop_session(ctx.manager[1])
    return ctx


def test_every_column_that_holds_an_agent_id_is_re_keyed(ledger: Ledger) -> None:
    # A new column that stores an agent or session id must be named *agent_id, reference
    # agents, or be added to rekey.SESSION_COLUMNS; then add it here.
    assert rekey.agent_id_columns(ledger.conn) == AGENT_ID_COLUMNS


def test_a_resume_under_a_new_session_id_re_keys_the_agent(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _stopped_manager(ledger, claude)
    old_id = ctx.manager[1]
    posted = ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")

    resumed = ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")

    new_id = claude.resumed[0]
    assert resumed["session_id"] == new_id
    assert resumed["reachable"] is True
    assert resumed["wakeups_sent"] == 1
    assert _all_values(ledger, old_id) == []
    manager = _row(ledger, new_id)
    assert manager["bg_id"] == new_id[:8]
    assert manager["resume_started_at"] is None
    assert _row(ledger, ctx.lead[1])["parent_agent_id"] == new_id
    assert ledger.is_mod_session(new_id)

    inbox = ledger.message_inbox("mgr-p1-phase-1", new_id)
    assert [m["message_id"] for m in inbox["messages"]] == [posted["message_id"]]
    assert ledger.run_status("mgr-p1-phase-1", new_id)["run"]["run_id"] == ctx.run_id
    with pytest.raises(LedgerError, match="no agent is registered"):
        ledger.message_inbox("mgr-p1-phase-1", old_id)


def test_a_resume_under_the_same_session_id_keeps_the_agent(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    same = FakeClaude()
    monkeypatch.setattr(sessions, "_run", same)
    ctx = _bootstrap(ledger, same)
    same.stop_session(ctx.manager[1])
    resumed = ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")
    assert resumed["session_id"] == ctx.manager[1]
    assert resumed["reachable"] is True
    assert _row(ledger, ctx.manager[1])["resume_started_at"] is None


def test_wake_ups_owed_after_the_re_key_go_to_the_new_session(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _stopped_manager(ledger, claude)
    ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")
    new_id = claude.resumed[0]
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "one more thing")

    answer = events.handle_owed(ledger, {"session_id": ctx.lead[1]})

    assert answer is not None
    assert [w["session_id"] for w in answer["wakeups"]] == [new_id]


def test_an_unconfirmed_resume_leaves_the_wake_up_owed_until_the_session_hooks_in(
    ledger: Ledger, claude: MovingClaude, host: Path
) -> None:
    ctx = _stopped_manager(ledger, claude)
    claude.visible = False
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")

    resumed = ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")

    assert resumed["reachable"] is False
    assert resumed["wakeups_sent"] == 0
    assert len(ledger.owed_wakeups(ctx.lead[1])) == 1
    assert _row(ledger, ctx.manager[1])["resume_started_at"] is not None

    new_id = claude.resumed[0]
    hook = {"session_id": new_id, "agent_type": "swarm-manager", "transcript_path": "t.jsonl"}
    run_event("session_start", json.dumps(hook), host)

    assert _row(ledger, new_id)["name"] == "mgr-p1-phase-1"
    assert ledger.message_inbox("mgr-p1-phase-1", new_id)["messages"]


def test_an_unknown_session_matches_a_pending_resume_through_the_listing(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _stopped_manager(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET resume_started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE agent_id = ?",
            (ctx.manager[1],),
        )
    session_name = _row(ledger, ctx.manager[1])["session_name"]
    claude.add("abcdef01-0000-0000-0000-000000000000", session_name, bg_id="abcdef01")

    assert ledger.adopt_session("abcdef01-0000-0000-0000-000000000000") is None
    adopted = ledger.adopt_session("abcdef01-0000-0000-0000-000000000000", use_listing=True)

    assert adopted == "abcdef01-0000-0000-0000-000000000000"
    assert _row(ledger, adopted)["name"] == "mgr-p1-phase-1"


def test_pre_ledger_asks_a_resumed_session_to_wait_until_it_is_matched(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _stopped_manager(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET resume_started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE agent_id = ?",
            (ctx.manager[1],),
        )
    hook = {
        "session_id": "ffff0000-0000-0000-0000-000000000000",
        "agent_type": "swarm-manager",
        "tool_name": "mcp__swarm-ledger__message_inbox",
        "tool_input": {"caller": "mgr-p1-phase-1"},
    }
    denied = events.handle_pre_ledger(ledger, hook)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "just resumed" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    started = events.handle_pre_ledger(
        ledger, {**hook, "tool_name": "mcp__swarm-ledger__run_start"}
    )
    assert started["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_agent_resume_re_keys_a_session_already_running_under_its_name(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _stopped_manager(ledger, claude)
    session_name = _row(ledger, ctx.manager[1])["session_name"]
    claude.add("0badc0de-0000-0000-0000-000000000000", session_name, bg_id="0badc0de")

    resumed = ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")

    assert resumed["rekeyed_from"] == ctx.manager[1]
    assert resumed["session_id"] == "0badc0de-0000-0000-0000-000000000000"
    assert claude.resumed == []


def test_a_coder_resumed_under_a_new_id_keeps_its_claim_and_handoff_history(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx)
    file_id = _row(ledger, coder[1])["file_id"]
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO handoffs (file_id, agent_id, state) VALUES (?, ?, 'submitted')",
            (file_id, coder[1]),
        )
    claude.stop_session(coder[1])

    ledger.agent_resume(*ctx.lead, coder[0])

    new_id = claude.resumed[0]
    handoff = ledger.conn.execute("SELECT agent_id FROM handoffs").fetchone()
    assert handoff["agent_id"] == new_id
    assert _all_values(ledger, coder[1]) == []
