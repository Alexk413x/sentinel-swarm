from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_ledger import graph as graph_module
from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import ROLES, LedgerError
from swarm_ledger.ledger import Ledger


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


@pytest.fixture
def tree(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="oracle-sess")
    oracle_id = started["oracle"]["agent_id"]
    phase = ledger.phase_add("oracle", oracle_id, "core")
    ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")
    manager = f"mgr-{phase['name']}"
    ledger.brief_create(
        "oracle", oracle_id, manager, "manager", "opus", "Own it.", phase_id=phase["phase_id"]
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    ledger.brief_ack(manager, "mgr-agent")
    module = ledger.module_add(manager, "mgr-agent", phase["phase_id"], "auth")
    lead = f"lead-p{phase['ordinal']}-auth"
    ledger.brief_create(
        manager, "mgr-agent", lead, "lead", "sonnet", "Own it.", module_id=module["module_id"]
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    ledger.brief_ack(lead, "lead-agent")
    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "lead": lead,
        "coder_prefix": f"coder-p{phase['ordinal']}-auth",
    }


def _finding(ledger: Ledger, run_id: int) -> int:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, focus, state) "
            "VALUES (?, 1, 'focus', 'open')",
            (run_id,),
        )
        found = conn.execute(
            "INSERT INTO drive_findings (request_id, run_id, fingerprint, title, steps, "
            "expected, actual, severity, area, evidence_json) "
            "VALUES (?, ?, 'login-crash', 'Login crashes', 'tap Login', 'home screen', "
            "'crash', 'major', 'auth', ?)",
            (cur.lastrowid, run_id, json.dumps(["shots/before.png", "logs/crash.txt"])),
        )
    assert found.lastrowid is not None
    return found.lastrowid


def _claim(ledger: Ledger, tree: dict, slug: str, path: str) -> dict:
    return ledger.claim_file(
        tree["lead"], "lead-agent", path, f"tests/test_{slug}.py", f"{tree['coder_prefix']}-{slug}"
    )


def _brief_coder(ledger: Ledger, tree: dict, slug: str, file_id: int, finding_ids: list[int]):
    return ledger.brief_create(
        tree["lead"],
        "lead-agent",
        f"{tree['coder_prefix']}-{slug}",
        "coder",
        "sonnet",
        "Fix it.",
        file_id=file_id,
        finding_ids=finding_ids,
    )


# -- 2. A fix Coder's brief -------------------------------------------------------------------


def test_a_fix_coder_brief_on_a_claim_older_than_its_finding_is_refused(
    tree: dict, ledger: Ledger
) -> None:
    claimed = _claim(ledger, tree, "login", "src/login.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE files SET claimed_at = '2000-01-01T00:00:00.000Z' WHERE file_id = ?",
            (claimed["file_id"],),
        )
    finding_id = _finding(ledger, tree["run_id"])

    with pytest.raises(LedgerError, match="release_file the path and claim_file it again"):
        _brief_coder(ledger, tree, "login", claimed["file_id"], [finding_id])

    ledger.release_file(tree["lead"], "lead-agent", "src/login.py")
    fresh = _claim(ledger, tree, "login", "src/login.py")
    brief = _brief_coder(ledger, tree, "login", fresh["file_id"], [finding_id])
    assert json.loads(brief["finding_ids_json"]) == [finding_id]


def test_brief_get_returns_the_evidence_of_each_finding(tree: dict, ledger: Ledger) -> None:
    finding_id = _finding(ledger, tree["run_id"])
    claimed = _claim(ledger, tree, "login", "src/login.py")
    _brief_coder(ledger, tree, "login", claimed["file_id"], [finding_id])

    brief = ledger.brief_get(f"{tree['coder_prefix']}-login", f"{tree['coder_prefix']}-login")

    assert brief["findings"] == [
        {
            "finding_id": finding_id,
            "fingerprint": "login-crash",
            "title": "Login crashes",
            "severity": "major",
            "area": "auth",
            "steps": "tap Login",
            "expected": "home screen",
            "actual": "crash",
            "evidence": ["shots/before.png", "logs/crash.txt"],
        }
    ]


