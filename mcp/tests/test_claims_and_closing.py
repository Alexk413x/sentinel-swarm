from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import insert_passing_test_run
from test_rekey import MovingClaude, claude
from test_sessions import (
    Ctx,
    _accept_module,
    _bootstrap,
    _brief_coder,
    _claim_coder,
    _row,
    _spawn_coder,
    host,
)

from swarm_ledger import close, front, lock, memory, watchdog
from swarm_ledger.clock import utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.queries import live_run
from swarm_ledger.settings import WatchdogSettings

__all__ = ["claude", "host"]


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> dict[str, int | None]:
    box: dict[str, int | None] = {"mb": 64_000}
    monkeypatch.setattr(memory, "free_mb", lambda: box["mb"])
    return box


def _file(ledger: Ledger, path: str) -> dict:
    return dict(
        ledger.conn.execute(
            "SELECT * FROM files WHERE path = ? ORDER BY file_id DESC LIMIT 1", (path,)
        ).fetchone()
    )


def _queue(ledger: Ledger, name: str) -> dict:
    return dict(
        ledger.conn.execute(
            "SELECT * FROM spawn_queue WHERE child_name = ? ORDER BY queue_id DESC", (name,)
        ).fetchone()
    )


def _started(claude: MovingClaude, name: str) -> bool:
    return any(name in " ".join(args) for args, _ in claude.calls if "--bg" in args)


# -- issue 8: claims of queued and live children ----------------------------------------------


def test_release_file_drops_the_owners_queued_spawn(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "screen")
    free["mb"] = 1_000
    assert ledger.agent_spawn(*ctx.lead, name)["queued"] is True

    released = ledger.release_file(*ctx.lead, "src/screen.py")

    assert released["dropped_spawn"] == name
    free["mb"] = 64_000
    assert ledger.start_queued_spawns(claude.listing) == []
    assert _queue(ledger, name)["state"] == "dropped"
    assert not _started(claude, name)


def test_a_queued_coder_whose_claim_is_gone_is_dropped_not_started(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "screen")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, name)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE files SET state = 'released', released_at = '2026-10-28T08:02:07Z' "
            "WHERE path = 'src/screen.py'"
        )

    free["mb"] = 64_000
    assert ledger.start_queued_spawns(claude.listing) == []
    queued = _queue(ledger, name)
    assert queued["state"] == "dropped"
    assert "no live claim" in queued["reason"]
    assert not _started(claude, name)


def test_agent_release_cancels_a_queued_spawn_and_keeps_its_claim(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "screen")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, name)

    cancelled = ledger.agent_release(*ctx.lead, name)

    assert cancelled["dropped"] is True
    assert _queue(ledger, name)["state"] == "dropped"
    claim = _file(ledger, "src/screen.py")
    assert (claim["state"], claim["released_at"]) == ("claimed", None)
    with pytest.raises(LedgerError, match="unknown agent_id"):
        ledger.agent_release(*ctx.manager, name)


