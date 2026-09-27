from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from swarm_ledger import notify, sessions
from swarm_ledger.db import connect, write_tx
from swarm_ledger.drive import compute_loop_status
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_ROLE_FILE = """---
name: swarm-{role}
description: The {role}.
model: sonnet
permissionMode: acceptEdits
tools: Read, SendMessage, Bash, Agent, mcp__swarm-ledger
---

You are a {role}.
"""

_FINDING = {
    "title": "Login button fails contrast",
    "steps": "Open the login screen.",
    "expected": "4.5:1 contrast",
    "actual": "2.1:1 contrast",
    "severity": "major",
    "area": "login",
    "evidence": ["knowledge/cartographer/runs/run-4/login.png"],
}


class FakeClaude:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.listing: list[dict] = []

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        del cwd
        self.calls.append(list(args))
        if args[:2] == ["agents", "--json"]:
            return json.dumps(self.listing)
        if args[0] == "stop":
            return ""
        name = args[args.index("--name") + 1]
        session_id = f"{len(self.calls):08x}-aaaa-bbbb-cccc-dddddddddddd"
        self.add(session_id, name)
        return f"backgrounded · {session_id[:8]} · {name}\n"

    def add(self, session_id: str, name: str) -> None:
        self.listing.append(
            {"pid": 1, "sessionId": session_id, "name": name, "id": session_id[:8]}
            | {"status": "busy", "state": "working"}
        )


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(sessions, "_run", fake)
    return fake


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (root / ".git").mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text("utf-8")
    settings = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    settings = settings.replace("build_command:\n", "build_command: npm run build\n")
    (root / ".claude" / "sentinel-swarm.local.md").write_text(settings, encoding="utf-8")
    for role in ("oracle", "manager", "lead", "coder", "driver"):
        (agents / f"swarm-{role}.md").write_text(_ROLE_FILE.format(role=role), "utf-8")
    records = root / ".sentinel-swarm"
    records.mkdir()
    (records / "server.json").write_text(
        json.dumps({"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}), "utf-8"
    )
    registry = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    plugins = ("cartographer@cartographer", "web-driver@accessibility-tools")
    registry.write_text(
        json.dumps({"plugins": {pid: [{"scope": "user"}] for pid in plugins}}), "utf-8"
    )
    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger, claude: FakeClaude) -> dict:
    claude.add("sess-oracle", "host-oracle")
    started = ledger.run_start(prd="Build X", session_id="sess-oracle")
    oracle_id = started["oracle"]["agent_id"]
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")
    return {
        "oracle_id": oracle_id,
        "phase_id": phase["phase_id"],
        "run_id": started["run"]["run_id"],
    }


def _seed(ledger: Ledger, run_id: int, ordinal: int, findings: list[tuple[str, str]]) -> None:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, focus, state, done_at) "
            "VALUES (?, ?, 'focus', 'done', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (run_id, ordinal),
        )
        for fp, area in findings:
            conn.execute(
                "INSERT INTO drive_findings (request_id, run_id, fingerprint, title, severity, "
                "area, evidence_json) VALUES (?, ?, ?, ?, 'major', ?, ?)",
                (
                    cur.lastrowid,
                    run_id,
                    fp,
                    f"title {fp}",
                    area,
                    json.dumps([f"{fp}-{ordinal}.png"]),
                ),
            )


def _explore(ledger: Ledger, ctx: dict, findings: list[dict], done: bool = True) -> dict:
    request = ledger.drive_request("oracle", ctx["oracle_id"], "recheck")
    name = f"driver-e{request['ordinal']}"
    ledger.brief_ack(name, request["agent_id"])
    ids = [
        ledger.drive_issue(name, request["agent_id"], request["request_id"], finding)["finding_id"]
        for finding in findings
    ]
    result = {"request": request, "name": name, "finding_ids": ids}
    if done:
        result["done"] = ledger.drive_done(name, request["agent_id"], request["request_id"])
    return result


def _brief_fix(ledger: Ledger, ctx: dict, name: str, finding_ids: list[int]) -> dict:
    return ledger.brief_create(
        "oracle", ctx["oracle_id"], name, "manager", "opus", "Fix it.", finding_ids=finding_ids
    )


def _notifications(ledger: Ledger) -> list[dict]:
    return [dict(r) for r in ledger.conn.execute("SELECT * FROM notifications ORDER BY 1")]


def _approve_phase(ledger: Ledger, phase_id: int) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (phase_id,))


