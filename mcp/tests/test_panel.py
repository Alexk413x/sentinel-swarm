from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from helpers import bootstrap_module, spawn_coder

from swarm_ledger import panel
from swarm_ledger.db import write_tx
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")


def _set_state(ledger: Ledger, table: str, key: str, value: int, state: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(f"UPDATE {table} SET state = ? WHERE {key} = ?", (state, value))


def _run_snapshot(ledger: Ledger) -> dict:
    runs = panel.snapshot(ledger.conn, {})["runs"]
    assert len(runs) == 1
    return runs[0]


def _node(run: dict, role: str) -> dict:
    found: list[dict] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            if node["role"] == role:
                found.append(node)
            walk(node["children"])

    walk(run["agents"])
    assert len(found) == 1, role
    return found[0]


def _statuses(run: dict) -> dict[str, str]:
    return {
        "run": _node(run, "oracle")["task"]["status"],
        "phase": _node(run, "manager")["task"]["status"],
        "module": _node(run, "lead")["task"]["status"],
        "file": _node(run, "coder")["task"]["status"],
    }


def _build(ledger: Ledger) -> dict:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-a", "src/a.py", "tests/test_a.py")
    ctx["file_id"] = coder["file_id"]
    return ctx


def _approve_all(ledger: Ledger, ctx: dict) -> None:
    _set_state(ledger, "files", "file_id", ctx["file_id"], "approved")
    _set_state(ledger, "modules", "module_id", ctx["module_id"], "approved")
    _set_state(ledger, "phases", "phase_id", ctx["phase_id"], "approved")


def test_agents_form_an_oracle_manager_lead_coder_tree(ledger: Ledger) -> None:
    _build(ledger)
    run = _run_snapshot(ledger)

    assert [n["role"] for n in run["agents"]] == ["oracle"]
    oracle = run["agents"][0]
    assert [n["role"] for n in oracle["children"]] == ["manager"]
    manager = oracle["children"][0]
    assert [n["role"] for n in manager["children"]] == ["lead"]
    lead = manager["children"][0]
    assert [n["role"] for n in lead["children"]] == ["coder"]
    assert lead["children"][0]["children"] == []


def test_each_role_owns_its_own_task(ledger: Ledger) -> None:
    _build(ledger)
    run = _run_snapshot(ledger)

    oracle = _node(run, "oracle")
    assert oracle["task"]["name"] == "all phases"
    assert oracle["task"]["id"] == f"run:{run['run']['run_id']}"
    assert _node(run, "manager")["task"]["name"] == "p1-phase-1"
    assert _node(run, "lead")["task"]["name"] == "module-1"
    coder = _node(run, "coder")
    assert coder["task"]["name"] == "src/a.py"
    assert coder["file"] == "src/a.py"
    assert coder["task"]["owners"] == [coder["name"]]


def test_unowned_work_lists_under_the_agent_above_it(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    claimed = ledger.claim_file(
        "lead-p1-module-1", ctx["lead"]["agent_id"], "src/a.py", None, "coder-p1-module-1-a"
    )
    run = _run_snapshot(ledger)

    lead_tasks = _node(run, "lead")["tasks"]
    assert [t["id"] for t in lead_tasks] == [f"file_id:{claimed['file_id']}"]
    assert _node(run, "manager")["tasks"] == []
    assert _node(run, "oracle")["tasks"] == []


def test_an_incomplete_file_makes_module_phase_and_run_a_problem(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _set_state(ledger, "files", "file_id", ctx["file_id"], "incomplete")

    assert _statuses(_run_snapshot(ledger)) == {
        "run": "problem",
        "phase": "problem",
        "module": "problem",
        "file": "problem",
    }


def test_a_released_file_is_a_problem(ledger: Ledger) -> None:
    ctx = _build(ledger)
    ledger.release_file("lead-p1-module-1", ctx["lead"]["agent_id"], "src/a.py")

    statuses = _statuses(_run_snapshot(ledger))
    assert statuses["file"] == "problem"
    assert statuses["module"] == "problem"


def test_a_reclaimed_file_counts_the_superseded_version_as_done(ledger: Ledger) -> None:
    ctx = _build(ledger)
    ledger.release_file("lead-p1-module-1", ctx["lead"]["agent_id"], "src/a.py")
    new = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/a.py",
        "tests/test_a.py",
        "coder-p1-module-1-b",
    )
    _set_state(ledger, "modules", "module_id", ctx["module_id"], "approved")
    _set_state(ledger, "phases", "phase_id", ctx["phase_id"], "approved")

    run = _run_snapshot(ledger)
    lead_tasks = _node(run, "lead")["tasks"]
    assert [t["id"] for t in lead_tasks] == [f"file_id:{new['file_id']}"]
    assert lead_tasks[0]["status"] == "active"
    assert _statuses(run)["file"] == "done"
    assert _statuses(run)["module"] == "active"
    assert _statuses(run)["phase"] == "active"
    assert _statuses(run)["run"] == "active"

    _set_state(ledger, "files", "file_id", new["file_id"], "approved")
    assert set(_statuses(_run_snapshot(ledger)).values()) == {"done"}


def test_the_superseded_file_is_done_and_hidden_from_the_module(ledger: Ledger) -> None:
    ctx = _build(ledger)
    ledger.release_file("lead-p1-module-1", ctx["lead"]["agent_id"], "src/a.py")
    new = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/a.py",
        "tests/test_a.py",
        "coder-p1-module-1-b",
    )
    row = ledger.conn.execute(
        "SELECT state FROM files WHERE file_id = ?", (ctx["file_id"],)
    ).fetchone()
    assert row["state"] == "superseded"

    run = _run_snapshot(ledger)
    assert _statuses(run)["file"] == "done"
    assert _statuses(run)["module"] != "problem"
    assert [t["id"] for t in _node(run, "lead")["tasks"]] == [f"file_id:{new['file_id']}"]


def test_everything_approved_is_done_at_every_level(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _approve_all(ledger, ctx)

    assert _statuses(_run_snapshot(ledger)) == {
        "run": "done",
        "phase": "done",
        "module": "done",
        "file": "done",
    }


def test_an_unapproved_module_keeps_the_phase_from_done(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _set_state(ledger, "files", "file_id", ctx["file_id"], "approved")
    _set_state(ledger, "phases", "phase_id", ctx["phase_id"], "approved")

    statuses = _statuses(_run_snapshot(ledger))
    assert statuses["file"] == "done"
    assert statuses["module"] == "active"
    assert statuses["phase"] == "active"
    assert statuses["run"] == "active"


def test_work_that_has_not_started_is_waiting(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _set_state(ledger, "phases", "phase_id", ctx["phase_id"], "locked")
    _set_state(ledger, "modules", "module_id", ctx["module_id"], "planned")

    statuses = _statuses_without_coder(_run_snapshot(ledger))
    assert statuses == {"run": "waiting", "phase": "waiting", "module": "waiting"}


def _statuses_without_coder(run: dict) -> dict[str, str]:
    return {
        "run": _node(run, "oracle")["task"]["status"],
        "phase": _node(run, "manager")["task"]["status"],
        "module": _node(run, "lead")["task"]["status"],
    }


def test_a_run_without_phases_is_waiting(ledger: Ledger) -> None:
    ledger.run_start(prd="Build X", session_id="sess-1")

    run = _run_snapshot(ledger)
    assert _node(run, "oracle")["task"]["status"] == "waiting"


def test_document_without_a_ledger_returns_an_error(tmp_path: Path) -> None:
    doc = panel.document(tmp_path, sessions_dir=tmp_path / "none")

    assert doc["runs"] == []
    assert doc["error"]
    assert doc["repo"] == str(tmp_path)
    assert doc["generated_at"]


def test_document_reads_the_ledger_of_a_host(ledger: Ledger, host: Path, tmp_path: Path) -> None:
    ctx = _build(ledger)
    doc = panel.document(host, sessions_dir=tmp_path / "none")

    assert "error" not in doc
    assert [r["run"]["run_id"] for r in doc["runs"]] == [ctx["run_id"]]
    assert doc["runs"][0]["counts"]["live"] == 4


def test_document_filters_by_run_id(ledger: Ledger, host: Path, tmp_path: Path) -> None:
    ctx = _build(ledger)

    assert panel.document(host, ctx["run_id"] + 1, tmp_path / "none")["runs"] == []
    kept = panel.document(host, ctx["run_id"], tmp_path / "none")["runs"]
    assert len(kept) == 1


def test_summary_is_the_first_sentence_of_the_brief(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE briefs SET body = ? WHERE phase_id = ?",
            ("# Heading\n\nGoal: Build the parser. Then wire it up.\nMore text.", ctx["phase_id"]),
        )

    task = _node(_run_snapshot(ledger), "manager")["task"]
    assert task["summary"] == "Build the parser"
    assert task["brief"].startswith("# Heading")


def test_summary_is_none_for_a_missing_brief(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("DELETE FROM briefs WHERE phase_id = ?", (ctx["phase_id"],))

    task = _node(_run_snapshot(ledger), "manager")["task"]
    assert task["summary"] is None
    assert task["brief"] is None


@pytest.mark.parametrize("body", [None, "", "# Only a heading", "\n\n  \n# A\n## B"])
def test_summary_is_none_without_prose(body: str | None) -> None:
    assert panel._summary(body) is None


def test_summary_skips_headings_and_blank_lines() -> None:
    assert panel._summary("# Title\n\n## Sub\n\nDo the thing.") == "Do the thing"


def test_summary_strips_the_goal_prefix() -> None:
    assert panel._summary("Goal: Ship it") == "Ship it"


def test_summary_keeps_text_up_to_the_first_sentence_break() -> None:
    assert panel._summary("First part. Second part.") == "First part"
    assert panel._summary("Version 1.2 works") == "Version 1.2 works"


def test_summary_caps_long_text_at_90_characters() -> None:
    long = "x" * 200
    summary = panel._summary(long)

    assert summary is not None
    assert len(summary) == panel.SUMMARY_CHARS == 90
    assert summary == "x" * 89 + "…"


def test_summary_keeps_text_of_exactly_90_characters() -> None:
    text = "y" * 90
    assert panel._summary(text) == text


def _write_session(folder: Path, name: str, content: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (folder / name).write_text(text, encoding="utf-8")


def test_live_sessions_returns_entries_with_a_live_pid(tmp_path: Path) -> None:
    entry = {"sessionId": "sess-live", "pid": os.getpid(), "name": "host-oracle", "status": "idle"}
    _write_session(tmp_path, "a.json", entry)

    assert panel.live_sessions(tmp_path) == {"sess-live": entry}


def test_live_sessions_skips_dead_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel, "pid_alive", lambda pid: pid == 111)
    _write_session(tmp_path, "a.json", {"sessionId": "alive", "pid": 111})
    _write_session(tmp_path, "b.json", {"sessionId": "dead", "pid": 222})

    assert list(panel.live_sessions(tmp_path)) == ["alive"]


def test_live_sessions_skips_malformed_files(tmp_path: Path) -> None:
    good = {"sessionId": "good", "pid": os.getpid()}
    _write_session(tmp_path, "bad-json.json", "{not json")
    _write_session(tmp_path, "list.json", [1, 2])
    _write_session(tmp_path, "no-id.json", {"pid": os.getpid()})
    _write_session(tmp_path, "id-not-text.json", {"sessionId": 5, "pid": os.getpid()})
    _write_session(tmp_path, "no-pid.json", {"sessionId": "nopid"})
    _write_session(tmp_path, "pid-text.json", {"sessionId": "pidtext", "pid": "1"})
    _write_session(tmp_path, "good.json", good)
    _write_session(tmp_path, "ignored.txt", good)

    assert panel.live_sessions(tmp_path) == {"good": good}


def test_live_sessions_of_a_missing_folder_is_empty(tmp_path: Path) -> None:
    assert panel.live_sessions(tmp_path / "missing") == {}


def test_live_sessions_defaults_to_the_config_sessions_folder() -> None:
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    entry = {"sessionId": "from-config", "pid": os.getpid()}
    _write_session(config / "sessions", "x.json", entry)

    assert panel.live_sessions() == {"from-config": entry}


def test_agent_session_state_follows_the_session_listing(ledger: Ledger) -> None:
    _build(ledger)
    oracle = ledger.conn.execute("SELECT agent_id FROM agents WHERE role = 'oracle'").fetchone()
    busy = {oracle["agent_id"]: {"sessionId": oracle["agent_id"], "status": "busy"}}
    runs = panel.snapshot(ledger.conn, busy)["runs"]
    gone = panel.snapshot(ledger.conn, {})["runs"]

    assert _node(runs[0], "oracle")["session"] == "busy"
    assert _node(runs[0], "manager")["session"] == "gone"
    assert _node(gone[0], "oracle")["session"] == "gone"
