from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from swarm_ledger import agentfiles, sessions, setup
from swarm_ledger.db import connect, write_tx
from swarm_ledger.drive import STOP_ATTEMPTS_IN_A_ROW, STOP_ATTEMPTS_TOTAL, compute_loop_status
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError, child_roles_of
from swarm_ledger.ledger import Ledger, repo_slug
from swarm_ledger.watchdog import (
    DRIVER_CHECKIN_GRACE,
    DRIVER_CHECKIN_INTERVAL,
    DRIVER_UNSENT_AFTER,
    driver_notices,
    record,
    scan,
    utcnow,
)

# -- Shared fixtures ------------------------------------------------------------------------

_ROLE_FILE = """---
name: swarm-{role}
description: The {role}.
model: sonnet
permissionMode: acceptEdits
tools: Read, SendMessage, Bash, Agent, mcp__swarm-ledger
mcpServers:
  - codebase-kg:
      command: python
      args: [".sentinel-swarm/hook.py", "mcp", "codebase-kg@codebase-kg", "codebase-kg"]
---

You are a {role}.
"""


class FakeClaude:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.listing: list[dict] = []
        self._count = 0

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        self.calls.append((list(args), cwd))
        if args[:2] == ["agents", "--json"]:
            return json.dumps(self.listing)
        if args[0] == "stop":
            return ""
        name = args[args.index("--name") + 1]
        self._count += 1
        session_id = f"{self._count:08x}-aaaa-bbbb-cccc-dddddddddddd"
        self.add(session_id, name, bg_id=session_id[:8])
        return f"backgrounded · {session_id[:8]} · {name}\n"

    def add(self, session_id: str, name: str, bg_id: str | None = None) -> dict:
        entry = {"pid": 100 + len(self.listing), "sessionId": session_id, "name": name}
        entry |= {"status": "busy", "state": "working"}
        if bg_id:
            entry["id"] = bg_id
        self.listing.append(entry)
        return entry


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(sessions, "_run", fake)
    return fake


def _settings_text(repo_root: Path) -> str:
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    text = text.replace("build_command:\n", "build_command: npm run build\n")
    return text


def _register_plugins(plugin_ids: list[str]) -> None:
    config_dir = Path(os.environ["CLAUDE_CONFIG_DIR"])
    path = config_dir / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"plugins": {pid: [{"scope": "user"}] for pid in plugin_ids}}), "utf-8"
    )


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    claude_dir = root / ".claude" / "agents"
    claude_dir.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".claude" / "sentinel-swarm.local.md").write_text(
        _settings_text(repo_root), encoding="utf-8"
    )
    for role in ("oracle", "manager", "lead", "coder", "driver"):
        (claude_dir / f"swarm-{role}.md").write_text(_ROLE_FILE.format(role=role), "utf-8")
    records = root / ".sentinel-swarm"
    records.mkdir()
    (records / "server.json").write_text(
        json.dumps({"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}), "utf-8"
    )
    _register_plugins(["cartographer@cartographer", "web-driver@accessibility-tools"])
    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger, claude: FakeClaude, oracle_session: str = "sess-oracle") -> dict:
    claude.add(oracle_session, "host-oracle")
    started = ledger.run_start(prd="Build X", session_id=oracle_session)
    oracle_id = started["oracle"]["agent_id"]
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")
    return {
        "oracle_id": oracle_id,
        "phase_id": phase["phase_id"],
        "run_id": started["run"]["run_id"],
    }


def _fabricate_fixer(ledger: Ledger, oracle_id: str, role: str, name: str) -> None:
    ledger.brief_create("oracle", oracle_id, name, "manager", "opus", "Own it.")
    ledger.agent_register_start(f"{name}-agent", role, parent_agent_id=oracle_id)
    ledger.brief_ack(name, f"{name}-agent")


def _seed_exploration(
    ledger: Ledger, run_id: int, ordinal: int, fingerprints: list[str], area: str = "screen-a"
) -> int:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, focus, state, done_at) "
            "VALUES (?, ?, 'focus', 'done', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (run_id, ordinal),
        )
        request_id = cur.lastrowid
        assert request_id is not None
        for fp in fingerprints:
            conn.execute(
                "INSERT INTO drive_findings (request_id, run_id, fingerprint, title, "
                "severity, area) VALUES (?, ?, ?, ?, 'major', ?)",
                (request_id, run_id, fp, fp, area),
            )
    return request_id