# -- 1. A Driver whose servers fail to load -----------------------------------------------------


def test_a_driver_marks_itself_unavailable_and_the_run_skips_explorations(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    explored = _explore(ledger, ctx, [], done=False)
    request = explored["request"]

    result = ledger.drive_unavailable(
        explored["name"], request["agent_id"], "cartographer's MCP server did not start"
    )

    assert result["driver_unavailable_reason"] == "cartographer's MCP server did not start"
    assert result["abandoned"] == [request["request_id"]]
    row = ledger.conn.execute(
        "SELECT state FROM drive_requests WHERE request_id = ?", (request["request_id"],)
    ).fetchone()
    assert row["state"] == "abandoned"
    driver = ledger.conn.execute(
        "SELECT state FROM agents WHERE agent_id = ?", (request["agent_id"],)
    ).fetchone()
    assert driver["state"] == "released"
    [notice] = _notifications(ledger)
    assert notice["kind"] == "error"
    assert notice["message"].startswith("Driver unavailable: cartographer's MCP server")

    with pytest.raises(LedgerError, match="the Driver is unavailable for this run"):
        ledger.drive_request("oracle", ctx["oracle_id"], "again")
    _approve_phase(ledger, ctx["phase_id"])
    assert ledger.run_finish("oracle", ctx["oracle_id"], "success")["state"] == "finished"


def test_the_oracle_marks_the_driver_unavailable_before_any_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="no exploration has run yet"):
        ledger._block_run_finish_for_driver(ctx["run_id"])

    result = ledger.drive_unavailable("oracle", ctx["oracle_id"], "the driver plugin failed")

    assert result["abandoned"] == []
    ledger._block_run_finish_for_driver(ctx["run_id"])


def test_drive_unavailable_needs_a_reason_and_the_oracle_or_the_driver(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="needs a reason"):
        ledger.drive_unavailable("oracle", ctx["oracle_id"], "  ")
    ledger.brief_create("oracle", ctx["oracle_id"], "mgr-1", "manager", "opus", "Own it.")
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack("mgr-1", "mgr-agent")
    with pytest.raises(LedgerError):
        ledger.drive_unavailable("mgr-1", "mgr-agent", "not mine to say")


# -- 2. Every dimension scored every time -------------------------------------------------------


