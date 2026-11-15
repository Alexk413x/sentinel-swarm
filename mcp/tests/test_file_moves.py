from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from helpers import (
    all_applicable,
    bootstrap_module,
    ensure_graph_source_base,
    make_host,
    settings_text,
    spawn_coder,
)

from swarm_ledger import graph
from swarm_ledger import graph as graph_module
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"
_LEAD = "lead-p1-module-1"
_CODER = "coder-p1-module-1-old"
_OLD = "pkg/old.py"
_OLD_TEST = "tests/test_old.py"
_NEW = "pkg/moved/new.py"
_NEW_TEST = "tests/test_new.py"
_SOURCE = "def add(a, b):\n    return a + b\n"
_TEST = "def test_ok():\n    assert True\n"

pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = make_host(tmp_path / "host", settings_text(test_command=_TEST_COMMAND))
    for rel in (_OLD, "pkg/other.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(_SOURCE, encoding="utf-8")
    (root / "tests").mkdir()
    (root / _OLD_TEST).write_text(_TEST, encoding="utf-8")
    knowledge = root / "knowledge"
    knowledge.mkdir()
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", knowledge / "code_graph.db")
    conn = sqlite3.connect(str(knowledge / "code_graph.db"))
    try:
        for node, anchors in (
            ("swarmtest_chrome", [(_OLD, "add")]),
            ("swarmtest_span", [(_OLD, "add"), ("pkg/other.py", "add")]),
        ):
            conn.execute(
                "INSERT INTO node (id, kind, description) VALUES (?, 'module', 'test fixture')",
                (node,),
            )
            for ordinal, (path, symbol) in enumerate(anchors):
                conn.execute(
                    "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, ?, ?, ?, ?)",
                    (node, ordinal, path, Path(path).name, symbol),
                )
        conn.commit()
    finally:
        conn.close()
    ensure_graph_source_base(root)
    return root


@pytest.fixture
def upserts(monkeypatch: pytest.MonkeyPatch) -> list[list[dict]]:
    sent: list[list[dict]] = []

    def fake_upsert(repo_root: Path, nodes: list[dict]) -> dict:
        del repo_root
        sent.append(nodes)
        return {"ok": True}

    monkeypatch.setattr(graph_module, "graph_upsert", fake_upsert)
    return sent


def _ratings() -> list[dict]:
    return [
        {"dimension": dim, "criterion": criterion, "value": 10}
        for dim, _, criteria in DIMENSIONS
        for criterion, _ in criteria
    ]


def _file(ledger: Ledger, file_id: int) -> dict:
    return dict(ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone())


def _moves(ledger: Ledger, file_id: int) -> list[str]:
    return [
        row["path"]
        for row in ledger.conn.execute(
            "SELECT path FROM file_moves WHERE file_id = ? ORDER BY path", (file_id,)
        )
    ]


def _write_new_files(host: Path) -> None:
    (host / _NEW).parent.mkdir(parents=True, exist_ok=True)
    (host / _NEW).write_text(_SOURCE, encoding="utf-8")
    (host / _NEW_TEST).write_text(_TEST, encoding="utf-8")


def _coder(ledger: Ledger) -> tuple[dict, dict]:
    ctx = bootstrap_module(ledger)
    return ctx, spawn_coder(ledger, ctx, _CODER, _OLD, _OLD_TEST)


def _move(ledger: Ledger, ctx: dict, **overrides: object) -> dict:
    args: dict = {"path": _OLD, "new_path": _NEW, "test_path": _NEW_TEST} | overrides
    return ledger.file_move(_LEAD, ctx["lead"]["agent_id"], **args)


def _shell(ledger: Ledger, coder: dict, command: str) -> dict | None:
    return events.handle_pre_shell(
        ledger, {"agent_id": coder["agent_id"], "tool_input": {"command": command}}
    )


def _hand_off(ledger: Ledger, coder: dict) -> dict:
    file_id = coder["file_id"]
    stale_since = _file(ledger, file_id)["stale_since"]
    while stale_since is not None and (
        ledger.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now").fetchone()["now"]
        <= stale_since
    ):
        time.sleep(0.001)
    ledger.score_record(
        coder["name"], coder["agent_id"], file_id, _ratings(), all_applicable(), "self"
    )
    return ledger.handoff_submit(coder["name"], coder["agent_id"], file_id, [], [])


# -- moving a live claim ------------------------------------------------------------------------


def test_file_move_repoints_the_claim_and_its_test_path(ledger: Ledger, host: Path) -> None:
    ctx, coder = _coder(ledger)

    moved = _move(ledger, ctx)

    assert (moved["path"], moved["test_path"]) == (_NEW, _NEW_TEST)
    assert moved["file_id"] == coder["file_id"]
    assert ledger.who_owns(_OLD)["owner"] is None
    assert ledger.who_owns(_NEW)["owner"] == _CODER
    assert _moves(ledger, coder["file_id"]) == sorted([_OLD, _OLD_TEST])
    assert "message_post" in moved["next"]
    _write_new_files(host)
    write = {"agent_id": coder["agent_id"], "tool_input": {"file_path": _NEW}}
    assert events.handle_pre_write(ledger, write) is None


def test_the_coder_runs_tests_on_the_moved_test_path(ledger: Ledger, host: Path) -> None:
    ctx, coder = _coder(ledger)
    with pytest.raises(LedgerError, match="may only run tests for its own file"):
        ledger.tests_run(_CODER, coder["agent_id"], "file", _NEW_TEST)
    _move(ledger, ctx)
    _write_new_files(host)

    result = ledger.tests_run(_CODER, coder["agent_id"], "file", _NEW_TEST)

    assert result["ok"] is True


def test_file_move_keeps_the_test_path_when_none_is_given(ledger: Ledger) -> None:
    ctx, coder = _coder(ledger)

    moved = _move(ledger, ctx, test_path=None)

    assert moved["test_path"] == _OLD_TEST
    assert _moves(ledger, coder["file_id"]) == [_OLD]


def test_file_move_refuses_what_it_cannot_move(ledger: Ledger) -> None:
    ctx, coder = _coder(ledger)
    with pytest.raises(LedgerError, match="no live claim"):
        _move(ledger, ctx, path="pkg/other.py")
    with pytest.raises(LedgerError, match="nothing to move"):
        _move(ledger, ctx, new_path=_OLD, test_path=None)
    ledger.claim_file(_LEAD, ctx["lead"]["agent_id"], "pkg/taken.py", None, "coder-p1-module-1-t")
    with pytest.raises(LedgerError, match="already has a live claim"):
        _move(ledger, ctx, new_path="pkg/taken.py")
    with pytest.raises(LedgerError, match="already has a live claim"):
        _move(ledger, ctx, test_path="pkg/taken.py")
    with pytest.raises(LedgerError, match="may not call this"):
        ledger.file_move("mgr-p1-phase-1", ctx["manager"]["agent_id"], _OLD, _NEW, _NEW_TEST)
    _hand_off(ledger, coder)
    with pytest.raises(LedgerError, match="handed up"):
        _move(ledger, ctx)


# -- a claim that names the files it replaces ---------------------------------------------------


def test_claim_file_records_the_paths_the_file_replaces(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)

    claimed = ledger.claim_file(
        _LEAD,
        ctx["lead"]["agent_id"],
        _NEW,
        _NEW_TEST,
        "coder-p1-module-1-new",
        moved_from=[_OLD, _OLD_TEST],
    )

    assert _moves(ledger, claimed["file_id"]) == sorted([_OLD, _OLD_TEST])


def test_claim_file_refuses_a_replaced_path_another_claim_holds(ledger: Ledger) -> None:
    ctx, _coder_row = _coder(ledger)

    with pytest.raises(LedgerError, match="live claim"):
        ledger.claim_file(
            _LEAD,
            ctx["lead"]["agent_id"],
            _NEW,
            _NEW_TEST,
            "coder-p1-module-1-new",
            moved_from=[_OLD],
        )


# -- re-anchoring a node from the old path to the new one --------------------------------------


def test_a_coder_cannot_re_anchor_a_node_before_its_file_moves(
    ledger: Ledger, upserts: list[list[dict]]
) -> None:
    _, coder = _coder(ledger)
    node = {"id": "swarmtest_chrome", "anchors": [f"{_NEW}#add"]}

    with pytest.raises(LedgerError, match="ask your Lead"):
        ledger.graph_upsert(_CODER, coder["agent_id"], [node])
    assert upserts == []


def test_a_coder_re_anchors_a_single_file_node_from_the_old_path_to_the_new(
    ledger: Ledger, upserts: list[list[dict]]
) -> None:
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    node = {"id": "swarmtest_chrome", "anchors": [f"{_NEW}#add"]}

    assert ledger.graph_upsert(_CODER, coder["agent_id"], [node]) == {"ok": True}
    assert upserts == [[node]]


def test_a_coder_drops_the_old_anchor_from_a_node_that_spans_files(
    ledger: Ledger, upserts: list[list[dict]]
) -> None:
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    node = {"id": "swarmtest_span", "anchors": [f"{_NEW}#add", "pkg/other.py#add"]}

    assert ledger.graph_upsert(_CODER, coder["agent_id"], [node]) == {"ok": True}
    assert upserts == [[node]]


def test_a_coder_still_may_not_drop_or_add_anchors_on_other_files(
    ledger: Ledger, upserts: list[list[dict]]
) -> None:
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    dropped_other = {"id": "swarmtest_span", "anchors": [f"{_NEW}#add"]}
    added_third = {
        "id": "swarmtest_span",
        "anchors": [f"{_NEW}#add", "pkg/other.py#add", "pkg/third.py"],
    }

    for node in (dropped_other, added_third):
        with pytest.raises(LedgerError, match="ask your Lead"):
            ledger.graph_upsert(_CODER, coder["agent_id"], [node])
    assert upserts == []


def test_the_lead_updates_a_spanning_node_that_still_names_the_old_path(
    ledger: Ledger, upserts: list[list[dict]]
) -> None:
    ctx, _ = _coder(ledger)
    _move(ledger, ctx)
    node = {"id": "swarmtest_span", "anchors": [f"{_NEW}#add", "pkg/other.py#add"]}

    assert ledger.graph_upsert(_LEAD, ctx["lead"]["agent_id"], [node]) == {"ok": True}
    assert upserts == [[node]]


# -- deleting the old file ----------------------------------------------------------------------


def test_the_coder_deletes_the_file_it_replaces_but_no_other(ledger: Ledger, host: Path) -> None:
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    _write_new_files(host)

    deleted = ledger.file_delete(_CODER, coder["agent_id"], _OLD)

    assert deleted["deleted"] is True
    assert not (host / _OLD).exists()
    with pytest.raises(LedgerError, match="only its own"):
        ledger.file_delete(_CODER, coder["agent_id"], "pkg/other.py")


def test_the_coders_shell_allows_git_rm_of_a_replaced_path(ledger: Ledger, host: Path) -> None:
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    _write_new_files(host)

    assert _shell(ledger, coder, f"git rm {_OLD} {_OLD_TEST}") is None
    assert _file(ledger, coder["file_id"])["stale_since"] is not None
    assert _shell(ledger, coder, "git rm pkg/other.py") is not None


def test_the_replaced_paths_stay_in_the_fingerprint_of_a_test_run(
    ledger: Ledger,
) -> None:
    ctx, _ = _coder(ledger)
    _move(ledger, ctx)

    assert _OLD in ledger._claimed_paths(ctx["run_id"])
    assert _OLD_TEST in ledger._claimed_paths(ctx["run_id"])


# -- the shell gate names file_delete -----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item pkg/old.py",
        "Remove-Item -Force -Path pkg\\old.py",
        "rm pkg/old.py",
        "del pkg\\old.py",
        "erase pkg\\old.py",
        "rmdir pkg",
        "git rm pkg/other.py",
    ],
)
def test_a_denied_delete_command_names_file_delete(ledger: Ledger, command: str) -> None:
    _, coder = _coder(ledger)

    denied = _shell(ledger, coder, command)

    assert denied is not None
    text = str(denied)
    assert "file_delete(path)" in text
    assert _OLD in text


