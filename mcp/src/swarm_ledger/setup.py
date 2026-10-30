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

from . import testing
from .agentfiles import agent_file_path as role_file
from .agentfiles import driver_available
from .db import append_git_excludes, main_git_dir
from .frontmatter import FrontmatterError
from .frontmatter import split_document as _split_document
from .paths import config_override
from .permissions import bypass_disabled, start_mode
from .settings import SETTINGS_PATH, Settings, load_settings

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
EXCLUDE_LINES = (
    ".sentinel-swarm/",
    ".claude/agents/swarm-*.md",
    KG_SETTINGS_PATH.as_posix(),
    SETTINGS_PATH.as_posix(),
)
WORKTREE_SETTINGS: dict[str, Any] = {"worktree": {"bgIsolation": "none"}}
# Exact names: an Agent(<name>) deny blocks the Agent tool but not a `claude --agent` launch.
ROLE_AGENT_DENY = tuple(f"Agent(swarm-{role})" for role in ROLES)

_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_HOOK_EVENT_LINE = re.compile(r"^  ([A-Za-z]+):\s*$")
_HOOK_ITEM_LINE = re.compile(r"^    - ")
_LEDGER_HOOK = re.compile(r"hook\.py hook (\w+)")
_TOOLS_LINE = re.compile(r"^tools\s*:\s*\S")
WHOLE_LEDGER = "mcp__swarm-ledger"
RELOAD_NOTE = (
    "A session loads plugin tools when it starts, and setup cannot run /reload-plugins: "
    "after a fresh install, start a new session, or run /reload-plugins in this one, "
    "before /sentinel-swarm:run"
)

_COLLECTED = re.compile(r"(\d+) tests? collected")
_OUTPUT_TAIL_LINES = 15
_SKIP_REASON_FLAGS = re.compile(r"(^|\s)-r\S*[sa]")


Executor = Callable[[str, Path, int], testing.Executed]


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


def template_file(role: str) -> Path:
    return TEMPLATES_DIR / "agents" / f"{role}.md"


def split_document(text: str) -> tuple[str, str]:
    try:
        return _split_document(text)
    except FrontmatterError as exc:
        raise SetupError(str(exc)) from None


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


def strip_permission_mode(frontmatter: str) -> tuple[str, str | None]:
    frontmatter = _with_newline(frontmatter)
    block = key_blocks(frontmatter).get("permissionMode")
    if block is None:
        return frontmatter, None
    return frontmatter.replace(block, "", 1), block.partition(":")[2].strip() or "(empty)"


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


def write_text(path: Path, text: str) -> None:
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
            write_text(target, render_default(template))
            report.add(f"wrote {shown}")
            continue
        existing = target.read_text(encoding="utf-8")
        try:
            frontmatter, body = split_document(existing)
            frontmatter, removed = strip_ledger_hooks(frontmatter)
            frontmatter, mode = strip_permission_mode(frontmatter)
            start = _join_document(frontmatter, body) if removed or mode else existing
            merged, added, body_changed = merge_role_file(start, template)
        except SetupError as exc:
            report.add(f"left {shown} unchanged: {exc}")
            continue
        if merged == existing:
            report.add(f"unchanged {shown}")
            continue
        write_text(target, merged)
        changes = []
        if body_changed:
            changes.append("replaced the prompt body")
        if added:
            changes.append(f"added keys from the template: {', '.join(added)}")
        if removed:
            changes.append(f"removed the ledger command hooks the mod runs: {', '.join(removed)}")
        if mode:
            changes.append(
                f"removed permissionMode: {mode} (set permission_mode.{role} in "
                f"{SETTINGS_PATH.as_posix()} to choose a mode)"
            )
        report.add(f"updated {shown}, kept your frontmatter: {'; '.join(changes) or 'normalized'}")


def report_permission_modes(repo: Path, report: SetupReport) -> None:
    configured = load_settings(repo).permission_mode
    for role in ROLES:
        if not role_file(repo, role).is_file():
            continue
        start = start_mode(repo, role, configured.get(role))
        report.add(f"swarm-{role} starts in {start.mode}: {start.source}")
    if bypass_disabled(repo):
        report.add(
            "policy sets permissions.disableBypassPermissionsMode, so a role that would start "
            "in bypassPermissions starts in acceptEdits"
        )


