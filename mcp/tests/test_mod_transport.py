from __future__ import annotations

import pytest
from test_hooks import _as_session, _bootstrap, host, ledger

from swarm_ledger.db import write_tx
from swarm_ledger.hooks import HANDLERS, events
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")
__all__ = ["host", "ledger"]


def test_the_mod_events_are_registered() -> None:
    for event in ("owed", "wake_sent", "inbox_take", "inbox_ack", "inbox_release"):
        assert HANDLERS[event] is getattr(events, f"handle_{event}")


def test_session_start_from_the_mod_records_the_session(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    events.handle_session_start(
        ledger, {"session_id": ctx["lead"]["agent_id"], "sentinel_swarm_transport": "mod"}
    )
    assert ledger.is_mod_session("lead-agent")
    assert not ledger.is_mod_session("mgr-agent")


def test_next_tells_a_mod_session_to_make_no_call(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.mod_session("lead-agent")
    posted = ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "done")
    assert posted["next"] == (
        "Nothing to call: the sentinel-swarm mod wakes mgr-p1-phase-1 for you. "
        "Your Stop hook names a call only if that wake-up is not delivered."
    )


def test_owed_lists_the_callers_unsent_wake_ups_per_target_with_the_time_signal(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    first = ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "one")
    second = ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "two")
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "not mine")

    answer = events.handle_owed(ledger, {"session_id": ctx["lead"]["agent_id"]})

    assert answer is not None
    [target] = answer["wakeups"]
    assert target["session_id"] == "mgr-agent"
    assert target["session_name"] == "host-r1-manager-1"
    assert target["role"] == "manager"
    assert len(target["wakeup_ids"]) == 2
    assert target["text"].startswith(f"Message {first['message_id']} from lead-p1-module-1")
    assert f"Message {second['message_id']} from" in target["text"]
    assert " elapsed " in target["text"]


def test_owed_gives_a_lead_target_no_time_signal(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "lead-agent", "host-r1-lead-1")
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "go")
    answer = events.handle_owed(ledger, {"session_id": "mgr-agent"})
    assert answer is not None
    assert "elapsed" not in answer["wakeups"][0]["text"]


def test_owed_ignores_an_unknown_caller(ledger: Ledger) -> None:
    _bootstrap(ledger)
    assert events.handle_owed(ledger, {"session_id": "stranger"}) is None


def test_wake_sent_marks_only_the_callers_rows(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    _as_session(ledger, "lead-agent", "host-r1-lead-1")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "up")
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "down")
    mine = [w["wakeup_id"] for w in ledger.owed_wakeups("lead-agent")]
    theirs = [w["wakeup_id"] for w in ledger.owed_wakeups("mgr-agent")]

    answer = events.handle_wake_sent(
        ledger, {"session_id": "lead-agent", "wakeup_ids": [*mine, *theirs, "x"]}
    )

    assert answer == {"sent": 1}
    assert ledger.owed_wakeups("lead-agent") == []
    assert [w["wakeup_id"] for w in ledger.owed_wakeups("mgr-agent")] == theirs


def test_a_failed_mod_send_leaves_the_row_owed_and_stop_names_the_call(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.mod_session("lead-agent")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "up")

    blocked = events.handle_stop(ledger, {"session_id": "lead-agent"})

    assert blocked is not None
    assert '- agent_resume(target_name="mgr-p1-phase-1")' in blocked["reason"]


def test_inbox_take_claims_and_ack_marks_read(ledger: Ledger) -> None:
    _bootstrap(ledger)
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "first")
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "second")

    taken = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})

    assert taken is not None
    assert [m["body"] for m in taken["messages"]] == ["first", "second"]
    assert taken["remaining"] == 0
    assert "read_at" not in taken["messages"][0]
    again = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})
    assert again == {"claim": None, "messages": [], "remaining": 0}
    assert ledger.message_inbox("lead-p1-module-1", "lead-agent") == {
        "messages": [],
        "remaining": 0,
    }

    acked = events.handle_inbox_ack(ledger, {"session_id": "lead-agent", "claim": taken["claim"]})
    assert acked == {"settled": 2}
    assert events.handle_stop(ledger, {"session_id": "lead-agent"}) is None


def test_inbox_release_returns_the_messages_to_unread(ledger: Ledger) -> None:
    _bootstrap(ledger)
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "first")
    taken = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})
    assert taken is not None

    released = events.handle_inbox_release(
        ledger, {"session_id": "lead-agent", "claim": taken["claim"]}
    )

    assert released == {"settled": 1}
    inbox = ledger.message_inbox("lead-p1-module-1", "lead-agent")
    assert [m["body"] for m in inbox["messages"]] == ["first"]


def test_an_unsettled_claim_counts_as_unread_after_two_minutes(ledger: Ledger) -> None:
    _bootstrap(ledger)
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "first")
    taken = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})
    assert taken is not None
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE messages SET claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','-121 seconds')"
        )

    inbox = ledger.message_inbox("lead-p1-module-1", "lead-agent")

    assert [m["body"] for m in inbox["messages"]] == ["first"]
    late = events.handle_inbox_ack(ledger, {"session_id": "lead-agent", "claim": taken["claim"]})
    assert late == {"settled": 0}


def test_a_claim_of_one_agent_does_not_settle_anothers_messages(ledger: Ledger) -> None:
    _bootstrap(ledger)
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "first")
    taken = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})
    assert taken is not None
    other = events.handle_inbox_ack(ledger, {"session_id": "mgr-agent", "claim": taken["claim"]})
    assert other == {"settled": 0}


def test_inbox_take_never_takes_an_earlier_runs_mail(ledger: Ledger) -> None:
    _bootstrap(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("INSERT INTO runs (run_id, prd, state) VALUES (9, 'old', 'finished')")
        conn.execute(
            "INSERT INTO messages (run_id, from_name, to_name, body) "
            "VALUES (9, 'mgr-p1-phase-1', 'lead-p1-module-1', 'Old mail.')"
        )
    taken = events.handle_inbox_take(ledger, {"session_id": "lead-agent"})
    assert taken == {"claim": None, "messages": [], "remaining": 0}
