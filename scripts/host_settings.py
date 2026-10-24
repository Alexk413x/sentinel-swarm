from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from swarm_ledger.frontmatter import FrontmatterError, split_document
from swarm_ledger.setup import deep_merge, write_text

SETTINGS = Path(".claude/sentinel-swarm.local.md")
ORACLE_FILE = Path(".claude/agents/swarm-oracle.md")


def split(text: str) -> tuple[str, str]:
    try:
        return split_document(text)
    except FrontmatterError:
        return "", text


def apply(repo: Path, override: dict[str, Any]) -> list[str]:
    settings_path = repo / SETTINGS
    frontmatter, body = split(settings_path.read_text(encoding="utf-8"))
    current = yaml.safe_load(frontmatter) or {}
    merged = deep_merge(current, override)
    dumped = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False)
    write_text(settings_path, f"---\n{dumped}---\n{body}")
    changed = [f"{SETTINGS.as_posix()}: {', '.join(sorted(override))}"]

    oracle_models = (override.get("models") or {}).get("oracle")
    if oracle_models:
        model = str(oracle_models[0] if isinstance(oracle_models, list) else oracle_models)
        oracle_path = repo / ORACLE_FILE
        head, rest = split(oracle_path.read_text(encoding="utf-8"))
        # The launcher starts the Oracle on its agent file's model before the approved list.
        head, count = re.subn(r"(?m)^model:.*$", f"model: {model}", head)
        if not count:
            head = f"{head}model: {model}\n"
        write_text(oracle_path, f"---\n{head}---\n{rest}")
        changed.append(f"{ORACLE_FILE.as_posix()}: model {model}")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge settings into a smoke host.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--override", type=Path, required=True, help="a YAML or JSON mapping")
    args = parser.parse_args(argv)
    override = yaml.safe_load(args.override.read_text(encoding="utf-8")) or {}
    if not isinstance(override, dict):
        parser.error(f"{args.override} does not hold a mapping")
    for line in apply(args.repo, override):
        print(f"settings override: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
