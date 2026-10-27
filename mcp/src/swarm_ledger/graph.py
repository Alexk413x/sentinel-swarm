from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from .agentfiles import CODEBASE_KG_PLUGIN, install_path
from .db import in_clause, ledger_path, open_readonly
from .identity import LedgerError
from .lock import file_lock

KG_LOCK_FILE = "kg.lock"

_ENV_VAR = "SENTINEL_SWARM_KG_ROOT"
_UPSERT_SCRIPT = (
    "import json,sys; from codebase_kg import edits; "
    "print(json.dumps(edits.upsert_node(sys.argv[1], json.loads(sys.stdin.read()))))"
)
_BASE_UPSERT_SCRIPT = (
    "import json,sys; sys.path.insert(0, sys.argv[1]); from codebase_kg import edits; "
    "print(json.dumps(edits.upsert_node(sys.argv[2], json.loads(sys.stdin.read()))))"
)
_DEFAULT_FLOOR = (3, 10)
_FLOOR_RE = re.compile(r'requires-python\s*=\s*"\s*>=\s*(\d+)\.(\d+)')
_NO_DEPENDENCIES = re.compile(r"^dependencies\s*=\s*\[\s*\]", re.MULTILINE)


def codebase_kg_root(repo_root: Path) -> Path:
    env = os.environ.get(_ENV_VAR)
    if env:
        root = Path(env)
        if not root.is_dir():
            raise LedgerError(f"{_ENV_VAR}={env!r} does not point at a directory")
        return root

    install = install_path(repo_root, CODEBASE_KG_PLUGIN)
    if install is None:
        raise LedgerError(
            f"{CODEBASE_KG_PLUGIN} is not installed for {repo_root}; "
            f"install it or set {_ENV_VAR} to override"
        )
    mcp_dir = install / "mcp"
    if not mcp_dir.is_dir():
        raise LedgerError(f"no mcp folder under {install}")
    return mcp_dir


def _check_edges(graph_path: Path, nodes: list[dict]) -> None:
    known = {str(n.get("id")) for n in nodes}
    if graph_path.is_file():
        conn = open_readonly(graph_path)
        try:
            known.update(row[0] for row in conn.execute("SELECT id FROM node"))
        finally:
            conn.close()
    for node in nodes:
        for target in node.get("edges") or []:
            if target not in known:
                raise LedgerError(
                    f"node {node.get('id')!r} has an edge to {target!r}, which is not in the "
                    "graph; drop the edge or name an existing node id"
                )


def _pyproject(kg_root: Path) -> str:
    try:
        return (kg_root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""


def python_floor(kg_root: Path) -> tuple[int, int]:
    match = _FLOOR_RE.search(_pyproject(kg_root))
    return (int(match.group(1)), int(match.group(2))) if match else _DEFAULT_FLOOR


def runs_on_base(kg_root: Path) -> bool:
    return (
        sys.version_info[:2] >= python_floor(kg_root)
        and _NO_DEPENDENCIES.search(_pyproject(kg_root)) is not None
        and (kg_root / "src" / "codebase_kg" / "edits.py").is_file()
    )


def upsert_command(kg_root: Path, graph_path: Path, base: bool) -> list[str]:
    if base:
        # The base interpreter, as codebase-kg's own workers run: a Windows venv's python.exe
        # is a launcher that starts the base interpreter as a second process.
        python = getattr(sys, "_base_executable", None) or sys.executable
        return [
            python,
            "-I",
            "-S",
            "-c",
            _BASE_UPSERT_SCRIPT,
            str(kg_root / "src"),
            str(graph_path),
        ]
    return [
        "uv",
        "run",
        "--project",
        str(kg_root),
        "--frozen",
        "--no-dev",
        "python",
        "-c",
        _UPSERT_SCRIPT,
        str(graph_path),
    ]


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.splitlines()[-lines:])


