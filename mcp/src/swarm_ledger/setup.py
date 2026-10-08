from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agentfiles import driver_available
from .db import _main_git_dir

CORE_ROLES = ("oracle", "manager", "lead", "coder")
ROLES = (*CORE_ROLES, "driver")
# A role in here is written only when its check passes; setup reports a skip otherwise.
_ROLE_GATES: dict[str, Callable[[Path], bool]] = {"driver": driver_available}
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = PLUGIN_ROOT / "templates"
SHIM_TEMPLATE = TEMPLATES_DIR / "hook_shim.py"
SHIM_PATH = Path(".sentinel-swarm") / "hook.py"
SETTINGS_LOCAL_PATH = Path(".claude") / "settings.local.json"
KG_SETTINGS_PATH = Path(".claude") / "codebase-kg.local.md"
EXCLUDE_LINES = (".sentinel-swarm/", ".claude/agents/swarm-*.md", KG_SETTINGS_PATH.as_posix())
WORKTREE_SETTINGS: dict[str, Any] = {"worktree": {"bgIsolation": "none"}}
# Exact names: an Agent(<name>) deny blocks the Agent tool but not a `claude --agent` launch.
ROLE_AGENT_DENY = tuple(f"Agent(swarm-{role})" for role in ROLES)

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_HOOK_EVENT_LINE = re.compile(r"^  ([A-Za-z]+):\s*$")
_HOOK_ITEM_LINE = re.compile(r"^    - ")
_LEDGER_HOOK = re.compile(r"hook\.py hook (\w+)")
_TOOLS_LINE = re.compile(r"^tools\s*:\s*\S")
WHOLE_LEDGER = "mcp__swarm-ledger"


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


def _tool_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def narrow_ledger_tools(user_frontmatter: str, template_frontmatter: str) -> tuple[str, bool]:
    template_tools = key_blocks(template_frontmatter).get("tools", "").partition(":")[2]
    ledger = [t for t in _tool_items(template_tools) if t.startswith(WHOLE_LEDGER + "__")]
    lines = user_frontmatter.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not _TOOLS_LINE.match(line):
            continue
        items = _tool_items(line.partition(":")[2])
        if WHOLE_LEDGER not in items or not ledger:
            return user_frontmatter, False
        at = items.index(WHOLE_LEDGER)
        items[at : at + 1] = [tool for tool in ledger if tool not in items]
        lines[index] = "tools: " + ", ".join(items) + ("\n" if line.endswith("\n") else "")
        return "".join(lines), True
    return user_frontmatter, False


def strip_ledger_hooks(frontmatter: str) -> tuple[str, list[str]]:
    frontmatter = _with_newline(frontmatter)
    block = key_blocks(frontmatter).get("hooks")
    if block is None:
        return frontmatter, []
    lines = block.splitlines(keepends=True)
    loose: list[str] = []
    events: list[tuple[str, list[list[str]]]] = []
    for line in lines[1:]:
        if _HOOK_EVENT_LINE.match(line):
            events.append((line, []))
        elif _HOOK_ITEM_LINE.match(line) and events:
            events[-1][1].append([line])
        elif events and events[-1][1]:
            events[-1][1][-1].append(line)
        else:
            loose.append(line)
    removed: list[str] = []
    kept: list[str] = []
    for header, entries in events:
        texts = []
        for entry in entries:
            text = "".join(entry)
            match = _LEDGER_HOOK.search(text)
            if match:
                removed.append(match.group(1))
            else:
                texts.append(text)
        if texts:
            kept.append(header + "".join(texts))
    if not removed:
        return frontmatter, []
    rest = "".join(loose) + "".join(kept)
    return frontmatter.replace(block, lines[0] + rest if rest.strip() else "", 1), removed


def role_template(role: str) -> str:
    return template_file(role).read_text(encoding="utf-8")


def merge_role_file(existing: str, template: str) -> tuple[str, list[str], bool]:
    user_frontmatter, user_body = split_document(existing)
    template_frontmatter, template_body = split_document(template)
    user_frontmatter, narrowed = narrow_ledger_tools(user_frontmatter, template_frontmatter)
    user_keys = key_blocks(user_frontmatter)
    template_keys = key_blocks(template_frontmatter)
    added = [key for key in template_keys if key not in user_keys]
    frontmatter = _with_newline(user_frontmatter) + "".join(template_keys[key] for key in added)
    if narrowed:
        added.append(f"the role's swarm-ledger tools in place of {WHOLE_LEDGER}")
    return _join_document(frontmatter, template_body), added, user_body != template_body


def python_command() -> str:
    # On Windows, python3 on PATH is often the Microsoft Store stub, which only prints a hint.
    if os.name == "nt":
        return "python"
    return "python3" if shutil.which("python3") else "python"


def render_default(template: str) -> str:
    return template.replace("      command: python\n", f"      command: {python_command()}\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)


def write_role_files(repo: Path, report: SetupReport) -> None:
    for role in ROLES:
        target = role_file(repo, role)
        shown = target.relative_to(repo).as_posix()
        gate = _ROLE_GATES.get(role)
        if gate is not None and not gate(repo):
            report.add(
                f"skipped {shown}: cartographer and a driver plugin (android-driver, "
                "ios-driver, or web-driver) are not installed"
            )
            continue
        template = role_template(role)
        if not target.is_file():
            _write_text(target, render_default(template))
            report.add(f"wrote {shown}")
            continue
        existing = target.read_text(encoding="utf-8")
        try:
            frontmatter, body = split_document(existing)
            frontmatter, removed = strip_ledger_hooks(frontmatter)
            start = _join_document(frontmatter, body) if removed else existing
            merged, added, body_changed = merge_role_file(start, template)
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
        if removed:
            changes.append(f"removed the ledger command hooks the mod runs: {', '.join(removed)}")
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
    permissions = merged.get("permissions", {})
    deny = permissions.get("deny", []) if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        report.add(f"left {shown} unchanged: its permissions.deny is not a list")
        return
    merged["permissions"] = {
        **permissions,
        "deny": [*deny, *(rule for rule in ROLE_AGENT_DENY if rule not in deny)],
    }
    if path.is_file() and merged == data:
        report.add(f"unchanged {shown}")
        return
    _write_text(path, json.dumps(merged, indent=2) + "\n")
    report.add(f"set worktree.bgIsolation to none and denied swarm roles as subagents in {shown}")


def quiet_codebase_kg_nudge(repo: Path, report: SetupReport) -> None:
    path = repo / KG_SETTINGS_PATH
    shown = KG_SETTINGS_PATH.as_posix()
    block = "---\npost_edit_nudge: false\n---\n"
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    if text is None:
        _write_text(path, block)
    else:
        try:
            head, _ = split_document(text)
        except SetupError:
            head = None
        if head is None:
            _write_text(path, block + text)
        elif "post_edit_nudge" in key_blocks(head):
            report.add(f"unchanged {shown}: post_edit_nudge is already set")
            return
        else:
            first = text.index("\n") + 1
            _write_text(path, text[:first] + "post_edit_nudge: false\n" + text[first:])
    report.add(
        f"set post_edit_nudge: false in {shown} (the host's own sessions lose the nudge too)"
    )


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
    quiet_codebase_kg_nudge(repo, report)
    ensure_excludes(repo, report)
    report.trusted = is_trusted(repo)
    report.trust_note = f"{repo} is trusted" if report.trusted else trust_instructions(repo)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py setup")
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
