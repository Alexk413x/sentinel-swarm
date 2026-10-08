from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_ledger import auth, sessions
from swarm_ledger.agentfiles import session_options
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import ROLES, LedgerError
from swarm_ledger.ledger import Ledger, looks_like_swarm_session
from swarm_ledger.settings import load_settings

# -- fixtures shared by the plain (non-spawning) tests ---------------------------------------


pytestmark = pytest.mark.usefixtures("claude_sessions")


def _settings_text(repo_root: Path) -> str:
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    return text


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase_a = ledger.phase_add("oracle", oracle_id, "phase-a")
    phase_b = ledger.phase_add("oracle", oracle_id, "phase-b")
    ledger.phase_update("oracle", oracle_id, phase_a["phase_id"], "unlocked")
    ledger.phase_update("oracle", oracle_id, phase_b["phase_id"], "unlocked")

    ledger.brief_create(
        "oracle",
        oracle_id,
        "mgr-p1-phase-a",
        "manager",
        "opus",
        "Own phase-a.",
        phase_id=phase_a["phase_id"],
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-a", "mgr-agent")
    _as_session(ledger, oracle_id, "host-r1-oracle")
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    manager = dict(manager) | {"session_name": "host-r1-manager-1"}

    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_a": phase_a["phase_id"],
        "phase_b": phase_b["phase_id"],
        "manager": manager,
    }


# -- fixtures for the agent_spawn (session-spawning) tests ------------------------------------

