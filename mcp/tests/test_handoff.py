from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from helpers import (
    all_applicable,
    all_ratings,
    bootstrap_module,
    ensure_graph_source_base,
    file_id_for,
    insert_passing_test_run,
    make_host,
    register_agent,
    review_scores,
    settings_text,
    spawn_coder,
    touch_module,
    write_module,
)

from swarm_ledger import graph as graph_module
from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"

# name -> the one top-level function each fixture module anchors in the graph.
_GRAPH_MODULES: dict[str, str] = {
    "good": "add",
    "unmapped": "mul",
    "stale": "add2",
    "round_improved": "bump_a",
    "round_plateau": "bump_b",
    "round_regression": "bump_c",
}


pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = make_host(tmp_path / "host", settings_text(test_command=_TEST_COMMAND))
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()

    for name, symbol in _GRAPH_MODULES.items():
        write_module(root, name, symbol)
    # `unmapped.py` additionally defines a top-level function the graph never
    # anchors, so graph_current_for's per-symbol coverage check has something
    # to catch.
    with (root / "pkg" / "unmapped.py").open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef div(a, b):\n    return a - b\n")

    (root / "pkg" / "nograph.py").write_text(
        "def sub(a, b):\n    return a - b\n", encoding="utf-8", newline="\n"
    )
    (root / "tests" / "test_nograph.py").write_text(
        "from pkg.nograph import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n",
        encoding="utf-8",
        newline="\n",
    )

    (root / "pkg" / "failing.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (root / "tests" / "test_failing.py").write_text(
        "def test_fail():\n    assert False\n", encoding="utf-8", newline="\n"
    )

    (root / "pkg" / "zero.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (root / "tests" / "test_zero.py").write_text(
        "# intentionally collects no tests\n", encoding="utf-8", newline="\n"
    )

    (root / "pkg" / "skipped.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (root / "tests" / "test_skipped.py").write_text(
        "import pytest\n\n\ndef test_skip():\n    pytest.skip('nope')\n",
        encoding="utf-8",
        newline="\n",
    )

    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir()
    graph_path = knowledge_dir / "code_graph.db"
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", graph_path)

    conn = sqlite3.connect(str(graph_path))
    try:
        for name, symbol in _GRAPH_MODULES.items():
            node_id = f"swarmtest_{name}"
            conn.execute(
                "INSERT INTO node (id, kind, description) VALUES (?, 'function', ?)",
                (node_id, f"test fixture for {name}"),
            )
            conn.execute(
                "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, 0, ?, ?, ?)",
                (node_id, f"pkg/{name}.py", f"{name}.py", symbol),
            )
        conn.commit()
    finally:
        conn.close()

    ensure_graph_source_base(root)
    return root


def _record_baseline(host: Path, rel_path: str, content: bytes | None = None) -> None:
    data = content if content is not None else (host / rel_path).read_bytes()
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute(
            "INSERT INTO source (path, sha) VALUES (?, ?) "
            "ON CONFLICT (path) DO UPDATE SET sha = excluded.sha",
            (rel_path, graph_module._content_sha(data)),
        )
        conn.commit()
    finally:
        conn.close()


def _accept_module_and_phase(ledger: Ledger, ctx: dict) -> None:
    ledger.agent_release("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=review_scores(),
    )
    ledger.phase_update("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=review_scores()
    )


# -- handoff_submit refusals -----------------------------------------------------


