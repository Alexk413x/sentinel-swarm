from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from helpers import (
    all_applicable,
    bootstrap_module,
    make_host,
    register_agent,
    settings_text,
    spawn_coder,
)

from swarm_ledger import graph
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"
_LEAD = "lead-p1-module-1"
_BINARY = b"SQLite format 3\x00\xff\xfe\x80 not text"

pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = make_host(tmp_path / "host", settings_text(test_command=_TEST_COMMAND))
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "data").mkdir()
    (root / "pkg" / "good.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_good.py").write_text(
        "from pkg.good import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "docs" / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (root / "data" / "graph.db").write_bytes(_BINARY)

    knowledge = root / "knowledge"
    knowledge.mkdir()
    graph_path = knowledge / "code_graph.db"
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", graph_path)
    conn = sqlite3.connect(str(graph_path))
    try:
        for node, path, symbol in (
            ("swarmtest_good", "pkg/good.py", "add"),
            ("swarmtest_prd", "docs/prd.md", None),
            ("swarmtest_graph_db", "data/graph.db", "Graph"),
        ):
            conn.execute(
                "INSERT INTO node (id, kind, description) VALUES (?, 'module', 'test fixture')",
                (node,),
            )
            conn.execute(
                "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, 0, ?, ?, ?)",
                (node, path, Path(path).name, symbol),
            )
        conn.commit()
    finally:
        conn.close()
    return root


def _ratings(value: int = 10) -> list[dict]:
    return [
        {"dimension": dim, "criterion": criterion, "value": value}
        for dim, _, criteria in DIMENSIONS
        for criterion, _ in criteria
    ]


def _past_last_edit(ledger: Ledger, file_id: int) -> None:
    # handoff_submit refuses a self review stamped in the same millisecond as the last edit,
    # and SQLite's clock can repeat a stamp across back-to-back calls on Windows.
    stale_since = _file(ledger, file_id)["stale_since"]
    if stale_since is None:
        return
    while (
        ledger.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS now").fetchone()["now"]
        <= stale_since
    ):
        time.sleep(0.001)


def _hand_off(ledger: Ledger, coder: dict, file_id: int) -> dict:
    name = coder["name"]
    _past_last_edit(ledger, file_id)
    ledger.score_record(name, coder["agent_id"], file_id, _ratings(), all_applicable(), "self")
    return ledger.handoff_submit(name, coder["agent_id"], file_id, [], [])


def _lead_review(ledger: Ledger, ctx: dict, file_id: int, handoff_id: int) -> None:
    lead_id = ctx["lead"]["agent_id"]
    ledger.score_record(_LEAD, lead_id, file_id, _ratings(), all_applicable(), "lead")
    ledger.review_compare(_LEAD, lead_id, handoff_id)


def _file(ledger: Ledger, file_id: int) -> dict:
    return dict(ledger.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone())


def _versions(ledger: Ledger, file_id: int) -> list[dict]:
    return [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT * FROM versions WHERE file_id = ? ORDER BY version_id", (file_id,)
        )
    ]


# -- binary files ---------------------------------------------------------------------------


def test_the_graph_check_passes_a_binary_file_without_decoding_it(host: Path) -> None:
    assert graph.graph_current_for(host, "data/graph.db") == (True, [])