# -- Pure loop-status computation -------------------------------------------------------------


def test_fingerprint_streak_and_attempts() -> None:
    status = compute_loop_status(
        requests=[{"request_id": i, "ordinal": i, "state": "done"} for i in range(1, 5)],
        findings=[{"request_id": i, "fingerprint": "F1", "area": "a"} for i in range(1, 5)],
    )
    fp = status["fingerprints"]["F1"]
    assert fp["attempts_in_a_row"] == STOP_ATTEMPTS_IN_A_ROW
    assert fp["total_attempts"] == 3
    assert fp["stop"] is True
    assert status["stopped"] is True
    assert any("fix attempts in a row" in reason for reason in status["reasons"])


def test_total_attempts_cap_stops_even_with_gaps() -> None:
    # Present, absent, present, absent, present, absent -- never 3 in a row, but 5 attempts.
    ordinals = [1, 2, 3, 4, 5, 6]
    status = compute_loop_status(
        requests=[{"request_id": o, "ordinal": o, "state": "done"} for o in ordinals],
        findings=[{"request_id": o, "fingerprint": "F1", "area": "a"} for o in (1, 2, 3, 4, 5, 6)],
    )
    fp = status["fingerprints"]["F1"]
    assert fp["total_attempts"] >= STOP_ATTEMPTS_TOTAL


def test_regression_is_detected_when_a_fixed_finding_returns() -> None:
    status = compute_loop_status(
        requests=[{"request_id": o, "ordinal": o, "state": "done"} for o in (1, 2, 3)],
        findings=[
            {"request_id": 1, "fingerprint": "F1", "area": "a"},
            {"request_id": 3, "fingerprint": "F1", "area": "a"},
        ],
    )
    assert status["fingerprints"]["F1"]["regressed"] is True
    assert any("regression" in reason for reason in status["reasons"])


def test_no_regression_for_a_single_occurrence() -> None:
    status = compute_loop_status(
        requests=[{"request_id": 1, "ordinal": 1, "state": "done"}],
        findings=[{"request_id": 1, "fingerprint": "F1", "area": "a"}],
    )
    assert status["fingerprints"]["F1"]["regressed"] is False
    assert status["stopped"] is False


def test_three_waves_in_a_row_fixing_nothing_stops_the_loop() -> None:
    # Exploration 1 finds F1, and it is never fixed across 2, 3, or 4: every consecutive
    # pair removes nothing, so 3 waves in a row (2, 3, 4) fixed nothing. F1 itself also
    # crosses the attempts-in-a-row threshold; both reasons are expected to fire.
    requests = [{"request_id": o, "ordinal": o, "state": "done"} for o in (1, 2, 3, 4)]
    findings = [{"request_id": o, "fingerprint": "F1", "area": "a"} for o in (1, 2, 3, 4)]
    status = compute_loop_status(requests, findings)
    assert any("fixed nothing" in reason for reason in status["reasons"])
    assert status["stopped"] is True


def test_fixes_causing_bugs_is_detected() -> None:
    requests = [{"request_id": o, "ordinal": o, "state": "done"} for o in (1, 2)]
    findings = [
        {"request_id": 1, "fingerprint": "F1", "area": "screen-a"},
        {"request_id": 1, "fingerprint": "F2", "area": "screen-a"},
        # F1 and F2 are gone in exploration 2 (fixed); two new findings appear in the
        # same area, at least as many as were fixed.
        {"request_id": 2, "fingerprint": "F3", "area": "screen-a"},
        {"request_id": 2, "fingerprint": "F4", "area": "screen-a"},
    ]
    status = compute_loop_status(requests, findings)
    assert any("caused new findings" in reason for reason in status["reasons"])


def test_ping_pong_between_two_fingerprints_is_detected() -> None:
    requests = [{"request_id": o, "ordinal": o, "state": "done"} for o in (1, 2, 3, 4)]
    findings = [
        {"request_id": 1, "fingerprint": "F1", "area": "a"},
        {"request_id": 2, "fingerprint": "F2", "area": "a"},
        {"request_id": 3, "fingerprint": "F1", "area": "a"},
        {"request_id": 4, "fingerprint": "F2", "area": "a"},
    ]
    status = compute_loop_status(requests, findings)
    assert any("ping-pong" in reason for reason in status["reasons"])


