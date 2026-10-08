from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROLES = ("oracle", "manager", "lead", "coder", "driver")
SKILLS = ("swarm-protocol", "run", "status", "resume", "setup")
KG_TOOLS = (
    "kg_search",
    "kg_node",
    "kg_neighborhood",
    "kg_find_by_kind",
    "kg_find_by_path",
    "kg_find_by_link",
    "kg_find_by_reference",
)
KG_CLI_TOOLS = ("kg_parity_gaps", "kg_stats", "kg_validate")
LEDGER_PREFIX = "mcp__swarm-ledger__"
LEDGER_MODULES = ("ledger", "review", "agreements", "oversight", "drive", "repo")
ALL_ROLES = frozenset(ROLES)
SYNC_POST_MATCHER = "SendMessage|PushNotification|Monitor|Write|Edit|MultiEdit|NotebookEdit"
ASYNC_EVENTS = frozenset({"post_activity"})
HOOK_TABLE = (
    ("SessionStart", None, "session_start", ALL_ROLES),
    ("PreToolUse", "Agent", "pre_agent", ALL_ROLES),
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "pre_write", ALL_ROLES),
    ("PreToolUse", "Bash|PowerShell", "pre_shell", ALL_ROLES),
    ("PreToolUse", "Monitor", "pre_monitor", ALL_ROLES),
    ("PreToolUse", "SendMessage", "pre_send_message", ALL_ROLES),
    ("PreToolUse", "Skill", "pre_skill", frozenset({"driver"})),
    ("PreToolUse", "mcp__swarm-ledger__.*", "pre_ledger", ALL_ROLES),
    ("PostToolUse", SYNC_POST_MATCHER, "post_any", ALL_ROLES),
    ("PostToolUse", None, "post_activity", ALL_ROLES),
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
    assert "a11y" not in names
    assert "optionalDependencies" not in data


def test_marketplace_manifest_parses(repo_root: Path):
    data = json.loads(
        (repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert data["name"] == "sentinel-swarm"
    assert data["plugins"][0]["name"] == "sentinel-swarm"
    assert data["plugins"][0]["source"] == "."
    assert "codebase-kg" in data["allowCrossMarketplaceDependenciesOn"]


def test_plugin_declares_no_mcp_server(repo_root: Path):
    assert not (repo_root / ".mcp.json").exists()
    manifest = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    assert "mcpServers" not in manifest


def test_plugin_has_no_agents_folder(repo_root: Path):
    assert not (repo_root / "agents").exists()


def test_templates_are_exactly_the_five_roles(repo_root: Path):
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
        ("oracle", "cyan", "opus", "default"),
        ("manager", "green", "opus", "default"),
        ("lead", "purple", "sonnet", "default"),
        ("coder", "orange", "sonnet", "acceptEdits"),
        ("driver", "yellow", "sonnet", "default"),
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
    # The Driver is the one role with an Agent tool, and only for cartographer's own
    # map-driver and map-reviewer subagents; pre_agent enforces that restriction.
    assert ("Agent" in tools) == (role == "driver")
    assert "Workflow" not in tools
    assert "ToolSearch" in tools
    assert "SendMessage" in tools
    assert "mcp__swarm-ledger" not in tools
    for tool in KG_TOOLS:
        assert f"mcp__codebase-kg__{tool}" in tools
    for tool in KG_CLI_TOOLS:
        assert f"mcp__codebase-kg__{tool}" not in tools
    assert not any(t.startswith("mcp__plugin_") for t in tools)
    assert not any("a11y" in t or "driver" in t for t in tools)


def _ledger_tools(names: list[str] | set[str]) -> set[str]:
    return {name[len(LEDGER_PREFIX) :] for name in names if name.startswith(LEDGER_PREFIX)}


def _required_roles() -> dict[str, set[str]]:
    import swarm_ledger
    from swarm_ledger.review import _SCOPE_ROLE
    from swarm_ledger.server import _TOOL_NAMES

    source = Path(swarm_ledger.__file__).parent
    required: dict[str, set[str]] = {}
    for module in LEDGER_MODULES:
        tree = ast.parse((source / f"{module}.py").read_text(encoding="utf-8"))
        for method in ast.walk(tree):
            if not isinstance(method, ast.FunctionDef) or method.name not in _TOOL_NAMES:
                continue
            checks = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "require_role"
            ]
            if not checks:
                continue
            roles = required.setdefault(method.name, set())
            for call in checks:
                for arg in call.args[1:]:
                    if isinstance(arg, ast.Constant):
                        roles.add(str(arg.value))
                    elif isinstance(arg, ast.Subscript) and ast.unparse(arg.value) == "_SCOPE_ROLE":
                        roles.update(_SCOPE_ROLE.values())
                    else:
                        raise AssertionError(f"unrecognized role check in {method.name}")
            roles.update(
                str(node.comparators[0].value)
                for node in ast.walk(method)
                if isinstance(node, ast.Compare)
                and ast.unparse(node.left).endswith(".role")
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)
            )
    return required