def test_handoff_submit_hashes_a_binary_file_and_records_its_version(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-db", "data/graph.db", "tests/test_good.py")
    file_id = coder["file_id"]

    handoff = _hand_off(ledger, coder, file_id)

    version = _versions(ledger, file_id)[0]
    assert handoff["version_id"] == version["version_id"]
    assert version["content"] == _BINARY
    assert version["sha256"] == hashlib.sha256(_BINARY).hexdigest()
    assert version["deleted"] == 0


# -- generated files ------------------------------------------------------------------------


def _source_and_product(ledger: Ledger, ctx: dict) -> tuple[dict, int]:
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-good", "pkg/good.py", "tests/test_good.py")
    product = ledger.claim_file(
        _LEAD,
        ctx["lead"]["agent_id"],
        "pkg/good.json",
        None,
        "coder-p1-module-1-good",
        product_of=coder["file_id"],
    )
    return coder, product["file_id"]


def test_a_generated_file_is_claimed_as_a_product_of_its_source(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder, product_id = _source_and_product(ledger, ctx)

    product = _file(ledger, product_id)
    assert product["source_file_id"] == coder["file_id"]
    assert product["owner_agent_id"] == "coder-p1-module-1-good"
    assert ledger.who_owns("pkg/good.json")["owner"] == "coder-p1-module-1-good"
    write = {"agent_id": coder["agent_id"], "tool_input": {"file_path": "pkg/good.json"}}
    assert events.handle_pre_write(ledger, write) is None


def test_a_product_claim_refuses_another_coder_or_a_test_path(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-good", "pkg/good.py", "tests/test_good.py")
    lead_id = ctx["lead"]["agent_id"]
    with pytest.raises(LedgerError, match="belongs to its source's Coder"):
        ledger.claim_file(
            _LEAD, lead_id, "pkg/x.json", None, "coder-p1-module-1-x", product_of=coder["file_id"]
        )
    with pytest.raises(LedgerError, match="no test_path"):
        ledger.claim_file(
            _LEAD,
            lead_id,
            "pkg/x.json",
            "tests/test_x.py",
            "coder-p1-module-1-good",
            product_of=coder["file_id"],
        )


def test_a_product_rides_its_sources_handoff_and_approval(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder, product_id = _source_and_product(ledger, ctx)
    (ledger.repo_root / "pkg" / "good.json").write_bytes(b'{"add": 1}\n')
    ledger.mark_stale(coder["agent_id"], "pkg/good.json")
    assert _file(ledger, coder["file_id"])["stale_since"] is not None

    handoff = _hand_off(ledger, coder, coder["file_id"])

    product_version = _versions(ledger, product_id)[0]
    assert handoff["product_version_ids"] == [product_version["version_id"]]
    assert product_version["content"] == b'{"add": 1}\n'
    assert _file(ledger, product_id)["state"] == "handed_up"

    _lead_review(ledger, ctx, coder["file_id"], handoff["handoff_id"])
    ledger.approve(_LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"])
    product = _file(ledger, product_id)
    assert product["state"] == "approved"
    assert product["released_at"] is not None


# -- deleting a file ------------------------------------------------------------------------


def test_file_delete_removes_only_the_coders_own_paths(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-good", "pkg/good.py", "tests/test_good.py")
    with pytest.raises(LedgerError, match="only its own"):
        ledger.file_delete(coder["name"], coder["agent_id"], "docs/prd.md")
    assert (ledger.repo_root / "docs" / "prd.md").is_file()

    deleted = ledger.file_delete(coder["name"], coder["agent_id"], "pkg/good.py")

    assert deleted["deleted"] is True
    assert not (ledger.repo_root / "pkg" / "good.py").exists()
    assert _file(ledger, coder["file_id"])["stale_since"] is not None


def test_a_deletion_is_the_files_version_and_is_reviewed_like_an_edit(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-prd", "docs/prd.md", None)
    ledger.file_delete(coder["name"], coder["agent_id"], "docs/prd.md")
    _past_last_edit(ledger, coder["file_id"])
    ledger.score_record(
        coder["name"], coder["agent_id"], coder["file_id"], _ratings(), all_applicable(), "self"
    )
    with pytest.raises(LedgerError, match="still anchors nodes"):
        ledger.handoff_submit(coder["name"], coder["agent_id"], coder["file_id"], [], [])

    conn = sqlite3.connect(str(ledger.repo_root / "knowledge" / "code_graph.db"))
    try:
        conn.execute("DELETE FROM anchor WHERE path = 'docs/prd.md'")
        conn.commit()
    finally:
        conn.close()
    handoff = ledger.handoff_submit(coder["name"], coder["agent_id"], coder["file_id"], [], [])

    assert handoff["deleted"] is True
    version = _versions(ledger, coder["file_id"])[0]
    assert (version["deleted"], version["content"], version["stored_path"]) == (1, b"", None)
    _lead_review(ledger, ctx, coder["file_id"], handoff["handoff_id"])
    approved = ledger.approve(_LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"])
    assert approved["state"] == "approved"


def test_restoring_a_deletion_version_deletes_the_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-prd", "docs/prd.md", None)
    with sqlite3.connect(str(ledger.repo_root / "knowledge" / "code_graph.db")) as conn:
        conn.execute("DELETE FROM anchor WHERE path = 'docs/prd.md'")
    ledger.file_delete(coder["name"], coder["agent_id"], "docs/prd.md")
    _hand_off(ledger, coder, coder["file_id"])
    version_id = _versions(ledger, coder["file_id"])[0]["version_id"]
    (ledger.repo_root / "docs" / "prd.md").write_text("# back\n", encoding="utf-8")

    ledger.version_restore(coder["name"], coder["agent_id"], version_id)

    assert not (ledger.repo_root / "docs" / "prd.md").exists()


def test_the_coders_shell_allows_git_rm_of_its_own_paths_only(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-good", "pkg/good.py", "tests/test_good.py")

    def shell(command: str) -> dict | None:
        return events.handle_pre_shell(
            ledger, {"agent_id": coder["agent_id"], "tool_input": {"command": command}}
        )

    assert shell("git rm pkg/good.py tests/test_good.py") is None
    assert _file(ledger, coder["file_id"])["stale_since"] is not None
    assert shell("git rm -q -- pkg/good.py") is None
    for command in ("git rm docs/prd.md", "git rm -r pkg", "git rm pkg/good.py docs/prd.md"):
        denied = shell(command)
        assert denied is not None, command
        assert "git rm of your own claimed paths" in str(denied)


# -- change requests on a file with no test_path ---------------------------------------------


def _cr_on_the_prd(ledger: Ledger, ctx: dict) -> tuple[dict, dict]:
    claimed = ledger.claim_file(
        _LEAD, ctx["lead"]["agent_id"], "docs/prd.md", None, "coder-p1-module-1-prd"
    )
    cr = ledger.cr_open(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], "docs/prd.md", "add the PROMO-12 row"
    )
    return claimed, cr


def _spawn_prd_coder(ledger: Ledger, ctx: dict, file_id: int) -> dict:
    name = "coder-p1-module-1-prd"
    ledger.brief_create(
        _LEAD, ctx["lead"]["agent_id"], name, "coder", "sonnet", "Write it.", file_id=file_id
    )
    register_agent(ledger, f"{name}-agent", "coder", parent_agent_id=ctx["lead"]["agent_id"])
    return ledger.brief_ack(name, f"{name}-agent") | {"file_id": file_id}


def test_a_reviewed_handoff_no_longer_deadlocks_on_its_change_requests(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    lead_id = ctx["lead"]["agent_id"]
    claimed, cr = _cr_on_the_prd(ledger, ctx)
    assert cr["to_agent_id"] == lead_id
    ledger.cr_accept(_LEAD, lead_id, cr["cr_id"], True, "the module's PRD Coder applies it")
    with pytest.raises(LedgerError, match="no test_path"):
        ledger.cr_complete(_LEAD, lead_id, cr["cr_id"], "done")

    coder = _spawn_prd_coder(ledger, ctx, claimed["file_id"])
    (ledger.repo_root / "docs" / "prd.md").write_text("# PRD\n\nPROMO-12\n", encoding="utf-8")
    handoff = _hand_off(ledger, coder, claimed["file_id"])
    _lead_review(ledger, ctx, claimed["file_id"], handoff["handoff_id"])
    with pytest.raises(LedgerError, match="not verified"):
        ledger.approve(_LEAD, lead_id, handoff["handoff_id"])

    completed = ledger.cr_complete(_LEAD, lead_id, cr["cr_id"], "the row is in")
    assert completed["evidence_handoff_id"] == handoff["handoff_id"]
    assert completed["evidence_test_run_id"] is None
    ledger.cr_verify("mgr-p1-phase-1", ctx["manager"]["agent_id"], cr["cr_id"], True, "present")
    assert ledger.approve(_LEAD, lead_id, handoff["handoff_id"])["state"] == "approved"


def test_a_coder_hands_off_a_file_with_no_tests_while_its_change_request_waits(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    claimed = ledger.claim_file(
        _LEAD, ctx["lead"]["agent_id"], "docs/prd.md", None, "coder-p1-module-1-prd"
    )
    coder = _spawn_prd_coder(ledger, ctx, claimed["file_id"])
    cr = ledger.cr_open(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], "docs/prd.md", "add the PROMO-7 row"
    )
    assert cr["to_agent_id"] == coder["agent_id"]
    ledger.cr_accept(coder["name"], coder["agent_id"], cr["cr_id"], True, "will do")

    handoff = _hand_off(ledger, coder, claimed["file_id"])
    completed = ledger.cr_complete(coder["name"], coder["agent_id"], cr["cr_id"], "added")

    assert completed["evidence_handoff_id"] == handoff["handoff_id"]
