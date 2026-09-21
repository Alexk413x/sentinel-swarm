from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

AGENTS = ("oracle", "manager", "lead", "coder")
SKILLS = ("swarm-protocol", "run", "plan", "status", "resume", "setup")
FORBIDDEN_KEYS = ("maxTurns", "hooks", "mcpServers", "permissionMode")


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip() == "---"]
    assert len(markers) >= 2, f"{path} has no frontmatter block"
    start, end = markers[0], markers[1]
    fields: dict[str, str] = {}
    for line in lines[start + 1 : end]:
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _tools(fields: dict[str, str]) -> list[str]:
    raw = fields.get("tools", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _agent_path(root: Path, name: str) -> Path:
    return root / "agents" / f"{name}.md"


def test_plugin_manifest_parses(repo_root: Path):
    data = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "sentinel-swarm"
    assert "version" in data
    deps = data["dependencies"]
    names = [d if isinstance(d, str) else d.get("name") for d in deps]
    assert "codebase-kg" in names


def test_marketplace_manifest_parses(repo_root: Path):
    data = json.loads(
        (repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert data["name"] == "sentinel-swarm"
    assert data["plugins"][0]["name"] == "sentinel-swarm"
    assert data["plugins"][0]["source"] == "."
    assert "codebase-kg" in data["allowCrossMarketplaceDependenciesOn"]


def test_mcp_json_parses(repo_root: Path):
    data = json.loads((repo_root / ".mcp.json").read_text(encoding="utf-8"))
    server = data["mcpServers"]["swarm-ledger"]
    assert server["command"] == "uv"
    for expected in ("${CLAUDE_PLUGIN_ROOT}/mcp", "--frozen", "--no-dev", "swarm-ledger"):
        assert expected in server["args"]


@pytest.mark.parametrize("name", AGENTS)
def test_agent_file_exists_with_name_and_description(repo_root: Path, name: str):
    path = _agent_path(repo_root, name)
    assert path.is_file()
    fields = _frontmatter(path)
    assert fields["name"] == name
    assert fields.get("description", "").strip()


@pytest.mark.parametrize(
    "name,color",
    [("oracle", "cyan"), ("manager", "green"), ("lead", "purple"), ("coder", "orange")],
)
def test_agent_color(repo_root: Path, name: str, color: str):
    fields = _frontmatter(_agent_path(repo_root, name))
    assert fields.get("color") == color


@pytest.mark.parametrize(
    "name,model",
    [("oracle", "fable"), ("manager", "opus"), ("lead", "sonnet"), ("coder", "sonnet")],
)
def test_agent_model(repo_root: Path, name: str, model: str):
    fields = _frontmatter(_agent_path(repo_root, name))
    assert fields.get("model") == model


@pytest.mark.parametrize("name", AGENTS)
def test_agent_frontmatter_has_no_forbidden_keys(repo_root: Path, name: str):
    fields = _frontmatter(_agent_path(repo_root, name))
    for key in FORBIDDEN_KEYS:
        assert key not in fields


def test_only_coder_writes_and_edits(repo_root: Path):
    coder_tools = _tools(_frontmatter(_agent_path(repo_root, "coder")))
    assert "Write" in coder_tools
    assert "Edit" in coder_tools
    assert "Agent" not in coder_tools

    for name in ("oracle", "manager", "lead"):
        tools = _tools(_frontmatter(_agent_path(repo_root, name)))
        assert "Agent" in tools
        for forbidden in ("Bash", "PowerShell", "Write", "Edit"):
            assert forbidden not in tools


@pytest.mark.parametrize("name", SKILLS)
def test_skill_file_exists_with_name_and_description(repo_root: Path, name: str):
    path = repo_root / "skills" / name / "SKILL.md"
    assert path.is_file()
    fields = _frontmatter(path)
    assert fields.get("name", "").strip()
    assert fields.get("description", "").strip()


def test_hooks_json_parses(repo_root: Path):
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert isinstance(data["hooks"], dict)


def test_mcp_pyproject_version_matches_package_version(repo_root: Path):
    from swarm_ledger import __version__

    text = (repo_root / "mcp" / "pyproject.toml").read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        version = tomllib.loads(text)["project"]["version"]
    else:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        assert match, "no version found in pyproject.toml"
        version = match.group(1)
    assert version == __version__
