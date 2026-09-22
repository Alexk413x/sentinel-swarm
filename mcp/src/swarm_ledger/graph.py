from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import threading
from pathlib import Path

from .identity import LedgerError

KG_LOCK = threading.Lock()

_ENV_VAR = "SENTINEL_SWARM_KG_ROOT"
_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache" / "codebase-kg" / "codebase-kg"
_UPSERT_SCRIPT = (
    "import json,sys; from codebase_kg import edits; "
    "print(json.dumps(edits.upsert_node(sys.argv[1], json.loads(sys.stdin.read()))))"
)
_WORD_RE = re.compile(r"\w+")


def _version_tuple(name: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(\.\d+)*", name):
        return None
    return tuple(int(part) for part in name.split("."))


def codebase_kg_root() -> Path:
    env = os.environ.get(_ENV_VAR)
    if env:
        root = Path(env)
        if not root.is_dir():
            raise LedgerError(f"{_ENV_VAR}={env!r} does not point at a directory")
        return root

    if not _CACHE_ROOT.is_dir():
        raise LedgerError(
            f"no codebase-kg plugin cache at {_CACHE_ROOT}; set {_ENV_VAR} to override"
        )

    candidates = [p for p in _CACHE_ROOT.iterdir() if p.is_dir()]
    if not candidates:
        raise LedgerError(f"no version folders under {_CACHE_ROOT}")

    numbered = [(_version_tuple(p.name), p) for p in candidates]
    numbered = [(key, p) for key, p in numbered if key is not None]
    if numbered:
        numbered.sort(key=lambda pair: pair[0])
        best = numbered[-1][1]
    else:
        # No numeric version folder. Fall back to the lexicographically last one
        # rather than failing outright.
        best = sorted(candidates, key=lambda p: p.name)[-1]

    mcp_dir = best / "mcp"
    if not mcp_dir.is_dir():
        raise LedgerError(f"no mcp folder under {best}")
    return mcp_dir


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.splitlines()[-lines:])


def graph_upsert(repo_root: Path, nodes: list[dict]) -> dict:
    kg_root = codebase_kg_root()
    graph_path = repo_root / "knowledge" / "code_graph.db"

    with KG_LOCK:
        try:
            completed = subprocess.run(
                [
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
                ],
                cwd=repo_root,
                input=json.dumps(nodes),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except OSError as exc:
            raise LedgerError(f"failed to launch uv for graph_upsert: {exc}") from exc

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
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not node.name.startswith("_"):
                names.append(node.name)
    return names


def graph_current_for(repo_root: Path, rel_path: str) -> tuple[bool, list[str]]:
    graph_path = repo_root / "knowledge" / "code_graph.db"
    if not graph_path.is_file():
        return False, ["no code graph at knowledge/code_graph.db"]

    uri = f"file:{graph_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
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
            for name in _top_level_py_symbols(source_text):
                if name not in mapped_symbols:
                    reasons.append(f"symbol {name} is unmapped")

        return (len(reasons) == 0, reasons)
    finally:
        conn.close()