def write_shim(repo: Path, report: SetupReport) -> None:
    target = repo / SHIM_PATH
    text = SHIM_TEMPLATE.read_text(encoding="utf-8")
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        report.add(f"unchanged {SHIM_PATH.as_posix()}")
        return
    verb = "updated" if target.exists() else "wrote"
    write_text(target, text)
    report.add(f"{verb} {SHIM_PATH.as_posix()}")


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = deep_merge(current, value)
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
    merged = deep_merge(data, WORKTREE_SETTINGS)
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
    write_text(path, json.dumps(merged, indent=2) + "\n")
    report.add(f"set worktree.bgIsolation to none and denied swarm roles as subagents in {shown}")


def quiet_codebase_kg_nudge(repo: Path, report: SetupReport) -> None:
    path = repo / KG_SETTINGS_PATH
    shown = KG_SETTINGS_PATH.as_posix()
    block = "---\npost_edit_nudge: false\n---\n"
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    if text is None:
        write_text(path, block)
    else:
        try:
            head, _ = split_document(text)
        except SetupError:
            head = None
        if head is None:
            write_text(path, block + text)
        elif "post_edit_nudge" in key_blocks(head):
            report.add(f"unchanged {shown}: post_edit_nudge is already set")
            return
        else:
            first = text.index("\n") + 1
            write_text(path, text[:first] + "post_edit_nudge: false\n" + text[first:])
    report.add(
        f"set post_edit_nudge: false in {shown} (the host's own sessions lose the nudge too)"
    )


def ensure_excludes(repo: Path, report: SetupReport) -> None:
    if not main_git_dir(repo).is_dir():
        report.add("not a git checkout: nothing added to .git/info/exclude")
        return
    missing = append_git_excludes(repo, EXCLUDE_LINES)
    if not missing:
        report.add("unchanged .git/info/exclude")
        return
    report.add(f"added to .git/info/exclude: {', '.join(missing)}")


def claude_json_path() -> Path:
    return (config_override() or Path.home()) / ".claude.json"


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


_DOC_GLOBS = ("*.md", ".claude/*.md", "docs/*.md", "docs/*/*.md")
_DOC_LIMIT = 60
_SCRIPT_COMMAND = re.compile(
    r"^(?:\$\s+)?(?P<interpreter>python3?|py -3|py|node|bash|sh|uv run python|uv run)\s+"
    r"(?P<run>(?P<script>[\w./\-]+\.(?:py|js|mjs|cjs|sh|ts))(?:\s+[^`|;&<>]*)?)$"
)


def doc_commands(repo: Path) -> list[dict[str, str]]:
    seen: set[str] = set()
    found: list[dict[str, str]] = []
    docs = sorted({p for pattern in _DOC_GLOBS for p in repo.glob(pattern) if p.is_file()})
    for doc in docs[:_DOC_LIMIT]:
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        candidates = re.findall(r"`([^`\n]+)`", text) + text.splitlines()
        for raw in candidates:
            match = _SCRIPT_COMMAND.match(raw.strip())
            if match is None or "test" in Path(match["script"]).name.lower():
                continue
            run = " ".join(match["run"].split())
            if run in seen or not (repo / match["script"]).is_file():
                continue
            seen.add(run)
            found.append(
                {
                    "name": Path(match["script"]).stem.replace("_", "-"),
                    "run": run,
                    "cwd": ".",
                    "interpreter": match["interpreter"],
                    "source": doc.relative_to(repo).as_posix(),
                }
            )
    return found


def propose_commands(repo: Path, report: SetupReport) -> None:
    known = {c["run"] for c in load_settings(repo).commands}
    for command in doc_commands(repo):
        if command["run"] in known:
            continue
        report.add(
            f"Proposed host command from {command['source']}: name: {command['name']}, "
            f"run: {command['run']}, cwd: {command['cwd']}, interpreter: "
            f"{command['interpreter']}. Add it to commands in {SETTINGS_PATH.as_posix()} "
            "when the swarm should run it."
        )


