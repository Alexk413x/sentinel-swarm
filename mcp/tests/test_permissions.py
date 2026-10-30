from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

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


def test_an_unset_mode_inherits_and_passes_no_flag(tmp_path: Path) -> None:
    _user({"defaultMode": "bypassPermissions"})

    start = permissions.start_mode(tmp_path, "lead", None)

    assert start.mode == "bypassPermissions"
    assert start.flag is None
    assert start.source == "inherited from the user's settings.json"


def test_the_repo_settings_outrank_the_users(tmp_path: Path) -> None:
    _user({"defaultMode": "bypassPermissions"})
    _settings_file(tmp_path / ".claude" / "settings.json", {"defaultMode": "plan"})
    _settings_file(tmp_path / ".claude" / "settings.local.json", {"defaultMode": "acceptEdits"})

    assert permissions.inherited_mode(tmp_path) == (
        "acceptEdits",
        "inherited from .claude/settings.local.json",
    )


def test_managed_settings_outrank_every_other_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed-settings.json"
    _settings_file(managed, {"defaultMode": "default"})
    monkeypatch.setattr(permissions, "managed_settings_path", lambda platform=None: managed)
    _user({"defaultMode": "bypassPermissions"})

    assert permissions.inherited_mode(tmp_path)[0] == "default"


def test_the_coder_gets_accept_edits_over_an_inherited_default(tmp_path: Path) -> None:
    coder = permissions.start_mode(tmp_path, "coder", None)
    lead = permissions.start_mode(tmp_path, "lead", None)

    assert (coder.mode, coder.flag) == ("acceptEdits", "acceptEdits")
    assert (lead.mode, lead.flag) == ("default", None)


def test_a_configured_mode_is_passed_as_the_flag(tmp_path: Path) -> None:
    _user({"defaultMode": "bypassPermissions"})

    start = permissions.start_mode(tmp_path, "coder", "default")

    assert (start.mode, start.flag) == ("default", "default")
    assert start.source == "permission_mode.coder in sentinel-swarm.local.md"


def test_policy_that_disables_bypass_falls_back_to_accept_edits(tmp_path: Path) -> None:
    _user({"defaultMode": "bypassPermissions", "disableBypassPermissionsMode": "disable"})

    inherited = permissions.start_mode(tmp_path, "manager", None)
    configured = permissions.start_mode(tmp_path, "oracle", "bypassPermissions")

    assert (inherited.mode, inherited.flag) == ("acceptEdits", "acceptEdits")
    assert (configured.mode, configured.flag) == ("acceptEdits", "acceptEdits")
    assert "disableBypassPermissionsMode" in inherited.source


def test_the_setting_is_read_per_role_and_drops_unknown_modes(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "sentinel-swarm.local.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\npermission_mode:\n  oracle: bypassPermissions\n  lead:\n  coder: yolo\n---\n",
        encoding="utf-8",
    )

    assert load_settings(tmp_path).permission_mode == {"oracle": "bypassPermissions"}


def test_session_options_ignore_the_role_files_permission_mode(tmp_path: Path) -> None:
    _role_file(tmp_path, "lead", "permissionMode: plan\n")
    _user({"defaultMode": "bypassPermissions"})

    assert _flag(session_options(tmp_path, "lead", None, URL)) is None
    assert _flag(session_options(tmp_path, "lead", None, URL, permission_mode="plan")) == "plan"


def test_session_options_pass_the_coder_floor(tmp_path: Path) -> None:
    _role_file(tmp_path, "coder")

    assert _flag(session_options(tmp_path, "coder", None, URL)) == "acceptEdits"
