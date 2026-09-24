from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from .db import write_tx
from .identity import LedgerError, require_role, resolve
from .settings import Settings

_GIT_TIMEOUT_S = 10
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise LedgerError(f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:
        raise LedgerError(f"cannot run git: {exc}") from exc
    return result.returncode, result.stdout, result.stderr


def _is_git_repo(repo_root: Path) -> bool:
    code, out, _ = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root)
    return code == 0 and out.strip() == "true"


def _branch_exists(repo_root: Path, name: str) -> bool:
    code, _, _ = _run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo_root)
    return code == 0


def _resolve_base_branch(repo_root: Path, configured: str | None) -> str | None:
    if configured:
        return configured
    if _branch_exists(repo_root, "main"):
        return "main"
    if _branch_exists(repo_root, "master"):
        return "master"
    return None


def _rev_list_counts(repo_root: Path, left: str, right: str) -> tuple[int, int] | None:
    code, out, _ = _run_git(["rev-list", "--left-right", "--count", f"{left}...{right}"], repo_root)
    if code != 0:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _advice(
    clean: bool,
    branch: str | None,
    resolved_base: str | None,
    base_exists: bool,
    behind_upstream: int | None,
) -> str:
    if not clean:
        return "ask the user: there are uncommitted changes"
    if resolved_base is None:
        return "ask the user: no base branch (main or master) exists"
    if branch != resolved_base or not base_exists:
        return f"ask the user: the repo is on a different branch ({branch or 'detached'})"
    if behind_upstream:
        return "ask the user: the branch is behind its remote"
    return "create a run branch for this run with repo_branch_create"


def check_repo(repo_root: Path, base_branch: str | None, fetch: bool) -> dict:
    """The pure git inspection behind `repo_check`. No ledger state, no identity."""
    if not _is_git_repo(repo_root):
        return {
            "branch": None,
            "clean": None,
            "dirty_paths": [],
            "base_branch": None,
            "base_exists": False,
            "ahead_of_base": None,
            "behind_base": None,
            "upstream": None,
            "ahead_of_upstream": None,
            "behind_of_upstream": None,
            "fetch_error": None,
            "obvious_start": False,
            "advice": "ask the user: this is not a git repository",
        }

    code, out, _ = _run_git(["symbolic-ref", "--short", "-q", "HEAD"], repo_root)
    detached = code != 0 or not out.strip()
    branch = None if detached else out.strip()

    code, out, _ = _run_git(["status", "--porcelain"], repo_root)
    dirty_paths = [line[3:].strip() for line in out.splitlines() if line.strip()]
    clean = code == 0 and not dirty_paths

    resolved_base = _resolve_base_branch(repo_root, base_branch)
    base_exists = resolved_base is not None and _branch_exists(repo_root, resolved_base)

    ahead_of_base = behind_base = None
    if base_exists and not detached:
        counts = _rev_list_counts(repo_root, resolved_base, "HEAD")  # type: ignore[arg-type]
        if counts is not None:
            behind_base, ahead_of_base = counts

    fetch_error: str | None = None
    if fetch:
        try:
            code, _, err = _run_git(["fetch", "--quiet"], repo_root)
            if code != 0:
                fetch_error = (err or "git fetch failed").strip()
        except LedgerError as exc:
            fetch_error = str(exc)

    upstream = None
    ahead_of_upstream = behind_of_upstream = None
    if not detached:
        code, out, _ = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo_root
        )
        if code == 0 and out.strip():
            upstream = out.strip()
            counts = _rev_list_counts(repo_root, "@{u}", "HEAD")
            if counts is not None:
                behind_of_upstream, ahead_of_upstream = counts

    obvious_start = bool(
        clean
        and not detached
        and resolved_base is not None
        and branch == resolved_base
        and base_exists
        and not behind_of_upstream
    )

    return {
        "branch": "detached" if detached else branch,
        "clean": clean,
        "dirty_paths": dirty_paths,
        "base_branch": resolved_base,
        "base_exists": base_exists,
        "ahead_of_base": ahead_of_base,
        "behind_base": behind_base,
        "upstream": upstream,
        "ahead_of_upstream": ahead_of_upstream,
        "behind_of_upstream": behind_of_upstream,
        "fetch_error": fetch_error,
        "obvious_start": obvious_start,
        "advice": _advice(
            clean,
            "detached" if detached else branch,
            resolved_base,
            base_exists,
            behind_of_upstream,
        ),
    }


class RepoMixin:
    """Repo tools: `repo_check` and `repo_branch_create`. Oracle only.

    Split out of `ledger.py` like `ReviewMixin`; every method here still assumes
    it is mixed into `Ledger` and relies on `self.conn` and `self.repo_root`.
    """

    conn: sqlite3.Connection
    settings: Settings
    repo_root: Path

    def repo_check(self, caller: str, agent_id: str, fetch: bool = False) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        result = check_repo(self.repo_root, self.settings.base_branch, fetch)

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE runs SET repo_check_json = ?, repo_checked_at = {_NOW} WHERE run_id = ?",
                (json.dumps(result), c.run_id),
            )

        return result

    def repo_branch_create(self, caller: str, agent_id: str, name: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")

        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (c.run_id,)).fetchone()
        if run is None or not run["repo_check_json"]:
            raise LedgerError("call repo_check first")
        check = json.loads(run["repo_check_json"])
        if not check.get("obvious_start"):
            raise LedgerError(
                "the last repo_check did not report obvious_start; ask the user instead of "
                "creating a branch"
            )

        live = self.conn.execute(
            "SELECT name FROM agents WHERE run_id = ? AND role IN ('manager', 'lead', 'coder') "
            "AND ended_at IS NULL",
            (c.run_id,),
        ).fetchall()
        if live:
            raise LedgerError(
                f"agent(s) still live for this run: {[row['name'] for row in live]}; release "
                "them before creating a branch"
            )

        code, _, err = _run_git(["check-ref-format", "--branch", name], self.repo_root)
        if code != 0:
            raise LedgerError(f"{name!r} is not a valid branch name: {(err or '').strip()}")

        code, out, _ = _run_git(["status", "--porcelain"], self.repo_root)
        if code != 0 or out.strip():
            raise LedgerError("the tree is no longer clean; call repo_check again")

        base = check.get("base_branch")
        if not base:
            raise LedgerError("the last repo_check found no base branch; ask the user instead")

        code, out, err = _run_git(["switch", "-c", name, base], self.repo_root)
        if code != 0:
            raise LedgerError(f"git switch -c {name} {base} failed: {(err or out).strip()}")

        with write_tx(self.conn) as conn:
            conn.execute("UPDATE runs SET branch = ? WHERE run_id = ?", (name, c.run_id))

        return {"branch": name, "base_branch": base}