def graph_upsert(repo_root: Path, nodes: list[dict]) -> dict:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    _check_edges(graph_path, nodes)
    kg_root = codebase_kg_root(repo_root)
    # A write states that the caller re-read each node against its files, so it re-baselines them.
    patches = [{**node, "rebaseline": True} for node in nodes]
    # A caller's venv leaks through VIRTUAL_ENV and makes uv refuse the kg project.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    base = runs_on_base(kg_root)
    with file_lock(ledger_path(repo_root).parent / KG_LOCK_FILE):
        try:
            completed = subprocess.run(
                upsert_command(kg_root, graph_path, base),
                cwd=repo_root,
                env=env,
                input=json.dumps(patches),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except OSError as exc:
            launcher = "Python" if base else "uv"
            raise LedgerError(f"failed to launch {launcher} for graph_upsert: {exc}") from exc

    if completed.returncode != 0:
        raise LedgerError(f"graph_upsert failed: {_tail(completed.stderr)}")

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        tail = _tail(completed.stderr or completed.stdout)
        raise LedgerError(f"graph_upsert produced unparsable output: {tail}") from exc


def _whole_word(symbol: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(symbol)}\b", text) is not None


def _top_level_py_symbols(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]


def code_symbols_for(repo_root: Path, rel_path: str) -> list[str]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not graph_path.is_file():
        return []
    conn = open_readonly(graph_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(anchor)")}
        if "symbol" not in columns:
            return []
        return [
            row[0]
            for row in conn.execute(
                "SELECT symbol FROM anchor WHERE path = ? AND symbol IS NOT NULL AND symbol != ''",
                (rel_path,),
            )
        ]
    finally:
        conn.close()


def anchored_paths(repo_root: Path, rel_paths: list[str]) -> set[str]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not rel_paths or not graph_path.is_file():
        return set()
    conn = open_readonly(graph_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT path FROM anchor WHERE path IN ({in_clause(rel_paths)})",
                rel_paths,
            )
        }
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def node_anchor_paths(repo_root: Path, node_id: str) -> list[str]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not graph_path.is_file():
        return []
    conn = open_readonly(graph_path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT path FROM anchor WHERE node_id = ? ORDER BY path", (node_id,)
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _content_sha(data: bytes) -> str:
    # Duplicates codebase-kg's content_sha (CRLF folded to LF, then SHA-256), which its own
    # git hooks also copy; the ledger does not import codebase-kg in-process.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def stale_files(repo_root: Path, rel_paths: list[str] | None = None) -> dict[str, list[str]]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not graph_path.is_file() or rel_paths == []:
        return {}
    conn = open_readonly(graph_path)
    try:
        query = "SELECT path, sha FROM source WHERE path IN (SELECT path FROM anchor)"
        params: list[str] = []
        if rel_paths is not None:
            query += f" AND path IN ({in_clause(rel_paths)})"
            params = list(rel_paths)
        baselines = {row[0]: row[1] for row in conn.execute(query, params)}
        stale: dict[str, list[str]] = {}
        for path, recorded in sorted(baselines.items()):
            try:
                current = _content_sha((repo_root / path).read_bytes())
            except OSError:
                continue
            if recorded and current != recorded:
                stale[path] = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT node_id FROM anchor WHERE path = ? ORDER BY node_id",
                        (path,),
                    )
                ]
        return stale
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def graph_current_for(repo_root: Path, rel_path: str) -> tuple[bool, list[str]]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not graph_path.is_file():
        return False, ["no code graph at knowledge/code_graph.db"]

    conn = open_readonly(graph_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(anchor)")}
        if "path" not in columns:
            raise LedgerError("the code graph's anchor table has no 'path' column")

        anchors = conn.execute("SELECT * FROM anchor WHERE path = ?", (rel_path,)).fetchall()
        if not anchors:
            return False, [f"no node anchors on {rel_path}"]

        full_path = repo_root / rel_path
        source_text = full_path.read_text(encoding="utf-8") if full_path.is_file() else ""

        reasons: list[str] = []
        mapped_symbols: set[str] = set()
        has_symbol_column = "symbol" in columns
        for anchor in anchors:
            symbol = anchor["symbol"] if has_symbol_column else None
            if symbol:
                mapped_symbols.add(symbol)
                if not _whole_word(symbol, source_text):
                    reasons.append(f"anchor symbol {symbol} not found")

        if rel_path.endswith(".py") and full_path.is_file():
            reasons.extend(
                f"symbol {name} is unmapped"
                for name in _top_level_py_symbols(source_text)
                if name not in mapped_symbols
            )

        return (len(reasons) == 0, reasons)
    finally:
        conn.close()
