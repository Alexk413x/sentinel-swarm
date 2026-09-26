from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.repo import check_repo


class _FakeClaude:
    """A minimal stand-in for the `claude` CLI: enough for agent_spawn's session-list and
    --bg calls, so no test here starts a real `claude` process."""

    def __init__(self) -> None:
        self.listing: list[dict] = []
        self._count = 0

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        del cwd
        if args[:2] == ["agents", "--json"]:
            return json.dumps(self.listing)
        name = args[args.index("--name") + 1]
        self._count += 1
        session_id = f"{self._count:08x}-aaaa-bbbb-cccc-dddddddddddd"
        bg_id = session_id[:8]
        entry = {"pid": 100 + self._count, "sessionId": session_id, "id": bg_id, "name": name}
        self.listing.append(entry)
        return f"backgrounded · {bg_id} · {name}\n"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=10
    )


def _init_repo(root: Path, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], root)
    _git(["config", "user.email", "swarm@example.com"], root)
    _git(["config", "user.name", "Swarm Test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)


def _dirty(root: Path) -> None:
    (root / "README.md").write_text("changed\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True)
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    return root


def _make_ledger(host: Path) -> Ledger:
    # A plain fixture would build the Ledger before a test's own _init_repo call creates
    # .git, so ensure_git_exclude would never add .sentinel-swarm/ to .git/info/exclude and
    # the ledger's own db file would show up as untracked, dirtying every check_repo result.
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


# -- check_repo: real temporary git repos ----------------------------------------------------


def test_check_repo_clean_on_main_is_the_obvious_start(host: Path) -> None:
    _init_repo(host, "main")
    result = check_repo(host, None, fetch=False)
    assert result["branch"] == "main"
    assert result["clean"] is True
    assert result["dirty_paths"] == []
    assert result["base_branch"] == "main"
    assert result["base_exists"] is True
    assert result["ahead_of_base"] == 0
    assert result["behind_base"] == 0
    assert result["upstream"] is None
    assert result["obvious_start"] is True
    assert "create a run branch" in result["advice"]


def test_check_repo_dirty_tree(host: Path) -> None:
    _init_repo(host, "main")
    _dirty(host)
    result = check_repo(host, None, fetch=False)
    assert result["clean"] is False
    assert result["dirty_paths"] == ["README.md"]
    assert result["obvious_start"] is False
    assert "uncommitted changes" in result["advice"]


def test_check_repo_on_a_different_branch(host: Path) -> None:
    _init_repo(host, "main")
    _git(["switch", "-c", "feature"], host)
    result = check_repo(host, None, fetch=False)
    assert result["branch"] == "feature"
    assert result["clean"] is True
    assert result["base_branch"] == "main"
    assert result["base_exists"] is True
    assert result["obvious_start"] is False
    assert "different branch" in result["advice"]


def test_check_repo_no_main_but_master(host: Path) -> None:
    _init_repo(host, "master")
    result = check_repo(host, None, fetch=False)
    assert result["branch"] == "master"
    assert result["base_branch"] == "master"
    assert result["base_exists"] is True
    assert result["obvious_start"] is True


def test_check_repo_behind_its_upstream(host: Path, tmp_path: Path) -> None:
    _init_repo(host, "main")
    # A bare origin, not host itself: pushing straight into a checked-out branch is refused.
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], host)
    _git(["push", "-q", "-u", "origin", "main"], host)

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(bare), str(clone)], tmp_path)
    _git(["checkout", "-q", "main"], clone)
    _git(["config", "user.email", "swarm@example.com"], clone)
    _git(["config", "user.name", "Swarm Test"], clone)
    (clone / "README.md").write_text("from the clone\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "clone commit"], clone)
    _git(["push", "-q"], clone)

    _git(["fetch", "-q"], host)
    result = check_repo(host, None, fetch=False)
    assert result["upstream"] == "origin/main"
    assert result["behind_of_upstream"] == 1
    assert result["ahead_of_upstream"] == 0
    assert result["obvious_start"] is False
    assert "behind its remote" in result["advice"]


def test_check_repo_fetch_reports_a_failure_instead_of_raising(host: Path) -> None:
    _init_repo(host, "main")
    _git(["remote", "add", "origin", "https://example.invalid/nowhere.git"], host)
    result = check_repo(host, None, fetch=True)
    assert result["fetch_error"]


def test_check_repo_not_a_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "not-a-repo"
    root.mkdir()
    result = check_repo(root, None, fetch=False)
    assert result["branch"] is None
    assert result["obvious_start"] is False
    assert "not a git repository" in result["advice"]


def test_check_repo_honors_a_configured_base_branch(host: Path) -> None:
    _init_repo(host, "trunk")
    result = check_repo(host, "trunk", fetch=False)
    assert result["base_branch"] == "trunk"
    assert result["base_exists"] is True
    assert result["obvious_start"] is True


# -- repo_check records on the run -----------------------------------------------------------


def test_repo_check_is_oracle_only_and_records_on_the_run(host: Path) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    with pytest.raises(LedgerError):
        ledger.repo_check("nobody", "nobody")

    result = ledger.repo_check("oracle", oracle_id)
    assert result["obvious_start"] is True

    run_row = ledger.conn.execute(
        "SELECT * FROM runs WHERE run_id = ?", (started["run"]["run_id"],)
    ).fetchone()
    assert run_row["repo_checked_at"] is not None
    assert "obvious_start" in run_row["repo_check_json"]


# -- repo_branch_create ------------------------------------------------------------------------


def _bootstrap_oracle(ledger: Ledger) -> str:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    return started["oracle"]["agent_id"]


def test_repo_branch_create_refuses_without_a_repo_check(host: Path) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    with pytest.raises(LedgerError, match="call repo_check first"):
        ledger.repo_branch_create("oracle", oracle_id, "run-1")


def test_repo_branch_create_refuses_when_the_start_was_not_obvious(host: Path) -> None:
    _init_repo(host, "main")
    _dirty(host)
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    ledger.repo_check("oracle", oracle_id)
    with pytest.raises(LedgerError, match="obvious_start"):
        ledger.repo_branch_create("oracle", oracle_id, "run-1")


def test_repo_branch_create_refuses_when_the_tree_became_dirty_since_the_check(
    host: Path,
) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    ledger.repo_check("oracle", oracle_id)
    _dirty(host)
    with pytest.raises(LedgerError, match="no longer clean"):
        ledger.repo_branch_create("oracle", oracle_id, "run-1")


def test_repo_branch_create_refuses_an_invalid_branch_name(host: Path) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    ledger.repo_check("oracle", oracle_id)
    with pytest.raises(LedgerError, match="not a valid branch name"):
        ledger.repo_branch_create("oracle", oracle_id, "not a valid name")


def test_repo_branch_create_refuses_while_a_manager_is_live(host: Path) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    ledger.repo_check("oracle", oracle_id)
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.brief_create(
        "oracle",
        oracle_id,
        "manager-1",
        "manager",
        "opus",
        "Own phase-1.",
        phase_id=phase["phase_id"],
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    ledger.brief_ack("manager-1", "mgr-agent")

    with pytest.raises(LedgerError, match="still live"):
        ledger.repo_branch_create("oracle", oracle_id, "run-1")


def test_repo_branch_create_succeeds_and_records_the_branch(host: Path) -> None:
    _init_repo(host, "main")
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    ledger.repo_check("oracle", oracle_id)

    result = ledger.repo_branch_create("oracle", oracle_id, "swarm-run-1")
    assert result == {"branch": "swarm-run-1", "base_branch": "main"}

    current = _git(["symbolic-ref", "--short", "HEAD"], host).stdout.strip()
    assert current == "swarm-run-1"

    run_id = ledger.conn.execute(
        "SELECT run_id FROM agents WHERE agent_id = ?", (oracle_id,)
    ).fetchone()["run_id"]
    run_row = ledger.conn.execute("SELECT branch FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert run_row["branch"] == "swarm-run-1"


# -- the agent_spawn gate -----------------------------------------------------------------------


def test_agent_spawn_refuses_a_manager_without_a_repo_check(host: Path) -> None:
    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.brief_create(
        "oracle",
        oracle_id,
        "manager-1",
        "manager",
        "opus",
        "Own phase-1.",
        phase_id=phase["phase_id"],
    )
    with pytest.raises(LedgerError, match="call repo_check first"):
        ledger.agent_spawn("oracle", oracle_id, "manager-1")


def test_agent_spawn_starts_a_manager_after_a_repo_check_even_when_not_obvious(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a git repo at all: repo_check still succeeds and its obvious_start is False.
    # agent_spawn only requires the check to have run, per 04-mcp-and-hooks.md.
    monkeypatch.setattr(sessions, "_run", _FakeClaude())
    agents_dir = host / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "swarm-manager.md").write_text(
        "---\nname: swarm-manager\ndescription: The manager.\ntools: Read\n---\n\n"
        "You are a manager.\n",
        encoding="utf-8",
    )
    server_json = host / ".sentinel-swarm" / "server.json"
    server_json.parent.mkdir(parents=True, exist_ok=True)
    server_json.write_text(
        json.dumps({"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}), encoding="utf-8"
    )

    ledger = _make_ledger(host)
    oracle_id = _bootstrap_oracle(ledger)
    check = ledger.repo_check("oracle", oracle_id)
    assert check["obvious_start"] is False

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    ledger.brief_create(
        "oracle",
        oracle_id,
        "manager-1",
        "manager",
        "opus",
        "Own phase-1.",
        phase_id=phase["phase_id"],
    )
    spawned = ledger.agent_spawn("oracle", oracle_id, "manager-1")
    assert spawned["role"] == "manager"