def test_clean_latest_is_true_only_for_a_finding_free_final_exploration() -> None:
    requests = [{"request_id": o, "ordinal": o, "state": "done"} for o in (1, 2)]
    findings = [{"request_id": 1, "fingerprint": "F1", "area": "a"}]
    status = compute_loop_status(requests, findings)
    assert status["clean_latest"] is True
    assert status["explorations_done"] == 2


def test_no_done_exploration_reports_clean_latest_false() -> None:
    status = compute_loop_status(requests=[], findings=[])
    assert status["clean_latest"] is False
    assert status["explorations_done"] == 0
    assert status["stopped"] is False


# -- Role plumbing: identity, settings, agentfiles ---------------------------------------------


def test_oracle_may_brief_a_manager_or_a_driver() -> None:
    assert child_roles_of("oracle") == ("manager", "driver")
    assert child_roles_of("manager") == ("lead",)
    assert child_roles_of("driver") == ()


def test_optional_servers_join_driver_plugins_only_for_the_driver_role(host: Path) -> None:
    servers = agentfiles.optional_servers(host, "driver")
    assert {"cartographer", "web-driver-kg"} <= set(servers)

    coder_servers = agentfiles.optional_servers(host, "coder")
    assert "cartographer" not in coder_servers
    assert "web-driver-kg" not in coder_servers


def test_driver_available_needs_cartographer_and_a_driver_plugin(tmp_path: Path) -> None:
    host = tmp_path / "bare"
    host.mkdir()
    assert agentfiles.driver_available(host) is False

    _register_plugins(["cartographer@cartographer"])
    assert agentfiles.driver_available(host) is False

    _register_plugins(["cartographer@cartographer", "web-driver@accessibility-tools"])
    assert agentfiles.driver_available(host) is True


# -- setup: the Driver's role file is conditional ----------------------------------------------


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    root = tmp_path / "bare-host"
    (root / ".git" / "info").mkdir(parents=True)
    return root


def test_setup_skips_the_driver_file_with_neither_plugin(bare_repo: Path) -> None:
    report = setup.run_setup(bare_repo)
    assert not setup.role_file(bare_repo, "driver").exists()
    assert any(line.startswith("skipped .claude/agents/swarm-driver.md") for line in report.lines)


def test_setup_skips_the_driver_file_with_only_cartographer(bare_repo: Path) -> None:
    _register_plugins(["cartographer@cartographer"])
    report = setup.run_setup(bare_repo)
    assert not setup.role_file(bare_repo, "driver").exists()
    assert any("skipped" in line and "driver" in line for line in report.lines)


def test_setup_writes_the_driver_file_with_cartographer_and_a_driver_plugin(
    bare_repo: Path,
) -> None:
    _register_plugins(["cartographer@cartographer", "android-driver@accessibility-tools"])
    report = setup.run_setup(bare_repo)
    path = setup.role_file(bare_repo, "driver")
    assert path.is_file()
    assert path.read_text("utf-8") == setup.template_file("driver").read_text("utf-8")
    assert "wrote .claude/agents/swarm-driver.md" in report.lines


# -- drive_request ------------------------------------------------------------------------------


def test_drive_request_spawns_the_driver_session(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    result = ledger.drive_request("oracle", ctx["oracle_id"], "cover every PRD feature")

    assert result["ordinal"] == 1
    assert result["state"] == "open"
    assert result["focus"] == "cover every PRD feature"
    assert result["driver"]["role"] == "driver"
    assert result["loop_status"]["stopped"] is False

    expected_name = ledger.session_name(ctx["run_id"], "driver-e1")
    driver_row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (result["agent_id"],)
    ).fetchone()
    assert driver_row["name"] == "driver-e1"
    assert driver_row["session_name"] == expected_name
    assert expected_name.startswith(f"{repo_slug(ledger.repo_root)}-r{ctx['run_id']}-")
    assert expected_name.endswith("-driver-e1")


def test_drive_request_refuses_without_a_driver_available(tmp_path: Path, repo_root: Path) -> None:
    root = tmp_path / "no-driver-host"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".claude" / "sentinel-swarm.local.md").write_text(
        _settings_text(repo_root), encoding="utf-8"
    )
    ledger = Ledger(root, db_path=root / ".sentinel-swarm" / "ledger.db")
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    with pytest.raises(LedgerError, match="no Driver is available"):
        ledger.drive_request("oracle", started["oracle"]["agent_id"], "cover everything")