# -- 3. Overrides tied to a run ---------------------------------------------------------------


def test_an_override_is_consumed_only_in_its_own_run(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    ledger.override_grant("oracle", tree["oracle_id"], "write", lead, "src/x.py", "urgent")

    assert ledger.override_consume(tree["run_id"] + 1, "write", lead, "src/x.py") is False
    assert ledger.override_consume(None, "write", lead, "src/x.py") is False
    assert ledger.override_consume(tree["run_id"], "write", lead, "src/x.py") is True


def test_the_write_gate_ignores_an_override_from_another_run(tree: dict, ledger: Ledger) -> None:
    ledger.override_grant("oracle", tree["oracle_id"], "write", tree["lead"], "src/x.py", "urgent")
    with write_tx(ledger.conn) as conn:
        earlier = conn.execute("INSERT INTO runs (prd, state) VALUES ('old', 'finished')")
        conn.execute("UPDATE overrides SET run_id = ?", (earlier.lastrowid,))
    data = {"session_id": "lead-agent", "tool_input": {"file_path": "src/x.py"}}

    denied = events.handle_pre_write(ledger, data)

    assert denied is not None
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- 4. A Driver only through drive_request ---------------------------------------------------


def test_brief_create_refuses_a_driver(tree: dict, ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="drive_request"):
        ledger.brief_create(
            "oracle", tree["oracle_id"], "driver-e1", "driver", "sonnet", "Explore.", finding_ids=[]
        )
    assert (
        ledger.conn.execute("SELECT 1 FROM briefs WHERE child_role = 'driver'").fetchone() is None
    )


# -- 7. Multi-file graph nodes through the Lead -----------------------------------------------


def _node(*anchors: str) -> list[dict]:
    return [{"id": "auth_flow", "kind": "module", "anchors": list(anchors)}]


def test_a_lead_upserts_a_node_that_spans_its_modules_files(
    tree: dict, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[dict]] = []

    def fake_upsert(repo_root: Path, nodes: list[dict]) -> dict:
        del repo_root
        calls.append(nodes)
        return {"ok": True}

    monkeypatch.setattr(graph_module, "graph_upsert", fake_upsert)
    _claim(ledger, tree, "login", "src/login.py")
    _claim(ledger, tree, "token", "src/token.py")

    nodes = _node("src/login.py#login", "src/token.py#issue", "tests/test_token.py")
    assert ledger.graph_upsert(tree["lead"], "lead-agent", nodes) == {"ok": True}
    assert calls == [nodes]

    with pytest.raises(LedgerError, match="its own module's files"):
        ledger.graph_upsert(tree["lead"], "lead-agent", _node("src/login.py", "src/other.py"))
    assert len(calls) == 1


def test_a_coder_still_upserts_only_its_own_file(
    tree: dict, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph_module, "graph_upsert", lambda repo_root, nodes: {"ok": True})
    login = _claim(ledger, tree, "login", "src/login.py")
    _claim(ledger, tree, "token", "src/token.py")
    _brief_coder(ledger, tree, "login", login["file_id"], [])
    name = f"{tree['coder_prefix']}-login"
    ledger.agent_register_start("coder-agent", "coder", parent_agent_id="lead-agent")
    ledger.brief_ack(name, "coder-agent")

    with pytest.raises(LedgerError, match="ask your Lead"):
        ledger.graph_upsert(name, "coder-agent", _node("src/login.py", "src/token.py"))


# -- 6. Graph search at each role's level -----------------------------------------------------


_LEVELS = {
    "oracle": "system",
    "manager": "component",
    "lead": "file",
    "coder": "symbol",
    "driver": "screen",
}


@pytest.mark.parametrize("role", ROLES)
def test_each_start_sequence_names_the_roles_graph_level(repo_root: Path, role: str) -> None:
    text = (repo_root / "templates" / "agents" / f"{role}.md").read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    start = next(s for s in sections if 'ToolSearch(query="select:' in s)
    assert f"graph at the {_LEVELS[role]} level" in " ".join(start.split())
