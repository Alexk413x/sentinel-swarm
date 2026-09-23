#!/usr/bin/env bash
set -euo pipefail

headless=false
if [ "${1:-}" = "--headless" ]; then
  headless=true
  shift
fi
prompt="${1:-Create hello.py. When it runs, it writes the text Hello, world! to hello_world.txt in the current folder.}"
root="$(cd "$(dirname "$0")/.." && pwd)"
run_dir="$root/runs/hello"
host="$run_dir/host"

kg_dir="${KG_PLUGIN_DIR:-$(ls -d "$HOME"/.claude/plugins/cache/codebase-kg/codebase-kg/[0-9]* | sort -V | tail -1)}"

win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# Claude Code asks before any write inside a loaded plugin's folder, and a background Coder
# cannot answer, so runs/ must not sit inside the plugin the run loads. Load a copy instead.
plugin_dir="${TMPDIR:-/tmp}/sentinel-swarm-plugin"
rm -rf "$plugin_dir"
mkdir -p "$plugin_dir"
(cd "$root" && tar cf - --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache .claude-plugin .mcp.json agents skills hooks templates mcp) |
  (cd "$plugin_dir" && tar xf -)
uv sync --quiet --project "$(win "$plugin_dir/mcp")" --frozen --no-dev

# Empties the folder instead of deleting it: Windows refuses to delete a folder that a shell has open.
mkdir -p "$run_dir"
find "$run_dir" -mindepth 1 -delete
mkdir -p "$host/.claude" "$host/knowledge"
cd "$host"

cat > README.md <<'EOF'
# hello-host

A scratch repo for the sentinel-swarm smoke test.
EOF

cat > pyproject.toml <<'EOF'
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF

sed 's|^test_command:.*|test_command: python -m pytest -q -p no:cacheprovider {target}|' \
  "$root/templates/sentinel-swarm.local.md.example" > .claude/sentinel-swarm.local.md

cat > "$run_dir/graph.json" <<EOF
{
  "codebase": "hello-host",
  "root": ".",
  "language": "python",
  "generated": "$(date +%F)",
  "covers": ["**/*.py"],
  "nodes": [
    {
      "id": "readme",
      "kind": "doc",
      "description": "Describes the scratch repo used for the sentinel-swarm smoke test.",
      "section": "DOCS",
      "anchors": ["README.md"],
      "edges": []
    }
  ]
}
EOF
uvx --quiet --from "$(win "$kg_dir/mcp")" codebase-kg-build "$(win "$run_dir/graph.json")" \
  -o "$(win "$host/knowledge/code_graph.db")"

git init -q
git config core.autocrlf false
git add -A
git commit -q -m init

claude_args=(
  --agent sentinel-swarm:oracle
  --plugin-dir "$(win "$plugin_dir")" --plugin-dir "$(win "$kg_dir")"
  --permission-mode acceptEdits --settings '{"permissions":{"defaultMode":"acceptEdits"}}'
  --allowedTools "mcp__plugin_sentinel-swarm_swarm-ledger,mcp__plugin_codebase-kg_codebase-kg,Agent,Read,Grep,Glob,Write,Edit,MultiEdit,SendMessage,ToolSearch,Bash(python -m pytest:*),Bash(git status:*),Bash(git diff:*),Bash(git log:*)"
)

if $headless; then
  # The transcript stays outside host/ so the swarm never sees it in git status or a Glob.
  echo "$prompt" | "${CLAUDE_BIN:-claude}" -p "${claude_args[@]}" \
    --output-format stream-json --verbose \
    > "$run_dir/transcript.jsonl" 2> "$run_dir/stderr.txt" || echo "claude exited with status $?" >&2
else
  "${CLAUDE_BIN:-claude}" "${claude_args[@]}" "$prompt" || echo "claude exited with status $?" >&2
fi

if [ -f "$host/hello.py" ]; then
  (cd "$host" && python hello.py) || echo "python hello.py exited with status $?" >&2
fi
if [ -f "$host/hello_world.txt" ]; then
  echo "hello_world.txt: $(cat "$host/hello_world.txt")"
else
  echo "hello_world.txt: missing"
fi

echo "Results: $run_dir"
echo "Report:  $host/.sentinel-swarm/report.md"
echo "Ledger:  $host/.sentinel-swarm/ledger.db"
