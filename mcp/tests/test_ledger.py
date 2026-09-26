from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import load_settings


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def fake_repo(tmp_path: Path, repo_root: Path) -> Path:
    host = tmp_path / "host"
    (host / ".git").mkdir(parents=True)
    claude_dir = host / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    return host


@pytest.fixture
def ledger(fake_repo: Path) -> Ledger:
    return Ledger(fake_repo, db_path=fake_repo / ".sentinel-swarm" / "ledger.db")


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
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _insert_passing_test_run(ledger: Ledger, run_id: int, agent_id: str, scope: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, ?, 'x', 'x', 0, 1, 0, 0, '')",
            (run_id, agent_id, scope),
        )


def _review_scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10},
        {"dimension": "integration", "value": 10},
        {"dimension": "open_items", "value": 10},
    ]


def _accept_module(ledger: Ledger, ctx: dict) -> dict:
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    return ledger.module_review(
        "manager-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=_review_scores(),
    )


def _hand_up_and_accept_phase(ledger: Ledger, ctx: dict) -> dict:
    ledger.phase_update("manager-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    return ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )


# -- settings -----------------------------------------------------------------


def test_settings_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)
    assert settings.tracking == "local"
    assert settings.runtime["oracle"] == "session"
    assert settings.runtime["coder"] == "session"
    assert settings.models["oracle"] == ["opus", "fable"]
    assert settings.models["coder"] == ["sonnet", "haiku"]
    assert settings.rubric.target == 90
    assert settings.rubric.floor == 70
    assert settings.escalation.rounds == 3
    assert settings.escalation.attempts_per_round == 3
    assert settings.test_command is None
    assert settings.parallelism_cap is None


def test_settings_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "does-not-exist")
    assert settings.tracking == "local"
    assert settings.models["lead"] == ["opus", "sonnet"]


def test_settings_overrides_from_frontmatter_fall_back_key_by_key(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "sentinel-swarm.local.md").write_text(
        "---\n"
        "rubric:\n"
        "  target: 95\n"
        "escalation:\n"
        "  rounds: 5\n"
        "test_command: pytest -q {target}\n"
        "parallelism_cap: 4\n"
        "---\n"
        "\n"
        "# local settings\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    assert settings.rubric.target == 95
    assert settings.rubric.floor == 70
    assert settings.escalation.rounds == 5
    assert settings.escalation.attempts_per_round == 3
    assert settings.test_command == "pytest -q {target}"
    assert settings.parallelism_cap == 4
    assert settings.models["oracle"] == ["opus", "fable"]


def test_settings_snapshot_is_json(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)
    data = json.loads(settings.snapshot())
    assert data["tracking"] == "local"
    assert data["rubric"]["target"] == 90
    assert data["escalation"]["rounds"] == 3


def test_fake_repo_settings_have_the_overridden_test_command(ledger: Ledger) -> None:
    assert ledger.settings.test_command == "pytest -q {target}"


# -- run lifecycle --------------------------------------------------------------


def test_run_start_binds_the_oracle(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]
    assert started["run"]["state"] == "active"
    assert oracle["agent_id"] == "sess-1"
    assert oracle["name"] == "oracle"
    assert oracle["role"] == "oracle"
    assert oracle["state"] == "working"
    assert oracle["model"] == "opus"
    assert oracle["runtime"] == "session"


def _write_oracle_file(repo: Path, model_line: str) -> None:
    path = repo / ".claude" / "agents" / "swarm-oracle.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: swarm-oracle\n{model_line}---\n\nBody.\n", encoding="utf-8")


def test_run_start_records_the_oracle_files_model(ledger: Ledger, fake_repo: Path) -> None:
    _write_oracle_file(fake_repo, "model: fable\n")
    assert ledger.run_start(prd="Build X", session_id="sess-1")["oracle"]["model"] == "fable"

    ledger.agent_stop("sess-1", end_reason="exit")
    _write_oracle_file(fake_repo, "model: sonnet\n")
    resumed = ledger.run_start(prd="ignored", session_id="sess-2")
    assert resumed["oracle"]["model"] == "sonnet"