def test_score_record_refuses_a_partial_score_set(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.brief_create(
        "oracle", ctx["oracle_id"], "mgr-1", "manager", "opus", "Own it.", phase_id=ctx["phase_id"]
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack("mgr-1", "mgr-agent")
    module = ledger.module_add("mgr-1", "mgr-agent", ctx["phase_id"], "mod")
    ledger.brief_create(
        "mgr-1", "mgr-agent", "lead-1", "lead", "sonnet", "Own it.", module_id=module["module_id"]
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    ledger.brief_ack("lead-1", "lead-agent")
    claimed = ledger.claim_file("lead-1", "lead-agent", "pkg/a.py", "tests/test_a.py", "coder-1")
    ledger.brief_create(
        "lead-1",
        "lead-agent",
        "coder-1",
        "coder",
        "sonnet",
        "Write it.",
        file_id=claimed["file_id"],
    )
    ledger.agent_register_start("coder-agent", "coder", parent_agent_id="lead-agent")
    ledger.brief_ack("coder-1", "coder-agent")

    ratings = [
        {"dimension": "meets_the_brief", "criterion": key, "value": 10}
        for key in ("does_what_was_asked", "nothing_extra")
    ]
    with pytest.raises(LedgerError, match="every dimension is scored on every review"):
        ledger.score_record(
            "coder-1", "coder-agent", claimed["file_id"], ratings, {"meets_the_brief": None}, "self"
        )
    with pytest.raises(LedgerError, match="every dimension is scored on every review"):
        ledger.score_record("coder-1", "coder-agent", claimed["file_id"], [], {}, "self")


# -- 3. Releasing a stuck Driver abandons its exploration ---------------------------------------


def test_releasing_a_stuck_driver_abandons_its_exploration(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    explored = _explore(ledger, ctx, [], done=False)
    request = explored["request"]

    ledger.agent_release("oracle", ctx["oracle_id"], request["agent_id"])

    row = ledger.conn.execute(
        "SELECT state, done_at FROM drive_requests WHERE request_id = ?", (request["request_id"],)
    ).fetchone()
    assert row["state"] == "abandoned"
    assert row["done_at"] is not None
    assert ["stop", request["agent_id"][:8]] in claude.calls
    second = ledger.drive_request("oracle", ctx["oracle_id"], "try again")
    assert second["ordinal"] == 2


def test_an_abandoned_exploration_is_never_clean() -> None:
    requests = [
        {"request_id": 1, "ordinal": 1, "state": "done"},
        {"request_id": 2, "ordinal": 2, "state": "abandoned"},
    ]
    status = compute_loop_status(requests, [])
    assert status["clean_latest"] is False
    assert status["explorations_done"] == 1


def test_run_finish_needs_a_clean_exploration_after_an_abandoned_one(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed(ledger, ctx["run_id"], 1, [])
    explored = _explore(ledger, ctx, [], done=False)
    ledger.agent_release("oracle", ctx["oracle_id"], explored["request"]["agent_id"])
    with pytest.raises(LedgerError, match="no exploration has ended clean"):
        ledger._block_run_finish_for_driver(ctx["run_id"])


# -- 4. A pattern pauses fixes in its area ------------------------------------------------------


def test_a_regression_pauses_fixes_in_its_area_until_the_directive_is_resolved(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed(ledger, ctx["run_id"], 1, [("F1", "login")])
    _seed(ledger, ctx["run_id"], 2, [("G1", "settings")])
    explored = _explore(
        ledger,
        ctx,
        [
            {**_FINDING, "fingerprint": "F1"},
            {**_FINDING, "fingerprint": "F2", "title": "Login label clipped"},
            {**_FINDING, "fingerprint": "G2", "area": "profile", "title": "Avatar missing"},
        ],
    )
    f1, f2, g2 = explored["finding_ids"]
    stop = ledger.conn.execute(
        "SELECT * FROM drive_stops WHERE kind = 'pattern' AND fingerprint = 'F1'"
    ).fetchone()
    assert stop["area"] == "login"

    with pytest.raises(LedgerError, match="fixes in area login are paused"):
        _brief_fix(ledger, ctx, "mgr-f1", [f1])
    with pytest.raises(LedgerError, match="fixes in area login are paused"):
        _brief_fix(ledger, ctx, "mgr-f2", [f2])
    assert _brief_fix(ledger, ctx, "mgr-g2", [g2])["finding_ids_json"] == json.dumps([g2])

    for directive in ledger.directive_inbox("oracle", ctx["oracle_id"]):
        with pytest.raises(LedgerError, match="paused"):
            _brief_fix(ledger, ctx, "mgr-f2", [f2])
        ledger.directive_resolve(
            "oracle", ctx["oracle_id"], directive["directive_id"], "applied", "ok"
        )
    assert _brief_fix(ledger, ctx, "mgr-f2", [f2])


def test_a_pattern_stops_a_briefed_fix_at_spawn_and_through_inheritance(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _seed(ledger, ctx["run_id"], 1, [("F1", "login")])
    _seed(ledger, ctx["run_id"], 2, [("G1", "settings")])
    explored = _explore(ledger, ctx, [{**_FINDING, "fingerprint": "F1"}], done=False)
    [f1] = explored["finding_ids"]
    _brief_fix(ledger, ctx, "mgr-f1", [f1])
    ledger.drive_done(
        explored["name"], explored["request"]["agent_id"], explored["request"]["request_id"]
    )

    with pytest.raises(LedgerError, match="paused"):
        ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-f1")

    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack("mgr-f1", "mgr-agent")
    with pytest.raises(LedgerError, match="paused"):
        ledger.brief_create("mgr-f1", "mgr-agent", "lead-f1", "lead", "sonnet", "Fix it.")


# -- 5. A stopped bug gets no more fixes, and its evidence reaches the user ---------------------


def _three_in_a_row(ledger: Ledger, ctx: dict) -> dict:
    for ordinal in (1, 2, 3):
        _seed(ledger, ctx["run_id"], ordinal, [("F1", "login"), (f"G{ordinal}", f"a{ordinal}")])
    return _explore(
        ledger,
        ctx,
        [
            {**_FINDING, "fingerprint": "F1"},
            {**_FINDING, "fingerprint": "G4", "area": "a4", "title": "title G4"},
        ],
    )


def test_a_stopped_finding_gets_no_more_fixes_even_after_its_directive_resolves(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    f1, g4 = _three_in_a_row(ledger, ctx)["finding_ids"]

    stop = ledger.conn.execute("SELECT * FROM drive_stops WHERE kind = 'finding'").fetchone()
    assert stop["fingerprint"] == "F1"
    ledger.directive_resolve("oracle", ctx["oracle_id"], stop["directive_id"], "applied", "told")
    with pytest.raises(LedgerError, match="gets no more fixes"):
        _brief_fix(ledger, ctx, "mgr-f1", [f1])
    assert _brief_fix(ledger, ctx, "mgr-g4", [g4])


def test_a_stop_carries_the_evidence_in_its_directive_and_notification(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _three_in_a_row(ledger, ctx)

    [directive] = ledger.conn.execute("SELECT * FROM directives WHERE source = 'driver'").fetchall()
    body = directive["body"]
    assert body.startswith("[driver-stop] finding F1: 3 fix attempts in a row")
    assert "Finding: Login button fails contrast (F1), area login" in body
    assert "Fix attempts: 3 in a row, 3 in all" in body
    assert "Seen in explorations: 1, 2, 3, 4" in body
    assert "knowledge/cartographer/runs/run-4/login.png" in body
    [notice] = _notifications(ledger)
    assert notice["message"] == (
        "Driver stopped: 3 attempts in a row with no progress on Login button fails contrast "
        "[F1], seen in explorations 1, 2, 3, 4"
    )


def test_a_finding_that_reaches_a_stop_mid_exploration_gets_no_fix(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    for ordinal in (1, 2, 3):
        _seed(ledger, ctx["run_id"], ordinal, [("F1", "login")])
    explored = _explore(ledger, ctx, [{**_FINDING, "fingerprint": "F1"}], done=False)
    with pytest.raises(LedgerError, match="reached a stop rule"):
        _brief_fix(ledger, ctx, "mgr-f1", explored["finding_ids"])


def test_brief_create_refuses_a_finding_of_another_run(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="not a Driver finding of this run"):
        _brief_fix(ledger, ctx, "mgr-x", [999])


# -- 6. Three waves that fix nothing: the Oracle reports what is left ---------------------------


def test_a_stalled_loop_records_what_is_left_and_the_report_shows_it(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    for ordinal in (1, 2, 3):
        _seed(ledger, ctx["run_id"], ordinal, [("F1", "login"), ("F9", "cart")])
    _explore(
        ledger,
        ctx,
        [
            {**_FINDING, "fingerprint": "F1"},
            {**_FINDING, "fingerprint": "F9", "area": "cart", "title": "Cart total wrong"},
        ],
    )

    stalled = ledger.conn.execute(
        "SELECT d.body FROM drive_stops s JOIN directives d ON d.directive_id = s.directive_id "
        "WHERE s.kind = 'stalled'"
    ).fetchone()
    assert "Left after exploration 4:" in stalled["body"]
    assert "- Login button fails contrast (F1), major, area login" in stalled["body"]
    assert "- Cart total wrong (F9), major, area cart" in stalled["body"]
    [notice] = _notifications(ledger)
    assert notice["message"].startswith(
        "Driver loop ended: 3 explorations in a row fixed nothing; 2 left: "
        "Login button fails contrast; Cart total wrong"
    )
    assert notify.owed(ledger.conn, ctx["run_id"])

    report = ledger.write_report(ctx["run_id"])["text"]
    explorations = report.split("## Explorations", 1)[1].split("## Open items", 1)[0]
    assert "### Stop rules" in explorations
    assert "  - Cart total wrong (F9), major, area cart" in explorations
    notifications = report.split("## Notifications to the user", 1)[1].split("## Final", 1)[0]
    assert notice["message"] in notifications


# -- 7. Role prompts match "fixes start while the Driver explores" ------------------------------


@pytest.mark.parametrize(
    ("role", "rule"),
    [
        ("driver", "Fixes start while you explore"),
        ("oracle", "Fixes start while the Driver explores"),
    ],
)
def test_the_role_prompts_let_fixes_run_while_the_driver_explores(
    repo_root: Path, role: str, rule: str
) -> None:
    text = (repo_root / "templates" / "agents" / f"{role}.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "while no Coder is editing" not in flat
    assert "never during yours" not in flat
    assert rule in flat


# -- 8. An issue that ends its last round below the floor notifies the user ----------------------


def _lead_setup(ledger: Ledger, claude: FakeClaude) -> dict:
    ctx = _bootstrap(ledger, claude)
    ledger.brief_create(
        "oracle", ctx["oracle_id"], "mgr-1", "manager", "opus", "Own it.", phase_id=ctx["phase_id"]
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack("mgr-1", "mgr-agent")
    module = ledger.module_add("mgr-1", "mgr-agent", ctx["phase_id"], "mod")
    ledger.brief_create(
        "mgr-1", "mgr-agent", "lead-1", "lead", "sonnet", "Own it.", module_id=module["module_id"]
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    ledger.brief_ack("lead-1", "lead-agent")
    claimed = ledger.claim_file("lead-1", "lead-agent", "pkg/a.py", "tests/test_a.py", "coder-1")
    return {**ctx, "file_id": claimed["file_id"]}


def _lead_review(ledger: Ledger, file_id: int, security: int) -> None:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO reviews (file_id, reviewer_agent_id, kind, outcome) "
            "VALUES (?, 'lead-agent', 'lead', 'fail')",
            (file_id,),
        )
        for dimension, _, criteria in DIMENSIONS:
            for criterion, _ in criteria:
                conn.execute(
                    "INSERT INTO scores (review_id, dimension, criterion, value) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        cur.lastrowid,
                        dimension,
                        criterion,
                        security if dimension == "security" else 10,
                    ),
                )


def _issue_at(ledger: Ledger, ctx: dict, round_: int, attempts: int) -> int:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO issues (run_id, file_id, opened_by_agent_id, title, state, round, "
            "attempts, dimension, criterion) VALUES (?, ?, 'lead-agent', 'Injection.', 'open', "
            "?, ?, 'security', 'injection')",
            (ctx["run_id"], ctx["file_id"], round_, attempts),
        )
        conn.execute(
            "INSERT INTO attempts (file_id, targeted_json, round) VALUES (?, '[\"security\"]', 9)",
            (ctx["file_id"],),
        )
        issue_id = cur.lastrowid
    assert issue_id is not None
    return issue_id


def test_an_issue_ending_round_three_below_the_floor_notifies_an_error(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ctx = _lead_setup(ledger, claude)
    _lead_review(ledger, ctx["file_id"], security=4)
    _lead_review(ledger, ctx["file_id"], security=4)
    issue_id = _issue_at(ledger, ctx, round_=3, attempts=2)

    result = ledger.attempt_record("lead-1", "lead-agent", ctx["file_id"])

    assert result["outcome"] == "plateau"
    [notice] = result["notifications"]
    assert notice["kind"] == "error"
    assert notice["event_key"] == f"issue:{issue_id}"
    assert notice["message"] == (
        f"Issue {issue_id} on pkg/a.py ended round 3 below the floor: Injection.; "
        "security 40 (floor 70), security.injection 4 (floor 5)"
    )
    assert notice["push_owed"] == 1
    assert len(os_notifications) == 1

    _issue_at(ledger, ctx, round_=3, attempts=5)
    ledger.conn.execute("UPDATE issues SET state = 'closed' WHERE issue_id != ?", (issue_id,))
    again = ledger.attempt_record("lead-1", "lead-agent", ctx["file_id"])
    assert again["notifications"] == []


@pytest.mark.parametrize(("round_", "security"), [(2, 4), (3, 10)])
def test_no_floor_notification_before_the_last_round_or_above_the_floor(
    ledger: Ledger, claude: FakeClaude, round_: int, security: int
) -> None:
    ctx = _lead_setup(ledger, claude)
    _lead_review(ledger, ctx["file_id"], security=security)
    _lead_review(ledger, ctx["file_id"], security=security)
    _issue_at(ledger, ctx, round_=round_, attempts=2)

    result = ledger.attempt_record("lead-1", "lead-agent", ctx["file_id"])

    assert result["notifications"] == []
    assert _notifications(ledger) == []


# -- Schema migration ---------------------------------------------------------------------------


def test_connect_adds_the_gap_columns_and_the_drive_stops_table(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL);"
        "INSERT INTO schema_version (id, version) VALUES (1, 1);"
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY, state TEXT NOT NULL);"
        "CREATE TABLE briefs (brief_id INTEGER PRIMARY KEY, child_name TEXT NOT NULL);"
        "INSERT INTO runs (run_id, state) VALUES (1, 'active');"
    )
    old.close()

    conn = connect(db_path)
    try:
        runs = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        assert {"driver_unavailable_at", "driver_unavailable_reason"} <= runs
        briefs = {r["name"] for r in conn.execute("PRAGMA table_info(briefs)")}
        assert "finding_ids_json" in briefs
        stops = {r["name"] for r in conn.execute("PRAGMA table_info(drive_stops)")}
        assert {"directive_id", "kind", "reason", "fingerprint", "area"} <= stops
    finally:
        conn.close()