@pytest.mark.parametrize("role", ROLES)
def test_template_ledger_tools_are_the_roles_tool_set(repo_root: Path, role: str):
    from swarm_ledger.identity import ROLE_TOOLS

    tools = _tools(_split(_template(repo_root, role))[0])
    assert _ledger_tools(tools) == ROLE_TOOLS[role]
    assert "ledger_info" in ROLE_TOOLS[role]


@pytest.mark.parametrize("role", ROLES)
def test_template_body_calls_only_the_roles_ledger_tools(repo_root: Path, role: str):
    from swarm_ledger.identity import ROLE_TOOLS
    from swarm_ledger.server import _TOOL_NAMES

    _, body = _split(_template(repo_root, role))
    selects = _selects(body)
    assert selects, f"{role} has no ToolSearch working set"
    for selected in selects:
        assert _ledger_tools(selected) <= ROLE_TOOLS[role]
    called = {name for name in _TOOL_NAMES if re.search(rf"\b{name}\(", body)}
    assert called <= ROLE_TOOLS[role]


def _selects(body: str) -> list[set[str]]:
    return [set(m.split(",")) for m in re.findall(r'ToolSearch\(query="select:([^"]+)"', body)]


def _ledger_set(*names: str) -> set[str]:
    return {f"{LEDGER_PREFIX}{name}" for name in names}


SELECT_SETS = {
    "oracle": _ledger_set(
        "ledger_info",
        "run_start",
        "repo_check",
        "repo_branch_create",
        "profile_set",
        "guidelines_set",
        "phase_add",
        "phase_update",
        "brief_create",
        "agent_spawn",
        "directive_inbox",
        "status_tree",
        "phase_review",
        "tests_run",
        "run_finish",
    )
    | {"mcp__codebase-kg__kg_search", "SendMessage", "Monitor"},
    "manager": _ledger_set(
        "message_inbox",
        "module_add",
        "who_owns",
        "brief_create",
        "agent_spawn",
        "status_tree",
        "module_review",
        "tests_run",
        "issue_list",
    )
    | {"mcp__codebase-kg__kg_search", "SendMessage"},
    "lead": _ledger_set(
        "message_inbox",
        "claim_file",
        "brief_create",
        "agent_spawn",
        "score_record",
        "review_compare",
        "approve",
        "return_work",
        "tests_run",
        "issue_list",
    )
    | {"mcp__codebase-kg__kg_search", "SendMessage"},
    "coder": _ledger_set(
        "who_owns", "tests_run", "graph_upsert", "message_inbox", "issue_list", "cr_list"
    )
    | {"mcp__codebase-kg__kg_search", "SendMessage"},
    "driver": _ledger_set("message_inbox", "drive_checkin", "drive_issue") | {"SendMessage"},
}
END_SELECTS = {
    "coder": _ledger_set("score_record", "handoff_submit"),
    "driver": _ledger_set("drive_done"),
}
HOOK_START_CALLS = _ledger_set(
    "ledger_info", "brief_get", "brief_ack", "guidelines_get", "run_status"
)


@pytest.mark.parametrize("role", ROLES)
def test_template_up_front_select_is_the_expected_set(repo_root: Path, role: str):
    _, body = _split(_template(repo_root, role))
    selects = _selects(body)
    assert selects[0] == SELECT_SETS[role]
    max_results = re.search(r'ToolSearch\(query="select:[^"]+", max_results=(\d+)\)', body)
    assert max_results and int(max_results.group(1)) == len(SELECT_SETS[role])


