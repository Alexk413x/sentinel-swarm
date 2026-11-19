from __future__ import annotations

import http.client
import json
import os
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import bootstrap_module, insert_passing_test_run, spawn_coder

from swarm_ledger import auth, copilot, front, panel, serve
from swarm_ledger.db import write_tx
from swarm_ledger.http_front import LedgerHttpServer
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")


def _set_state(ledger: Ledger, table: str, key: str, value: int, state: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(f"UPDATE {table} SET state = ? WHERE {key} = ?", (state, value))


def _run_snapshot(ledger: Ledger) -> dict:
    runs = panel.snapshot(ledger.conn, {})["runs"]
    assert len(runs) == 1
    return runs[0]


def _snapshot_with_brief(ledger: Ledger, task_id: str) -> dict:
    runs = panel.snapshot(ledger.conn, {}, brief=task_id)["runs"]
    assert len(runs) == 1
    return runs[0]


def _brief_of(ledger: Ledger, role: str, task_id: str) -> str | None:
    return _node(_snapshot_with_brief(ledger, task_id), role)["task"]["brief"]


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
    ctx["coder_id"] = coder["agent_id"]
    return ctx


def _release_claim(ledger: Ledger, ctx: dict) -> None:
    ledger.agent_release("lead-p1-module-1", ctx["lead"]["agent_id"], ctx["coder_id"])
    ledger.release_file("lead-p1-module-1", ctx["lead"]["agent_id"], "src/a.py")


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
    assert coder["task"]["name"] == "a.py"
    assert coder["task"]["path"] == "src/a.py"
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


def test_an_incomplete_file_is_a_problem_and_keeps_its_parents_in_progress(
    ledger: Ledger,
) -> None:
    ctx = _build(ledger)
    _set_state(ledger, "files", "file_id", ctx["file_id"], "incomplete")
    _set_state(ledger, "modules", "module_id", ctx["module_id"], "approved")
    _set_state(ledger, "phases", "phase_id", ctx["phase_id"], "approved")

    assert _statuses(_run_snapshot(ledger)) == {
        "run": "active",
        "phase": "active",
        "module": "active",
        "file": "problem",
    }


def test_an_accepted_module_review_shows_the_module_done(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _set_state(ledger, "files", "file_id", ctx["file_id"], "approved")
    _set_state(ledger, "modules", "module_id", ctx["module_id"], "returned")
    manager = ("mgr-p1-phase-1", ctx["manager"]["agent_id"])
    ledger.agent_release("lead-p1-module-1", ctx["lead"]["agent_id"], ctx["coder_id"])
    ledger.agent_release(*manager, ctx["lead"]["agent_id"])
    assert _statuses(_run_snapshot(ledger))["module"] == "active"
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    ledger.module_review(
        *manager,
        ctx["module_id"],
        "accepted",
        "ok",
        scores=[
            {"dimension": d, "value": 10} for d in ("completeness", "integration", "open_items")
        ],
    )

    assert _statuses(_run_snapshot(ledger))["module"] == "done"


def test_a_released_file_is_a_problem(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _release_claim(ledger, ctx)

    statuses = _statuses(_run_snapshot(ledger))
    assert statuses["file"] == "problem"
    assert statuses["module"] == "active"
    assert statuses["phase"] == "active"
    assert statuses["run"] == "active"


def test_a_fixed_incomplete_file_returns_its_parents_to_done(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _approve_all(ledger, ctx)
    _set_state(ledger, "files", "file_id", ctx["file_id"], "incomplete")
    ledger.conn.execute(
        "UPDATE files SET released_at = CURRENT_TIMESTAMP WHERE file_id = ?", (ctx["file_id"],)
    )
    ledger.conn.commit()
    new = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/a.py",
        "tests/test_a.py",
        "coder-p1-module-1-b",
    )

    statuses = _statuses(_run_snapshot(ledger))
    assert statuses["file"] == "done"
    assert {statuses["module"], statuses["phase"], statuses["run"]} == {"active"}

    _set_state(ledger, "files", "file_id", new["file_id"], "approved")
    assert set(_statuses(_run_snapshot(ledger)).values()) == {"done"}


def test_a_reclaimed_file_counts_the_superseded_version_as_done(ledger: Ledger) -> None:
    ctx = _build(ledger)
    _release_claim(ledger, ctx)
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
    _release_claim(ledger, ctx)
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
    assert task["has_brief"] is True
    shown = _node(_snapshot_with_brief(ledger, task["id"]), "manager")["task"]
    assert shown["brief"].startswith("# Heading")


def test_summary_is_none_for_a_missing_brief(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("DELETE FROM briefs WHERE phase_id = ?", (ctx["phase_id"],))

    task = _node(_run_snapshot(ledger), "manager")["task"]
    assert task["summary"] is None
    assert task["has_brief"] is False
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


def test_the_oracle_task_shows_the_prd_until_the_run_has_an_outcome(ledger: Ledger) -> None:
    _build(ledger)
    run_task = _node(_run_snapshot(ledger), "oracle")["task"]
    assert run_task["has_brief"] is True
    assert _brief_of(ledger, "oracle", run_task["id"]) == "Build X"

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET outcome = 'success: built'")
    assert _brief_of(ledger, "oracle", run_task["id"]) == "success: built"


def test_only_a_file_task_carries_a_path(ledger: Ledger) -> None:
    _build(ledger)
    run = _run_snapshot(ledger)

    assert _node(run, "coder")["task"]["path"] == "src/a.py"
    assert _node(run, "lead")["task"]["path"] is None
    assert _node(run, "manager")["task"]["path"] is None
    assert _node(run, "oracle")["task"]["path"] is None


def test_file_name_keeps_the_last_part_of_either_separator() -> None:
    assert panel._file_name("src/pkg/a.py") == "a.py"
    assert panel._file_name(r"src\pkg\b.py") == "b.py"
    assert panel._file_name("top.py") == "top.py"


def test_an_agent_carries_its_session_name_and_transcript(ledger: Ledger) -> None:
    _build(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = 'host-r1-lead', transcript_path = '/t/lead.jsonl' "
            "WHERE role = 'lead'"
        )
    lead = _node(_run_snapshot(ledger), "lead")

    assert lead["agent_id"] == "lead-agent"
    assert lead["session_name"] == "host-r1-lead"
    assert lead["transcript_path"] == "/t/lead.jsonl"
    assert lead["queued"] is False


def _queue_coder(ledger: Ledger, ctx: dict, name: str, path: str) -> int:
    claimed = ledger.claim_file("lead-p1-module-1", ctx["lead"]["agent_id"], path, None, name)
    brief = ledger.brief_create(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        name,
        "coder",
        "sonnet",
        f"Write {path}.",
        file_id=claimed["file_id"],
    )
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO spawn_queue (run_id, brief_id, child_name, role, parent_agent_id, reason) "
            "VALUES (?, ?, ?, 'coder', ?, 'waits for memory')",
            (ctx["run_id"], brief["brief_id"], name, ctx["lead"]["agent_id"]),
        )
    return claimed["file_id"]


def test_a_queued_child_sits_under_its_parent_with_its_task(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    file_id = _queue_coder(ledger, ctx, "coder-p1-module-1-q", "src/q.py")
    run = _run_snapshot(ledger)

    lead = _node(run, "lead")
    assert [c["name"] for c in lead["children"]] == ["coder-p1-module-1-q"]
    queued = lead["children"][0]
    assert queued["queued"] is True
    assert queued["state"] == "queued"
    assert queued["session"] is None
    assert queued["agent_id"] is None
    assert queued["park_reason"] == "waits for memory"
    assert queued["task"]["id"] == f"file_id:{file_id}"
    assert lead["tasks"] == []
    assert run["counts"]["queued"] == 1
    assert run["counts"]["live"] == 3


def test_a_started_or_dropped_spawn_is_not_queued(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _queue_coder(ledger, ctx, "coder-p1-module-1-q", "src/q.py")
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE spawn_queue SET state = 'dropped'")

    run = _run_snapshot(ledger)
    assert _node(run, "lead")["children"] == []
    assert run["counts"]["queued"] == 0


def test_a_needs_user_directive_waits_on_the_user_until_answered(ledger: Ledger) -> None:
    ctx = _build(ledger)
    asked = ledger.directive_submit(source="skill", sender_name="alex", body="Ship it?")
    ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "needs_user", "Which branch?"
    )

    waiting = _run_snapshot(ledger)["waiting"]
    assert waiting["questions"] == [
        {
            "directive_id": asked["directive_id"],
            "question": "Which branch?",
            "asked_at": asked["created_at"],
        }
    ]

    ledger.directive_submit(
        source="user_chat", sender_name="run panel", body="main", reply_to=asked["directive_id"]
    )
    assert _run_snapshot(ledger)["waiting"]["questions"] == []


def test_the_panel_lists_each_user_directive_with_its_state(ledger: Ledger) -> None:
    ctx = _build(ledger)
    first = ledger.directive_submit("user_chat", "alex", "Scope it to phase 1.")
    ledger.directive_inbox("oracle", ctx["oracle_id"])
    queued = ledger.directive_submit("outside_session", "alex", "Drop the CLI.")
    ledger.directive_ack("oracle", ctx["oracle_id"], queued["directive_id"], "after the review")
    ledger.directive_submit("watchdog", "watchdog", "Not shown.")
    unread = ledger.directive_submit("user_chat", "alex", "Rename the flag.")
    done = ledger.directive_submit("user_chat", "alex", "Skip docs.")
    ledger.directive_resolve("oracle", ctx["oracle_id"], done["directive_id"], "applied", "Done.")

    rows = {d["directive_id"]: d for d in _run_snapshot(ledger)["directives"]}

    assert set(rows) == {d["directive_id"] for d in (first, queued, unread, done)}
    assert rows[first["directive_id"]]["status"] == "seen"
    assert rows[queued["directive_id"]]["status"] == "queued"
    assert rows[queued["directive_id"]]["note"] == "after the review"
    assert rows[unread["directive_id"]]["status"] == "unread"
    assert rows[unread["directive_id"]]["body"] == "Rename the flag."
    assert rows[done["directive_id"]]["status"] == "resolved"
    assert rows[done["directive_id"]]["outcome"] == "applied"


def test_the_panel_reads_a_ledger_that_lacks_the_directive_acknowledgement_columns(
    ledger: Ledger,
) -> None:
    ctx = _build(ledger)
    sent = ledger.directive_submit("user_chat", "alex", "Scope it to phase 1.")
    ledger.directive_inbox("oracle", ctx["oracle_id"])
    with write_tx(ledger.conn) as conn:
        for column in ("seen_at", "queued_at", "queue_note"):
            conn.execute(f"ALTER TABLE directives DROP COLUMN {column}")

    [row] = _run_snapshot(ledger)["directives"]

    assert (row["directive_id"], row["status"]) == (sent["directive_id"], "unread")


def test_open_needs_user_and_stall_findings_wait_on_the_user(ledger: Ledger) -> None:
    ctx = _build(ledger)
    with write_tx(ledger.conn) as conn:
        for agent_id, kind, cleared in (
            ("lead-agent", "needs_user", None),
            (ctx["oracle_id"], "run_stalled", None),
            ("lead-agent", "stuck", None),
            ("mgr-agent", "needs_user", "2026-10-28T00:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO watchdog_findings (run_id, agent_id, kind, detail, first_seen_at, "
                "last_seen_at, cleared_at) VALUES (?, ?, ?, ?, 'T0', 'T0', ?)",
                (ctx["run_id"], agent_id, kind, f"{kind} detail", cleared),
            )

    findings = _run_snapshot(ledger)["waiting"]["findings"]
    assert [(f["kind"], f["agent"], f["detail"]) for f in findings] == [
        ("needs_user", "lead-p1-module-1", "needs_user detail"),
        ("run_stalled", "oracle", "run_stalled detail"),
    ]


def test_a_paused_run_carries_its_reason(ledger: Ledger) -> None:
    ctx = _build(ledger)
    assert _run_snapshot(ledger)["waiting"]["paused"] is None

    ledger.run_pause("oracle", ctx["oracle_id"], "the build needs a licence key")
    assert _run_snapshot(ledger)["waiting"]["paused"] == "the build needs a licence key"


def _transcript(path: Path, *blocks: dict) -> str:
    record = {
        "type": "assistant",
        "timestamp": "2026-11-14T10:00:00Z",
        "message": {"content": list(blocks)},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return str(path)


def _set_transcript(ledger: Ledger, agent_id: str, path: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET transcript_path = ? WHERE agent_id = ?", (path, agent_id))


def test_a_waiting_agent_carries_its_pending_request(ledger: Ledger, tmp_path: Path) -> None:
    ctx = _build(ledger)
    use = {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "npm ci"}}
    _set_transcript(ledger, ctx["coder_id"], _transcript(tmp_path / "c.jsonl", use))

    asked = panel.snapshot(ledger.conn, {}, waiting=frozenset({ctx["coder_id"]}))["runs"][0]
    plain = _run_snapshot(ledger)

    assert _node(asked, "coder")["request"] == {"kind": "tool", "tool": "Bash", "input": "npm ci"}
    assert _node(plain, "coder")["request"] is None
    assert _node(asked, "lead")["request"] is None


def test_the_detail_agent_carries_its_recent_activity(ledger: Ledger, tmp_path: Path) -> None:
    ctx = _build(ledger)
    said = {"type": "text", "text": "Waiting for the lint gate."}
    _set_transcript(ledger, ctx["coder_id"], _transcript(tmp_path / "c.jsonl", said))

    run = panel.snapshot(ledger.conn, {}, detail=ctx["coder_id"])["runs"][0]

    assert _node(run, "coder")["recent"] == [
        {"at": "2026-11-14T10:00:00Z", "kind": "say", "text": "Waiting for the lint gate."}
    ]
    assert _node(run, "lead")["recent"] is None


def test_a_planned_phase_shows_its_goal_until_its_manager_is_briefed(ledger: Ledger) -> None:
    ctx = _build(ledger)
    later = ledger.phase_add(
        "oracle", ctx["oracle_id"], "people", goal="Members and roles.\nAdmins approve."
    )

    task_id = f"phase_id:{later['phase_id']}"
    waiting = {t["id"]: t for t in _run_snapshot(ledger)["agents"][0]["tasks"]}
    opened = _snapshot_with_brief(ledger, task_id)
    shown = {t["id"]: t for t in opened["agents"][0]["tasks"]}

    assert later["goal"] == "Members and roles.\nAdmins approve."
    assert waiting[task_id]["has_brief"] is True
    assert shown[task_id]["brief"] == "Members and roles.\nAdmins approve."
    assert waiting[task_id]["summary"] == "Members and roles"


def test_phase_add_without_a_goal_stores_none(ledger: Ledger) -> None:
    ctx = _build(ledger)
    assert ledger.phase_add("oracle", ctx["oracle_id"], "later")["goal"] is None


def _every_brief(run: dict) -> list[str | None]:
    found: list[str | None] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            for task in [node["task"], *node["tasks"]]:
                if task is not None:
                    found.append(task["brief"])
            walk(node["children"])

    walk(run["agents"])
    return found


def test_the_snapshot_carries_no_brief_body_until_a_task_is_named(ledger: Ledger) -> None:
    ctx = _build(ledger)

    run = _run_snapshot(ledger)
    assert set(_every_brief(run)) == {None}
    assert _node(run, "coder")["task"]["has_brief"] is True

    task_id = f"file_id:{ctx['file_id']}"
    opened = _snapshot_with_brief(ledger, task_id)
    carried = [brief for brief in _every_brief(opened) if brief is not None]
    assert carried == [_node(opened, "coder")["task"]["brief"]]
    assert _node(opened, "coder")["task"]["id"] == task_id


def test_the_document_passes_the_brief_id_through(ledger: Ledger, host: Path) -> None:
    ctx = _build(ledger)
    task_id = f"file_id:{ctx['file_id']}"

    doc = panel.document(host, brief=task_id, sessions_dir=host / "none")
    assert _node(doc["runs"][0], "coder")["task"]["brief"] is not None
    plain = panel.document(host, sessions_dir=host / "none")
    assert _node(plain["runs"][0], "coder")["task"]["brief"] is None


def test_the_copilot_status_keeps_every_brief(ledger: Ledger, host: Path) -> None:
    _build(ledger)

    run = copilot.status(host)["runs"][0]
    assert _node(run, "coder")["task"]["brief"] is not None
    assert _node(run, "oracle")["task"]["brief"] == "Build X"


@pytest.fixture
def lean(host: Path) -> Iterator[int]:
    token = auth.ensure_token(host)
    sock = serve.bind_socket(host)
    port = sock.getsockname()[1]
    front.configure(host)
    httpd = LedgerHttpServer(
        sock,
        host,
        lambda client, headers, path: serve.refusal(client, headers, path, token, port),
        {"name": "swarm-ledger", "repo_root": str(host), "pid": os.getpid()},
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(
    port: int, host: Path, query: str, *, token: bool = True, repo: Path | None = None
) -> tuple[int, dict[str, str], str]:
    headers = auth.auth_headers(host) if token else {}
    headers[serve.REPO_HEADER] = urllib.parse.quote(str(repo or host))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", f"{serve.PANEL_PATH}{query}", headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode("utf-8")
    finally:
        conn.close()


def test_the_panel_route_answers_the_launchers_document(
    ledger: Ledger, host: Path, lean: int
) -> None:
    ctx = _build(ledger)
    task_id = f"file_id:{ctx['file_id']}"

    status, headers, body = _get(lean, host, "")
    assert status == 200
    assert serve.REPO_HEADER in headers
    plain = json.loads(body)
    assert plain["repo"] == str(host)
    assert _node(plain["runs"][0], "coder")["task"]["brief"] is None

    status, _, body = _get(lean, host, f"?brief={urllib.parse.quote(task_id)}")
    assert status == 200
    assert _node(json.loads(body)["runs"][0], "coder")["task"]["brief"] is not None

    status, _, body = _get(lean, host, "?detail=" + ctx["coder_id"])
    assert status == 200
    assert "recent" in _node(json.loads(body)["runs"][0], "coder")


def test_the_panel_route_needs_the_token_and_this_repo(
    ledger: Ledger, host: Path, lean: int, tmp_path: Path
) -> None:
    _build(ledger)

    assert _get(lean, host, "", token=False)[0] == 403
    assert _get(lean, host, "", repo=tmp_path / "elsewhere")[0] == 409