def test_run_start_falls_back_to_the_approved_list_without_a_file_model(
    ledger: Ledger, fake_repo: Path
) -> None:
    _write_oracle_file(fake_repo, "")
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    assert started["oracle"]["model"] == ledger.settings.models["oracle"][0]


def test_run_start_twice_keeps_one_active_run(ledger: Ledger) -> None:
    first = ledger.run_start(prd="Build X", session_id="sess-1")
    second = ledger.run_start(prd="Build Y", session_id="sess-2")
    assert second["resumed"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert second["run"]["prd"] == "Build X"
    active = ledger.conn.execute("SELECT COUNT(*) AS n FROM runs WHERE state = 'active'")
    assert active.fetchone()["n"] == 1
    live_oracles = ledger.conn.execute(
        "SELECT agent_id FROM agents WHERE role = 'oracle' AND ended_at IS NULL"
    ).fetchall()
    assert [r["agent_id"] for r in live_oracles] == ["sess-2"]


def test_run_status_reports_phases_agents_and_open_items(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    status = ledger.run_status("oracle", ctx["oracle_id"])
    assert status["run"]["state"] == "active"
    assert len(status["phases"]) == 1
    assert len(status["agents"]) == 3
    assert status["issues"] == []
    assert status["directives"] == []


def test_run_finish_refuses_when_a_phase_is_not_approved(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.run_finish("oracle", ctx["oracle_id"], "success")


def test_run_finish_refuses_when_a_file_claim_is_live(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-1")
    # Bypasses phase_update's own review gates: this test isolates run_finish's live-claim
    # check, which a live claim's owning module would otherwise never let reach "approved".
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    with pytest.raises(LedgerError):
        ledger.run_finish("oracle", ctx["oracle_id"], "success")


def test_run_finish_refuses_when_a_directive_is_open(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    # Bypasses phase_update's own review gates: this test isolates run_finish's open-directive
    # check.
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    ledger.directive_submit("watchdog", "watchdog", "Agent X looks stuck.")
    with pytest.raises(LedgerError):
        ledger.run_finish("oracle", ctx["oracle_id"], "success")


def test_handed_up_refuses_while_a_lead_of_the_phase_is_live(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="lead-1"):
        ledger.phase_update("manager-1", "mgr-agent", ctx["phase_id"], "handed_up")

    ledger.agent_release("manager-1", "mgr-agent", ctx["lead"]["agent_id"])
    _accept_module(ledger, ctx)
    phase = ledger.phase_update("manager-1", "mgr-agent", ctx["phase_id"], "handed_up")
    assert phase["state"] == "handed_up"


def test_phase_approval_releases_the_manager_and_everything_under_it(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module(ledger, ctx)
    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    _hand_up_and_accept_phase(ledger, ctx)
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")

    rows = ledger.conn.execute(
        "SELECT name, state, ended_at FROM agents WHERE role IN ('manager', 'lead')"
    ).fetchall()
    assert {(r["name"], r["state"]) for r in rows} == {
        ("manager-1", "released"),
        ("lead-1", "released"),
    }
    assert all(r["ended_at"] is not None for r in rows)


def test_run_finish_releases_every_agent_left_live(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    ledger.run_finish("oracle", ctx["oracle_id"], "success")

    live = ledger.conn.execute("SELECT name FROM agents WHERE ended_at IS NULL").fetchall()
    assert live == []


def test_run_finish_succeeds_once_the_run_is_clear(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module(ledger, ctx)
    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    _hand_up_and_accept_phase(ledger, ctx)
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    result = ledger.run_finish("oracle", ctx["oracle_id"], "success")
    assert result["state"] == "finished"
    assert result["outcome"] == "success"
    assert result["ended_at"] is not None


def test_profile_set_is_oracle_only_and_updates_settings(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.profile_set("manager-1", ctx["manager"]["agent_id"], test_command="pytest")

    result = ledger.profile_set("oracle", ctx["oracle_id"], build_command="npm run build")
    assert result["build_command"] == "npm run build"
    assert result["test_command"] == "pytest -q {target}"
    assert ledger.settings.build_command == "npm run build"


def test_guidelines_set_is_oracle_only_and_get_returns_the_latest(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.guidelines_set("manager-1", ctx["manager"]["agent_id"], "nope")

    ledger.guidelines_set("oracle", ctx["oracle_id"], "Keep functions under 40 lines.")
    ledger.guidelines_set("oracle", ctx["oracle_id"], "Keep functions under 30 lines.")
    latest = ledger.guidelines_get("manager-1", ctx["manager"]["agent_id"])
    assert latest["body"] == "Keep functions under 30 lines."


# -- phases and dependencies ----------------------------------------------------


def test_phase_deps_and_plan_unlocked(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]
    run_id = started["run"]["run_id"]

    phase_1 = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_2 = ledger.phase_add("oracle", oracle_id, "phase-2", depends_on=[phase_1["phase_id"]])

    unlocked = {p["phase_id"] for p in ledger.plan_unlocked("oracle", oracle_id)}
    assert unlocked == {phase_1["phase_id"]}

    # phase-1 has no modules, so its own module-review gate is vacuous; it still needs to go
    # through handed_up and an accepted phase_review before it can be approved.
    ledger.phase_update("oracle", oracle_id, phase_1["phase_id"], "handed_up")
    _insert_passing_test_run(ledger, run_id, oracle_id, "full")
    ledger.phase_review(
        "oracle",
        oracle_id,
        phase_1["phase_id"],
        "accepted",
        "nothing to build",
        scores=_review_scores(),
    )
    ledger.phase_update("oracle", oracle_id, phase_1["phase_id"], "approved")

    unlocked_after = {p["phase_id"] for p in ledger.plan_unlocked("oracle", oracle_id)}
    assert unlocked_after == {phase_2["phase_id"]}


def test_module_add_refuses_a_phase_that_is_not_the_managers_own(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    other_phase = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    with pytest.raises(LedgerError):
        ledger.module_add(
            "manager-1", ctx["manager"]["agent_id"], other_phase["phase_id"], "module-x"
        )


# -- briefs -----------------------------------------------------------------------


def test_brief_create_refuses_wrong_child_role(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.brief_create(
            "manager-1", ctx["manager"]["agent_id"], "coder-x", "coder", "sonnet", "body"
        )


def test_brief_create_refuses_unapproved_model(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.brief_create(
            "manager-1", ctx["manager"]["agent_id"], "lead-x", "lead", "haiku", "body"
        )


def test_brief_create_refuses_a_name_a_live_agent_holds(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.brief_create(
            "manager-1", ctx["manager"]["agent_id"], "lead-1", "lead", "sonnet", "body"
        )


def test_brief_create_refuses_a_duplicate_unacked_brief(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.brief_create(
        "manager-1", ctx["manager"]["agent_id"], "lead-2", "lead", "sonnet", "first"
    )
    with pytest.raises(LedgerError):
        ledger.brief_create(
            "manager-1", ctx["manager"]["agent_id"], "lead-2", "lead", "sonnet", "second"
        )


def test_brief_get_returns_the_latest_and_raises_when_missing(ledger: Ledger) -> None:
    _bootstrap(ledger)
    brief = ledger.brief_get("lead-1", "lead-1")
    assert brief["child_name"] == "lead-1"
    with pytest.raises(LedgerError):
        ledger.brief_get("nobody", "does-not-exist")


def test_brief_ack_binds_the_placeholder_row_from_agent_register_start(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.brief_create(
        "manager-1",
        ctx["manager"]["agent_id"],
        "lead-2",
        "lead",
        "sonnet",
        "body",
        module_id=ctx["module_id"],
    )
    placeholder = ledger.agent_register_start(
        "lead-2-agent", "lead", parent_agent_id=ctx["manager"]["agent_id"]
    )
    assert placeholder["state"] == "registered"
    assert placeholder["name"] == "lead-2-agent"

    bound = ledger.brief_ack("lead-2", "lead-2-agent")
    assert bound["agent_id"] == "lead-2-agent"
    assert bound["name"] == "lead-2"
    assert bound["role"] == "lead"
    assert bound["state"] == "working"
    assert bound["module_id"] == ctx["module_id"]


def test_brief_ack_refuses_when_no_unacked_brief_exists(ledger: Ledger) -> None:
    with pytest.raises(LedgerError):
        ledger.brief_ack("nobody", "agent-x")


def test_brief_ack_refuses_when_the_agent_id_is_already_bound(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.brief_create("manager-1", ctx["manager"]["agent_id"], "lead-2", "lead", "sonnet", "body")
    with pytest.raises(LedgerError):
        ledger.brief_ack("lead-2", ctx["lead"]["agent_id"])


def test_brief_ack_refuses_when_a_live_agent_already_holds_the_name(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.brief_create("manager-1", ctx["manager"]["agent_id"], "lead-2", "lead", "sonnet", "body")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, state, started_at) "
            "VALUES ('ghost', 'lead-2', 'lead', 'working', strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
    ledger.agent_register_start("lead-2-agent", "lead", parent_agent_id=ctx["manager"]["agent_id"])
    with pytest.raises(LedgerError):
        ledger.brief_ack("lead-2", "lead-2-agent")


# -- identity -----------------------------------------------------------------


def test_resolve_refuses_a_caller_name_that_does_not_match_the_agent_id(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.run_status("someone-else", ctx["oracle_id"])


# -- agent lifecycle ------------------------------------------------------------


def test_agent_heartbeat_ignores_an_unknown_agent(ledger: Ledger) -> None:
    assert ledger.agent_heartbeat("nope") == {"known": False}


def test_agent_heartbeat_does_not_write_an_agent_event(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    before = len(ledger.events(agent_id=ctx["lead"]["agent_id"]))
    ledger.agent_heartbeat(ctx["lead"]["agent_id"], activity="writing code")
    after = len(ledger.events(agent_id=ctx["lead"]["agent_id"]))
    assert after == before


def test_agent_stop_does_not_end_a_working_agent(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    result = ledger.agent_stop(ctx["lead"]["agent_id"])
    assert result == {"ended": False, "state": "working"}
    row = ledger.conn.execute(
        "SELECT ended_at FROM agents WHERE agent_id = ?", (ctx["lead"]["agent_id"],)
    ).fetchone()
    assert row["ended_at"] is None


def test_agent_stop_ignores_an_unknown_agent(ledger: Ledger) -> None:
    assert ledger.agent_stop("nope") == {"ended": False, "state": "unknown"}


def test_agent_stop_ends_a_released_agent_and_records_tokens(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    result = ledger.agent_stop(
        ctx["lead"]["agent_id"],
        transcript_path="/tmp/t.jsonl",
        tokens={"input_tokens": 10, "output_tokens": 5},
        end_reason="handoff",
    )
    assert result["ended"] is True
    assert result["ended_at"] is not None
    assert result["transcript_path"] == "/tmp/t.jsonl"
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert result["end_reason"] == "handoff"


def test_agent_release_requires_the_actual_parent(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.agent_release("oracle", ctx["oracle_id"], ctx["lead"]["agent_id"])


# -- file ownership -------------------------------------------------------------


def test_claim_file_requires_lead_role(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.claim_file(
            "manager-1", ctx["manager"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-1"
        )


def test_claim_file_refuses_a_duplicate_live_claim(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_name, lead_id = "lead-1", ctx["lead"]["agent_id"]
    ledger.claim_file(lead_name, lead_id, "src/a.py", "tests/test_a.py", "coder-1")
    with pytest.raises(LedgerError):
        ledger.claim_file(lead_name, lead_id, "src/a.py", "tests/test_a.py", "coder-2")


def test_claim_file_refuses_a_second_live_claim_for_one_coder(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_name, lead_id = "lead-1", ctx["lead"]["agent_id"]
    ledger.claim_file(lead_name, lead_id, "src/a.py", "tests/test_a.py", "coder-1")
    with pytest.raises(LedgerError, match="one Coder owns one file"):
        ledger.claim_file(lead_name, lead_id, ".gitignore", None, "coder-1")
    ledger.release_file(lead_name, lead_id, "src/a.py")
    assert ledger.claim_file(lead_name, lead_id, ".gitignore", None, "coder-1")["path"] == (
        ".gitignore"
    )


def test_who_owns_and_release_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-1")
    owned = ledger.who_owns("src/a.py")
    assert owned["owner"] == "coder-1"

    ledger.release_file("lead-1", ctx["lead"]["agent_id"], "src/a.py")
    released = ledger.who_owns("src/a.py")
    assert released["owner"] is None


# -- messages -----------------------------------------------------------------


def test_message_post_and_inbox_marks_messages_read(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.message_post("manager-1", ctx["manager"]["agent_id"], "lead-1", "Start on module-1.")
    inbox = ledger.message_inbox("lead-1", ctx["lead"]["agent_id"])
    assert len(inbox) == 1
    assert inbox[0]["body"] == "Start on module-1."
    assert ledger.message_inbox("lead-1", ctx["lead"]["agent_id"]) == []


def test_message_post_refuses_a_name_not_registered_in_the_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="registered names"):
        ledger.message_post("manager-1", ctx["manager"]["agent_id"], "lead-l", "Typo.")
    assert ledger.message_inbox("lead-1", ctx["lead"]["agent_id"]) == []


# -- directives -----------------------------------------------------------------


def test_directive_round_trip(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    directive = ledger.directive_submit("user-chat", "alex", "Change the plan.")
    inbox = ledger.directive_inbox("oracle", ctx["oracle_id"])
    assert directive["directive_id"] in {d["directive_id"] for d in inbox}

    resolved = ledger.directive_resolve(
        "oracle", ctx["oracle_id"], directive["directive_id"], "applied", "Updated phase 1."
    )
    assert resolved["state"] == "resolved"
    assert resolved["outcome"] == "applied"

    inbox_after = ledger.directive_inbox("oracle", ctx["oracle_id"])
    assert directive["directive_id"] not in {d["directive_id"] for d in inbox_after}


def test_directive_submit_refuses_an_unknown_source(ledger: Ledger) -> None:
    _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.directive_submit("carrier-pigeon", "alex", "body")


def test_directive_inbox_and_resolve_are_oracle_only(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    directive = ledger.directive_submit("skill", "setup", "Detected pytest as the runner.")
    with pytest.raises(LedgerError):
        ledger.directive_inbox("manager-1", ctx["manager"]["agent_id"])
    with pytest.raises(LedgerError):
        ledger.directive_resolve(
            "manager-1", ctx["manager"]["agent_id"], directive["directive_id"], "applied", "ok"
        )


def test_a_needs_user_directive_stays_open_until_the_user_replies(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    asked = ledger.directive_submit("user-chat", "alex", "Use Postgres or SQLite?")

    waiting = ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "needs_user", "Which database?"
    )
    assert (waiting["state"], waiting["outcome"]) == ("open", "needs_user")
    assert waiting["resolved_at"] is None
    inbox = ledger.directive_inbox("oracle", ctx["oracle_id"])
    assert asked["directive_id"] in {d["directive_id"] for d in inbox}

    reply = ledger.directive_submit(
        "outside-session", "alex", "SQLite.", reply_to=asked["directive_id"]
    )
    parent = ledger.conn.execute(
        "SELECT * FROM directives WHERE directive_id = ?", (asked["directive_id"],)
    ).fetchone()
    assert (parent["state"], parent["outcome"]) == ("resolved", "needs_user")
    assert parent["resolved_at"] is not None
    inbox = ledger.directive_inbox("oracle", ctx["oracle_id"])
    assert [d["directive_id"] for d in inbox] == [reply["directive_id"]]
    assert inbox[0]["reply_to"] == asked["directive_id"]


def test_a_needs_user_directive_can_be_resolved_again_by_the_oracle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    asked = ledger.directive_submit("user-chat", "alex", "Add a CLI?")
    ledger.directive_resolve("oracle", ctx["oracle_id"], asked["directive_id"], "needs_user", "?")

    done = ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "scheduled", "Phase 3."
    )
    assert (done["state"], done["outcome"]) == ("resolved", "scheduled")


def test_directive_resolve_refuses_an_unknown_outcome(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    directive = ledger.directive_submit("user-chat", "alex", "Rename it.")
    with pytest.raises(LedgerError, match="unknown directive outcome 'needs-user'"):
        ledger.directive_resolve(
            "oracle", ctx["oracle_id"], directive["directive_id"], "needs-user", "?"
        )


def test_directive_submit_refuses_a_reply_to_an_unknown_directive(ledger: Ledger) -> None:
    _bootstrap(ledger)
    with pytest.raises(LedgerError, match="reply_to 99"):
        ledger.directive_submit("user-chat", "alex", "Yes.", reply_to=99)


# -- overrides --------------------------------------------------------------------


def test_override_grant_is_oracle_only(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.override_grant(
            "manager-1", ctx["manager"]["agent_id"], "hook-4", "coder-1", "src/a.py", "urgent fix"
        )


def test_override_consume_marks_the_first_match_used_once(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.override_grant("oracle", ctx["oracle_id"], "hook-4", "coder-1", "src/a.py", "urgent fix")
    assert ledger.override_consume("hook-4", "coder-1", "src/a.py") is True
    assert ledger.override_consume("hook-4", "coder-1", "src/a.py") is False


# -- issues -----------------------------------------------------------------------


def test_issue_lifecycle_and_escalation_stops_at_the_configured_round_limit(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-1")
    file_row = ledger.who_owns("src/a.py")["file"]

    issue = ledger.issue_open(
        "lead-1", ctx["lead"]["agent_id"], file_row["file_id"], "Flaky test", "Fails 1 in 10."
    )
    ledger.idea_record(
        "lead-1", ctx["lead"]["agent_id"], issue["issue_id"], "Add a retry.", "tried"
    )

    issues = ledger.issue_list("lead-1", ctx["lead"]["agent_id"], file_id=file_row["file_id"])
    assert len(issues) == 1
    assert issues[0]["round"] == 1

    escalated = ledger.issue_escalate("lead-1", ctx["lead"]["agent_id"], issue["issue_id"])
    assert escalated["round"] == 2
    assert escalated["escalated_to"] == "manager-1"
    escalated = ledger.issue_escalate("manager-1", "mgr-agent", issue["issue_id"])
    assert escalated["round"] == 3
    assert escalated["escalated_to"] == "oracle"
    with pytest.raises(LedgerError):
        ledger.issue_escalate("lead-1", ctx["lead"]["agent_id"], issue["issue_id"])

    manager_inbox = ledger.message_inbox("manager-1", "mgr-agent")
    assert [(m["from_name"], m["to_name"]) for m in manager_inbox] == [("lead-1", "manager-1")]
    oracle_inbox = ledger.message_inbox("oracle", ctx["oracle_id"])
    assert [(m["from_name"], m["to_name"]) for m in oracle_inbox] == [("manager-1", "oracle")]


def test_issue_escalate_refuses_an_agent_outside_the_parent_chain(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-1")
    file_row = ledger.who_owns("src/a.py")["file"]
    issue = ledger.issue_open(
        "lead-1", ctx["lead"]["agent_id"], file_row["file_id"], "Flaky test", "Fails 1 in 10."
    )
    ledger.brief_create(
        "manager-1",
        "mgr-agent",
        "lead-2",
        "lead",
        "sonnet",
        "Own module-2.",
        module_id=ctx["module_id"],
    )
    ledger.agent_register_start("lead-2-agent", "lead", parent_agent_id="mgr-agent")
    ledger.brief_ack("lead-2", "lead-2-agent")

    with pytest.raises(LedgerError, match="parent chain"):
        ledger.issue_escalate("lead-2", "lead-2-agent", issue["issue_id"])
    assert ledger.issue_list("lead-1", ctx["lead"]["agent_id"])[0]["round"] == 1


# -- agent_events audit trail -----------------------------------------------------


def test_every_lifecycle_transition_writes_an_agent_event(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)

    oracle_events = ledger.events(agent_id=ctx["oracle_id"])
    assert any(e["to_state"] == "working" and e["reason"] == "run_start" for e in oracle_events)

    manager_events = ledger.events(agent_id=ctx["manager"]["agent_id"])
    assert any(e["to_state"] == "working" and e["reason"] == "brief_ack" for e in manager_events)

    ledger.agent_register_start("coder-agent", "coder", parent_agent_id=ctx["lead"]["agent_id"])
    register_events = ledger.events(agent_id="coder-agent")
    assert any(e["to_state"] == "registered" for e in register_events)

    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    lead_events = ledger.events(agent_id=ctx["lead"]["agent_id"])
    assert any(e["to_state"] == "released" for e in lead_events)


# -- resume and phase updates by a Manager ------------------------------------------


def test_run_start_resumes_the_active_run_for_a_new_session(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.agent_stop("sess-1", end_reason="exit")
    resumed = ledger.run_start(prd="ignored", session_id="sess-2")
    assert resumed["resumed"] is True
    assert resumed["oracle"]["agent_id"] == "sess-2"
    assert resumed["oracle"]["state"] == "working"
    status = ledger.run_status("oracle", "sess-2")
    assert status["run"]["run_id"] == resumed["run"]["run_id"]
    manager = ledger.conn.execute(
        "SELECT parent_agent_id FROM agents WHERE agent_id = ?", (ctx["manager"]["agent_id"],)
    ).fetchone()
    assert manager["parent_agent_id"] == "sess-2"
    with pytest.raises(LedgerError):
        ledger.run_status("oracle", "sess-1")


def test_manager_sets_its_own_phase_to_working_and_handed_up_only(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    mgr = ctx["manager"]["agent_id"]
    assert ledger.phase_update("manager-1", mgr, ctx["phase_id"], "working")["state"] == "working"
    ledger.agent_release("manager-1", mgr, ctx["lead"]["agent_id"])
    _accept_module(ledger, ctx)
    assert (
        ledger.phase_update("manager-1", mgr, ctx["phase_id"], "handed_up")["state"] == "handed_up"
    )
    with pytest.raises(LedgerError):
        ledger.phase_update("manager-1", mgr, ctx["phase_id"], "approved")
    other = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    with pytest.raises(LedgerError):
        ledger.phase_update("manager-1", mgr, other["phase_id"], "working")


# -- run_pause ----------------------------------------------------------------------


def test_run_pause_is_oracle_only_and_sets_the_run_paused(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError):
        ledger.run_pause("manager-1", ctx["manager"]["agent_id"], "needs a token")
    with pytest.raises(LedgerError, match="reason"):
        ledger.run_pause("oracle", ctx["oracle_id"], "  ")

    paused = ledger.run_pause("oracle", ctx["oracle_id"], "the API key is missing")
    assert paused["state"] == "paused"
    assert ledger.pause_reason(paused["run_id"]) == "the API key is missing"
    assert ledger.run_status("oracle", ctx["oracle_id"])["run"]["state"] == "paused"
    with pytest.raises(LedgerError, match="already paused"):
        ledger.run_pause("oracle", ctx["oracle_id"], "again")


def test_run_start_resumes_a_paused_run_to_active(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.run_pause("oracle", ctx["oracle_id"], "the API key is missing")
    ledger.agent_stop("sess-1", end_reason="exit")

    resumed = ledger.run_start(prd="ignored", session_id="sess-2")
    assert resumed["resumed"] is True
    assert resumed["run"]["state"] == "active"
    assert resumed["oracle"]["agent_id"] == "sess-2"
    assert any(e["reason"] == "resumed from pause" for e in ledger.events(agent_id="sess-2"))


def test_run_finish_works_while_paused(ledger: Ledger) -> None:
    ledger.run_start(prd="Build X", session_id="sess-1")
    ledger.run_pause("oracle", "sess-1", "waiting on the user")
    finished = ledger.run_finish("oracle", "sess-1", "done")
    assert finished["state"] == "finished"


def test_report_build_shows_the_pause_reason_and_the_run_total(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.agent_stop(
        ctx["oracle_id"],
        tokens={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 100,
            "cache_write_tokens": 7,
        },
    )
    ledger.agent_stop(
        ctx["manager"]["agent_id"],
        tokens={"input_tokens": 1, "output_tokens": 2, "cache_write_tokens": 3},
    )
    ledger.run_pause("oracle", ctx["oracle_id"], "the API key is missing")

    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "Outcome: paused" in text
    assert "Paused: the API key is missing" in text
    assert "cache_write=7" in text
    assert "Run total: tokens in=11 out=7 cache_read=100 cache_write=10" in text
    assert "Duration: " in text and " so far" in text
    assert ", cost " in text
    assert "Costs are at list prices" in text


def test_report_cost_uses_list_prices_per_model() -> None:
    from swarm_ledger import pricing, review

    tokens = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000,
        "cache_write_tokens": 1_000_000,
    }
    assert pricing.estimate("sonnet", tokens) == 2.0 + 10.0 + 0.20 + 4.0
    assert pricing.estimate("claude-haiku-4-5", tokens) == 1.0 + 5.0 + 0.10 + 2.0
    assert pricing.estimate(None, tokens) is None
    usage = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
    }
    assert pricing.response_cost("claude-opus-5-5", usage) == 4.0 + 5.0 + 8.0
    assert review._duration("2026-09-25T02:00:00.000Z", "2026-09-25T02:35:10.000Z") == "35m 10s"
    assert review._duration("2026-09-25T02:00:00.000Z", "2026-09-25T03:05:00.000Z") == "1h 5m"


def test_report_times_a_phase_from_its_first_manager_start(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE phases SET started_at = '2026-09-25T06:00:00.000Z', "
            "ended_at = '2026-09-25T06:16:00.000Z' WHERE phase_id = ?",
            (ctx["phase_id"],),
        )
        conn.execute(
            "UPDATE agents SET started_at = '2026-09-25T06:10:00.000Z' WHERE agent_id = ?",
            (ctx["manager"]["agent_id"],),
        )
    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "## Phase: phase-1 (unlocked, 6m 0s)" in text


@pytest.mark.parametrize("given", ["None", "null", "", "  "])
def test_claim_file_reads_a_text_null_test_path_as_no_test_file(ledger: Ledger, given: str) -> None:
    ctx = _bootstrap(ledger)
    claimed = ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "pkg/__init__.py", given, "c-1")
    assert claimed["test_path"] is None
