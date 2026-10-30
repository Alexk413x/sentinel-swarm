from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from swarm_ledger import auth
from swarm_ledger.db import NOW, write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import ROLES
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "sentinel-swarm.local.md.example"

SERVER_INFO = {"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}
SWARM_ROLES = ("oracle", "manager", "lead", "coder")


def settings_text(*, swaps: Iterable[tuple[str, str]] = (), **commands: str) -> str:
    text = _TEMPLATE.read_text(encoding="utf-8")
    for key, value in commands.items():
        text = text.replace(f"{key}:\n", f"{key}: {value}\n")
    for old, new in swaps:
        text = text.replace(old, new)
    return text


def write_settings(root: Path, text: str) -> None:
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")


def make_host(root: Path, settings: str | None = None) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    write_settings(root, settings if settings is not None else settings_text())
    return root


def write_server_info(root: Path, info: dict | None = None) -> None:
    records = root / ".sentinel-swarm"
    records.mkdir(parents=True, exist_ok=True)
    (records / "server.json").write_text(json.dumps(info or SERVER_INFO), encoding="utf-8")


def enable_plugins(plugin_ids: Iterable[str], settings_path: Path, enabled: bool = True) -> None:
    """Set each id in the `enabledPlugins` of a settings file, keeping its other keys."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("enabledPlugins", {}).update({plugin_id: enabled for plugin_id in plugin_ids})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data), encoding="utf-8")


def write_registry(
    plugins: dict[str, list[dict]], config_dir: Path | None = None, enable: bool = True
) -> None:
    """Write the install registry and, unless `enable` is false, enable each id for the user."""
    config = config_dir if config_dir is not None else Path(os.environ["CLAUDE_CONFIG_DIR"])
    path = config / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    if enable:
        enable_plugins(plugins, config / "settings.json")


def register_plugins(plugin_ids: Iterable[str]) -> None:
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    plugins = {}
    for plugin_id in plugin_ids:
        install = config / "plugins" / "cache" / plugin_id.replace("@", "-")
        install.mkdir(parents=True, exist_ok=True)
        plugins[plugin_id] = [{"scope": "user", "installPath": str(install)}]
    write_registry(plugins)


def role_template(*, tools: str, codebase_kg: bool = True) -> str:
    text = f"---\nname: swarm-{{role}}\ndescription: The {{role}}.\nmodel: sonnet\ntools: {tools}\n"
    if codebase_kg:
        text += (
            "mcpServers:\n"
            "  - codebase-kg:\n"
            "      command: python\n"
            '      args: [".sentinel-swarm/hook.py", "mcp", "codebase-kg@alexk413x", '
            '"codebase-kg"]\n'
        )
    return text + "---\n\nYou are a {role}.\n"


ROLE_FILE = role_template(tools="Read, SendMessage, mcp__swarm-ledger")


def make_swarm_host(
    root: Path,
    settings: str,
    role_file: str,
    *,
    roles: Iterable[str] = SWARM_ROLES,
    plugins: Iterable[str] | None = None,
) -> Path:
    make_host(root, settings)
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for role in roles:
        (agents / f"swarm-{role}.md").write_text(role_file.format(role=role), encoding="utf-8")
    write_server_info(root)
    auth.ensure_token(root)
    if plugins is not None:
        register_plugins(plugins)
    return root


def ledger_for(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


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
        return self.spawn(args)

    def spawn(self, args: list[str], *, register: bool = True) -> str:
        name = args[args.index("--name") + 1]
        self._count += 1
        session_id = f"{self._count:08x}-aaaa-bbbb-cccc-dddddddddddd"
        if register:
            self.add(session_id, name, bg_id=session_id[:8])
        return f"backgrounded · {session_id[:8]} · {name}\n"

    def add(self, session_id: str, name: str, bg_id: str | None = None) -> dict:
        entry = {"pid": 100 + len(self.listing), "sessionId": session_id, "name": name}
        entry |= {"status": "busy", "state": "working"}
        if bg_id:
            entry["id"] = bg_id
        self.listing.append(entry)
        return entry


def register_agent(
    ledger: Ledger, agent_id: str, agent_type: str, parent_agent_id: str | None = None
) -> dict:
    with write_tx(ledger.conn) as conn:
        existing = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if existing is not None:
            return dict(existing)

        role = agent_type if agent_type in ROLES else "unknown"
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, parent_agent_id, state, started_at) "
            f"VALUES (?, ?, ?, ?, 'registered', {NOW})",
            (agent_id, agent_id, role, parent_agent_id),
        )
        ledger._log_event(conn, agent_id, None, "registered", "register_agent")

    return ledger._agent_dict(agent_id)


def as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def running(listing: list[dict], agent_id: str, session_name: str) -> None:
    listing.append({"pid": 7, "sessionId": agent_id, "name": session_name, "status": "idle"})


def go_idle(ledger: Ledger, agent_id: str) -> None:
    events.handle_stop(ledger, {"session_id": agent_id, "stop_hook_active": True})


def untimed(text: str) -> str:
    assert re.search(r" elapsed \d+s(?: / \d+s)?", text), text
    return re.sub(r" elapsed \d+s(?: / \d+s)?", "", text)


def insert_passing_test_run(ledger: Ledger, run_id: int, agent_id: str, scope: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, ?, 'x', 'x', 0, 1, 0, 0, '')",
            (run_id, agent_id, scope),
        )


def review_scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10},
        {"dimension": "integration", "value": 10},
        {"dimension": "open_items", "value": 10},
    ]


def all_applicable() -> dict[str, str | None]:
    return {dim_key: None for dim_key, _, _ in DIMENSIONS}


def all_ratings(
    value: int = 10, reason: str = "needs work", overrides: dict[str, int] | None = None
) -> list[dict]:
    overrides = overrides or {}
    ratings = []
    for dim_key, _, criteria in DIMENSIONS:
        dim_value = overrides.get(dim_key, value)
        for criterion_key, _ in criteria:
            ratings.append(
                {
                    "dimension": dim_key,
                    "criterion": criterion_key,
                    "value": dim_value,
                    "reason": reason if dim_value < 9 else None,
                    "ref": "pkg/good.py:1" if dim_value < 9 else None,
                }
            )
    return ratings


def file_id_for(ledger: Ledger, path: str) -> int:
    return ledger.who_owns(path)["file"]["file_id"]


def write_module(host: Path, name: str, symbol: str) -> None:
    (host / "pkg" / f"{name}.py").write_text(
        f"def {symbol}(a, b):\n    return a + b\n", encoding="utf-8", newline="\n"
    )
    (host / "tests" / f"test_{name}.py").write_text(
        f"from pkg.{name} import {symbol}\n\n\n"
        f"def test_{symbol}():\n    assert {symbol}(1, 2) == 3\n",
        encoding="utf-8",
        newline="\n",
    )


def touch_module(host: Path, name: str, marker: str) -> None:
    path = host / "pkg" / f"{name}.py"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n# {marker}\n", encoding="utf-8", newline="\n"
    )


def spawn_coder(
    ledger: Ledger, ctx: dict, coder_name: str, path: str, test_path: str | None
) -> dict:
    claimed = ledger.claim_file(
        "lead-p1-module-1", ctx["lead"]["agent_id"], path, test_path, coder_name
    )
    ledger.brief_create(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        coder_name,
        "coder",
        "sonnet",
        "Implement it.",
        module_id=ctx["module_id"],
        file_id=claimed["file_id"],
    )
    agent_id = f"{coder_name}-agent"
    register_agent(ledger, agent_id, "coder", parent_agent_id=ctx["lead"]["agent_id"])
    return ledger.brief_ack(coder_name, agent_id)


def bootstrap_module(ledger: Ledger, *, watch_armed_ms: int | None = None) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]
    if watch_armed_ms is not None:
        ledger.watch_armed(watch_armed_ms)

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "mgr-p1-phase-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    register_agent(ledger, "mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-1", "mgr-agent")

    module = ledger.module_add("mgr-p1-phase-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "mgr-p1-phase-1",
        "mgr-agent",
        "lead-p1-module-1",
        "lead",
        "sonnet",
        "Own module-1.",
        module_id=module_id,
    )
    register_agent(ledger, "lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-p1-module-1", "lead-agent")

    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def bootstrap_two_phases(ledger: Ledger) -> dict:
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
    register_agent(ledger, "mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-a", "mgr-agent")
    as_session(ledger, oracle_id, "host-r1-oracle")
    as_session(ledger, "mgr-agent", "host-r1-manager-1")
    manager = dict(manager) | {"session_name": "host-r1-manager-1"}

    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_a": phase_a["phase_id"],
        "phase_b": phase_b["phase_id"],
        "manager": manager,
    }


def bootstrap_oracle(
    ledger: Ledger, claude: FakeClaude, oracle_session: str = "sess-oracle"
) -> dict:
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


def ensure_graph_source_base(host: Path) -> None:
    # codebase-kg finds a graph's source tree by sampling its first anchors, and a re-baseline
    # fails without one. The copied repo graph anchors files the host lacks, so create one.
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        row = conn.execute("SELECT path FROM anchor ORDER BY path LIMIT 1").fetchone()
    finally:
        conn.close()
    stub = host / row[0]
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("", encoding="utf-8")