_ROLE_FILE = """---
name: swarm-{role}
description: The {role}.
model: sonnet
permissionMode: acceptEdits
tools: Read, SendMessage, mcp__swarm-ledger
mcpServers:
  - codebase-kg:
      command: python
      args: [".sentinel-swarm/hook.py", "mcp", "codebase-kg@alexk413x", "codebase-kg"]
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


@pytest.fixture
def spawn_host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "spawn-host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    (claude_dir / "agents").mkdir(parents=True)
    (claude_dir / "sentinel-swarm.local.md").write_text(_settings_text(repo_root), encoding="utf-8")
    for role in ("oracle", "manager", "lead", "coder"):
        (claude_dir / "agents" / f"swarm-{role}.md").write_text(
            _ROLE_FILE.format(role=role), encoding="utf-8"
        )
    records = root / ".sentinel-swarm"
    records.mkdir()
    (records / "server.json").write_text(
        json.dumps({"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}), encoding="utf-8"
    )
    auth.ensure_token(root)
    return root


@pytest.fixture
def spawn_ledger(spawn_host: Path) -> Ledger:
    return Ledger(spawn_host, db_path=spawn_host / ".sentinel-swarm" / "ledger.db")


def _spawn_bootstrap(
    ledger: Ledger, claude: FakeClaude, oracle_session: str = "sess-oracle"
) -> dict:
    claude.add(oracle_session, "spawn-host-oracle")
    started = ledger.run_start(prd="Build X", session_id=oracle_session)
    oracle_id = started["oracle"]["agent_id"]
    ledger.repo_check("oracle", oracle_id)
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle",
        oracle_id,
        "mgr-p1-phase-1",
        "manager",
        "opus",
        "Own it.",
        phase_id=phase["phase_id"],
    )
    return {
        "oracle_id": oracle_id,
        "phase_id": phase["phase_id"],
        "run_id": started["run"]["run_id"],
    }


# == Item 9: scoped pause ======================================================================


def test_run_pause_with_phases_pauses_only_those_phases(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    result = ledger.run_pause(
        "oracle", ctx["oracle_id"], "phase-a is blocked", phases=[ctx["phase_a"]]
    )
    assert result["state"] == "active"
    assert [p["phase_id"] for p in result["paused_phases"]] == [ctx["phase_a"]]

    status = ledger.run_status("oracle", ctx["oracle_id"])
    phase_a = next(p for p in status["phases"] if p["phase_id"] == ctx["phase_a"])
    phase_b = next(p for p in status["phases"] if p["phase_id"] == ctx["phase_b"])
    assert phase_a["paused_at"] is not None
    assert phase_a["pause_reason"] == "phase-a is blocked"
    assert phase_b["paused_at"] is None


def test_run_pause_with_phases_refuses_an_unknown_phase_id(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="unknown phase_id"):
        ledger.run_pause("oracle", ctx["oracle_id"], "blocked", phases=[9999])


def test_phase_resume_clears_the_pause(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.run_pause("oracle", ctx["oracle_id"], "blocked", phases=[ctx["phase_a"]])
    resumed = ledger.phase_resume("oracle", ctx["oracle_id"], [ctx["phase_a"]])
    assert resumed["resumed_phases"][0]["paused_at"] is None


def test_plan_unlocked_excludes_a_paused_phase(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    before = {p["phase_id"] for p in ledger.plan_unlocked("oracle", ctx["oracle_id"])}
    assert before == {ctx["phase_a"], ctx["phase_b"]}

    ledger.run_pause("oracle", ctx["oracle_id"], "blocked", phases=[ctx["phase_a"]])
    after = {p["phase_id"] for p in ledger.plan_unlocked("oracle", ctx["oracle_id"])}
    assert after == {ctx["phase_b"]}


def test_agent_spawn_refuses_into_a_paused_phase(claude: FakeClaude, spawn_ledger: Ledger) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    spawn_ledger.run_pause("oracle", ctx["oracle_id"], "blocked", phases=[ctx["phase_id"]])
    with pytest.raises(LedgerError, match="is paused"):
        spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")


def test_agent_spawn_succeeds_once_the_phase_is_resumed(
    claude: FakeClaude, spawn_ledger: Ledger
) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    spawn_ledger.run_pause("oracle", ctx["oracle_id"], "blocked", phases=[ctx["phase_id"]])
    spawn_ledger.phase_resume("oracle", ctx["oracle_id"], [ctx["phase_id"]])
    spawned = spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")
    assert spawned["state"] == "registered"


def test_oracle_stop_hook_does_not_cite_a_paused_phase(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]
    ledger.watch_armed(1_800_000)
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")

    # Unpaused, the unlocked phase is pending work with nobody assigned: the hook blocks.
    blocked = events.handle_stop(ledger, {"session_id": oracle_id})
    assert blocked is not None
    assert blocked["decision"] == "block"

    ledger.run_pause("oracle", oracle_id, "blocked", phases=[phase["phase_id"]])
    result = events.handle_stop(ledger, {"session_id": oracle_id})
    assert result is None


# == Item 11: parallelism_cap counts other swarms ==============================================


def test_looks_like_swarm_session_matches_the_naming_shape() -> None:
    assert looks_like_swarm_session("other-repo-r7-coder-a") is True
    assert looks_like_swarm_session("my-host-r1-lead-1") is True
    assert looks_like_swarm_session("plain-name") is False
    assert looks_like_swarm_session("no-run-marker-name") is False


def test_agent_spawn_cap_counts_other_swarms_on_the_machine(
    claude: FakeClaude, spawn_ledger: Ledger
) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    # A session from an unrelated repo's run, still running on this machine.
    claude.add("other-session-id", "other-repo-r9-coder-a")
    spawn_ledger.settings.parallelism_cap = 1
    with pytest.raises(LedgerError, match="parallelism cap of 1 is reached"):
        spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")


def test_agent_spawn_cap_ignores_a_non_swarm_session_name(
    claude: FakeClaude, spawn_ledger: Ledger
) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    claude.add("other-session-id", "some-developers-terminal")
    spawn_ledger.settings.parallelism_cap = 2
    spawned = spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")
    assert spawned["state"] == "registered"


def test_agent_spawn_applies_a_per_role_cap(claude: FakeClaude, spawn_ledger: Ledger) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    spawn_ledger.settings.role_parallelism_cap = {"manager": 1}
    spawned = spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")
    manager = ("mgr-p1-phase-1", spawned["agent_id"])
    spawn_ledger.brief_ack(*manager)

    second = spawn_ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    spawn_ledger.phase_update("oracle", ctx["oracle_id"], second["phase_id"], "unlocked")
    spawn_ledger.brief_create(
        "oracle",
        ctx["oracle_id"],
        "mgr-p2-phase-2",
        "manager",
        "opus",
        "Own it too.",
        phase_id=second["phase_id"],
    )
    with pytest.raises(LedgerError, match="the manager parallelism cap of 1 is reached"):
        spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p2-phase-2")


# == Item 16: per-role settings ===================================================================


def test_load_settings_parses_the_new_per_role_keys(repo_root: Path, tmp_path: Path) -> None:
    root = tmp_path / "host"
    (root / ".claude").mkdir(parents=True)
    text = _settings_text(repo_root)
    text = text.replace("effort:\n  oracle: medium\n", "effort:\n  oracle: high\n")
    text = text.replace("  lead: medium\n", "  lead:\n")
    text = text.replace("prompt_cache_ttl:\n  oracle:\n", "prompt_cache_ttl:\n  oracle: 1h\n")
    text = text.replace(
        "role_parallelism_cap:\n  oracle:\n", "role_parallelism_cap:\n  oracle: 2\n"
    )
    (root / ".claude" / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")

    settings = load_settings(root)
    assert settings.effort == {
        "oracle": "high",
        "manager": "medium",
        "coder": "medium",
        "driver": "medium",
    }
    assert settings.prompt_cache_ttl == {"oracle": "1h", "coder": "5m"}
    assert settings.role_parallelism_cap == {"oracle": 2}


def test_load_settings_defaults_every_role_to_medium_effort(tmp_path: Path) -> None:
    root = tmp_path / "host"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "sentinel-swarm.local.md").write_text("---\ntracking: local\n---\n")

    assert load_settings(root).effort == {role: "medium" for role in ROLES}


def test_session_options_passes_effort_and_prompt_cache_ttl(spawn_host: Path) -> None:
    options = session_options(
        spawn_host,
        "lead",
        "sonnet",
        "http://127.0.0.1:4321/mcp",
        effort="high",
        prompt_cache_ttl="1h",
    )
    assert "--effort" in options
    assert options[options.index("--effort") + 1] == "high"
    settings_json = options[options.index("--settings") + 1]
    assert json.loads(settings_json)["promptCacheTtl"] == "1h"


def test_session_options_omits_effort_and_keeps_the_settings_literal_by_default(
    spawn_host: Path,
) -> None:
    options = session_options(spawn_host, "lead", "sonnet", "http://127.0.0.1:4321/mcp")
    assert "--effort" not in options
    settings_json = options[options.index("--settings") + 1]
    assert settings_json == '{"worktree":{"bgIsolation":"none"}}'


def test_agent_spawn_passes_the_role_s_effort_and_cache_ttl(
    claude: FakeClaude, spawn_ledger: Ledger
) -> None:
    ctx = _spawn_bootstrap(spawn_ledger, claude)
    spawn_ledger.settings.effort = {"manager": "xhigh"}
    spawn_ledger.settings.prompt_cache_ttl = {"manager": "1h"}
    spawn_ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-phase-1")

    spawn_calls = [args for args, _ in claude.calls if "--bg" in args]
    options = spawn_calls[-1]
    assert options[options.index("--effort") + 1] == "xhigh"
    settings_json = options[options.index("--settings") + 1]
    assert json.loads(settings_json)["promptCacheTtl"] == "1h"
