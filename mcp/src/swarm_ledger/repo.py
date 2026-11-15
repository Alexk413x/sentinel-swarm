from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .db import NOW, write_tx
from .host import LedgerHost
from .identity import LedgerError, require_role, resolve

_GIT_TIMEOUT_S = 10
# A host's commit hooks run, so a commit may take far longer than a read.
_COMMIT_TIMEOUT_S = 300
_START_ADVICE = "create a run branch for this run with repo_branch_create"


def _run_git(args: list[str], cwd: Path, timeout: float = _GIT_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LedgerError(f"git {' '.join(args)} timed out after {timeout:.0f}s") from exc
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


def commit_paths(repo_root: Path, base_branch: str | None, paths: list[str], message: str) -> dict:
    if not _is_git_repo(repo_root):
        return {"committed": False, "reason": "the repo is not a git repository"}
    code, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    branch = out.strip() if code == 0 else ""
    base = _resolve_base_branch(repo_root, base_branch)
    if not branch or branch == "HEAD":
        return {"committed": False, "reason": "HEAD is detached; commit the phase yourself"}
    if branch == base:
        return {
            "committed": False,
            "reason": f"the run works on the base branch {base}; commit the phase yourself",
        }
    _, tracked, _ = _run_git(["ls-files", "--", *paths], repo_root)
    known = set(tracked.splitlines())
    wanted = sorted(p for p in set(paths) if p in known or (repo_root / p).exists())
    if not wanted:
        return {"committed": False, "reason": "the phase has no files on disk or in git"}
    code, _, err = _run_git(["add", "-A", "--", *wanted], repo_root)
    if code != 0:
        return {"committed": False, "reason": f"git add failed: {err.strip()}"}
    code, _, _ = _run_git(["diff", "--cached", "--quiet", "--", *wanted], repo_root)
    if code == 0:
        return {"committed": False, "reason": "nothing to commit: the phase's files match HEAD"}
    code, _, err = _run_git(
        ["commit", "-q", "-m", message, "--", *wanted], repo_root, _COMMIT_TIMEOUT_S
    )
    if code != 0:
        return {"committed": False, "reason": f"git commit failed: {err.strip()[-500:]}"}
    _, sha, _ = _run_git(["rev-parse", "--short", "HEAD"], repo_root)
    return {"committed": True, "sha": sha.strip(), "branch": branch, "paths": wanted}


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
    return _START_ADVICE


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
    if resolved_base is not None and base_exists and not detached:
        counts = _rev_list_counts(repo_root, resolved_base, "HEAD")
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

    advice = _advice(
        clean,
        "detached" if detached else branch,
        resolved_base,
        base_exists,
        behind_of_upstream,
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
        "obvious_start": advice == _START_ADVICE and not detached,
        "advice": advice,
    }


class RepoMixin(LedgerHost):
    def repo_check(self, caller: str, agent_id: str, fetch: bool = False) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        result = check_repo(self.repo_root, self.settings.base_branch, fetch)

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE runs SET repo_check_json = ?, repo_checked_at = {NOW} WHERE run_id = ?",
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
