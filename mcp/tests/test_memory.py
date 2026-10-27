from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import make_host, settings_text
from test_rekey import MovingClaude, claude
from test_sessions import (
    Ctx,
    _bootstrap,
    _brief_coder,
    _brief_second_lead,
    _row,
    _spawn_coder,
    host,
)

from swarm_ledger import memory, watchdog
from swarm_ledger.clock import utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import Settings, WatchdogSettings, load_settings

__all__ = ["claude", "host"]

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           50000.
Pages speculative:                         4000.
Pages purgeable:                           2000.
"""


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> dict[str, int | None]:
    box: dict[str, int | None] = {"mb": 64_000}
    monkeypatch.setattr(memory, "free_mb", lambda: box["mb"])
    return box


def _daemon_log(text: str) -> None:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "daemon.log"
    path.write_text(text, encoding="utf-8")


def _owed_rows(ledger: Ledger, to_agent_id: str) -> list[dict]:
    return [
        dict(r)
        for r in ledger.conn.execute(
            "SELECT * FROM wakeups WHERE to_agent_id = ? ORDER BY wakeup_id", (to_agent_id,)
        )
    ]


def _park_manager(ledger: Ledger, claude: MovingClaude, ctx: Ctx) -> None:
    claude.stop_session(ctx.manager[1])
    ledger.agent_idle(ctx.manager[1], "stop")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET parked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "park_reason = 'retired' WHERE agent_id = ?",
            (ctx.manager[1],),
        )


# -- measuring -----------------------------------------------------------------------------------


def test_vm_stat_counts_free_inactive_speculative_and_purgeable_pages() -> None:
    assert memory.parse_vm_stat(VM_STAT) == (10000 + 50000 + 4000 + 2000) * 16384 // 1024**2


def test_meminfo_reads_mem_available() -> None:
    text = "MemTotal:       32000000 kB\nMemAvailable:    2048000 kB\n"
    assert memory.parse_meminfo(text) == 2000


def test_free_memory_is_sampled_on_this_machine() -> None:
    assert isinstance(memory.platform_free_mb(), int)


def test_the_daemon_logs_last_retire_line_gives_the_reason() -> None:
    _daemon_log(
        "bg retire 1234abcd: settled\n"
        "bg: low memory (685MB free) - retiring settled workers before spawning 99999999\n"
        "bg retire 1234abcd: idle-prompt, idle 6m [low memory]\n"
    )
    assert memory.retire_reason("1234abcd") == "idle-prompt, idle 6m [low memory]"
    assert memory.retire_reason("00000000") is None


def test_a_missing_daemon_log_gives_no_reason() -> None:
    assert memory.retire_reason("1234abcd") is None


def test_role_estimates_start_at_the_defaults_and_follow_samples(ledger: Ledger) -> None:
    assert memory.estimate_mb(ledger.conn, "lead") == memory.SESSION_MB
    assert memory.estimate_mb(ledger.conn, "coder") == memory.SESSION_MB + memory.CODER_CHILDREN_MB
    with write_tx(ledger.conn) as conn:
        memory.record_sample(conn, "lead", 200)
        memory.record_sample(conn, "lead", 400)
        memory.record_sample(conn, "coder", 250)
    assert memory.estimate_mb(ledger.conn, "lead") == 300
    assert memory.estimate_mb(ledger.conn, "coder") == 250 + memory.CODER_CHILDREN_MB


def test_the_watchdog_samples_each_live_sessions_memory(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    monkeypatch.setattr(memory, "process_rss_mb", lambda pid: 500)
    ledger.sample_memory(claude.listing)
    assert memory.estimate_mb(ledger.conn, "manager") == 500
    assert memory.estimate_mb(ledger.conn, "coder") == memory.default_mb("coder")
    assert ctx.run_id


# -- admission -----------------------------------------------------------------------------------


def test_a_spawn_that_would_cross_the_headroom_is_queued_not_started(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    free["mb"] = 1_800
    spawns = len([a for a, _ in claude.calls if "--bg" in a])

    queued = ledger.agent_spawn(*ctx.lead, name)

    assert queued["queued"] is True
    assert queued["child_name"] == name
    assert "do not call agent_spawn for it again" in queued["next"]
    assert len([a for a, _ in claude.calls if "--bg" in a]) == spawns
    assert ledger.agent_spawn(*ctx.lead, name)["queue_id"] == queued["queue_id"]
    capacity = ledger.run_status(*ctx.lead)["capacity"]
    assert capacity["queued_spawns"] == [
        {"child_name": name, "role": "coder", "queued_at": queued["queued_at"]}
    ]
    assert capacity["free_mb"] == 1_800
    assert capacity["headroom_mb"] == 1_500
    assert capacity["spawn_slots"]["coder"] == 0


def test_a_spawn_behind_a_queued_one_waits_its_turn(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    first = _brief_coder(ledger, ctx, "c1")
    second = _brief_coder(ledger, ctx, "c2")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, first)
    free["mb"] = 64_000
    assert ledger.agent_spawn(*ctx.lead, second)["queued"] is True


def test_queued_spawns_start_oldest_first_when_memory_frees(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    first = _brief_coder(ledger, ctx, "c1")
    second = _brief_coder(ledger, ctx, "c2")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, first)
    ledger.agent_spawn(*ctx.lead, second)

    assert ledger.start_queued_spawns(claude.listing) == []
    free["mb"] = 1_500 + 600 + 100
    started = ledger.start_queued_spawns(claude.listing)

    assert [s["name"] for s in started] == [first]
    assert _row(ledger, started[0]["agent_id"])["state"] == "registered"
    queue = ledger.conn.execute("SELECT child_name, state FROM spawn_queue ORDER BY queue_id")
    assert [tuple(r) for r in queue] == [(first, "started"), (second, "queued")]


def test_a_queued_spawn_whose_parent_ended_is_dropped(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, name)
    ledger.agent_release(*ctx.manager, ctx.lead[1])
    free["mb"] = 64_000
    assert ledger.start_queued_spawns(claude.listing) == []
    state = ledger.conn.execute("SELECT state FROM spawn_queue").fetchone()["state"]
    assert state == "dropped"


def test_unknown_free_memory_admits_the_spawn(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    free["mb"] = None
    assert "queued" not in ledger.agent_spawn(*ctx.lead, name)


def test_the_oracle_does_not_stop_on_work_while_a_spawn_is_queued(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, name)
    for agent_id in (ctx.manager[1], ctx.lead[1]):
        ledger.agent_idle(agent_id, "stop")
    run = dict(ledger.conn.execute("SELECT * FROM runs").fetchone())
    assert events._oracle_work_block(ledger, run, _row(ledger, ctx.oracle[1])) is None
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE spawn_queue SET state = 'dropped'")
    assert events._oracle_work_block(ledger, run, _row(ledger, ctx.oracle[1])) is not None


# -- parking and resuming ------------------------------------------------------------------------


def test_a_live_agent_whose_session_is_gone_is_parked_with_the_daemons_reason(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.agent_idle(ctx.manager[1], "stop")
    bg_id = _row(ledger, ctx.manager[1])["bg_id"]
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx.manager[1]]
    _daemon_log(f"bg retire {bg_id}: idle-prompt, idle 6m [low memory]\n")

    parked = ledger.mark_parked(claude.listing, utcnow() + timedelta(minutes=5), 120)

    assert [p["name"] for p in parked] == ["mgr-p1-phase-1"]
    row = _row(ledger, ctx.manager[1])
    assert row["park_reason"] == "the daemon retired its session: idle-prompt, idle 6m [low memory]"
    assert row["ended_at"] is None
    assert row["state"] == "idle"
    assert ledger.run_status(*ctx.oracle)["capacity"]["parked"] == [
        {"name": "mgr-p1-phase-1", "reason": row["park_reason"]}
    ]


def test_a_session_seen_running_again_is_no_longer_parked(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _park_manager(ledger, claude, ctx)
    entry = claude.entry(ctx.manager[1])
    assert entry is not None
    entry["status"] = "idle"
    ledger.mark_parked(claude.listing, utcnow(), 120)
    assert _row(ledger, ctx.manager[1])["parked_at"] is None


def test_a_wake_up_owed_to_a_parked_agent_resumes_it_and_reaches_the_new_session(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _park_manager(ledger, claude, ctx)
    ledger.mod_session(ctx.lead[1])
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")

    answer = events.handle_owed(ledger, {"session_id": ctx.lead[1]})

    assert answer == {"wakeups": []}
    new_id = claude.resumed[0]
    resume = next(a for a, _ in claude.calls if a[0] == "--resume")
    assert "module-1 is done" not in resume[3]
    assert "is waiting in the ledger" in resume[3]
    manager = _row(ledger, new_id)
    assert manager["parked_at"] is None
    assert all(w["sent_at"] for w in _owed_rows(ledger, new_id))
    assert ledger.message_inbox("mgr-p1-phase-1", new_id)["messages"]


def test_a_second_wake_up_while_a_resume_is_in_flight_starts_no_second_copy(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _park_manager(ledger, claude, ctx)
    claude.visible = False
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "one")
    events.handle_owed(ledger, {"session_id": ctx.lead[1]})
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "two")
    events.handle_owed(ledger, {"session_id": ctx.lead[1]})
    assert len(claude.resumed) == 1


def test_a_wake_up_owed_to_the_oracle_never_resumes_it(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.stop_session(ctx.oracle[1])
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer) VALUES (?, ?, ?, 'oracle', 'my-host-oracle', 'test', 'x')",
            (ctx.run_id, ctx.manager[1], ctx.oracle[1]),
        )
    events.handle_owed(ledger, {"session_id": ctx.manager[1]})
    assert claude.resumed == []


def test_unknown_liveness_resumes_nothing(ledger: Ledger, claude: MovingClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.stop_session(ctx.manager[1])
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")
    claude.fail["agents"] = "boom"
    answer = events.handle_owed(ledger, {"session_id": ctx.lead[1]})
    assert claude.resumed == []
    assert answer is not None and len(answer["wakeups"]) == 1


def test_a_resume_waits_for_memory_while_other_sessions_of_the_run_run(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _park_manager(ledger, claude, ctx)
    free["mb"] = 1_000
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")

    events.handle_owed(ledger, {"session_id": ctx.lead[1]})

    assert claude.resumed == []
    assert _row(ledger, ctx.manager[1])["park_reason"] == (
        "waiting for memory to resume it for a wake-up"
    )
    free["mb"] = 64_000
    resumed = ledger.resume_parked(claude.listing)
    assert [r["session_id"] for r in resumed] == claude.resumed


def test_parked_agents_resume_parents_of_finished_children_first(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    second = _brief_second_lead(ledger, ctx)
    lead2 = ledger.agent_spawn(*ctx.manager, second)
    ledger.brief_ack(second, lead2["agent_id"])
    for agent_id in (ctx.lead[1], lead2["agent_id"]):
        claude.stop_session(agent_id)
        ledger.agent_idle(agent_id, "stop")
        with write_tx(ledger.conn) as conn:
            conn.execute(
                "UPDATE agents SET parked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE agent_id = ?",
                (agent_id,),
            )
    ledger.message_post(*ctx.manager, ctx.lead[0], "first, but its Coder still works")
    ledger.message_post(*ctx.manager, second, "second, with no working child")
    free["mb"] = 1_500 + 300 + 100

    resumed = ledger.resume_parked(claude.listing)

    assert [r["agent"]["name"] for r in resumed] == [second]


def test_an_idle_parent_with_working_children_is_parked_under_memory_pressure(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "c1")
    ledger.agent_idle(ctx.lead[1], "stop")
    free["mb"] = 1_200

    parked = ledger.park_idle_parents(claude.listing)

    assert [p["name"] for p in parked] == [ctx.lead[0]]
    assert claude.commands("stop") == [["stop", _row(ledger, ctx.lead[1])["bg_id"]]]
    assert _row(ledger, ctx.lead[1])["parked_at"] is not None
    assert _row(ledger, coder[1])["parked_at"] is None


def test_a_parent_that_is_owed_a_wake_up_is_not_parked(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "c1")
    ledger.agent_idle(ctx.lead[1], "stop")
    ledger.message_post(*coder, ctx.lead[0], "a question")
    free["mb"] = 1_200
    assert ledger.park_idle_parents(claude.listing) == []


def test_parking_is_off_when_the_setting_says_so(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    ledger.agent_idle(ctx.lead[1], "stop")
    ledger.settings.memory.park_idle_parents = False
    free["mb"] = 1_200
    assert ledger.park_idle_parents(claude.listing) == []


def test_no_pressure_parks_nobody(ledger: Ledger, claude: MovingClaude, free: dict) -> None:
    ctx = _bootstrap(ledger, claude)
    _spawn_coder(ledger, ctx, "c1")
    ledger.agent_idle(ctx.lead[1], "stop")
    assert ledger.park_idle_parents(claude.listing) == []


# -- the watchdog --------------------------------------------------------------------------------


def test_a_wake_up_unsent_for_minutes_is_reported(ledger: Ledger, claude: MovingClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")
    settings = WatchdogSettings(unsent_wakeup_minutes=3)
    later = utcnow() + timedelta(minutes=4)
    findings = watchdog.scan(ledger.conn, claude.listing, later, settings)
    unsent = [f for f in findings if f.kind == "wake_unsent"]
    assert [f.name for f in unsent] == ["mgr-p1-phase-1"]
    assert "lead-p1-module-1 has owed it a wake-up for 4 minutes" in unsent[0].detail
    assert not [
        f
        for f in watchdog.scan(ledger.conn, claude.listing, utcnow(), settings)
        if f.kind == "wake_unsent"
    ]


def test_the_watchdog_tick_runs_the_memory_passes(
    ledger: Ledger, claude: MovingClaude, free: dict, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    free["mb"] = 1_000
    ledger.agent_spawn(*ctx.lead, name)
    free["mb"] = 64_000
    dog = watchdog.Watchdog(
        host, ledger.conn, ledger.settings.watchdog, exit_server=lambda: None, ledger=ledger
    )
    dog.tick(utcnow())
    state = ledger.conn.execute("SELECT state FROM spawn_queue").fetchone()["state"]
    assert state == "started"


def test_the_memory_settings_load_from_the_settings_file(tmp_path: Path) -> None:
    defaults = load_settings(make_host(tmp_path / "a", settings_text()))
    assert defaults.memory.headroom_mb == 1_500
    assert defaults.memory.park_idle_parents is True
    assert defaults.watchdog.unsent_wakeup_minutes == 5
    swaps = [
        ("headroom_mb: 1500", "headroom_mb:"),
        ("park_idle_parents: true", "park_idle_parents: false"),
    ]
    changed = load_settings(make_host(tmp_path / "b", settings_text(swaps=swaps)))
    assert changed.memory.headroom_mb is None
    assert changed.memory.park_idle_parents is False
    assert Settings.from_snapshot(changed.snapshot()).memory == changed.memory


def test_parking_an_agent_twice_for_the_same_reason_logs_it_once(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _park_manager(ledger, claude, ctx)
    free["mb"] = 1_000
    for body in ("one", "two"):
        ledger.message_post(*ctx.lead, "mgr-p1-phase-1", body)
        events.handle_owed(ledger, {"session_id": ctx.lead[1]})
    parked = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM agent_events WHERE agent_id = ? AND to_state = 'parked'",
        (ctx.manager[1],),
    ).fetchone()["n"]
    assert parked == 1
