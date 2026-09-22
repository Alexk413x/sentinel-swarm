#!/usr/bin/env bash
set -euo pipefail

prompt="${1:-Create a hello world file.}"
root="$(cd "$(dirname "$0")/.." && pwd)"
run_dir="$root/runs/hello"
host="$run_dir/host"

kg_dir="${KG_PLUGIN_DIR:-$(ls -d "$HOME"/.claude/plugins/cache/codebase-kg/codebase-kg/[0-9]* | sort -V | tail -1)}"

win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

rm -rf "$run_dir"
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

# The transcript stays outside host/ so the swarm never sees it in git status or a Glob.
echo "$prompt" | claude -p --agent sentinel-swarm:oracle \
  --plugin-dir "$(win "$root")" --plugin-dir "$(win "$kg_dir")" \
  --permission-mode acceptEdits \
  --allowedTools "mcp__plugin_sentinel-swarm_swarm-ledger,mcp__plugin_codebase-kg_codebase-kg,Agent,Read,Grep,Glob,Write,Edit,MultiEdit,SendMessage,ToolSearch,Bash(python -m pytest:*),Bash(git status:*),Bash(git diff:*),Bash(git log:*)" \
  --output-format stream-json --verbose \
  > "$run_dir/transcript.jsonl" 2> "$run_dir/stderr.txt" || echo "claude exited with status $?" >&2

echo "Results: $run_dir"
echo "Report:  $host/.sentinel-swarm/report.md"
echo "Ledger:  $host/.sentinel-swarm/ledger.db"
