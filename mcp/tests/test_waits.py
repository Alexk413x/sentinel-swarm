from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from helpers import (
    all_applicable,
    all_ratings,
    as_session,
    bootstrap_module,
    file_id_for,
    make_host,
    register_agent,
    settings_text,
    spawn_coder,
    write_module,
)

from swarm_ledger import watchdog
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import ROLE_TOOLS, LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import WatchdogSettings

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"
_GRAPH_MODULES = {"good": "add", "disagree": "sub"}
LEAD = "lead-p1-module-1"

pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = make_host(tmp_path / "host", settings_text(test_command=_TEST_COMMAND))
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    for name, symbol in _GRAPH_MODULES.items():
        write_module(root, name, symbol)
    (root / "knowledge").mkdir()
    graph_path = root / "knowledge" / "code_graph.db"
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", graph_path)
    conn = sqlite3.connect(str(graph_path))
    try:
        for name, symbol in _GRAPH_MODULES.items():
            conn.execute(
                "INSERT INTO node (id, kind, description) VALUES (?, 'function', ?)",
                (f"waitstest_{name}", f"test fixture for {name}"),
            )
            conn.execute(
                "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, 0, ?, ?, ?)",
                (f"waitstest_{name}", f"pkg/{name}.py", f"{name}.py", symbol),
            )
        conn.commit()
    finally:
        conn.close()
    return root


def _coder(ledger: Ledger, ctx: dict, slug: str, path: str, test_path: str, session: str) -> dict:
    coder = spawn_coder(ledger, ctx, f"coder-p1-module-1-{slug}", path, test_path)
    as_session(ledger, coder["agent_id"], session)
    return coder


