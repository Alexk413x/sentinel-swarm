from __future__ import annotations

from pathlib import Path

import pytest
from helpers import bootstrap_module, make_host, settings_text, spawn_coder

from swarm_ledger import setup, testing
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import command_line, load_settings
from swarm_ledger.testing import Executed

pytestmark = pytest.mark.usefixtures("claude_sessions")

COMMANDS = (
    "commands:\n"
    "  - name: build-recipes\n"
    "    run: scripts/build_recipes.py\n"
    "    cwd: .\n"
    "    interpreter: python\n"
    "  - name: regen\n"
    "    run: gen.py --all\n"
    "    cwd: tools\n"
    "    interpreter: node\n"
    "  - name: no-run\n"
)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    settings = settings_text(test_command="pytest -q {target}", swaps=[("commands:\n", COMMANDS)])
    return make_host(tmp_path / "host", settings)


def _shell(ledger: Ledger, agent_id: str, command: str) -> dict | None:
    return events.handle_pre_shell(
        ledger, {"agent_id": agent_id, "tool_input": {"command": command}}
    )


def test_the_commands_setting_loads_each_named_host_command(host: Path) -> None:
    commands = load_settings(host).commands
    assert commands == [
        {
            "name": "build-recipes",
            "run": "scripts/build_recipes.py",
            "cwd": ".",
            "interpreter": "python",
        },
        {"name": "regen", "run": "gen.py --all", "cwd": "tools", "interpreter": "node"},
    ]
    assert [command_line(c) for c in commands] == [
        "python scripts/build_recipes.py",
        "cd tools && node gen.py --all",
    ]
    assert load_settings(make_host(host.parent / "plain")).commands == []


def test_the_coder_and_manager_shells_allow_a_named_host_command(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sh", "src/mine.py", "tests/test_mine.py")
    for agent_id in (coder["agent_id"], ctx["manager"]["agent_id"]):
        assert _shell(ledger, agent_id, "python scripts/build_recipes.py") is None
        assert _shell(ledger, agent_id, "cd tools  &&  node gen.py --all") is None
        for command in (
            "python scripts/build_recipes.py --evil",
            "python scripts/build_recipes.py; rm -rf /",
            "cd tools && node other.js",
        ):
            denied = _shell(ledger, agent_id, command)
            assert denied is not None, (agent_id, command)
            assert "python scripts/build_recipes.py" in str(denied)


def test_the_manager_shell_runs_nothing_else(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    manager = ctx["manager"]["agent_id"]
    for command in ("pytest -q tests/test_mine.py", "git status"):
        assert _shell(ledger, manager, command) is not None, command
    lead = _shell(ledger, ctx["lead"]["agent_id"], "python scripts/build_recipes.py")
    assert lead is not None
    assert "no shell" in str(lead)


def test_a_manager_shell_without_commands_says_none_is_set(tmp_path: Path) -> None:
    host = make_host(tmp_path / "plain", settings_text(test_command="pytest -q {target}"))
    ledger = Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")
    ctx = bootstrap_module(ledger)
    denied = _shell(ledger, ctx["manager"]["agent_id"], "python scripts/build_recipes.py")
    assert denied is not None
    assert "none is set" in str(denied)


def test_a_failing_test_that_names_a_command_is_reported_with_it(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, "coder-p1-module-1-sh", "src/mine.py", "tests/test_mine.py")
    output = "FAILED test_recipes - driver_recipes.db is stale; run scripts/build_recipes.py\n"

    def failing(command: str, cwd: Path, timeout_s: int, log_path: Path | None = None) -> Executed:
        return Executed(exit_code=1, output=output, duration_ms=10, timed_out=False)

    monkeypatch.setattr(testing, "execute", failing)
    result = ledger.tests_run(coder["name"], coder["agent_id"], "file", "tests/test_mine.py")

    named = [{"name": "build-recipes", "command": "python scripts/build_recipes.py"}]
    assert result["state"] == "done"
    assert result["commands"] == named
    got = ledger.test_run_get(coder["name"], coder["agent_id"], result["test_run_id"])
    assert got["commands"] == named


def test_setup_proposes_the_host_commands_the_docs_name(tmp_path: Path) -> None:
    repo = tmp_path / "docs-host"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / "scripts" / "recipes").mkdir(parents=True)
    (repo / "scripts" / "recipes" / "build_recipes.py").write_text("", encoding="utf-8")
    (repo / "scripts" / "check_tests.py").write_text("", encoding="utf-8")
    (repo / "README.md").write_text(
        "Rebuild the recipes with `python scripts/recipes/build_recipes.py` after a graph "
        "change.\n\n```bash\npython scripts/check_tests.py\npython scripts/missing.py\n```\n",
        encoding="utf-8",
    )

    found = setup.doc_commands(repo)

    assert found == [
        {
            "name": "build-recipes",
            "run": "scripts/recipes/build_recipes.py",
            "cwd": ".",
            "interpreter": "python",
            "source": "README.md",
        }
    ]
    report = setup.SetupReport()
    setup.propose_commands(repo, report)
    assert any(
        line.startswith("Proposed host command from README.md: name: build-recipes")
        for line in report.lines
    )