def test_a_denied_command_that_is_not_a_delete_does_not_mention_file_delete(
    ledger: Ledger,
) -> None:
    _, coder = _coder(ledger)

    assert "file_delete" not in str(_shell(ledger, coder, "curl example.com"))


# -- a deletion handoff and the graph -----------------------------------------------------------


@pytest.mark.integration
def test_a_deletion_handoff_drops_the_deleted_files_anchors(
    ledger: Ledger, host: Path, kg_root: Path
) -> None:
    del kg_root
    _, coder = _coder(ledger)
    ledger.file_delete(_CODER, coder["agent_id"], _OLD)
    ledger.file_delete(_CODER, coder["agent_id"], _OLD_TEST)

    handoff = _hand_off(ledger, coder)

    assert handoff["deleted"] is True
    assert handoff["graph_pruned"] == {
        "deleted_nodes": ["swarmtest_chrome"],
        "reanchored_nodes": ["swarmtest_span"],
    }
    assert graph.anchored_paths(host, [_OLD]) == set()
    assert graph.node_anchors(host, "swarmtest_chrome") == []
    assert graph.node_anchors(host, "swarmtest_span") == [("pkg/other.py", "add")]


@pytest.mark.integration
def test_an_edge_to_a_deleted_node_is_dropped_with_it(
    ledger: Ledger, host: Path, kg_root: Path
) -> None:
    del kg_root
    graph_path = host / "knowledge" / "code_graph.db"
    conn = sqlite3.connect(str(graph_path))
    try:
        conn.execute("INSERT INTO edge (src, dst) VALUES ('swarmtest_span', 'swarmtest_chrome')")
        conn.commit()
    finally:
        conn.close()
    _, coder = _coder(ledger)
    ledger.file_delete(_CODER, coder["agent_id"], _OLD)
    ledger.file_delete(_CODER, coder["agent_id"], _OLD_TEST)

    _hand_off(ledger, coder)

    conn = sqlite3.connect(f"file:{graph_path.as_posix()}?mode=ro", uri=True)
    try:
        edges = conn.execute("SELECT COUNT(*) FROM edge WHERE dst = 'swarmtest_chrome'").fetchone()
    finally:
        conn.close()
    assert edges == (0,)


