from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROLES = ("oracle", "manager", "lead", "coder")
SKILLS = ("swarm-protocol", "run", "plan", "status", "resume", "setup")
KG_TOOLS = (
    "kg_search",
    "kg_node",
    "kg_neighborhood",
    "kg_find_by_kind",
    "kg_find_by_path",
    "kg_find_by_link",
    "kg_find_by_reference",
    "kg_parity_gaps",
    "kg_stats",
    "kg_validate",
)
ALL_ROLES = frozenset(ROLES)
HOOK_TABLE = (
    ("SessionStart", None, "session_start", ALL_ROLES),
    ("PreToolUse", "Agent", "pre_agent", ALL_ROLES),
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "pre_write", ALL_ROLES),
    ("PreToolUse", "Bash|PowerShell", "pre_shell", ALL_ROLES),
    ("PreToolUse", "mcp__swarm-ledger__.*", "pre_ledger", ALL_ROLES),
    ("PostToolUse", None, "post_any", ALL_ROLES),
    ("PostToolUse", "Bash|PowerShell", "post_shell", frozenset({"coder"})),
    ("PreCompact", None, "pre_compact", ALL_ROLES),
    ("Stop", None, "stop", ALL_ROLES),
    ("SessionEnd", None, "session_end", ALL_ROLES),
)


def _split(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    assert lines and lines[0].strip() == "---", f"{path} has no frontmatter block"
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fields = yaml.safe_load("".join(lines[1:end]))
    assert isinstance(fields, dict)
    return fields, "".join(lines[end + 1 :])


def _template(root: Path, role: str) -> Path:
    return root / "templates" / "agents" / f"{role}.md"


def _tools(fields: dict) -> list[str]:
    return [t.strip() for t in str(fields.get("tools", "")).split(",") if t.strip()]


def _hook_command(event: str) -> str:
    shim = ".sentinel-swarm/hook.py hook"
    return f"python3 {shim} {event} || python {shim} {event}"


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


def test_plugin_has_no_agents_folder(repo_root: Path):
    assert not (repo_root / "agents").exists()


def test_templates_are_exactly_the_four_roles(repo_root: Path):
    found = sorted(p.name for p in (repo_root / "templates" / "agents").iterdir())
    assert found == sorted(f"{role}.md" for role in ROLES)


@pytest.mark.parametrize("role", ROLES)
def test_template_name_and_description(repo_root: Path, role: str):
    fields, body = _split(_template(repo_root, role))
    assert fields["name"] == f"swarm-{role}"
    assert str(fields.get("description", "")).strip()
    assert body.strip()


@pytest.mark.parametrize(
    "role,color,model,mode",
    [
        ("oracle", "cyan", "fable", "default"),
        ("manager", "green", "opus", "default"),
        ("lead", "purple", "sonnet", "default"),
        ("coder", "orange", "sonnet", "acceptEdits"),
    ],
)
def test_template_color_model_and_permission_mode(
    repo_root: Path, role: str, color: str, model: str, mode: str
):
    fields, _ = _split(_template(repo_root, role))
    assert fields["color"] == color
    assert fields["model"] == model
    assert fields["permissionMode"] == mode


@pytest.mark.parametrize("role", ROLES)
def test_template_never_sets_max_turns(repo_root: Path, role: str):
    fields, _ = _split(_template(repo_root, role))
    assert "maxTurns" not in fields


@pytest.mark.parametrize("role", ROLES)
def test_template_tools(repo_root: Path, role: str):
    tools = _tools(_split(_template(repo_root, role))[0])
    assert "Agent" not in tools
    assert "ToolSearch" in tools
    assert "SendMessage" in tools
    assert "mcp__swarm-ledger" in tools
    for tool in KG_TOOLS:
        assert f"mcp__codebase-kg__{tool}" in tools
    assert not any(t.startswith("mcp__plugin_") for t in tools)


def test_only_coder_writes_edits_and_runs_a_shell(repo_root: Path):
    for role in ROLES:
        tools = _tools(_split(_template(repo_root, role))[0])
        for tool in ("Write", "Edit", "Bash"):
            assert (tool in tools) == (role == "coder"), f"{role} and {tool}"


@pytest.mark.parametrize("role", ROLES)
def test_template_mcp_servers_go_through_the_shim(repo_root: Path, role: str):
    fields, _ = _split(_template(repo_root, role))
    assert fields["mcpServers"] == [
        {
            "codebase-kg": {
                "command": "python",
                "args": [
                    ".sentinel-swarm/hook.py",
                    "mcp",
                    "codebase-kg@codebase-kg",
                    "codebase-kg",
                ],
            }
        }
    ]


@pytest.mark.parametrize("role", ROLES)
def test_template_hooks_match_the_spec_table(repo_root: Path, role: str):
    hooks = _split(_template(repo_root, role))[0]["hooks"]
    expected: dict[str, list[dict]] = {}
    for event, matcher, ledger_event, roles in HOOK_TABLE:
        if role not in roles:
            continue
        group: dict = {} if matcher is None else {"matcher": matcher}
        group["hooks"] = [
            {"type": "command", "command": _hook_command(ledger_event), "timeout": 60}
        ]
        expected.setdefault(event, []).append(group)
    assert hooks == expected


def test_hook_shim_template_exists(repo_root: Path):
    text = (repo_root / "templates" / "hook_shim.py").read_text(encoding="utf-8")
    assert "installed_plugins.json" in text
    imported = {
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize("name", SKILLS)
def test_skill_file_exists_with_name_and_description(repo_root: Path, name: str):
    fields, _ = _split(repo_root / "skills" / name / "SKILL.md")
    assert str(fields.get("name", "")).strip()
    assert str(fields.get("description", "")).strip()


@pytest.mark.parametrize("name", SKILLS)
def test_skills_do_not_name_subagents_or_old_prefixes(repo_root: Path, name: str):
    text = (repo_root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__plugin_" not in text
    assert "sentinel-swarm:oracle" not in text
    assert "subagent_type" not in text


def test_hooks_json_has_no_hooks(repo_root: Path):
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"] == {}


def _pyproject_version(repo_root: Path) -> str:
    text = (repo_root / "mcp" / "pyproject.toml").read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(text)["project"]["version"]
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "no version found in pyproject.toml"
    return match.group(1)


def test_versions_match(repo_root: Path):
    from swarm_ledger import __version__

    plugin = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert _pyproject_version(repo_root) == __version__
    assert plugin["version"] == __version__