def _handoff(ledger: Ledger, ctx: dict, coder: dict, path: str) -> int:
    file_id = file_id_for(ledger, path)
    ledger.score_record(
        coder["name"], coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder["name"], coder["agent_id"], file_id, [], [])
    ledger.score_record(
        LEAD, ctx["lead"]["agent_id"], file_id, all_ratings(10), all_applicable(), "lead"
    )
    ledger.review_compare(LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"])
    return handoff["handoff_id"]


def _owed(ledger: Ledger, from_agent_id: str) -> list[dict]:
    return ledger.owed_wakeups(from_agent_id)


def test_wait_for_is_a_coder_and_lead_tool() -> None:
    holders = {role for role, tools in ROLE_TOOLS.items() if "wait_for" in tools}
    assert holders == {"coder", "lead"}


def test_approve_owes_each_waiter_a_wake_up_and_closes_the_wait(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    as_session(ledger, ctx["lead"]["agent_id"], "host-r1-lead")

    wait = ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")
    assert wait["waiting"] is True
    assert wait["file_state"] == "claimed"
    status = ledger.run_status(LEAD, ctx["lead"]["agent_id"])
    assert [(w["waiter"], w["path"], w["reason"]) for w in status["file_waits"]] == [
        (waiter["name"], "pkg/good.py", "needs add()")
    ]

    handoff_id = _handoff(ledger, ctx, helper, "pkg/good.py")
    approved = ledger.approve(LEAD, ctx["lead"]["agent_id"], handoff_id)

    assert [w["name"] for w in approved["woken_waiters"]] == [waiter["name"]]
    assert waiter["name"] in approved["woken_waiters"][0]["next"]
    owed = [w for w in _owed(ledger, ctx["lead"]["agent_id"]) if w["reason"] == "file_wait"]
    assert [w["to_name"] for w in owed] == [waiter["name"]]
    assert "pkg/good.py was approved" in owed[0]["pointer"]
    assert "needs add()" in owed[0]["pointer"]
    assert ledger.run_status(LEAD, ctx["lead"]["agent_id"])["file_waits"] == []


def test_a_wake_up_owed_for_a_wait_lets_the_approving_lead_send_it(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET parent_agent_id = NULL WHERE agent_id = ?", (waiter["agent_id"],)
        )
    data = {"session_id": ctx["lead"]["agent_id"], "tool_input": {"to": "host-r1-main"}}
    assert events.handle_pre_send_message(ledger, data) is not None

    ledger.approve(LEAD, ctx["lead"]["agent_id"], _handoff(ledger, ctx, helper, "pkg/good.py"))

    assert events.handle_pre_send_message(ledger, data) is None


def test_wait_for_answers_at_once_when_the_file_is_already_approved(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.approve(LEAD, ctx["lead"]["agent_id"], _handoff(ledger, ctx, helper, "pkg/good.py"))

    answer = ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")

    assert answer["waiting"] is False
    assert answer["file_state"] == "approved"
    assert ledger.run_status(LEAD, ctx["lead"]["agent_id"])["file_waits"] == []


def test_wait_for_twice_keeps_one_wait_with_the_new_reason(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )

    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "first")
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "second")

    waits = ledger.run_status(LEAD, ctx["lead"]["agent_id"])["file_waits"]
    assert [w["reason"] for w in waits] == ["second"]


def test_wait_for_refuses_bad_requests(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    name, agent_id = waiter["name"], waiter["agent_id"]

    with pytest.raises(LedgerError, match="needs a reason"):
        ledger.wait_for(name, agent_id, "pkg/good.py", " ")
    with pytest.raises(LedgerError, match="no module plans"):
        ledger.wait_for(name, agent_id, "pkg/nowhere.py", "x")
    with pytest.raises(LedgerError, match="your own file"):
        ledger.wait_for(name, agent_id, "pkg/disagree.py", "x")
    with pytest.raises(LedgerError, match="belongs to your module"):
        ledger.wait_for(LEAD, ctx["lead"]["agent_id"], "pkg/good.py", "x")
    with pytest.raises(LedgerError, match="may not call"):
        ledger.wait_for("mgr-p1-phase-1", ctx["manager"]["agent_id"], "pkg/good.py", "x")


def test_a_released_waiter_closes_its_wait(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")
    ledger.agent_release(LEAD, ctx["lead"]["agent_id"], waiter["agent_id"])

    assert ledger.run_status(LEAD, ctx["lead"]["agent_id"])["file_waits"] == []
    approved = ledger.approve(
        LEAD, ctx["lead"]["agent_id"], _handoff(ledger, ctx, helper, "pkg/good.py")
    )
    assert "woken_waiters" not in approved


def test_accepting_a_file_as_incomplete_wakes_its_waiters(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        helper["name"], helper["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(helper["name"], helper["agent_id"], file_id, ["blocked"], [])
    ledger.score_record(
        LEAD, ctx["lead"]["agent_id"], file_id, all_ratings(10), all_applicable(), "lead"
    )
    ledger.review_compare(LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"])

    result = ledger.accept_incomplete(LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"], "stops")

    assert [w["name"] for w in result["woken_waiters"]] == [waiter["name"]]
    owed = [w for w in _owed(ledger, ctx["lead"]["agent_id"]) if w["reason"] == "file_wait"]
    assert "accepted as incomplete" in owed[0]["pointer"]


def test_a_coder_with_an_open_wait_is_not_blocked_at_stop(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")

    assert events.handle_stop(ledger, {"session_id": waiter["agent_id"]}) is None


def test_the_watchdog_reports_a_wait_whose_file_is_approved(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    run_id = ctx["run_id"]
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO files (module_id, path, state, released_at) "
            "VALUES (?, 'pkg/late.py', 'approved', '2026-01-01T00:00:00.000Z')",
            (ctx["module_id"],),
        )
        conn.execute(
            "INSERT INTO file_waits (run_id, agent_id, path, reason) "
            "VALUES (?, ?, 'pkg/late.py', ?)",
            (run_id, waiter["agent_id"], "needs late()"),
        )
    now = datetime.now().astimezone()

    findings = watchdog.scan(ledger.conn, [], now, WatchdogSettings())

    landed = [f for f in findings if f.kind == "wait_landed"]
    assert [f.name for f in landed] == [waiter["name"]]
    assert "pkg/late.py" in landed[0].detail
    assert waiter["name"] in landed[0].next_step


def test_the_watchdog_ignores_a_wait_on_an_unfinished_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")
    waiter = _coder(
        ledger, ctx, "main", "pkg/disagree.py", "tests/test_disagree.py", "host-r1-main"
    )
    ledger.wait_for(waiter["name"], waiter["agent_id"], "pkg/good.py", "needs add()")

    findings = watchdog.scan(ledger.conn, [], datetime.now().astimezone(), WatchdogSettings())

    assert [f for f in findings if f.kind == "wait_landed"] == []


def test_a_lead_waits_on_a_file_of_another_module(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    ledger.module_add("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "module-2")
    ledger.brief_create(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        "lead-p1-module-2",
        "lead",
        "sonnet",
        "Own module-2.",
        module_id=ledger.conn.execute(
            "SELECT module_id FROM modules WHERE name = 'module-2'"
        ).fetchone()["module_id"],
    )
    register_agent(ledger, "lead2-agent", "lead", parent_agent_id=ctx["manager"]["agent_id"])
    lead2 = ledger.brief_ack("lead-p1-module-2", "lead2-agent")
    as_session(ledger, lead2["agent_id"], "host-r1-lead-2")
    helper = _coder(ledger, ctx, "dep", "pkg/good.py", "tests/test_good.py", "host-r1-dep")

    wait = ledger.wait_for("lead-p1-module-2", lead2["agent_id"], "pkg/good.py", "needs add()")
    assert wait["waiting"] is True

    approved = ledger.approve(
        LEAD, ctx["lead"]["agent_id"], _handoff(ledger, ctx, helper, "pkg/good.py")
    )
    assert [w["name"] for w in approved["woken_waiters"]] == ["lead-p1-module-2"]
