#!/usr/bin/env bash
set -euo pipefail

# The suite runs against a copy without mcp/.venv: uv hard-links a venv's files from its
# cache, and plugin eval refuses a plugin that holds a hard-linked file.

root="$(cd "$(dirname "$0")/.." && pwd)"
win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
claude_bin="${CLAUDE_BIN:-claude}"

copy="${TMPDIR:-/tmp}/sentinel-swarm-evals-$(date +%s)"
mkdir -p "$copy"
(cd "$root" && tar cf - --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache --exclude=evals/results .claude-plugin assets skills hooks templates mcp evals) |
  (cd "$copy" && tar xf -)
# An eval run starts from an empty configuration, where the codebase-kg dependency is not
# installed, and a plugin with a missing dependency loads no skill.
python -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d.pop('dependencies', None); \
json.dump(d, open(p, 'w'), indent=2)" "$(win "$copy/.claude-plugin/plugin.json")"

status=0
results="$root/evals/results/$(date +%Y%m%dT%H%M%S)"
"$claude_bin" plugin eval "$(win "$copy")" --output-dir "$(win "$results")" "$@" || status=$?
rm -rf "$copy" 2>/dev/null || true
exit "$status"