def test_release_file_refuses_while_the_owning_coder_is_live(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    with pytest.raises(LedgerError, match="agent_release first"):
        ledger.release_file(*ctx.lead, "src/c1.py")
    assert _file(ledger, "src/c1.py")["released_at"] is None


def test_parking_or_replacing_a_parent_keeps_its_childrens_claims(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    queued = _brief_coder(ledger, ctx, "c2")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, queued)
    ledger.agent_idle(ctx.lead[1], "stop")
    free["mb"] = 1_200

    assert [p["name"] for p in ledger.park_idle_parents(claude.listing)] == [ctx.lead[0]]
    for path in ("src/c1.py", "src/c2.py"):
        assert _file(ledger, path)["released_at"] is None, path

    ledger.agent_release(*ctx.manager, ctx.lead[1])
    for path in ("src/c1.py", "src/c2.py"):
        claim = _file(ledger, path)
        assert (claim["state"], claim["released_at"]) == ("claimed", None), path


def test_a_manager_claims_an_unowned_path_into_one_of_its_modules(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    claimed = ledger.claim_file(
        *ctx.manager, "src/index.md", None, "coder-p1-module-1-index", module_id=ctx.module_id
    )
    assert claimed["module_id"] == ctx.module_id
    assert ledger.who_owns("src/index.md")["owner"] == "coder-p1-module-1-index"

    with pytest.raises(LedgerError, match="names the module_id"):
        ledger.claim_file(*ctx.manager, "src/other.md", None, "coder-p1-module-1-other")
    with pytest.raises(LedgerError, match="not a module of"):
        ledger.claim_file(
            *ctx.manager, "src/other.md", None, "coder-p1-module-1-other", module_id=999
        )
    with pytest.raises(LedgerError, match="only in its own module"):
        ledger.claim_file(*ctx.lead, "src/other.md", None, "coder-p1-module-1-other", module_id=999)


# -- issue 8: a stray released claim; issue 9: a deferred file ---------------------------------


def test_a_released_claim_on_a_missing_path_counts_as_closed(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _claim_coder(ledger, ctx, "stray")
    ledger.release_file(*ctx.lead, "src/stray.py")

    review = _accept_module(ledger, ctx)

    assert review["outcome"] == "accepted"
    assert _file(ledger, "src/stray.py")["state"] == "superseded"


def test_a_released_claim_on_a_path_that_exists_still_holds_the_module(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _claim_coder(ledger, ctx, "kept")
    ledger.release_file(*ctx.lead, "src/kept.py")
    (ledger.repo_root / "src").mkdir(exist_ok=True)
    (ledger.repo_root / "src" / "kept.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(LedgerError, match="not approved or incomplete"):
        _accept_module(ledger, ctx)


def _defer(ledger: Ledger, ctx: Ctx, file_id: int, kind: str) -> None:
    proposer = ctx.manager if kind != "file" else ctx.lead
    deferral = ledger.deferral_propose(
        *proposer, "binary file; verified by export", file_id, kind=kind
    )
    decider = ctx.oracle if kind != "file" else ctx.manager
    ledger.agreement_decide(*decider, deferral["deferral_id"], "agreed", "covered by its export")


def test_a_phase_whose_only_blocker_is_a_deferred_file_hands_up(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _, file_id = _claim_coder(ledger, ctx, "graph-db")
    _defer(ledger, ctx, file_id, "plan")

    assert _accept_module(ledger, ctx)["outcome"] == "accepted"
    deferred = _file(ledger, "src/graph-db.py")
    assert deferred["state"] == "incomplete"
    assert deferred["released_at"] is not None
    ledger.agent_release(*ctx.manager, ctx.lead[1])
    assert ledger.phase_update(*ctx.manager, ctx.phase_id, "handed_up")["state"] == "handed_up"


def test_a_file_level_deferral_does_not_settle_the_file(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _, file_id = _claim_coder(ledger, ctx, "edge")
    _defer(ledger, ctx, file_id, "file")
    with pytest.raises(LedgerError, match="not approved or incomplete"):
        _accept_module(ledger, ctx)


def test_the_watchdog_flags_an_unlocked_phase_with_no_live_manager(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)

    def unmanaged() -> list[watchdog.Finding]:
        findings = watchdog.scan(ledger.conn, claude.listing, utcnow(), WatchdogSettings())
        return [f for f in findings if f.kind == "phase_unmanaged"]

    assert unmanaged() == []
    phase = ledger.phase_add(*ctx.oracle, "phase-2")
    ledger.phase_update(*ctx.oracle, phase["phase_id"], "unlocked")
    assert unmanaged() == []

    ledger.agent_release(*ctx.oracle, ctx.manager[1])

    [finding] = unmanaged()
    assert finding.name == "oracle"
    assert f"{ctx.phase_id} (p1-phase-1, unlocked; last Manager mgr-p1-phase-1)" in finding.detail
    assert "phase-2" not in finding.detail


# -- issue 14: file_accept_incomplete -----------------------------------------------------------


def _passing_run(ledger: Ledger, ctx: Ctx) -> int:
    insert_passing_test_run(ledger, ctx.run_id, ctx.oracle[1], "full")
    return ledger.conn.execute("SELECT MAX(test_run_id) AS n FROM test_runs").fetchone()["n"]


def test_file_accept_incomplete_settles_a_file_verified_another_way(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "graph-db")
    file_id = _file(ledger, "src/graph-db.py")["file_id"]
    evidence = _passing_run(ledger, ctx)

    with pytest.raises(LedgerError, match="may not call"):
        ledger.file_accept_incomplete(*ctx.manager, file_id, "binary", [evidence])
    with pytest.raises(LedgerError, match="needs evidence"):
        ledger.file_accept_incomplete(*ctx.oracle, file_id, "binary", [])
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, passed, "
            "failed, skipped, output) VALUES (?, ?, 'full', 'x', 'x', 1, 3, 1, 0, '')",
            (ctx.run_id, ctx.oracle[1]),
        )
    failing = evidence + 1
    with pytest.raises(LedgerError, match="did not pass"):
        ledger.file_accept_incomplete(*ctx.oracle, file_id, "binary", [evidence, failing])

    settled = ledger.file_accept_incomplete(
        *ctx.oracle, file_id, "binary .db; covered by its export round-trip", [evidence]
    )

    assert settled["state"] == "incomplete"
    assert settled["deferral"]["state"] == "agreed"
    assert json.loads(settled["deferral"]["evidence_json"]) == [evidence]
    assert _row(ledger, coder[1])["state"] == "released"
    module = ledger.conn.execute(
        "SELECT state FROM modules WHERE module_id = ?", (ctx.module_id,)
    ).fetchone()
    assert module["state"] == "approved"
    assert _accept_module(ledger, ctx)["outcome"] == "accepted"
    with pytest.raises(LedgerError, match="already settled"):
        ledger.file_accept_incomplete(*ctx.oracle, file_id, "again", [evidence])


# -- issue 14: run_close ------------------------------------------------------------------------


def test_run_close_needs_a_reason_for_every_unsettled_file(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    with pytest.raises(LedgerError, match="not settled; name each"):
        ledger.run_close(*ctx.oracle, "built by hand")
    file_id = _file(ledger, "src/c1.py")["file_id"]
    with pytest.raises(LedgerError, match="non-empty reason"):
        ledger.run_close(*ctx.oracle, "built", [{"file_id": file_id, "reason": " "}])
    with pytest.raises(LedgerError, match=r"\{file_id, reason\}"):
        ledger.run_close(*ctx.oracle, "built", [{"file": file_id}])


def test_run_close_records_the_outcome_and_releases_every_agent(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "c1")
    file_id = _file(ledger, "src/c1.py")["file_id"]
    bg_ids = [
        _row(ledger, agent_id)["bg_id"] for agent_id in (ctx.manager[1], ctx.lead[1], coder[1])
    ]

    closed = ledger.run_close(
        *ctx.oracle,
        "committed by hand after the full suite passed",
        [{"file_id": file_id, "reason": "written by the coordinator"}],
    )

    assert (closed["state"], closed["closed_by"]) == ("closed", "oracle")
    assert closed["outcome"] == "committed by hand after the full suite passed"
    assert closed["files_incomplete"] == [file_id]
    assert Path(closed["report_path"]).is_file()
    assert _file(ledger, "src/c1.py")["state"] == "incomplete"
    reason = ledger.conn.execute(
        "SELECT decision_reason FROM deferrals WHERE file_id = ?", (file_id,)
    ).fetchone()
    assert reason["decision_reason"] == "written by the coordinator"
    for agent_id in (ctx.oracle[1], ctx.manager[1], ctx.lead[1], coder[1]):
        assert _row(ledger, agent_id)["ended_at"] is not None, agent_id
    assert sorted(c[1] for c in claude.commands("stop")) == sorted(bg_ids)
    assert lock.read_lock(ledger.repo_root) is None
    assert live_run(ledger.conn) is None


def test_the_launcher_closes_the_run(
    ledger: Ledger, claude: MovingClaude, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    file_id = _file(ledger, "src/c1.py")["file_id"]
    root = str(ledger.repo_root)

    assert close.main(["--repo", root, "done by hand"]) == 1
    assert "not settled" in capsys.readouterr().err

    assert close.main(["--repo", root, "--file", f"{file_id}=finished by hand", "done"]) == 0
    closed = json.loads(capsys.readouterr().out)
    assert (closed["state"], closed["closed_by"]) == ("closed", "launcher")
    assert _row(ledger, ctx.oracle[1])["state"] == "released"


def test_run_close_lets_the_server_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    finished: list[str | None] = []
    monkeypatch.setattr(front, "_pool", None)
    monkeypatch.setattr(front, "_ledger", lambda: None)
    monkeypatch.setattr(front, "run_tool", lambda ledger, request: {"run_id": 3})
    monkeypatch.setattr(front, "_oracle_of", lambda run_id: f"oracle-of-{run_id}")
    monkeypatch.setattr(front, "on_run_finish", finished.append)

    front.call_method("run_close", "sess-oracle", "run_close", {})

    assert finished == ["oracle-of-3"]