def _tail(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return "\n".join(f"    {line}" for line in lines[-_OUTPUT_TAIL_LINES:])


def _dry_run(
    repo: Path, command: str, timeout_s: int, report: SetupReport, execute: Executor
) -> None:
    dry = testing.collect_only_command(command)
    if dry is None:
        report.add(
            f"no collection dry run for {command!r}: only a pytest command has one; "
            "run setup with --time-full-run to try the command once"
        )
        return
    ran = execute(dry, repo, timeout_s)
    collected = _COLLECTED.search(ran.output)
    if ran.timed_out:
        report.add(f"warning: the collection dry run {dry!r} did not end within {timeout_s} s")
    elif ran.exit_code == 0 and collected:
        report.add(f"the full test command collects {collected.group(1)} tests: {dry}")
    else:
        report.add(
            f"warning: the collection dry run {dry!r} failed with exit code {ran.exit_code}; "
            "every full run fails the same way until the command or full_target changes:\n"
            + _tail(ran.output)
        )


def _timed_run(
    repo: Path, command: str, settings: Settings, report: SetupReport, execute: Executor
) -> None:
    cap = settings.test_timeout_s
    ran = execute(command, repo, cap)
    result = testing.result_of(command, ran, settings.allowed_skips)
    seconds = ran.duration_ms / 1000
    if ran.timed_out:
        report.add(
            f"warning: one full run did not end within test_timeout_s ({cap} s); shard the "
            "suite (pytest -n auto, from pytest-xdist) or raise test_timeout_s"
        )
        return
    report.add(
        f"one full run took {seconds:.0f} s of the {cap} s test_timeout_s: {result.passed} "
        f"passed, {result.failed} failed, {result.skipped} skipped, exit code {ran.exit_code}"
    )
    if seconds > cap / 2:
        report.add(
            f"warning: the full run takes more than half of test_timeout_s ({cap} s); shard the "
            "suite (pytest -n auto, from pytest-xdist) or raise test_timeout_s"
        )
    if result.skipped > result.allowed_skipped:
        report.add(
            f"warning: {result.skipped - result.allowed_skipped} skipped test(s) that "
            "allowed_skips does not name; a skip fails the run, so it blocks every handoff "
            "and phase review"
        )
    if not result.ok and result.reason:
        report.add(f"warning: the full run does not count as clean: {result.reason}")


def check_tests(
    repo: Path,
    report: SetupReport,
    time_full_run: bool = False,
    execute: Executor = testing.execute,
) -> None:
    settings = load_settings(repo)
    template = settings.test_template("full")
    if not template:
        report.add("test_command is not set: fill it in before a run")
        return
    target = settings.full_target
    if target is None and "{target}" in template:
        report.add(
            "warning: full_target is not set, so tests_run(scope='full') refuses: set it to the "
            "folder that holds the tests, such as tests"
        )
        return
    command = testing.build_command(template, target, settings.slow_tests)
    pytest = testing.is_pytest(command)
    _dry_run(repo, command, settings.test_timeout_s, report, execute)
    if settings.slow_tests and not pytest:
        report.add(
            "warning: slow_tests applies only to a pytest command (--deselect); this command "
            "runs them every time"
        )
    if not settings.allowed_skips:
        report.add(
            "note: allowed_skips is empty, so any skipped test fails its run and blocks every "
            "handoff and phase review; name a test the environment skips on purpose there"
        )
    elif pytest and not _SKIP_REASON_FLAGS.search(command):
        report.add(
            "warning: pytest prints skip reasons only with -rs or -ra; add one to the test "
            "command, or no skip can match allowed_skips"
        )
    if time_full_run:
        _timed_run(repo, command, settings, report, execute)
    else:
        report.add(
            "to time one full run against test_timeout_s, run setup again with --time-full-run"
        )


def run_setup(repo: Path, time_full_run: bool = False) -> SetupReport:
    repo = repo.resolve()
    report = SetupReport()
    write_role_files(repo, report)
    write_shim(repo, report)
    merge_settings_local(repo, report)
    quiet_codebase_kg_nudge(repo, report)
    ensure_excludes(repo, report)
    report_permission_modes(repo, report)
    check_tests(repo, report, time_full_run)
    propose_commands(repo, report)
    report.add(RELOAD_NOTE)
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
    parser.add_argument(
        "--time-full-run",
        action="store_true",
        help="run the full test command once and compare its time with test_timeout_s",
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
    print(run_setup(repo, args.time_full_run).text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