def test_drive_request_refuses_a_second_open_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.drive_request("oracle", ctx["oracle_id"], "first pass")
    with pytest.raises(LedgerError, match="is still open"):
        ledger.drive_request("oracle", ctx["oracle_id"], "second pass")


def test_drive_request_refuses_while_a_fix_is_running(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    _fabricate_fixer(ledger, ctx["oracle_id"], "manager", "manager-1")
    with pytest.raises(LedgerError, match="a fix is still running"):
        ledger.drive_request("oracle", ctx["oracle_id"], "next pass")


def test_drive_request_is_oracle_only(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    _fabricate_fixer(ledger, ctx["oracle_id"], "manager", "manager-1")
    with pytest.raises(LedgerError):
        ledger.drive_request("manager-1", "manager-1-agent", "not yours to request")


# -- drive_issue, drive_checkin, drive_done ------------------------------------------------------


def _open_request(ledger: Ledger, claude: FakeClaude) -> dict:
    ctx = _bootstrap(ledger, claude)
    request = ledger.drive_request("oracle", ctx["oracle_id"], "cover everything")
    driver_id = request["agent_id"]
    ledger.brief_ack("driver-e1", driver_id)
    return {**ctx, "request_id": request["request_id"], "driver_id": driver_id}


_FINDING = {
    "fingerprint": "contrast:login-button:2.1",
    "title": "Login button fails contrast",
    "steps": "Open the login screen.",
    "expected": "4.5:1 contrast",
    "actual": "2.1:1 contrast",
    "severity": "major",
    "area": "login screen",
    "evidence": ["knowledge/cartographer/runs/run-1/screenshot.png"],
}


def test_drive_issue_records_a_finding_and_wakes_the_oracle(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_issue("driver-e1", ctx["driver_id"], ctx["request_id"], _FINDING)

    assert result["fingerprint"] == _FINDING["fingerprint"]
    assert json.loads(result["evidence_json"]) == _FINDING["evidence"]
    assert result["next"] is not None
    assert result["loop_status"]["explorations_done"] == 0


@pytest.mark.parametrize(
    "broken, message",
    [
        ({"title": "x", "severity": "major"}, "fingerprint and a title"),
        ({"fingerprint": "x", "severity": "major"}, "fingerprint and a title"),
        ({"fingerprint": "x", "title": "x", "severity": "urgent"}, "severity must be one of"),
    ],
)
def test_drive_issue_validates_the_finding_shape(
    ledger: Ledger, claude: FakeClaude, broken: dict, message: str
) -> None:
    ctx = _open_request(ledger, claude)
    with pytest.raises(LedgerError, match=message):
        ledger.drive_issue("driver-e1", ctx["driver_id"], ctx["request_id"], broken)


def test_drive_issue_refuses_a_non_owner(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    with pytest.raises(LedgerError):
        ledger.drive_issue("oracle", ctx["oracle_id"], ctx["request_id"], _FINDING)


def test_drive_checkin_records_progress_and_wakes_the_oracle(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_checkin(
        "driver-e1",
        ctx["driver_id"],
        ctx["request_id"],
        "3 of 8 screens",
        "tapped every button",
        "on track",
    )
    assert result["last_covered"] == "3 of 8 screens"
    assert result["last_checkin_at"] is not None
    assert result["next"] is not None


def _driver_row(ledger: Ledger, driver_id: str) -> sqlite3.Row:
    return ledger.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (driver_id,)).fetchone()


def _age_wakeup(ledger: Ledger, driver_id: str, age: timedelta) -> None:
    stamp = (utcnow() - age).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE wakeups SET created_at = ? WHERE from_agent_id = ? AND sent_at IS NULL",
            (stamp, driver_id),
        )


def _stopped(claude: FakeClaude, driver_id: str) -> bool:
    return ["stop", driver_id[:8]] in [c[0] for c in claude.calls]


def _owed_to_oracle(ledger: Ledger, driver_id: str, reason: str) -> dict:
    [wakeup] = [w for w in ledger.owed_wakeups(driver_id) if w["reason"] == reason]
    return wakeup


def _send(ledger: Ledger, agent_id: str, to: str) -> None:
    events.handle_post_any(
        ledger, {"agent_id": agent_id, "tool_name": "SendMessage", "tool_input": {"to": to}}
    )


def test_drive_done_owes_the_oracle_a_wake_up_and_returns_the_send_message(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    assert result["state"] == "done"
    assert result["done_at"] is not None
    wakeup = _owed_to_oracle(ledger, ctx["driver_id"], "drive_done")
    assert wakeup["to_agent_id"] == ctx["oracle_id"]
    assert wakeup["pointer"] == (
        f"Driver exploration 1 (request {ctx['request_id']}) ended: clean, no findings. "
        "Its result is in the ledger."
    )
    assert result["next"] == (
        f"SendMessage(to={json.dumps(wakeup['to_session_name'])}, "
        f"message={json.dumps(wakeup['pointer'])})"
    )


def test_the_driver_stays_live_until_its_wake_up_is_sent_then_is_released(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    wakeup = _owed_to_oracle(ledger, ctx["driver_id"], "drive_done")

    assert _driver_row(ledger, ctx["driver_id"])["ended_at"] is None
    assert not _stopped(claude, ctx["driver_id"])

    _send(ledger, ctx["driver_id"], wakeup["to_session_name"])

    row = _driver_row(ledger, ctx["driver_id"])
    assert row["state"] == "released"
    assert row["ended_at"] is not None
    assert _stopped(claude, ctx["driver_id"])


def test_a_send_message_elsewhere_does_not_release_a_driver_that_still_owes_the_oracle(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    _send(ledger, ctx["driver_id"], "some-other-session")

    assert _driver_row(ledger, ctx["driver_id"])["ended_at"] is None


def test_the_stop_hook_blocks_a_closed_driver_until_its_wake_up_is_sent(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    blocked = events.handle_stop(ledger, {"session_id": ctx["driver_id"]})
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert result["next"] in blocked["reason"]
    assert _driver_row(ledger, ctx["driver_id"])["ended_at"] is None

    wakeup = _owed_to_oracle(ledger, ctx["driver_id"], "drive_done")
    _send(ledger, ctx["driver_id"], wakeup["to_session_name"])
    assert events.handle_stop(ledger, {"session_id": ctx["driver_id"]}) is None


def test_the_stop_hook_releases_a_closed_driver_whose_wake_up_was_delivered(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE wakeups SET sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE from_agent_id = ?",
            (ctx["driver_id"],),
        )

    assert events.handle_stop(ledger, {"session_id": ctx["driver_id"]}) is None
    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"
    assert _stopped(claude, ctx["driver_id"])


def test_a_driver_that_resumes_the_oracle_is_released_by_the_resume(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx["oracle_id"]]
    result = ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    assert result["next"] == 'agent_resume(target_name="oracle")'

    ledger.agent_resume("driver-e1", ctx["driver_id"], "oracle")

    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"


def test_drive_done_names_the_stop_rule_and_the_block_in_its_wake_up(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    for ordinal in (1, 2, 3):
        _seed_exploration(ledger, ctx["run_id"], ordinal, ["F1"])
    request = ledger.drive_request("oracle", ctx["oracle_id"], "recheck F1")
    ledger.brief_ack("driver-e4", request["agent_id"])
    ledger.drive_issue(
        "driver-e4", request["agent_id"], request["request_id"], {**_FINDING, "fingerprint": "F1"}
    )
    ledger.drive_done(
        "driver-e4", request["agent_id"], request["request_id"], blocked="the\nbuild failed"
    )

    pointer = _owed_to_oracle(ledger, request["agent_id"], "drive_done")["pointer"]
    assert pointer == (
        f"Driver exploration 4 (request {request['request_id']}) ended: blocked: the build "
        "failed; stopped by a stop rule: 3 explorations in a row fixed nothing (+1 more). "
        "Its result is in the ledger."
    )


def test_drive_done_counts_the_findings_of_an_exploration_that_did_not_stop(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_issue("driver-e1", ctx["driver_id"], ctx["request_id"], _FINDING)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    pointer = _owed_to_oracle(ledger, ctx["driver_id"], "drive_done")["pointer"]
    assert "ended: done, 1 finding(s)." in pointer


def test_drive_done_releases_at_once_when_no_oracle_can_be_woken(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = NULL WHERE agent_id = ?", (ctx["oracle_id"],)
        )

    result = ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    assert result["next"] is None
    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"
    assert _stopped(claude, ctx["driver_id"])


def test_drive_unavailable_by_the_driver_wakes_the_oracle_before_its_release(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_unavailable("driver-e1", ctx["driver_id"], "cartographer did not load")

    wakeup = _owed_to_oracle(ledger, ctx["driver_id"], "drive_unavailable")
    assert wakeup["pointer"] == (
        f"Driver unavailable: cartographer did not load. Directive {result['directive_id']} "
        "waits in the ledger, and the exploration is abandoned."
    )
    assert result["next"] is not None
    assert wakeup["pointer"] in result["next"]
    assert _driver_row(ledger, ctx["driver_id"])["ended_at"] is None
    blocked = events.handle_stop(ledger, {"session_id": ctx["driver_id"]})
    assert blocked is not None and result["next"] in blocked["reason"]

    _send(ledger, ctx["driver_id"], wakeup["to_session_name"])

    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"
    assert _stopped(claude, ctx["driver_id"])


def test_drive_unavailable_by_the_oracle_owes_nothing_and_releases_the_driver_at_once(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_unavailable("oracle", ctx["oracle_id"], "cartographer did not load")

    assert result["next"] is None
    assert ledger.owed_wakeups(ctx["oracle_id"]) == []
    assert ledger.owed_wakeups(ctx["driver_id"]) == []
    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"
    assert _stopped(claude, ctx["driver_id"])


def test_a_closed_driver_does_not_block_the_next_request_or_run_finish(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    second = ledger.drive_request("oracle", ctx["oracle_id"], "a full pass")
    assert second["ordinal"] == 2
    assert _driver_row(ledger, ctx["driver_id"])["state"] == "released"

    ledger.brief_ack("driver-e2", second["agent_id"])
    ledger.drive_done("driver-e2", second["agent_id"], second["request_id"])
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    assert ledger.run_finish("oracle", ctx["oracle_id"], "success")["state"] == "finished"
    assert _driver_row(ledger, second["agent_id"])["state"] == "released"


def test_drive_done_refuses_twice(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    with pytest.raises(LedgerError, match="already"):
        ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])


def test_a_second_exploration_gets_the_next_ordinal(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    second = ledger.drive_request("oracle", ctx["oracle_id"], "recheck the last wave")
    assert second["ordinal"] == 2


# -- Stop rules become directives, and gate run_finish --------------------------------------


def test_drive_done_files_a_directive_once_per_stop_reason(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    for ordinal in (1, 2, 3):
        _seed_exploration(ledger, ctx["run_id"], ordinal, ["F1"])
    request = ledger.drive_request("oracle", ctx["oracle_id"], "recheck F1")
    ledger.drive_issue(
        "driver-e4",
        request["agent_id"],
        request["request_id"],
        {**_FINDING, "fingerprint": "F1", "severity": "major"},
    )
    ledger.drive_done("driver-e4", request["agent_id"], request["request_id"])

    directives = ledger.conn.execute(
        "SELECT * FROM directives WHERE run_id = ? AND source = 'driver'", (ctx["run_id"],)
    ).fetchall()
    assert directives
    bodies = [d["body"] for d in directives]
    assert any("fix attempts in a row" in b for b in bodies)
    count_before = len(directives)

    # A repeat of the same reasons does not file a second directive for any of them.
    status = ledger._drive_loop_status(ctx["run_id"])
    with write_tx(ledger.conn) as conn:
        ledger._flag_stop_rules(conn, ctx["run_id"], status)
    count_after = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM directives WHERE run_id = ? AND source = 'driver'",
        (ctx["run_id"],),
    ).fetchone()["n"]
    assert count_after == count_before


def test_run_finish_driver_gate_skips_without_a_driver_available(
    tmp_path: Path, repo_root: Path
) -> None:
    root = tmp_path / "no-driver"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".claude" / "sentinel-swarm.local.md").write_text(
        _settings_text(repo_root), encoding="utf-8"
    )
    ledger = Ledger(root, db_path=root / ".sentinel-swarm" / "ledger.db")
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    ledger._block_run_finish_for_driver(started["run"]["run_id"])  # does not raise


def test_run_finish_driver_gate_refuses_without_any_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="no exploration has run yet"):
        ledger._block_run_finish_for_driver(ctx["run_id"])


def test_run_finish_driver_gate_refuses_without_a_clean_latest_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed_exploration(ledger, ctx["run_id"], 1, ["F1"])
    with pytest.raises(LedgerError, match="no exploration has ended clean"):
        ledger._block_run_finish_for_driver(ctx["run_id"])


def test_run_finish_driver_gate_passes_on_a_clean_latest_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed_exploration(ledger, ctx["run_id"], 1, ["F1"])
    _seed_exploration(ledger, ctx["run_id"], 2, [])
    ledger._block_run_finish_for_driver(ctx["run_id"])  # does not raise


def test_run_finish_driver_gate_passes_once_the_loop_stopped(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    for ordinal in (1, 2, 3, 4):
        _seed_exploration(ledger, ctx["run_id"], ordinal, ["F1"])
    ledger._block_run_finish_for_driver(ctx["run_id"])  # does not raise: the loop stopped


def test_run_finish_refuses_while_the_driver_gate_is_unmet(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    with pytest.raises(LedgerError, match="no exploration has run yet"):
        ledger.run_finish("oracle", ctx["oracle_id"], "success")


def test_run_finish_succeeds_once_the_driver_gate_is_met(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed_exploration(ledger, ctx["run_id"], 1, [])
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    result = ledger.run_finish("oracle", ctx["oracle_id"], "success")
    assert result["state"] == "finished"


# -- Hooks: pre_agent and pre_shell for the Driver -----------------------------------------------


def test_pre_agent_allows_only_cartographers_subagents_for_a_driver(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    for subagent in ("map-driver", "map-reviewer"):
        data = {"agent_id": ctx["driver_id"], "tool_input": {"subagent_type": subagent}}
        assert events.handle_pre_agent(ledger, data) is None

    denied = events.handle_pre_agent(
        ledger, {"agent_id": ctx["driver_id"], "tool_input": {"subagent_type": "general-purpose"}}
    )
    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "map-driver" in denied["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_shell_allows_only_the_build_command_for_a_driver(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ok = {"agent_id": ctx["driver_id"], "tool_input": {"command": "npm run build"}}
    assert events.handle_pre_shell(ledger, ok) is None

    denied = events.handle_pre_shell(
        ledger, {"agent_id": ctx["driver_id"], "tool_input": {"command": "pytest -q"}}
    )
    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "npm run build" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pytest" not in denied["hookSpecificOutput"]["permissionDecisionReason"]


# -- Watchdog: check-in staleness and crash detection --------------------------------------------


def test_watchdog_reports_an_overdue_driver_check_in(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    stale = utcnow() - (DRIVER_CHECKIN_INTERVAL + DRIVER_CHECKIN_GRACE + timedelta(minutes=1))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE drive_requests SET last_checkin_at = ? WHERE request_id = ?",
            (stale.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", ctx["request_id"]),
        )

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert any(f.kind == "driver_overdue" for f in findings)


def test_watchdog_does_not_report_a_driver_whose_check_ins_are_on_time(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_checkin("driver-e1", ctx["driver_id"], ctx["request_id"], "half done", "-", "-")

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert not any(f.kind == "driver_overdue" for f in findings)


def test_watchdog_reports_a_crashed_driver(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx["driver_id"]]

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert any(f.kind == "crashed" and f.agent_id == ctx["driver_id"] for f in findings)


def test_a_closed_driver_that_dies_before_its_wake_up_is_reported_crashed_once(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    ledger.agent_idle(ctx["driver_id"], "stop")
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx["driver_id"]]

    passes = [scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog) for _ in "ab"]
    for findings in passes:
        reported = record(ledger.conn, ctx["run_id"], findings, utcnow())
        driver_notices(ledger.conn, ctx["run_id"], reported, ())

    [finding] = [f for f in passes[-1] if f.agent_id == ctx["driver_id"]]
    assert finding.kind == "crashed"
    assert f"exploration {ctx['request_id']} ended (done)" in finding.detail
    assert f'agent_release(target_agent_id="{ctx["driver_id"]}")' in finding.next_step
    directives = ledger.conn.execute(
        "SELECT * FROM directives WHERE source = 'watchdog' AND body LIKE '%crashed%'"
    ).fetchall()
    assert len(directives) == 1
    crashed = ledger.conn.execute(
        "SELECT * FROM notifications WHERE message LIKE 'Driver crashed:%'"
    ).fetchall()
    assert len(crashed) == 1

    ledger.agent_release("oracle", ctx["oracle_id"], ctx["driver_id"])
    after = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert not any(f.agent_id == ctx["driver_id"] for f in after)


def _idle_closed_driver(ledger: Ledger, claude: FakeClaude) -> dict:
    # Simulates a Driver that ignores its Stop hook block twice: `stop_hook_active`
    # skips the owed-wake-up check, release_closed_driver refuses because the
    # wake-up is still unsent, and the stop falls through to agent_idle.
    ctx = _open_request(ledger, claude)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])
    stop = events.handle_stop(ledger, {"session_id": ctx["driver_id"], "stop_hook_active": True})
    assert stop is None
    assert _driver_row(ledger, ctx["driver_id"])["state"] == "idle"
    assert _driver_row(ledger, ctx["driver_id"])["ended_at"] is None
    return ctx


def test_watchdog_does_not_report_an_idle_closed_driver_before_the_window(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _idle_closed_driver(ledger, claude)

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert not any(f.agent_id == ctx["driver_id"] for f in findings)


def test_watchdog_reports_an_idle_closed_driver_that_never_sent_its_wake_up(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))

    [finding] = [
        f
        for f in scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
        if f.agent_id == ctx["driver_id"]
    ]
    assert finding.kind == "driver_unsent"
    assert f"exploration {ctx['request_id']} ended (done)" in finding.detail
    assert "owed oracle an unsent wake-up for 3 minutes" in finding.detail
    assert f'agent_release(target_agent_id="{ctx["driver_id"]}")' in finding.next_step


def test_driver_unsent_is_reported_once_across_ticks(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))

    settings = ledger.settings.watchdog
    first = record(
        ledger.conn, ctx["run_id"], scan(ledger.conn, claude.listing, utcnow(), settings), utcnow()
    )
    second = record(
        ledger.conn, ctx["run_id"], scan(ledger.conn, claude.listing, utcnow(), settings), utcnow()
    )

    assert [r["kind"] for r in first] == ["driver_unsent"]
    assert second == []
    directives = ledger.conn.execute(
        "SELECT * FROM directives WHERE source = 'watchdog' AND body LIKE '%driver_unsent%'"
    ).fetchall()
    assert len(directives) == 1


def test_driver_unsent_clears_once_the_wake_up_is_sent(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))
    assert any(
        f.kind == "driver_unsent"
        for f in scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    )

    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE wakeups SET sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE from_agent_id = ?",
            (ctx["driver_id"],),
        )

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert not any(f.agent_id == ctx["driver_id"] for f in findings)


def test_driver_unsent_clears_once_the_driver_is_released(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))
    assert any(
        f.kind == "driver_unsent"
        for f in scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    )

    ledger.agent_release("oracle", ctx["oracle_id"], ctx["driver_id"])

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    assert not any(f.agent_id == ctx["driver_id"] for f in findings)


def test_a_dead_session_with_an_unsent_wake_up_is_still_crashed_not_driver_unsent(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx["driver_id"]]

    findings = [
        f
        for f in scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
        if f.agent_id == ctx["driver_id"]
    ]
    assert [f.kind for f in findings] == ["crashed"]


def test_driver_unsent_records_no_user_notification(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _idle_closed_driver(ledger, claude)
    _age_wakeup(ledger, ctx["driver_id"], DRIVER_UNSENT_AFTER + timedelta(minutes=1))
    before = ledger.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    findings = scan(ledger.conn, claude.listing, utcnow(), ledger.settings.watchdog)
    reported = record(ledger.conn, ctx["run_id"], findings, utcnow())
    assert [r["kind"] for r in reported] == ["driver_unsent"]

    notices = driver_notices(ledger.conn, ctx["run_id"], reported, ())
    assert notices == []
    after = ledger.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert after == before


# -- The report's Explorations section -----------------------------------------------------------


def test_report_lists_each_exploration_and_its_findings(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_issue("driver-e1", ctx["driver_id"], ctx["request_id"], _FINDING)
    ledger.drive_done("driver-e1", ctx["driver_id"], ctx["request_id"])

    report = ledger.write_report(ctx["run_id"])["text"]
    section = report.split("## Explorations", 1)[1].split("## Open items", 1)[0]
    assert "Exploration 1 (request" in section
    assert _FINDING["title"] in section
    assert _FINDING["fingerprint"] in section
    assert "not seen again" in section


# -- Schema migration ----------------------------------------------------------------------------


def test_connect_adds_the_drive_tables_to_an_older_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL);"
        "INSERT INTO schema_version (id, version) VALUES (1, 1);"
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, state TEXT NOT NULL);"
        "INSERT INTO runs (run_id, state) VALUES (1, 'active');"
    )
    old.close()

    conn = connect(db_path)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"drive_requests", "drive_findings"} <= tables
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(drive_requests)")}
        assert {"ordinal", "focus", "agent_id", "state", "last_checkin_at"} <= columns
    finally:
        conn.close()