@pytest.mark.parametrize("role", [r for r in ROLES if r != "oracle"])
def test_hook_start_calls_stay_out_of_the_up_front_select(repo_root: Path, role: str):
    _, body = _split(_template(repo_root, role))
    assert not _selects(body)[0] & HOOK_START_CALLS
    assert "SessionStart" in body
    assert "brief_ack(caller=<your name>)" not in body


@pytest.mark.parametrize("role", sorted(END_SELECTS))
def test_end_tools_load_in_their_own_select_before_the_first_call(repo_root: Path, role: str):
    _, body = _split(_template(repo_root, role))
    found = [
        m.start()
        for m in re.finditer(r'ToolSearch\(query="select:([^"]+)"', body)
        if set(m.group(1).split(",")) == END_SELECTS[role]
    ]
    assert len(found) == 1
    end_select = found[0]
    for tool in END_SELECTS[role]:
        name = tool.removeprefix(LEDGER_PREFIX)
        assert end_select < body.index(f"{name}("), f"{role} calls {name} before loading it"


def test_role_tool_sets_match_the_ledgers_role_checks():
    from swarm_ledger.identity import ROLE_TOOLS

    required = _required_roles()
    assert {"run_finish", "tests_run", "score_record", "drive_done"} <= set(required)
    for tool, roles in required.items():
        holders = {role for role in ROLES if tool in ROLE_TOOLS[role]}
        assert holders == roles, tool


def test_only_a_role_with_children_briefs_spawns_and_releases():
    from swarm_ledger.identity import ROLE_TOOLS, child_roles_of

    parents = {role for role in ROLES if child_roles_of(role)}
    for tool in ("brief_create", "agent_spawn", "agent_release"):
        assert {role for role in ROLES if tool in ROLE_TOOLS[role]} == parents, tool


def test_every_ledger_tool_belongs_to_a_role():
    from swarm_ledger.identity import ROLE_TOOLS
    from swarm_ledger.server import _TOOL_NAMES

    held = set().union(*ROLE_TOOLS.values())
    assert held == set(_TOOL_NAMES)


def test_only_coder_writes_and_edits(repo_root: Path):
    for role in ROLES:
        tools = _tools(_split(_template(repo_root, role))[0])
        for tool in ("Write", "Edit"):
            assert (tool in tools) == (role == "coder"), f"{role} and {tool}"


def test_only_the_coder_and_driver_run_a_shell(repo_root: Path):
    for role in ROLES:
        tools = _tools(_split(_template(repo_root, role))[0])
        assert ("Bash" in tools) == (role in ("coder", "driver")), role
        assert ("PowerShell" in tools) == (role in ("coder", "driver")), role


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
                    "codebase-kg@alexk413x",
                    "codebase-kg",
                ],
            }
        }
    ]


def test_the_sync_post_matcher_names_the_tools_post_activity_skips():
    from swarm_ledger.hooks.events import SYNC_POST_TOOLS

    assert SYNC_POST_MATCHER.split("|") == list(SYNC_POST_TOOLS)


def test_only_the_oracle_lists_monitor(repo_root: Path):
    for role in ROLES:
        tools = _tools(_split(_template(repo_root, role))[0])
        assert ("Monitor" in tools) == (role == "oracle"), role


def test_only_the_oracle_lists_push_notification(repo_root: Path):
    for role in ROLES:
        tools = _tools(_split(_template(repo_root, role))[0])
        assert ("PushNotification" in tools) == (role == "oracle"), role


def test_the_oracle_body_arms_the_exact_watchdog_call(repo_root: Path):
    from swarm_ledger.watchdog import MONITOR_CALL

    _, body = _split(_template(repo_root, "oracle"))
    assert MONITOR_CALL in body


