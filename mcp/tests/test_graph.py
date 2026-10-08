from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from swarm_ledger import graph
from swarm_ledger.identity import LedgerError


def _kg(
    tmp_path: Path,
    requires: str | None = ">=3.10",
    edits: bool = True,
    dependencies: str = "[]",
) -> Path:
    root = tmp_path / "kg" / "mcp"
    (root / "src" / "codebase_kg").mkdir(parents=True)
    if edits:
        (root / "src" / "codebase_kg" / "edits.py").write_text("", "utf-8")
    if requires is not None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "codebase-kg"\nrequires-python = "{requires}"\n'
            f"dependencies = {dependencies}\n",
            "utf-8",
        )
    return root


def test_the_python_floor_comes_from_codebase_kgs_pyproject(tmp_path: Path) -> None:
    assert graph.python_floor(_kg(tmp_path, ">=3.12")) == (3, 12)
    assert graph.python_floor(_kg(tmp_path / "none", None)) == (3, 10)


def test_graph_upsert_runs_on_the_base_python_at_or_above_the_floor(tmp_path: Path) -> None:
    current = f">={sys.version_info[0]}.{sys.version_info[1]}"
    assert graph.runs_on_base(_kg(tmp_path, current)) is True


def test_graph_upsert_falls_back_to_uv_below_the_floor_or_with_dependencies(
    tmp_path: Path,
) -> None:
    assert graph.runs_on_base(_kg(tmp_path / "new", f">={sys.version_info[0]}.99")) is False
    assert graph.runs_on_base(_kg(tmp_path / "deps", dependencies='["fastmcp>=2"]')) is False
    assert graph.runs_on_base(_kg(tmp_path / "bare", None)) is False
    assert graph.runs_on_base(_kg(tmp_path / "old", edits=False)) is False


def test_the_base_command_runs_isolated_with_codebase_kgs_source_on_the_path(
    tmp_path: Path,
) -> None:
    kg = _kg(tmp_path)
    target = tmp_path / "knowledge" / "code_graph.db"
    command = graph.upsert_command(kg, target, base=True)
    assert command[0] == (getattr(sys, "_base_executable", None) or sys.executable)
    assert command[1:4] == ["-I", "-S", "-c"]
    assert command[-2:] == [str(kg / "src"), str(target)]
    fallback = graph.upsert_command(kg, target, base=False)
    assert fallback[:6] == ["uv", "run", "--project", str(kg), "--frozen", "--no-dev"]
    assert fallback[-1] == str(target)


@pytest.mark.integration
def test_graph_upsert_returns_the_same_result_on_both_paths(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        kg = graph.codebase_kg_root()
    except LedgerError:
        pytest.skip("codebase-kg is not installed in this environment")
    if not graph.runs_on_base(kg):
        pytest.skip("the base Python is below codebase-kg's floor")

    results = []
    for base in (False, True):
        host = tmp_path / ("base" if base else "uv")
        (host / "pkg").mkdir(parents=True)
        (host / "pkg" / "good.py").write_text("def add(a, b):\n    return a + b\n", "utf-8")
        (host / "knowledge").mkdir()
        shutil.copy(repo_root / "knowledge" / "code_graph.db", host / "knowledge")
        monkeypatch.setattr(graph, "runs_on_base", lambda _root, base=base: base)
        node = {
            "id": "swarmtest_good",
            "kind": "function",
            "description": "adds two numbers",
            "anchors": ["pkg/good.py#add"],
        }
        result = graph.graph_upsert(host, [node])
        result.pop("path", None)
        results.append(result)
    assert results[0]["ok"] is True
    assert results[0] == results[1]