def test_handoff_refused_on_failing_tests(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-fail", "pkg/failing.py", "tests/test_failing.py"
    )
    file_id = file_id_for(ledger, "pkg/failing.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-p1-module-1-fail", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_zero_tests(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-zero", "pkg/zero.py", "tests/test_zero.py")
    file_id = file_id_for(ledger, "pkg/zero.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-p1-module-1-zero", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_skipped_test(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-skip", "pkg/skipped.py", "tests/test_skipped.py"
    )
    file_id = file_id_for(ledger, "pkg/skipped.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-p1-module-1-skip", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_missing_node(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-nograph", "pkg/nograph.py", "tests/test_nograph.py"
    )
    file_id = file_id_for(ledger, "pkg/nograph.py")
    with pytest.raises(LedgerError, match="no node anchors"):
        ledger.handoff_submit("coder-p1-module-1-nograph", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_an_unmapped_symbol(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-unmapped", "pkg/unmapped.py", "tests/test_unmapped.py"
    )
    file_id = file_id_for(ledger, "pkg/unmapped.py")
    with pytest.raises(LedgerError, match="is unmapped"):
        ledger.handoff_submit("coder-p1-module-1-unmapped", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_stale_self_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-stale", "pkg/stale.py", "tests/test_stale.py"
    )
    file_id = file_id_for(ledger, "pkg/stale.py")

    review = ledger.score_record(
        "coder-p1-module-1-stale",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    review_row = ledger.conn.execute(
        "SELECT created_at FROM reviews WHERE review_id = ?", (review["review_id"],)
    ).fetchone()
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE files SET stale_since = ? WHERE file_id = ?",
            (review_row["created_at"], file_id),
        )

    with pytest.raises(LedgerError, match="self review is missing or older"):
        ledger.handoff_submit("coder-p1-module-1-stale", coder["agent_id"], file_id, [], [])


# -- a clean handoff ----------------------------------------------------------------


def test_handoff_succeeds_and_saves_two_versions(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-good", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-good",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )

    handoff = ledger.handoff_submit("coder-p1-module-1-good", coder["agent_id"], file_id, [], [])
    assert handoff["state"] == "submitted"

    versions = ledger.conn.execute(
        "SELECT * FROM versions WHERE file_id = ? ORDER BY version_id", (file_id,)
    ).fetchall()
    assert len(versions) == 2
    assert {v["version_id"] for v in versions} == {
        handoff["version_id"],
        handoff["test_version_id"],
    }
    host_path = ledger.repo_root / "pkg" / "good.py"
    assert bytes(versions[0]["content"]) == host_path.read_bytes()
    assert host_path.is_file()

    file_row = ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    assert file_row["state"] == "handed_up"


# -- blind scoring order ----------------------------------------------------------------


def test_review_compare_refuses_without_a_lead_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-blind1", "pkg/good.py", "tests/test_good.py"
    )
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-blind1",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-blind1", coder["agent_id"], file_id, [], [])

    with pytest.raises(LedgerError, match="no lead review exists"):
        ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_score_record_lead_refuses_after_review_compare_already_ran(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-blind2", "pkg/good.py", "tests/test_good.py"
    )
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-blind2",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-blind2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    with pytest.raises(LedgerError, match="blind scoring is closed"):
        ledger.score_record(
            "lead-p1-module-1",
            ctx["lead"]["agent_id"],
            file_id,
            all_ratings(10),
            all_applicable(),
            "lead",
        )


# -- approve ------------------------------------------------------------------------


def test_approve_refused_without_compare(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-appr1", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-appr1",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-appr1", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )

    with pytest.raises(LedgerError, match="review_compare has not run"):
        ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_refused_with_an_open_issue(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-appr2", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-appr2",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-appr2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.issue_open(
        "lead-p1-module-1", ctx["lead"]["agent_id"], file_id, "Stray issue", "Found later."
    )

    with pytest.raises(LedgerError, match="open issue"):
        ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_releases_the_claim_and_the_coder(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-appr3", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-appr3",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-appr3", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    approved = ledger.approve(
        "lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "nice work"
    )
    assert approved["state"] == "approved"

    file_row = ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    assert file_row["state"] == "approved"
    assert file_row["released_at"] is not None

    coder_row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (coder["agent_id"],)
    ).fetchone()
    assert coder_row["state"] == "released"
    assert coder_row["ended_at"] is not None


def test_handoff_next_wakes_the_lead_and_approve_stops_the_coders_session(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    lead_session = {"pid": 9, "sessionId": "lead-agent", "name": "host-r1-lead-1", "status": "idle"}

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        calls.append(list(args))
        return json.dumps([lead_session]) if args[:2] == ["agents", "--json"] else ""

    monkeypatch.setattr(sessions, "_run", fake_run)
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-next", "pkg/good.py", "tests/test_good.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = 'host-r1-lead-1' WHERE name = 'lead-p1-module-1'"
        )
        conn.execute("UPDATE agents SET bg_id = 'coderbg' WHERE agent_id = ?", (coder["agent_id"],))
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-next",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )

    handoff = ledger.handoff_submit("coder-p1-module-1-next", coder["agent_id"], file_id, [], [])
    assert handoff["next"] == (
        'SendMessage(to="host-r1-lead-1", '
        f'message="Handoff {handoff["handoff_id"]} for pkg/good.py is waiting in the ledger.")'
    )
    assert [w["reason"] for w in ledger.owed_wakeups(coder["agent_id"])] == ["handoff_submit"]

    lead_id = ctx["lead"]["agent_id"]
    ledger.score_record(
        "lead-p1-module-1", lead_id, file_id, all_ratings(10), all_applicable(), "lead"
    )
    ledger.review_compare("lead-p1-module-1", lead_id, handoff["handoff_id"])
    ledger.approve("lead-p1-module-1", lead_id, handoff["handoff_id"])
    assert ["stop", "coderbg"] in calls


# -- return, a second handoff, and attempt_record ------------------------------------------


def _round_trip(
    ledger: Ledger,
    ctx: dict,
    name: str,
    round1_overrides: dict[str, int],
    round2_overrides: dict[str, int],
    targeted: list[str],
) -> str:
    coder_name = f"coder-p1-module-1-{name.replace('_', '-')}"
    path, test_path = f"pkg/{name}.py", f"tests/test_{name}.py"
    coder = spawn_coder(ledger, ctx, coder_name, path, test_path)
    file_id = file_id_for(ledger, path)

    ledger.score_record(
        coder_name, coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff1 = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(overrides=round1_overrides),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff1["handoff_id"])
    ledger.return_work(
        "lead-p1-module-1", ctx["lead"]["agent_id"], handoff1["handoff_id"], [], targeted
    )
    returned = ledger.conn.execute(
        "SELECT state FROM agents WHERE agent_id = ?", (coder["agent_id"],)
    ).fetchone()
    assert returned["state"] == "idle"

    touch_module(ledger.repo_root, name, "revised")
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    ledger.brief_read(coder["agent_id"], coder_name)
    ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(overrides=round2_overrides),
        all_applicable(),
        "lead",
    )
    handoff2_id = ledger.conn.execute(
        "SELECT handoff_id FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
        (file_id,),
    ).fetchone()["handoff_id"]
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff2_id)

    return ledger.attempt_record("lead-p1-module-1", ctx["lead"]["agent_id"], file_id)["outcome"]


def test_return_work_keeps_a_parked_coder_parked(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder_name = "coder-p1-module-1-parked"
    coder = spawn_coder(ledger, ctx, coder_name, "pkg/parked.py", "tests/test_parked.py")
    file_id = file_id_for(ledger, "pkg/parked.py")
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger._park(coder["agent_id"], "retired")
    lead_id = ctx["lead"]["agent_id"]
    ledger.score_record(
        "lead-p1-module-1",
        lead_id,
        file_id,
        all_ratings(overrides={"testing": 5}),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", lead_id, handoff["handoff_id"])

    ledger.return_work("lead-p1-module-1", lead_id, handoff["handoff_id"], [], ["testing"])

    row = ledger.conn.execute(
        "SELECT state, state_before_park, parked_at FROM agents WHERE agent_id = ?",
        (coder["agent_id"],),
    ).fetchone()
    assert (row["state"], row["state_before_park"]) == ("parked", "idle")
    assert row["parked_at"] is not None


def test_attempt_record_classifies_improved(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    outcome = _round_trip(
        ledger,
        ctx,
        "round_improved",
        round1_overrides={"testing": 5},
        round2_overrides={},
        targeted=["testing"],
    )
    assert outcome == "improved"


def test_attempt_record_classifies_plateau(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    outcome = _round_trip(
        ledger,
        ctx,
        "round_plateau",
        round1_overrides={"testing": 8},
        round2_overrides={"testing": 8},
        targeted=["testing"],
    )
    assert outcome == "plateau"


def test_attempt_record_classifies_regression_and_restores_the_version(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    name = "round_regression"
    path = ledger_path_for(ledger, name)
    original_content = path.read_text(encoding="utf-8")

    outcome = _round_trip(
        ledger,
        ctx,
        name,
        round1_overrides={},
        round2_overrides={"meets_the_brief": 3},
        targeted=["testing"],
    )
    assert outcome == "regression"
    assert path.read_text(encoding="utf-8") == original_content
    assert "# revised" not in path.read_text(encoding="utf-8")


def ledger_path_for(ledger: Ledger, name: str) -> Path:
    return ledger.repo_root / "pkg" / f"{name}.py"


# -- accept_incomplete and run_finish -------------------------------------------------------


def test_accept_incomplete_creates_a_deferral_and_run_finish_refuses_until_decided(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-incomplete", "pkg/good.py", "tests/test_good.py"
    )
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-incomplete",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit(
        "coder-p1-module-1-incomplete",
        coder["agent_id"],
        file_id,
        ["blocked on an external API"],
        [],
    )
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    deferral = ledger.accept_incomplete(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff["handoff_id"],
        "blocked on an external API",
    )
    assert deferral["state"] == "open"

    handoff_row = ledger.conn.execute(
        "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff["handoff_id"],)
    ).fetchone()
    assert handoff_row["state"] == "incomplete"
    file_row = ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    assert file_row["state"] == "incomplete"
    assert file_row["released_at"] is not None

    with pytest.raises(LedgerError, match="deferral"):
        ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")

    ledger.agreement_decide(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        deferral["deferral_id"],
        "agreed",
        "acceptable for this run",
    )
    _accept_module_and_phase(ledger, ctx)
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    result = ledger.run_finish("oracle", ctx["oracle_id"], "success")
    assert result["state"] == "finished"


def test_agreement_decide_refuses_a_decider_below_the_proposers_parent_role(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-defer", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-p1-module-1-defer",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit(
        "coder-p1-module-1-defer", coder["agent_id"], file_id, ["blocked"], []
    )
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    deferral = ledger.accept_incomplete(
        "lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "blocked"
    )

    with pytest.raises(LedgerError):
        ledger.agreement_decide(
            "lead-p1-module-1", ctx["lead"]["agent_id"], deferral["deferral_id"], "agreed", "ok"
        )


def test_agreement_decide_refuses_a_decider_outside_the_deferrals_scope(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-scope", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    deferral = ledger.deferral_propose(
        "coder-p1-module-1-scope", coder["agent_id"], "later", file_id, kind="file"
    )

    module_2 = ledger.module_add("mgr-p1-phase-1", "mgr-agent", ctx["phase_id"], "module-2")
    ledger.brief_create(
        "mgr-p1-phase-1",
        "mgr-agent",
        "lead-p1-module-2",
        "lead",
        "sonnet",
        "Own module-2.",
        module_id=module_2["module_id"],
    )
    register_agent(ledger, "lead-2-agent", "lead", parent_agent_id="mgr-agent")
    ledger.brief_ack("lead-p1-module-2", "lead-2-agent")

    phase_2 = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    ledger.phase_update("oracle", ctx["oracle_id"], phase_2["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle",
        ctx["oracle_id"],
        "mgr-p2-phase-2",
        "manager",
        "opus",
        "Own phase-2.",
        phase_id=phase_2["phase_id"],
    )
    register_agent(ledger, "mgr-2-agent", "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack("mgr-p2-phase-2", "mgr-2-agent")

    with pytest.raises(LedgerError, match="outside the lead scope"):
        ledger.agreement_decide(
            "lead-p1-module-2", "lead-2-agent", deferral["deferral_id"], "agreed", "ok"
        )
    with pytest.raises(LedgerError, match="outside the manager scope"):
        ledger.agreement_decide(
            "mgr-p2-phase-2", "mgr-2-agent", deferral["deferral_id"], "agreed", "ok"
        )

    decided = ledger.agreement_decide(
        "lead-p1-module-1", ctx["lead"]["agent_id"], deferral["deferral_id"], "agreed", "ok"
    )
    assert decided["state"] == "agreed"


# -- analytics_query and report_build ------------------------------------------------------


def test_analytics_query_rejects_non_select(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    with pytest.raises(LedgerError):
        ledger.analytics_query("oracle", ctx["oracle_id"], "DELETE FROM agents")
    with pytest.raises(LedgerError):
        ledger.analytics_query("oracle", ctx["oracle_id"], "SELECT 1; DROP TABLE agents")


def test_analytics_query_runs_a_select(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    result = ledger.analytics_query("oracle", ctx["oracle_id"], "SELECT COUNT(*) AS n FROM agents")
    assert result["rows"][0]["n"] >= 1


def test_report_build_writes_the_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    result = ledger.report_build("oracle", ctx["oracle_id"])
    assert Path(result["path"]).is_file()
    assert "Run report" in result["text"]
    assert Path(result["path"]).read_text(encoding="utf-8") == result["text"]


def test_handoff_refused_while_the_file_is_stale_against_its_baseline(
    ledger: Ledger, host: Path
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sb", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    _record_baseline(host, "pkg/good.py")
    touch_module(host, "good", "reworked after a return")

    with pytest.raises(LedgerError, match=r"code graph is stale.*pkg/good\.py.*swarmtest_good"):
        ledger.handoff_submit("coder-p1-module-1-sb", coder["agent_id"], file_id, [], [])
    with pytest.raises(LedgerError, match="call graph_upsert for it again"):
        ledger.handoff_submit("coder-p1-module-1-sb", coder["agent_id"], file_id, [], [])

    _record_baseline(host, "pkg/good.py")
    with pytest.raises(LedgerError, match="self review is missing"):
        ledger.handoff_submit("coder-p1-module-1-sb", coder["agent_id"], file_id, [], [])


def test_handoff_refused_while_the_test_file_is_stale_against_its_baseline(
    ledger: Ledger, host: Path
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-st", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base, symbol) "
            "VALUES ('swarmtest_good', 1, 'tests/test_good.py', 'test_good.py', 'test_add')"
        )
        conn.commit()
    finally:
        conn.close()
    _record_baseline(host, "pkg/good.py")
    _record_baseline(host, "tests/test_good.py", b"an earlier version\n")

    with pytest.raises(LedgerError, match=r"code graph is stale.*tests/test_good\.py"):
        ledger.handoff_submit("coder-p1-module-1-st", coder["agent_id"], file_id, [], [])


def test_a_file_without_a_baseline_is_not_stale(ledger: Ledger, host: Path) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-nb", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    touch_module(host, "good", "no baseline recorded")

    with pytest.raises(LedgerError, match="self review is missing"):
        ledger.handoff_submit("coder-p1-module-1-nb", coder["agent_id"], file_id, [], [])


@pytest.mark.integration
def test_a_reworked_file_hands_off_after_graph_upsert_re_baselines_its_node(
    ledger: Ledger, host: Path, kg_root: Path
) -> None:
    del kg_root
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-rw", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    node = {
        "id": "swarmtest_good",
        "kind": "function",
        "description": "adds two numbers",
        "anchors": ["pkg/good.py#add"],
    }
    ledger.graph_upsert("coder-p1-module-1-rw", coder["agent_id"], [node])
    assert graph_module.stale_files(host, ["pkg/good.py"]) == {}

    touch_module(host, "good", "reworked after a return")
    assert graph_module.stale_files(host, ["pkg/good.py"]) == {"pkg/good.py": ["swarmtest_good"]}
    with pytest.raises(LedgerError, match="code graph is stale"):
        ledger.handoff_submit("coder-p1-module-1-rw", coder["agent_id"], file_id, [], [])

    ledger.graph_upsert("coder-p1-module-1-rw", coder["agent_id"], [node])
    assert graph_module.stale_files(host, ["pkg/good.py"]) == {}
    ledger.score_record(
        "coder-p1-module-1-rw",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-rw", coder["agent_id"], file_id, [], [])
    assert handoff["state"] == "submitted"


def test_the_report_lists_stale_mapped_files(ledger: Ledger, host: Path) -> None:
    ctx = bootstrap_module(ledger)
    spawn_coder(ledger, ctx, "coder-p1-module-1-rp", "pkg/good.py", "tests/test_good.py")
    _record_baseline(host, "pkg/good.py")
    _record_baseline(host, "pkg/unmapped.py")
    before = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "## Stale mapped files" in before
    assert "changed since its baseline; nodes swarmtest_good" not in before

    touch_module(host, "good", "changed after its baseline")
    touch_module(host, "unmapped", "changed after its baseline")
    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert (
        "- pkg/good.py (a file of this run): changed since its baseline; nodes swarmtest_good"
        in text
    )
    assert "- pkg/unmapped.py: changed since its baseline; nodes swarmtest_unmapped" in text


# -- graph_upsert (Ledger method) ------------------------------------------------------------


def test_graph_upsert_refuses_an_anchor_outside_the_owned_file(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-graph1", "pkg/good.py", "tests/test_good.py"
    )
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})

    with pytest.raises(LedgerError, match="updates only the nodes"):
        ledger.graph_upsert(
            "coder-p1-module-1-graph1",
            coder["agent_id"],
            [{"id": "x", "kind": "function", "anchors": ["pkg/other.py#foo"]}],
        )


def test_graph_upsert_passes_through_for_the_owned_file(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-graph2", "pkg/good.py", "tests/test_good.py"
    )
    seen: dict = {}

    def fake_upsert(repo_root: Path, nodes: list[dict]) -> dict:
        seen["repo_root"] = repo_root
        seen["nodes"] = nodes
        return {"ok": True, "written": True}

    monkeypatch.setattr(graph_module, "graph_upsert", fake_upsert)

    result = ledger.graph_upsert(
        "coder-p1-module-1-graph2",
        coder["agent_id"],
        [{"id": "swarmtest_good", "kind": "function", "anchors": ["pkg/good.py#add"]}],
    )
    assert result == {"ok": True, "written": True}
    assert seen["repo_root"] == ledger.repo_root
    assert seen["nodes"][0]["id"] == "swarmtest_good"


def test_graph_upsert_refuses_a_node_edit_without_anchors_on_another_files_node(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-graph4", "pkg/good.py", "tests/test_good.py"
    )
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})

    with pytest.raises(LedgerError, match="updates only the nodes"):
        ledger.graph_upsert(
            "coder-p1-module-1-graph4",
            coder["agent_id"],
            [{"id": "swarmtest_unmapped", "description": "rewritten"}],
        )
    assert ledger.graph_upsert(
        "coder-p1-module-1-graph4",
        coder["agent_id"],
        [{"id": "swarmtest_good", "description": "rewritten"}],
    ) == {"ok": True}


@pytest.mark.parametrize("anchors", [[], ["pkg/good.py#good"]])
def test_graph_upsert_refuses_new_anchors_on_another_files_node(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch, anchors: list[str]
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-graph5", "pkg/good.py", "tests/test_good.py"
    )
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})

    with pytest.raises(LedgerError, match="updates only the nodes"):
        ledger.graph_upsert(
            "coder-p1-module-1-graph5",
            coder["agent_id"],
            [{"id": "swarmtest_unmapped", "anchors": anchors, "description": "taken over"}],
        )


@pytest.mark.integration
def test_graph_upsert_integration_calls_the_real_codebase_kg(ledger: Ledger, kg_root: Path) -> None:
    del kg_root
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(
        ledger, ctx, "coder-p1-module-1-graph3", "pkg/good.py", "tests/test_good.py"
    )

    result = ledger.graph_upsert(
        "coder-p1-module-1-graph3",
        coder["agent_id"],
        [
            {
                "id": "swarmtest_good",
                "kind": "function",
                "description": "updated by the integration test",
                "anchors": ["pkg/good.py#add"],
            }
        ],
    )
    assert result.get("ok") is True


def _shared_node(host: Path) -> None:
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute("INSERT INTO node (id, kind, description) VALUES ('shared', 'module', 'd')")
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES "
            "('shared', 0, 'pkg/unmapped.py', 'unmapped.py', 'mul'), "
            "('shared', 1, 'pkg/good.py', 'good.py', 'add')"
        )
        conn.commit()
    finally:
        conn.close()


def test_a_coder_adds_anchors_on_its_own_file_to_a_node_that_spans_files(
    ledger: Ledger, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sh1", "pkg/good.py", "tests/test_good.py")
    _shared_node(host)
    sent: list[list[dict]] = []

    def fake_upsert(repo_root: Path, nodes: list[dict]) -> dict:
        del repo_root
        sent.append(nodes)
        return {"ok": True}

    monkeypatch.setattr(graph_module, "graph_upsert", fake_upsert)
    name, agent = "coder-p1-module-1-sh1", coder["agent_id"]
    added = {
        "id": "shared",
        "anchors": [
            "pkg/unmapped.py#mul",
            "pkg/good.py#add",
            "pkg/good.py#helper",
            "tests/test_good.py#test_add",
        ],
    }
    assert ledger.graph_upsert(name, agent, [added]) == {"ok": True}
    assert ledger.graph_upsert(name, agent, [{"id": "shared"}]) == {"ok": True}
    assert len(sent) == 2

    refused = [
        {**added, "description": "rewritten"},
        {"id": "shared", "anchors": ["pkg/good.py#add", "pkg/good.py#helper"]},
        {"id": "shared", "anchors": [*added["anchors"], "pkg/unmapped.py#div"]},
        {"id": "swarmtest_unmapped"},
    ]
    for node in refused:
        with pytest.raises(LedgerError, match="may only add anchors on its own file"):
            ledger.graph_upsert(name, agent, [node])
    assert len(sent) == 2


def test_a_coder_cannot_re_baseline_another_files_change_on_a_shared_node(
    ledger: Ledger, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sh2", "pkg/good.py", "tests/test_good.py")
    _shared_node(host)
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})
    _record_baseline(host, "pkg/unmapped.py")
    touch_module(host, "unmapped", "another coder's edit")

    with pytest.raises(LedgerError, match=r"also anchors pkg/unmapped\.py, which changed"):
        ledger.graph_upsert("coder-p1-module-1-sh2", coder["agent_id"], [{"id": "shared"}])


@pytest.mark.integration
def test_a_coder_re_baselines_only_its_own_file_on_a_shared_node(
    ledger: Ledger, host: Path, kg_root: Path
) -> None:
    del kg_root
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sh3", "pkg/good.py", "tests/test_good.py")
    _shared_node(host)
    _record_baseline(host, "pkg/unmapped.py")
    _record_baseline(host, "pkg/good.py")
    touch_module(host, "good", "reworked")
    assert graph_module.stale_files(host, ["pkg/good.py"]) == {
        "pkg/good.py": ["shared", "swarmtest_good"]
    }

    ledger.graph_upsert("coder-p1-module-1-sh3", coder["agent_id"], [{"id": "shared"}])
    assert graph_module.stale_files(host, ["pkg/good.py", "pkg/unmapped.py"]) == {}


# -- issue lifecycle ---------------------------------------------------------------


def test_lead_review_closes_resolved_issues_and_dedupes_open_ones(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    lead = ctx["lead"]["agent_id"]
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-iss", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    low = all_ratings(10, overrides={"performance": 4})
    ledger.score_record(
        "coder-p1-module-1-iss", coder["agent_id"], file_id, low, all_applicable(), "self"
    )
    first = ledger.handoff_submit("coder-p1-module-1-iss", coder["agent_id"], file_id, [], [])
    opened = ledger.score_record("lead-p1-module-1", lead, file_id, low, all_applicable(), "lead")
    assert opened["issues"]
    open_now = [
        i for i in ledger.issue_list("lead-p1-module-1", lead, file_id) if i["state"] == "open"
    ]
    assert len(open_now) == len(opened["issues"])
    ledger.review_compare("lead-p1-module-1", lead, first["handoff_id"])
    ledger.return_work("lead-p1-module-1", lead, first["handoff_id"], ["slow"], ["performance"])

    ledger.score_record(
        "coder-p1-module-1-iss",
        coder["agent_id"],
        file_id,
        all_ratings(10),
        all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-iss")
    second = ledger.handoff_submit("coder-p1-module-1-iss", coder["agent_id"], file_id, [], [])
    closed = ledger.score_record(
        "lead-p1-module-1", lead, file_id, all_ratings(10), all_applicable(), "lead"
    )
    assert sorted(closed["closed_issues"]) == sorted(opened["issues"])
    assert closed["issues"] == []
    ledger.review_compare("lead-p1-module-1", lead, second["handoff_id"])
    assert ledger.approve("lead-p1-module-1", lead, second["handoff_id"])["state"] == "approved"

    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    returns = text.split("## Returns and fix attempts")[1].split("## Open items")[0]
    assert (
        "pkg/good.py, fix round 1, returned by lead-p1-module-1 -- targeted: performance" in returns
    )
    assert "  - slow" in returns
    assert "attempts): " in text.split("## Open items")[1]


def test_issue_close_is_for_the_lead_manager_or_oracle(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    lead = ctx["lead"]["agent_id"]
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-cl", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    issue = ledger.issue_open("lead-p1-module-1", lead, file_id, "Stray", "Found.")
    with pytest.raises(LedgerError):
        ledger.issue_close("coder-p1-module-1-cl", coder["agent_id"], issue["issue_id"], "fixed")
    closed = ledger.issue_close("lead-p1-module-1", lead, issue["issue_id"], "fixed in review")
    assert closed["state"] == "closed"
    with pytest.raises(LedgerError, match="already closed"):
        ledger.issue_close("oracle", ctx["oracle_id"], issue["issue_id"], "again")


# -- readable refusals -------------------------------------------------------------


def test_score_record_names_the_shape_on_a_malformed_rating(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-shape", "pkg/good.py", "tests/test_good.py")
    file_id = file_id_for(ledger, "pkg/good.py")
    with pytest.raises(LedgerError, match=r"missing \['criterion', 'value'\].*meets_the_brief"):
        ledger.score_record(
            "coder-p1-module-1-shape",
            coder["agent_id"],
            file_id,
            [{"dimension": "Functionality", "score": 10}],
            all_applicable(),
            "self",
        )
    with pytest.raises(LedgerError, match="unknown dimension 'Functionality'.*Keys: "):
        ledger.score_record(
            "coder-p1-module-1-shape",
            coder["agent_id"],
            file_id,
            [{"dimension": "Functionality", "criterion": "x", "value": 10}],
            all_applicable(),
            "self",
        )


def test_graph_upsert_refuses_an_edge_to_an_unknown_node(host: Path) -> None:
    from swarm_ledger import graph

    with pytest.raises(LedgerError, match="edge to 'nowhere'"):
        graph.graph_upsert(
            host,
            [
                {
                    "id": "x",
                    "kind": "python module",
                    "description": "d",
                    "anchors": ["pkg/good.py"],
                    "edges": ["nowhere"],
                }
            ],
        )