@pytest.mark.integration
def test_a_moved_files_handoff_needs_the_old_file_gone_and_its_nodes_re_anchored(
    ledger: Ledger, host: Path, kg_root: Path
) -> None:
    del kg_root
    ctx, coder = _coder(ledger)
    _move(ledger, ctx)
    _write_new_files(host)
    ledger.file_delete(_CODER, coder["agent_id"], _OLD_TEST)
    ledger.score_record(
        _CODER, coder["agent_id"], coder["file_id"], _ratings(), all_applicable(), "self"
    )
    with pytest.raises(LedgerError, match=r"pkg/old\.py is the file this one replaces"):
        ledger.handoff_submit(_CODER, coder["agent_id"], coder["file_id"], [], [])

    ledger.file_delete(_CODER, coder["agent_id"], _OLD)
    with pytest.raises(LedgerError, match=r"swarmtest_chrome.*graph_upsert"):
        _hand_off(ledger, coder)

    chrome = {"id": "swarmtest_chrome", "anchors": [f"{_NEW}#add"]}
    span = {"id": "swarmtest_span", "anchors": [f"{_NEW}#add", "pkg/other.py#add"]}
    ledger.graph_upsert(_CODER, coder["agent_id"], [chrome])
    ledger.graph_upsert(_LEAD, ctx["lead"]["agent_id"], [span])

    handoff = _hand_off(ledger, coder)

    assert handoff["state"] == "submitted"
    assert graph.node_anchors(host, "swarmtest_chrome") == [(_NEW, "add")]


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.py",
        "/etc/passwd",
        "C:/Windows/x.py",
        ":(glob)*",
        ".git/config",
        ".sentinel-swarm/ledger.db",
        ".claude/agents/swarm-coder.md",
        "src/../../x.py",
        ".GIT/config",
        ".Sentinel-Swarm/ledger.db",
        "vendor/lib/.git/config",
        ".git./config",
    ],
)
def test_a_claim_refuses_a_path_outside_the_repo_or_in_its_records(
    ledger: Ledger, bad: str
) -> None:
    ctx = bootstrap_module(ledger)
    lead = ("lead-p1-module-1", ctx["lead"]["agent_id"])
    with pytest.raises(LedgerError, match="repo path"):
        ledger.claim_file(*lead, bad, None, "coder-p1-module-1-x")
    with pytest.raises(LedgerError, match="repo path"):
        ledger.claim_file(*lead, "src/new.py", bad, "coder-p1-module-1-y")
    with pytest.raises(LedgerError, match="repo path"):
        ledger.claim_file(*lead, "src/new2.py", None, "coder-p1-module-1-z", moved_from=[bad])


def test_a_claim_stores_the_canonical_repo_path(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    lead = ("lead-p1-module-1", ctx["lead"]["agent_id"])
    windows_style = "src" + chr(92) + "test_x.py"
    claimed = ledger.claim_file(*lead, "./src//x.py", windows_style, "coder-p1-module-1-x")
    assert (claimed["path"], claimed["test_path"]) == ("src/x.py", "src/test_x.py")
    with pytest.raises(LedgerError):
        ledger.claim_file(*lead, "src/./x.py", None, "coder-p1-module-1-y")
