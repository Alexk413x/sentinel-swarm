from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .db import _main_git_dir

ROLES = ("oracle", "manager", "lead", "coder")
PLUGIN_ID = "sentinel-swarm@sentinel-swarm"
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PLUGIN_ROOT / "templates"
SHIM_TEMPLATE = TEMPLATES_DIR / "hook_shim.py"
SHIM_PATH = Path(".sentinel-swarm") / "hook.py"
SETTINGS_LOCAL_PATH = Path(".claude") / "settings.local.json"
PROJECT_SETTINGS_PATH = Path(".claude") / "settings.json"
EXCLUDE_LINES = (".sentinel-swarm/", ".claude/agents/swarm-*.md")
WORKTREE_SETTINGS: dict[str, Any] = {"worktree": {"bgIsolation": "none"}}

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_HOOK_EVENT_LINE = re.compile(r"^  ([A-Za-z]+):\s*$")
_HOOK_ITEM_LINE = re.compile(r"^    - ")
_LEDGER_HOOK = re.compile(r"hook\.py hook (\w+)")


class SetupError(Exception):
    pass


@dataclass
class SetupReport:
    lines: list[str] = field(default_factory=list)
    trusted: bool = False
    trust_note: str = ""

    def add(self, line: str) -> None:
        self.lines.append(line)

    def text(self, with_trust: bool = True) -> str:
        lines = [*self.lines, self.trust_note] if with_trust and self.trust_note else self.lines
        return "\n".join(lines)


def role_file(repo: Path, role: str) -> Path:
    return repo / ".claude" / "agents" / f"swarm-{role}.md"


def template_file(role: str) -> Path:
    return TEMPLATES_DIR / "agents" / f"{role}.md"


