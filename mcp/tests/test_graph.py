from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import ensure_graph_source_base, write_registry

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


def _register_kg(host: Path, install: Path) -> None:
    entry = {"scope": "project", "projectPath": str(host), "installPath": str(install)}
    write_registry({"codebase-kg@alexk413x": [entry]})


def test_codebase_kg_root_reads_the_repos_install_from_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_SWARM_KG_ROOT", raising=False)
    host = tmp_path / "host"
    host.mkdir()
    install = _kg(tmp_path).parent
    _register_kg(host, install)

    assert graph.codebase_kg_root(host) == install / "mcp"
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": {"codebase-kg@alexk413x": False}}), encoding="utf-8"
    )
    with pytest.raises(LedgerError, match="codebase-kg@alexk413x is not enabled and installed for"):
        graph.codebase_kg_root(other)


def test_the_override_wins_over_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("SENTINEL_SWARM_KG_ROOT", str(override))
    assert graph.codebase_kg_root(tmp_path) == override


@pytest.mark.integration
def test_graph_upsert_returns_the_same_result_on_both_paths(
    tmp_path: Path, repo_root: Path, kg_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kg = kg_root
    if not graph.runs_on_base(kg):
        pytest.skip("the base Python is below codebase-kg's floor")

    results = []
    for base in (False, True):
        host = tmp_path / ("base" if base else "uv")
        (host / "pkg").mkdir(parents=True)
        (host / "pkg" / "good.py").write_text("def add(a, b):\n    return a + b\n", "utf-8")
        (host / "knowledge").mkdir()
        shutil.copy(repo_root / "knowledge" / "code_graph.db", host / "knowledge")
        ensure_graph_source_base(host)
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


def _graph_host(tmp_path: Path, repo_root: Path) -> Path:
    host = tmp_path / "host"
    (host / "pkg").mkdir(parents=True)
    (host / "knowledge").mkdir()
    shutil.copy(repo_root / "knowledge" / "code_graph.db", host / "knowledge")
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute("INSERT INTO node (id, kind, description) VALUES ('n', 'module', 'd')")
        for ordinal, path in enumerate(("pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/gone.py")):
            conn.execute(
                "INSERT INTO anchor (node_id, ord, path, base) VALUES ('n', ?, ?, ?)",
                (ordinal, path, Path(path).name),
            )
        conn.commit()
    finally:
        conn.close()
    return host


def _baseline(host: Path, path: str, data: bytes) -> None:
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute(
            "INSERT INTO source (path, sha) VALUES (?, ?)", (path, graph._content_sha(data))
        )
        conn.commit()
    finally:
        conn.close()


def test_stale_files_compares_content_the_way_codebase_kg_does(
    tmp_path: Path, repo_root: Path
) -> None:
    host = _graph_host(tmp_path, repo_root)
    (host / "pkg" / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    (host / "pkg" / "b.py").write_bytes(b"x = 2\n")
    (host / "pkg" / "c.py").write_bytes(b"unrecorded\n")
    _baseline(host, "pkg/a.py", b"x = 1\ny = 2\n")
    _baseline(host, "pkg/b.py", b"x = 1\n")
    _baseline(host, "pkg/gone.py", b"removed\n")

    assert graph.stale_files(host) == {"pkg/b.py": ["n"]}
    assert graph.stale_files(host, ["pkg/a.py", "pkg/c.py"]) == {}
    assert graph.stale_files(host, ["pkg/b.py"]) == {"pkg/b.py": ["n"]}
    assert graph.stale_files(host, []) == {}
    assert graph.stale_files(tmp_path / "no-graph") == {}


def test_graph_upsert_asks_codebase_kg_to_re_baseline_every_node_it_writes(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _graph_host(tmp_path, repo_root)
    sent: dict = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        sent["input"] = json.loads(str(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

    monkeypatch.setattr(graph, "codebase_kg_root", lambda _root: _kg(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake_run)
    nodes = [{"id": "n", "description": "d"}, {"id": "m", "kind": "module", "rebaseline": False}]

    assert graph.graph_upsert(host, nodes) == {"ok": True}
    assert [n["rebaseline"] for n in sent["input"]] == [True, True]
    assert "rebaseline" not in nodes[0]
