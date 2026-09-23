from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from swarm_ledger import graph as graph_module
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

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


def _write_module(host: Path, name: str, symbol: str) -> None:
    (host / "pkg" / f"{name}.py").write_text(
        f"def {symbol}(a, b):\n    return a + b\n", encoding="utf-8", newline="\n"
    )
    (host / "tests" / f"test_{name}.py").write_text(
        f"from pkg.{name} import {symbol}\n\n\n"
        f"def test_{symbol}():\n    assert {symbol}(1, 2) == 3\n",
        encoding="utf-8",
        newline="\n",
    )


def _touch_module(host: Path, name: str, marker: str) -> None:
    path = host / "pkg" / f"{name}.py"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n# {marker}\n", encoding="utf-8", newline="\n"
    )


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()

    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", f"test_command: {_TEST_COMMAND}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")

    for name, symbol in _GRAPH_MODULES.items():
        _write_module(root, name, symbol)
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

    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "manager-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("manager-1", "mgr-agent")

    module = ledger.module_add("manager-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "manager-1", "mgr-agent", "lead-1", "lead", "sonnet", "Own module-1.", module_id=module_id
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-1", "lead-agent")

    return {
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _spawn_coder(ledger: Ledger, ctx: dict, coder_name: str, path: str, test_path: str) -> dict:
    claimed = ledger.claim_file("lead-1", ctx["lead"]["agent_id"], path, test_path, coder_name)
    ledger.brief_create(
        "lead-1",
        ctx["lead"]["agent_id"],
        coder_name,
        "coder",
        "sonnet",
        "Implement it.",
        module_id=ctx["module_id"],
        file_id=claimed["file_id"],
    )
    agent_id = f"{coder_name}-agent"
    ledger.agent_register_start(agent_id, "coder", parent_agent_id=ctx["lead"]["agent_id"])
    return ledger.brief_ack(coder_name, agent_id)


def _all_ratings(
    value: int = 10, reason: str = "needs work", overrides: dict[str, int] | None = None
) -> list[dict]:
    overrides = overrides or {}
    ratings = []
    for dim_key, _, criteria in DIMENSIONS:
        dim_value = overrides.get(dim_key, value)
        for criterion_key, _ in criteria:
            ratings.append(
                {
                    "dimension": dim_key,
                    "criterion": criterion_key,
                    "value": dim_value,
                    "reason": reason if dim_value < 9 else None,
                }
            )
    return ratings


def _all_applicable() -> dict[str, str | None]:
    return {dim_key: None for dim_key, _, _ in DIMENSIONS}


def _file_id_for(ledger: Ledger, path: str) -> int:
    return ledger.who_owns(path)["file"]["file_id"]


# -- handoff_submit refusals -----------------------------------------------------


def test_handoff_refused_on_failing_tests(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-fail", "pkg/failing.py", "tests/test_failing.py")
    file_id = _file_id_for(ledger, "pkg/failing.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-fail", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_zero_tests(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-zero", "pkg/zero.py", "tests/test_zero.py")
    file_id = _file_id_for(ledger, "pkg/zero.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-zero", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_skipped_test(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-skip", "pkg/skipped.py", "tests/test_skipped.py")
    file_id = _file_id_for(ledger, "pkg/skipped.py")
    with pytest.raises(LedgerError, match="tests are not passing"):
        ledger.handoff_submit("coder-skip", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_missing_node(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-nograph", "pkg/nograph.py", "tests/test_nograph.py")
    file_id = _file_id_for(ledger, "pkg/nograph.py")
    with pytest.raises(LedgerError, match="no node anchors"):
        ledger.handoff_submit("coder-nograph", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_an_unmapped_symbol(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-unmapped", "pkg/unmapped.py", "tests/test_unmapped.py")
    file_id = _file_id_for(ledger, "pkg/unmapped.py")
    with pytest.raises(LedgerError, match="is unmapped"):
        ledger.handoff_submit("coder-unmapped", coder["agent_id"], file_id, [], [])


def test_handoff_refused_on_a_stale_self_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-stale", "pkg/stale.py", "tests/test_stale.py")
    file_id = _file_id_for(ledger, "pkg/stale.py")

    review = ledger.score_record(
        "coder-stale", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
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
        ledger.handoff_submit("coder-stale", coder["agent_id"], file_id, [], [])


# -- a clean handoff ----------------------------------------------------------------


def test_handoff_succeeds_and_saves_two_versions(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )

    handoff = ledger.handoff_submit("coder-good", coder["agent_id"], file_id, [], [])
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
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-blind1", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-blind1", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-blind1", coder["agent_id"], file_id, [], [])

    with pytest.raises(LedgerError, match="no lead review exists"):
        ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_score_record_lead_refuses_after_review_compare_already_ran(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-blind2", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-blind2", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-blind2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    with pytest.raises(LedgerError, match="blind scoring is closed"):
        ledger.score_record(
            "lead-1",
            ctx["lead"]["agent_id"],
            file_id,
            _all_ratings(10),
            _all_applicable(),
            "lead",
        )


# -- approve ------------------------------------------------------------------------


def test_approve_refused_without_compare(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-appr1", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-appr1", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-appr1", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(10), _all_applicable(), "lead"
    )

    with pytest.raises(LedgerError, match="review_compare has not run"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_refused_with_an_open_issue(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-appr2", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-appr2", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-appr2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(10), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.issue_open("lead-1", ctx["lead"]["agent_id"], file_id, "Stray issue", "Found later.")

    with pytest.raises(LedgerError, match="open issue"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_releases_the_claim_and_the_coder(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-appr3", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-appr3", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-appr3", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(10), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    approved = ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "nice work")
    assert approved["state"] == "approved"

    file_row = ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    assert file_row["state"] == "approved"
    assert file_row["released_at"] is not None

    coder_row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (coder["agent_id"],)
    ).fetchone()
    assert coder_row["state"] == "released"
    assert coder_row["ended_at"] is not None


# -- return, a second handoff, and attempt_record ------------------------------------------


def _round_trip(
    ledger: Ledger,
    ctx: dict,
    name: str,
    symbol: str,
    round1_overrides: dict[str, int],
    round2_overrides: dict[str, int],
    targeted: list[str],
) -> str:
    coder_name = f"coder-{name}"
    path, test_path = f"pkg/{name}.py", f"tests/test_{name}.py"
    coder = _spawn_coder(ledger, ctx, coder_name, path, test_path)
    file_id = _file_id_for(ledger, path)

    ledger.score_record(
        coder_name, coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff1 = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides=round1_overrides),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff1["handoff_id"])
    ledger.return_work("lead-1", ctx["lead"]["agent_id"], handoff1["handoff_id"], [], targeted)
    returned = ledger.conn.execute(
        "SELECT state FROM agents WHERE agent_id = ?", (coder["agent_id"],)
    ).fetchone()
    assert returned["state"] == "idle"

    _touch_module(ledger.repo_root, name, "revised")
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides=round2_overrides),
        _all_applicable(),
        "lead",
    )
    handoff2_id = ledger.conn.execute(
        "SELECT handoff_id FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
        (file_id,),
    ).fetchone()["handoff_id"]
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff2_id)

    outcome = ledger.attempt_record("lead-1", ctx["lead"]["agent_id"], file_id)["outcome"]
    return outcome


def test_attempt_record_classifies_improved(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    outcome = _round_trip(
        ledger,
        ctx,
        "round_improved",
        "bump_a",
        round1_overrides={"testing": 5},
        round2_overrides={},
        targeted=["testing"],
    )
    assert outcome == "improved"


def test_attempt_record_classifies_plateau(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    outcome = _round_trip(
        ledger,
        ctx,
        "round_plateau",
        "bump_b",
        round1_overrides={"testing": 8},
        round2_overrides={"testing": 8},
        targeted=["testing"],
    )
    assert outcome == "plateau"


def test_attempt_record_classifies_regression_and_restores_the_version(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    name = "round_regression"
    path = ledger_path_for(ledger, name)
    original_content = path.read_text(encoding="utf-8")

    outcome = _round_trip(
        ledger,
        ctx,
        name,
        "bump_c",
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
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-incomplete", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-incomplete",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-incomplete", coder["agent_id"], file_id, [], [])

    deferral = ledger.accept_incomplete(
        "lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "blocked on an external API"
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
        "manager-1",
        ctx["manager"]["agent_id"],
        deferral["deferral_id"],
        "agreed",
        "acceptable for this run",
    )
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    result = ledger.run_finish("oracle", ctx["oracle_id"], "success")
    assert result["state"] == "finished"


def test_agreement_decide_refuses_a_decider_below_the_proposers_parent_role(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-defer", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-defer", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-defer", coder["agent_id"], file_id, [], [])
    deferral = ledger.accept_incomplete(
        "lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "blocked"
    )

    with pytest.raises(LedgerError):
        ledger.agreement_decide(
            "lead-1", ctx["lead"]["agent_id"], deferral["deferral_id"], "agreed", "ok"
        )


# -- analytics_query and report_build ------------------------------------------------------


def test_analytics_query_rejects_non_select(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.analytics_query("oracle", ctx["oracle_id"], "DELETE FROM agents")
    with pytest.raises(LedgerError):
        ledger.analytics_query("oracle", ctx["oracle_id"], "SELECT 1; DROP TABLE agents")


def test_analytics_query_runs_a_select(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    result = ledger.analytics_query("oracle", ctx["oracle_id"], "SELECT COUNT(*) AS n FROM agents")
    assert result["rows"][0]["n"] >= 1


def test_report_build_writes_the_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    result = ledger.report_build("oracle", ctx["oracle_id"])
    assert Path(result["path"]).is_file()
    assert "Run report" in result["text"]
    assert Path(result["path"]).read_text(encoding="utf-8") == result["text"]


# -- graph_upsert (Ledger method) ------------------------------------------------------------


def test_graph_upsert_refuses_an_anchor_outside_the_owned_file(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-graph1", "pkg/good.py", "tests/test_good.py")
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})

    with pytest.raises(LedgerError, match="updates only the nodes"):
        ledger.graph_upsert(
            "coder-graph1",
            coder["agent_id"],
            [{"id": "x", "kind": "function", "anchors": ["pkg/other.py#foo"]}],
        )


def test_graph_upsert_passes_through_for_the_owned_file(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-graph2", "pkg/good.py", "tests/test_good.py")
    seen: dict = {}

    def fake_upsert(repo_root: Path, nodes: list[dict]) -> dict:
        seen["repo_root"] = repo_root
        seen["nodes"] = nodes
        return {"ok": True, "written": True}

    monkeypatch.setattr(graph_module, "graph_upsert", fake_upsert)

    result = ledger.graph_upsert(
        "coder-graph2",
        coder["agent_id"],
        [{"id": "swarmtest_good", "kind": "function", "anchors": ["pkg/good.py#add"]}],
    )
    assert result == {"ok": True, "written": True}
    assert seen["repo_root"] == ledger.repo_root
    assert seen["nodes"][0]["id"] == "swarmtest_good"


@pytest.mark.integration
def test_graph_upsert_integration_calls_the_real_codebase_kg(ledger: Ledger) -> None:
    try:
        graph_module.codebase_kg_root()
    except LedgerError:
        pytest.skip("codebase-kg is not installed in this environment")

    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-graph3", "pkg/good.py", "tests/test_good.py")

    result = ledger.graph_upsert(
        "coder-graph3",
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


# -- issue lifecycle ---------------------------------------------------------------


def test_lead_review_closes_resolved_issues_and_dedupes_open_ones(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead = ctx["lead"]["agent_id"]
    coder = _spawn_coder(ledger, ctx, "coder-iss", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    low = _all_ratings(10, overrides={"performance": 4})
    ledger.score_record("coder-iss", coder["agent_id"], file_id, low, _all_applicable(), "self")
    first = ledger.handoff_submit("coder-iss", coder["agent_id"], file_id, [], [])
    opened = ledger.score_record("lead-1", lead, file_id, low, _all_applicable(), "lead")
    assert opened["issues"]
    open_now = [i for i in ledger.issue_list("lead-1", lead, file_id) if i["state"] == "open"]
    assert len(open_now) == len(opened["issues"])
    ledger.review_compare("lead-1", lead, first["handoff_id"])
    ledger.return_work("lead-1", lead, first["handoff_id"], ["slow"], ["performance"])

    ledger.score_record(
        "coder-iss", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    second = ledger.handoff_submit("coder-iss", coder["agent_id"], file_id, [], [])
    closed = ledger.score_record(
        "lead-1", lead, file_id, _all_ratings(10), _all_applicable(), "lead"
    )
    assert sorted(closed["closed_issues"]) == sorted(opened["issues"])
    assert closed["issues"] == []
    ledger.review_compare("lead-1", lead, second["handoff_id"])
    assert ledger.approve("lead-1", lead, second["handoff_id"])["state"] == "approved"


def test_issue_close_is_for_the_lead_manager_or_oracle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead = ctx["lead"]["agent_id"]
    coder = _spawn_coder(ledger, ctx, "coder-cl", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    issue = ledger.issue_open("lead-1", lead, file_id, "Stray", "Found.")
    with pytest.raises(LedgerError):
        ledger.issue_close("coder-cl", coder["agent_id"], issue["issue_id"], "fixed")
    closed = ledger.issue_close("lead-1", lead, issue["issue_id"], "fixed in review")
    assert closed["state"] == "closed"
    with pytest.raises(LedgerError, match="already closed"):
        ledger.issue_close("oracle", ctx["oracle_id"], issue["issue_id"], "again")


# -- readable refusals -------------------------------------------------------------


def test_score_record_names_the_shape_on_a_malformed_rating(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-shape", "pkg/good.py", "tests/test_good.py")
    file_id = _file_id_for(ledger, "pkg/good.py")
    with pytest.raises(LedgerError, match=r"missing \['criterion', 'value'\].*meets_the_brief"):
        ledger.score_record(
            "coder-shape",
            coder["agent_id"],
            file_id,
            [{"dimension": "Functionality", "score": 10}],
            _all_applicable(),
            "self",
        )
    with pytest.raises(LedgerError, match="unknown dimension 'Functionality'.*Keys: "):
        ledger.score_record(
            "coder-shape",
            coder["agent_id"],
            file_id,
            [{"dimension": "Functionality", "criterion": "x", "value": 10}],
            _all_applicable(),
            "self",
        )


def test_graph_upsert_refuses_an_edge_to_an_unknown_node(ledger: Ledger, host: Path) -> None:
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