def load_shim() -> ModuleType:
    # The shim holds the only copy of the registry lookup. It must stay a standalone,
    # standard-library file, so setup loads it from the template instead of importing a module.
    spec = importlib.util.spec_from_file_location("sentinel_swarm_hook_shim", SHIM_TEMPLATE)
    if spec is None or spec.loader is None:
        raise SetupError(f"cannot load {SHIM_TEMPLATE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_document(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SetupError("the file has no frontmatter block")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise SetupError("the frontmatter block has no closing ---")


def key_blocks(frontmatter: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in frontmatter.splitlines(keepends=True):
        match = _KEY_LINE.match(line)
        if match:
            current = str(match.group(1))
            blocks[current] = [line]
        elif current is not None:
            blocks[current].append(line)
    return {key: _with_newline("".join(lines)) for key, lines in blocks.items()}


def _with_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def _join_document(frontmatter: str, body: str) -> str:
    return f"---\n{_with_newline(frontmatter)}---\n{body}"


def _hook_entries(frontmatter: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    parent: str | None = None
    current: list[str] = []

    def flush() -> None:
        if parent is not None and current:
            text = "".join(current)
            match = _LEDGER_HOOK.search(text)
            if match:
                entries.append((parent, match.group(1), text))

    for line in key_blocks(frontmatter).get("hooks", "").splitlines(keepends=True)[1:]:
        event = _HOOK_EVENT_LINE.match(line)
        if event:
            flush()
            parent, current = event.group(1), []
        elif _HOOK_ITEM_LINE.match(line):
            flush()
            current = [line]
        elif current:
            current.append(line)
    flush()
    return entries


def add_missing_hooks(user_frontmatter: str, template_frontmatter: str) -> tuple[str, list[str]]:
    have = {event for _, event, _ in _hook_entries(user_frontmatter)}
    lines = _with_newline(user_frontmatter).splitlines(keepends=True)
    added: list[str] = []
    for parent, event, text in _hook_entries(template_frontmatter):
        if event in have:
            continue
        header = f"  {parent}:"
        at = next((i for i, line in enumerate(lines) if line.rstrip() == header), None)
        if at is None:
            hooks_at = next((i for i, line in enumerate(lines) if line.rstrip() == "hooks:"), None)
            if hooks_at is None:
                continue
            lines.insert(hooks_at + 1, header + "\n")
            at = hooks_at + 1
        lines.insert(at + 1, text)
        have.add(event)
        added.append(event)
    return "".join(lines), added


def merge_role_file(existing: str, template: str) -> tuple[str, list[str], bool]:
    user_frontmatter, user_body = split_document(existing)
    template_frontmatter, template_body = split_document(template)
    user_keys = key_blocks(user_frontmatter)
    template_keys = key_blocks(template_frontmatter)
    added = [key for key in template_keys if key not in user_keys]
    frontmatter = _with_newline(user_frontmatter) + "".join(template_keys[key] for key in added)
    if "hooks" in user_keys:
        frontmatter, hooks = add_missing_hooks(frontmatter, template_frontmatter)
        added += [f"hook {event}" for event in hooks]
    return _join_document(frontmatter, template_body), added, user_body != template_body


def _yaml_name(name: str) -> str:
    return name if _PLAIN_NAME.fullmatch(name) else json.dumps(name)


def python_command() -> str:
    # On Windows, python3 on PATH is often the Microsoft Store stub, which only prints a hint.
    if os.name == "nt":
        return "python"
    return "python3" if shutil.which("python3") else "python"


def shim_server_entry(plugin_id: str, server: str) -> str:
    args = json.dumps([SHIM_PATH.as_posix(), "mcp", plugin_id, server])
    return f"  - {_yaml_name(server)}:\n      command: {python_command()}\n      args: {args}\n"


def render_default(template: str, servers: list[tuple[str, str]]) -> str:
    template = template.replace("      command: python\n", f"      command: {python_command()}\n")
    if not servers:
        return template
    frontmatter, body = split_document(template)
    blocks = key_blocks(frontmatter)
    parsed = yaml.safe_load(frontmatter) or {}
    tools = [t.strip() for t in str(parsed.get("tools") or "").split(",") if t.strip()]
    for _, server in servers:
        tool = f"mcp__{server}"
        if tool not in tools:
            tools.append(tool)
    blocks["tools"] = f"tools: {', '.join(tools)}\n"
    entries = "".join(shim_server_entry(plugin_id, server) for plugin_id, server in servers)
    blocks["mcpServers"] = blocks.get("mcpServers", "mcpServers:\n") + entries
    return _join_document("".join(blocks.values()), body)


def _template_server_names(template: str) -> set[str]:
    frontmatter, _ = split_document(template)
    value = (yaml.safe_load(frontmatter) or {}).get("mcpServers")
    names: set[str] = set()
    if isinstance(value, dict):
        names.update(str(name) for name in value)
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                names.update(str(name) for name in entry)
            elif isinstance(entry, str):
                names.add(entry)
    return names


def project_plugin_servers(repo: Path, report: SetupReport) -> list[tuple[str, str]]:
    path = repo / PROJECT_SETTINGS_PATH
    if not path.is_file():
        return []
    try:
        enabled = json.loads(path.read_text(encoding="utf-8")).get("enabledPlugins") or {}
    except (OSError, ValueError, AttributeError) as exc:
        report.add(f"could not read {PROJECT_SETTINGS_PATH.as_posix()}: {exc}")
        return []
    if not isinstance(enabled, dict):
        return []
    shim = load_shim()
    found: list[tuple[str, str]] = []
    for plugin_id, on in enabled.items():
        if on is not True or plugin_id == PLUGIN_ID:
            continue
        try:
            install = shim.find_install(plugin_id, repo)
            servers = shim.plugin_servers(install)
        except shim.ShimError as exc:
            report.add(f"skipped the MCP servers of {plugin_id}: {exc}")
            continue
        found.extend((plugin_id, name) for name in servers)
    return found


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_role_files(repo: Path, report: SetupReport) -> None:
    servers: list[tuple[str, str]] | None = None
    for role in ROLES:
        target = role_file(repo, role)
        shown = target.relative_to(repo).as_posix()
        template = template_file(role).read_text(encoding="utf-8")
        if not target.is_file():
            if servers is None:
                servers = project_plugin_servers(repo, report)
            known = _template_server_names(template)
            extra = [(plugin, name) for plugin, name in servers if name not in known]
            _write_text(target, render_default(template, extra))
            report.add(f"wrote {shown}")
            if extra:
                names = ", ".join(f"{name} ({plugin})" for plugin, name in extra)
                report.add(f"  added project plugin MCP servers: {names}")
            continue
        existing = target.read_text(encoding="utf-8")
        try:
            merged, added, body_changed = merge_role_file(existing, template)
        except SetupError as exc:
            report.add(f"left {shown} unchanged: {exc}")
            continue
        if merged == existing:
            report.add(f"unchanged {shown}")
            continue
        _write_text(target, merged)
        changes = []
        if body_changed:
            changes.append("replaced the prompt body")
        if added:
            changes.append(f"added keys from the template: {', '.join(added)}")
        report.add(f"updated {shown}, kept your frontmatter: {'; '.join(changes) or 'normalized'}")


def write_shim(repo: Path, report: SetupReport) -> None:
    target = repo / SHIM_PATH
    text = SHIM_TEMPLATE.read_text(encoding="utf-8")
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        report.add(f"unchanged {SHIM_PATH.as_posix()}")
        return
    verb = "updated" if target.exists() else "wrote"
    _write_text(target, text)
    report.add(f"{verb} {SHIM_PATH.as_posix()}")


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def merge_settings_local(repo: Path, report: SetupReport) -> None:
    path = repo / SETTINGS_LOCAL_PATH
    shown = SETTINGS_LOCAL_PATH.as_posix()
    data: Any = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            report.add(f"left {shown} unchanged: it is not valid JSON ({exc})")
            return
        if not isinstance(data, dict):
            report.add(f"left {shown} unchanged: it is not a JSON object")
            return
    merged = _deep_merge(data, WORKTREE_SETTINGS)
    if path.is_file() and merged == data:
        report.add(f"unchanged {shown}")
        return
    _write_text(path, json.dumps(merged, indent=2) + "\n")
    report.add(f"set worktree.bgIsolation to none in {shown}")


def ensure_excludes(repo: Path, report: SetupReport) -> None:
    git_dir = _main_git_dir(repo)
    if not git_dir.is_dir():
        report.add("not a git checkout: nothing added to .git/info/exclude")
        return
    path = git_dir / "info" / "exclude"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in EXCLUDE_LINES if line not in present]
    if not missing:
        report.add("unchanged .git/info/exclude")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("".join(line + "\n" for line in missing))
    report.add(f"added to .git/info/exclude: {', '.join(missing)}")


def claude_json_path() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) / ".claude.json" if raw else Path.home() / ".claude.json"


def _path_key(raw: str) -> str:
    return os.path.normcase(os.path.normpath(raw))


def is_trusted(repo: Path) -> bool:
    try:
        data = json.loads(claude_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return False
    target = _path_key(str(repo.resolve()))
    return any(
        isinstance(entry, dict)
        and entry.get("hasTrustDialogAccepted") is True
        and _path_key(key) == target
        for key, entry in projects.items()
    )


def trust_instructions(repo: Path) -> str:
    return (
        f"{repo} is not trusted yet. Run this once, accept the trust prompt, then exit:\n"
        f'  cd "{repo}" && claude'
    )


def run_setup(repo: Path) -> SetupReport:
    repo = repo.resolve()
    report = SetupReport()
    write_role_files(repo, report)
    write_shim(repo, report)
    merge_settings_local(repo, report)
    ensure_excludes(repo, report)
    report.trusted = is_trusted(repo)
    report.trust_note = f"{repo} is trusted" if report.trusted else trust_instructions(repo)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarm_ledger.setup")
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument(
        "--check-trust",
        action="store_true",
        help="only check that the repo is trusted; exit 1 with the trust command when it is not",
    )
    args = parser.parse_args(argv)
    repo = (args.repo or Path.cwd()).resolve()
    if args.check_trust:
        if is_trusted(repo):
            return 0
        sys.stderr.write(trust_instructions(repo) + "\n")
        return 1
    if not repo.is_dir():
        sys.stderr.write(f"{repo} is not a folder\n")
        return 1
    print(run_setup(repo).text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
