#!/usr/bin/env bash
set -euo pipefail

mode=interactive
case "${1:-}" in
  --headless | --bg | --results)
    mode="${1#--}"
    shift
    ;;
esac
prompt="${1:-Create hello.py. When it runs, it writes the text Hello, world! to hello_world.txt in the current folder.}"
root="$(cd "$(dirname "$0")/.." && pwd)"
run_dir="$root/runs/hello"
host="$run_dir/host"
claude_bin="${CLAUDE_BIN:-claude}"
export SENTINEL_SWARM_CLAUDE="${SENTINEL_SWARM_CLAUDE:-$claude_bin}"

kg_dir="${KG_PLUGIN_DIR:-$(ls -d "$HOME"/.claude/plugins/cache/codebase-kg/codebase-kg/[0-9]* | sort -V | tail -1)}"

win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

report_results() {
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
}

if [ "$mode" = results ]; then
  report_results
  exit 0
fi

# A background session cannot answer the trust prompt. Checking first also keeps the launcher
# from printing the trust command a second time after setup.
if [ "$mode" = bg ]; then
  mkdir -p "$host"
  uv run --quiet --project "$(win "$root/mcp")" \
    python -m swarm_ledger.setup --check-trust --repo "$(win "$host")" || exit 1
fi

# The run installs the plugin from a clean copy of its files. Installing from the repo would
# copy runs/ and mcp/.venv into the plugin cache.
# A fresh folder per run: an open session can hold files in an earlier copy.
for old in "${TMPDIR:-/tmp}"/sentinel-swarm-plugin*; do rm -rf "$old" 2>/dev/null || true; done
plugin_dir="${TMPDIR:-/tmp}/sentinel-swarm-plugin-$(date +%s)"
mkdir -p "$plugin_dir"
(cd "$root" && tar cf - --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache .claude-plugin .mcp.json skills hooks templates mcp) |
  (cd "$plugin_dir" && tar xf -)

# Empties the folder instead of deleting it: Windows refuses to delete a folder that a shell has open.
mkdir -p "$run_dir"
find "$run_dir" -mindepth 1 ! -path "$host" -delete
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

# This repo's own .mcp.json sits above host/, and its trust prompt stops a background session
# that nobody can answer. Setup merges its own keys into this file.
cat > .claude/settings.local.json <<'EOF'
{
  "disabledMcpjsonServers": ["swarm-ledger"]
}
EOF

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

# -b main: some machines default init.defaultBranch to something else, and repo_check's
# obvious_start needs a base branch it can resolve without a base_branch override.
git init -q -b main
git config core.autocrlf false

# A project-scope install, not --plugin-dir: the launcher does not resolve --plugin-dir. The
# installed copy lives in the plugin cache, outside host/, so Claude Code does not ask before a
# Coder's writes.
"$claude_bin" plugin marketplace add --scope project "$(win "$plugin_dir")" >/dev/null
"$claude_bin" plugin uninstall sentinel-swarm@sentinel-swarm --scope project >/dev/null 2>&1 || true
"$claude_bin" plugin install sentinel-swarm@sentinel-swarm --scope project -y >/dev/null
version=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" \
  "$(win "$plugin_dir/.claude-plugin/plugin.json")")
installed_mcp="$(win "$HOME/.claude/plugins/cache/sentinel-swarm/sentinel-swarm/$version/mcp")"
uv sync --quiet --project "$installed_mcp" --frozen --no-dev

swarm() {
  local module="$1"
  shift
  uv run --quiet --project "$installed_mcp" --frozen --no-dev \
    python -m "swarm_ledger.$module" --repo "$(win "$host")" "$@"
}
launch() { swarm launch "$@"; }

# Setup runs before the commit: it edits .claude/settings.local.json, and a tracked file that
# changes during the run shows up in the Coders' git status checks.
swarm setup

git add -A
git commit -q -m init

case "$mode" in
  headless)
    # The transcript stays outside host/ so the swarm never sees it in git status or a Glob.
    launch --headless --transcript "$(win "$run_dir/transcript.jsonl")" "$prompt" \
      2> "$run_dir/stderr.txt" || echo "the launcher exited with status $?" >&2
    report_results
    ;;
  bg)
    # The launcher refuses an untrusted repo and prints the one command that trusts it.
    launch --bg "$prompt"
    echo "When it finishes: bash scripts/smoke.sh --results"
    ;;
  *)
    launch "$prompt" || echo "the launcher exited with status $?" >&2
    report_results
    ;;
esac