def test_hook_shim_template_exists(repo_root: Path):
    text = (repo_root / "templates" / "hook_shim.py").read_text(encoding="utf-8")
    assert "installed_plugins.json" in text
    imported = {
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_nothing_shipped_names_the_removed_channel(repo_root: Path):
    shipped = [
        *(repo_root / "templates").rglob("*.md"),
        *(repo_root / "templates").rglob("*.example"),
        repo_root / "templates" / "hook_shim.py",
        *(repo_root / "skills").rglob("*.md"),
        *(repo_root / "hooks").iterdir(),
        *(repo_root / ".claude-plugin").glob("*.json"),
    ]
    for path in shipped:
        text = path.read_text(encoding="utf-8")
        for word in ("swarm-events", "wake_transport", "development channel"):
            assert word not in text, f"{path} names {word}"
    assert not (repo_root / "mcp" / "src" / "swarm_ledger" / "bridge.py").exists()


@pytest.mark.parametrize("name", SKILLS)
def test_skill_file_exists_with_name_and_description(repo_root: Path, name: str):
    fields, _ = _split(repo_root / "skills" / name / "SKILL.md")
    assert str(fields.get("name", "")).strip()
    assert str(fields.get("description", "")).strip()


@pytest.mark.parametrize("name", SKILLS)
def test_only_the_skills_that_start_sessions_are_user_invoked(repo_root: Path, name: str):
    fields, _ = _split(repo_root / "skills" / name / "SKILL.md")
    assert fields.get("disable-model-invocation", False) is (name in ("run", "resume"))


@pytest.mark.parametrize("name", SKILLS)
def test_skills_do_not_name_subagents_or_old_prefixes(repo_root: Path, name: str):
    text = (repo_root / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__plugin_" not in text
    assert "sentinel-swarm:oracle" not in text
    assert "subagent_type" not in text


def test_hooks_json_has_no_hooks(repo_root: Path):
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data["hooks"] == {}


def test_hooks_json_names_the_one_mod_module(repo_root: Path):
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data["modules"] == ["./register.ts"]
    assert (repo_root / "hooks" / "register.ts").is_file()
    plugin = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["types"] == "./types/index.d.ts"
    assert (repo_root / "types" / "index.d.ts").is_file()


def _mod_source(repo_root: Path) -> str:
    return (repo_root / "hooks" / "register.ts").read_text(encoding="utf-8")


def test_the_mod_runs_every_ledger_hook_event_of_the_table(repo_root: Path):
    source = _mod_source(repo_root)
    for _, _, ledger_event, _ in HOOK_TABLE:
        assert f"'{ledger_event}'" in source, ledger_event


def test_the_mod_sync_post_tools_match_the_server(repo_root: Path):
    from swarm_ledger.hooks.events import SYNC_POST_TOOLS

    source = _mod_source(repo_root)
    assert "const WRITE_TOOLS = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit']" in source
    assert "['SendMessage', 'PushNotification', 'Monitor', ...WRITE_TOOLS]" in source
    assert list(SYNC_POST_TOOLS) == [
        "SendMessage",
        "PushNotification",
        "Monitor",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
    ]


def test_the_mod_posts_only_events_the_server_handles(repo_root: Path):
    from swarm_ledger.hooks import HANDLERS

    posted = set(re.findall(r"(?:runHook|post)\(\$, s, '(\w+)'", _mod_source(repo_root)))
    posted |= set(re.findall(r"judge\(\$, e, next, '(\w+)'", _mod_source(repo_root)))
    assert posted
    assert posted <= set(HANDLERS)


@pytest.mark.parametrize("role", ROLES)
def test_a_role_template_has_no_hooks(role: str):
    from swarm_ledger import setup

    head, _ = setup.split_document(setup.role_template(role))
    fields = yaml.safe_load(head)
    assert "hooks" not in fields
    assert fields["name"] == f"swarm-{role}"
    assert "mcpServers" in fields


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


def test_user_config_test_command_schema(repo_root: Path) -> None:
    data = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    option = data["userConfig"]["test_command"]
    allowed = {
        "type",
        "title",
        "description",
        "required",
        "default",
        "options",
        "multiple",
        "sensitive",
        "min",
        "max",
    }
    assert set(option) <= allowed
    assert option["type"] == "string"
    assert str(option["title"]).strip()
    assert str(option["description"]).strip()


def test_setup_skill_reads_the_user_config_option(repo_root: Path) -> None:
    text = (repo_root / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "${user_config.test_command}" in text


@pytest.mark.parametrize(
    ("role", "rule"),
    [
        ("driver", "Fixes start while you explore"),
        ("oracle", "Fixes start while the Driver explores"),
    ],
)
def test_the_role_prompts_let_fixes_run_while_the_driver_explores(
    repo_root: Path, role: str, rule: str
) -> None:
    text = (repo_root / "templates" / "agents" / f"{role}.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "while no Coder is editing" not in flat
    assert "never during yours" not in flat
    assert rule in flat
