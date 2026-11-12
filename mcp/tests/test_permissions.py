from __future__ import annotations

import json
import os
from pathlib import Path

from swarm_ledger import permissions
from swarm_ledger.agentfiles import agent_file_path, session_options
from swarm_ledger.auth import ensure_token
from swarm_ledger.settings import load_settings

URL = "http://127.0.0.1:5000/mcp"


def _settings_file(path: Path, permissions_block: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": permissions_block}), encoding="utf-8")


def _user(block: dict) -> None:
    _settings_file(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json", block)


def _role_file(root: Path, role: str, extra: str = "") -> None:
    path = agent_file_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: swarm-{role}\n{extra}---\nbody\n", encoding="utf-8")
    ensure_token(root)


def _flag(options: list[str]) -> str | None:
    if "--permission-mode" not in options:
        return None
    return options[options.index("--permission-mode") + 1]


def test_an_unset_mode_starts_in_bypass_whatever_the_user_inherits(tmp_path: Path) -> None:
    _user({"defaultMode": "plan"})
    _settings_file(tmp_path / ".claude" / "settings.local.json", {"defaultMode": "default"})

    for role in ("oracle", "manager", "lead", "coder", "driver"):
        start = permissions.start_mode(tmp_path, role, None)

        assert (start.mode, start.flag) == ("bypassPermissions", "bypassPermissions")
        assert start.source == "the swarm's default for every role"


def test_an_unknown_configured_mode_reads_as_unset(tmp_path: Path) -> None:
    start = permissions.start_mode(tmp_path, "lead", "yolo")

    assert (start.mode, start.flag) == ("bypassPermissions", "bypassPermissions")


def test_manual_is_a_mode_a_role_may_be_set_to(tmp_path: Path) -> None:
    start = permissions.start_mode(tmp_path, "lead", "manual")

    assert (start.mode, start.flag) == ("manual", "manual")


def test_a_configured_mode_is_passed_as_the_flag(tmp_path: Path) -> None:
    _user({"defaultMode": "bypassPermissions"})

    start = permissions.start_mode(tmp_path, "coder", "default")

    assert (start.mode, start.flag) == ("default", "default")
    assert start.source == "permission_mode.coder in sentinel-swarm.local.md"


def test_policy_that_disables_bypass_falls_back_to_accept_edits(tmp_path: Path) -> None:
    _user({"disableBypassPermissionsMode": "disable"})

    unset = permissions.start_mode(tmp_path, "manager", None)
    configured = permissions.start_mode(tmp_path, "oracle", "bypassPermissions")
    other = permissions.start_mode(tmp_path, "lead", "plan")

    assert (unset.mode, unset.flag) == ("acceptEdits", "acceptEdits")
    assert (configured.mode, configured.flag) == ("acceptEdits", "acceptEdits")
    assert "disableBypassPermissionsMode" in unset.source
    assert (other.mode, other.flag) == ("plan", "plan")


def test_the_setting_is_read_per_role_and_drops_unknown_modes(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "sentinel-swarm.local.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\npermission_mode:\n  oracle: bypassPermissions\n  lead:\n  coder: yolo\n---\n",
        encoding="utf-8",
    )

    assert load_settings(tmp_path).permission_mode == {"oracle": "bypassPermissions"}
    path.write_text("---\npermission_mode:\n  lead: manual\n---\n", encoding="utf-8")
    assert load_settings(tmp_path).permission_mode == {"lead": "manual"}


def test_session_options_ignore_the_role_files_permission_mode(tmp_path: Path) -> None:
    _role_file(tmp_path, "lead", "permissionMode: plan\n")
    _user({"defaultMode": "default"})

    assert _flag(session_options(tmp_path, "lead", None, URL)) == "bypassPermissions"
    assert _flag(session_options(tmp_path, "lead", None, URL, permission_mode="plan")) == "plan"


def test_session_options_pass_bypass_for_every_role(tmp_path: Path) -> None:
    for role in ("oracle", "manager", "lead", "coder", "driver"):
        _role_file(tmp_path, role)

        assert _flag(session_options(tmp_path, role, None, URL)) == "bypassPermissions"
