from __future__ import annotations

import os
from pathlib import Path

import pytest

from swarm_ledger import lock
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")


def test_repo_identity_falls_back_to_the_repo_root_without_a_real_git_repo(host: Path) -> None:
    # host/.git is an empty directory, not a real git checkout, so git rev-parse fails.
    assert lock.repo_identity(host) == host.resolve()


def test_pid_alive_is_true_for_this_process_and_false_for_an_unlikely_pid() -> None:
    assert lock.pid_alive(os.getpid()) is True
    assert lock.pid_alive(2**30) is False


def test_lock_acquire_refuses_a_second_run_while_the_holder_is_alive(host: Path) -> None:
    lock.acquire(host, run_id=1, server_pid=os.getpid())
    with pytest.raises(lock.LockHeldError, match="is locked by run 1"):
        lock.acquire(host, run_id=2, server_pid=os.getpid())
    held = lock.read_lock(host)
    assert held is not None
    assert held["run_id"] == 1


def test_lock_acquire_steals_a_stale_lock(host: Path) -> None:
    lock.acquire(host, run_id=1, server_pid=2**30)
    # run_id 1's recorded server pid is not alive, so a new run may take the lock.
    lock.acquire(host, run_id=2, server_pid=1)
    held = lock.read_lock(host)
    assert held is not None
    assert held["run_id"] == 2


def test_lock_release_and_release_owned(host: Path) -> None:
    lock.acquire(host, run_id=1, server_pid=os.getpid())
    lock.release(host, run_id=2)  # a different run_id: not released
    assert lock.read_lock(host) is not None
    lock.release_owned(host, server_pid=999999)  # not the recorded pid: not released
    assert lock.read_lock(host) is not None
    lock.release_owned(host, server_pid=os.getpid())
    assert lock.read_lock(host) is None


def test_run_start_refuses_a_second_run_on_the_same_repo(host: Path) -> None:
    ledger_a = Ledger(host, db_path=host / ".sentinel-swarm" / "a.db")
    ledger_b = Ledger(host, db_path=host / ".sentinel-swarm" / "b.db")
    ledger_a.run_start(prd="Build X", session_id="sess-a")
    with pytest.raises(LedgerError, match="is locked by run"):
        ledger_b.run_start(prd="Build Y", session_id="sess-b")


def test_run_finish_releases_the_lock_for_a_later_run(host: Path) -> None:
    ledger_a = Ledger(host, db_path=host / ".sentinel-swarm" / "a.db")
    started = ledger_a.run_start(prd="Build X", session_id="sess-a")
    oracle_id = started["oracle"]["agent_id"]
    ledger_a.run_finish("oracle", oracle_id, "success")

    ledger_b = Ledger(host, db_path=host / ".sentinel-swarm" / "b.db")
    resumed = ledger_b.run_start(prd="Build Y", session_id="sess-b")
    assert resumed["resumed"] is False
