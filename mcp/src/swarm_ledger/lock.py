from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .identity import LedgerError

_GIT_TIMEOUT_S = 10
_FILE_LOCK_POLL_S = 0.05


class LockHeldError(LedgerError):
    pass


def _locks_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(raw) if raw else Path.home() / ".claude"
    return base / "sentinel-swarm" / "locks"


def _git_common_dir(repo_root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def repo_identity(repo_root: Path) -> Path:
    """The resolved main checkout for a repo: the same path for every worktree of it."""
    common = _git_common_dir(repo_root)
    if common is not None:
        return common.resolve().parent
    return repo_root.resolve()


def _lock_path(repo_root: Path) -> Path:
    key = hashlib.sha256(str(repo_identity(repo_root)).encode("utf-8")).hexdigest()
    return _locks_dir() / f"{key}.json"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock(repo_root: Path) -> dict | None:
    try:
        data = json.loads(_lock_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def holder_alive(entry: dict) -> bool:
    pid = entry.get("server_pid")
    return isinstance(pid, int) and pid_alive(pid)


def acquire(repo_root: Path, run_id: int, server_pid: int) -> None:
    # No run_id exemption: acquire runs exactly once per run's lifetime, right after that
    # run's own row is created, so a live holder here is always a genuinely different run,
    # never this one re-entering. Two independent ledger.db files (a second clone or
    # worktree) can each assign the same run_id to their own first run, and a same-run_id
    # bypass would let that pair through unlocked.
    existing = read_lock(repo_root)
    if existing is not None and holder_alive(existing):
        raise LockHeldError(
            f"repo {repo_identity(repo_root)} is locked by run {existing.get('run_id')} "
            f"(server pid {existing.get('server_pid')}), which is still running. Finish or stop "
            "that run first; the lock also clears on its own once that server is gone"
        )
    path = _lock_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo": str(repo_identity(repo_root)), "run_id": run_id, "server_pid": server_pid}
    path.write_text(json.dumps(payload), encoding="utf-8")


def release(repo_root: Path, run_id: int | None = None) -> None:
    existing = read_lock(repo_root)
    if existing is None:
        return
    if run_id is not None and existing.get("run_id") != run_id:
        return
    try:
        _lock_path(repo_root).unlink()
    except OSError:
        pass


def release_owned(repo_root: Path, server_pid: int) -> None:
    existing = read_lock(repo_root)
    if existing is not None and existing.get("server_pid") == server_pid:
        try:
            _lock_path(repo_root).unlink()
        except OSError:
            pass


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fd = handle.fileno()
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(_FILE_LOCK_POLL_S)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
