#!/usr/bin/env bash
set -euo pipefail

mode=interactive
case "${1:-}" in
  --headless | --bg | --results)
    mode="${1#--}"
    shift
    ;;
esac
root="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = --prd ]; then
  prd_file="$root/scripts/prds/${2:?--prd needs a name from scripts/prds}.md"
  [ -f "$prd_file" ] || { echo "no PRD at $prd_file" >&2; exit 1; }
  prompt="$(cat "$prd_file")"
  shift 2
elif [ -z "${1:-}" ] && [ -n "${SMOKE_PRD_FILE:-}" ]; then
  prompt="$(cat "$SMOKE_PRD_FILE")"
else
  prompt="${1:-Create hello.py. When it runs, it writes the text Hello, world! to hello_world.txt in the current folder.}"
fi
run_dir="$root/runs/hello"
host="$run_dir/host"
claude_bin="${CLAUDE_BIN:-claude}"
config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
export SENTINEL_SWARM_CLAUDE="${SENTINEL_SWARM_CLAUDE:-$claude_bin}"

kg_dir="${KG_PLUGIN_DIR:-$(ls -d "$HOME"/.claude/plugins/cache/codebase-kg/codebase-kg/[0-9]* | sort -V | tail -1)}"

win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# The venv the hooks use: in the plugin data folder, keyed by mcp/uv.lock. The installed
# copy has the same lock as this repo, so both share it.
ledger_venv="$(python "$(win "$root/mcp/ledger_venv.py")")"
ledger_uv() { UV_PROJECT_ENVIRONMENT="$ledger_venv" uv "$@"; }

marketplace_state() {
  python -c '
import json, os, sys
def load(name):
    try:
        with open(os.path.join(sys.argv[1], name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
source = (load("known_marketplaces.json").get("sentinel-swarm") or {}).get("source") or {}
path = str(source.get("path") or "").rstrip("/\\")
if source.get("source") != "directory" or not os.path.basename(path).startswith("sentinel-swarm-plugin-"):
    sys.exit()
installed = load("installed_plugins.json")
records = installed.get("plugins", installed).get("sentinel-swarm@sentinel-swarm") or []
users = [str(r.get("projectPath") or r.get("scope")) for r in records if isinstance(r, dict)]
print("kept " + ", ".join(users) if users else "remove")
' "$(win "$config_dir/plugins")"
}

run_live() {
  [ -f "$host/.sentinel-swarm/ledger.db" ] || return 1
  python -c '
import sqlite3, sys
try:
    conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
    row = conn.execute("SELECT ended_at FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
except sqlite3.Error:
    sys.exit(1)
sys.exit(0 if row is not None and row[0] is None else 1)
' "$(win "$host/.sentinel-swarm/ledger.db")"
}

# --keep-data: the ledger venv lives in the plugin data folder, which every smoke host and live
# session shares. For the same reason the marketplace goes only once no install uses it:
# removing it uninstalls every plugin from it and deletes their data.
teardown() {
  if [ -d "$host/.claude" ]; then
    (cd "$host" && "$claude_bin" plugin uninstall sentinel-swarm@sentinel-swarm --scope project \
      --keep-data) >/dev/null 2>&1 || true
  fi
  local state
  state="$(marketplace_state)"
  case "$state" in
    remove)
      (cd "$host" 2>/dev/null || cd "$root"
        "$claude_bin" plugin marketplace remove sentinel-swarm) >/dev/null 2>&1 || true
      if [ "$(marketplace_state)" = remove ]; then
        echo "teardown: could not remove the sentinel-swarm marketplace;" \
          "run: claude plugin marketplace remove sentinel-swarm" >&2
      fi
      ;;
    kept*) echo "teardown: the sentinel-swarm marketplace stays: ${state#kept } still uses it" >&2 ;;
  esac
}

# Uninstalling while the run goes on would break its sessions' hooks, which find the plugin
# through the install record.
teardown_after_run() {
  if run_live; then
    echo "The run is still going. When it ends: bash scripts/smoke.sh --results" >&2
  else
    teardown
  fi
}

report_results() {
  for script in hello.py hello_world.py; do
    if [ -f "$host/$script" ]; then
      (cd "$host" && python "$script") || echo "python $script exited with status $?" >&2
    fi
  done
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
  echo
  # || rc=$? instead of a bare call: set -e would otherwise stop the script here on a FAIL
  # check, skipping the exit below that turns it into this script's own exit code.
  rc=0
  ledger_uv run --quiet --project "$(win "$root/mcp")" --frozen --no-dev \
    python -m swarm_ledger.checklist --repo "$(win "$host")" || rc=$?
  teardown_after_run
  exit "$rc"
fi

# A background or headless session cannot answer the trust prompt, and -p does not count as
# accepting it. Checking first also keeps the launcher from printing the trust command twice.
if [ "$mode" = bg ] || [ "$mode" = headless ]; then
  mkdir -p "$host"
  ledger_uv run --quiet --project "$(win "$root/mcp")" --frozen --no-dev \
    python -m swarm_ledger.setup --check-trust --repo "$(win "$host")" || exit 1
fi

# An earlier run's install and marketplace go first: Windows empties the temp folder that the
# marketplace record points at.
teardown

# The run installs the plugin from a clean copy of its files. Installing from the repo would
# copy runs/ and mcp/.venv into the plugin cache.
# A fresh folder per run: an open session can hold files in an earlier copy.
for old in "${TMPDIR:-/tmp}"/sentinel-swarm-plugin*; do rm -rf "$old" 2>/dev/null || true; done
# Same reasoning for the dev copies this run's own install left in the plugin cache: skip
# one a live session still has open instead of forcing the removal.
for old in "$HOME/.claude/plugins/cache/sentinel-swarm/sentinel-swarm"/*-dev.*; do
  rm -rf "$old" 2>/dev/null || true
done
plugin_dir="${TMPDIR:-/tmp}/sentinel-swarm-plugin-$(date +%s)"
mkdir -p "$plugin_dir"
(cd "$root" && tar cf - --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache .claude-plugin assets skills hooks templates mcp) |
  (cd "$plugin_dir" && tar xf -)

# Installs under its own dev version instead of the repo's pinned version, so this run's
# install never replaces a cached copy another session still has open.
dev_version="0.0.1-dev.$(date +%s)"
python -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d['version']=sys.argv[2]; \
json.dump(d, open(p, 'w'), indent=2)" \
  "$(win "$plugin_dir/.claude-plugin/plugin.json")" "$dev_version"

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
"$claude_bin" plugin uninstall sentinel-swarm@sentinel-swarm --scope project --keep-data \
  >/dev/null 2>&1 || true
"$claude_bin" plugin install sentinel-swarm@sentinel-swarm --scope project -y >/dev/null
version=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" \
  "$(win "$plugin_dir/.claude-plugin/plugin.json")")
installed_mcp="$(win "$HOME/.claude/plugins/cache/sentinel-swarm/sentinel-swarm/$version/mcp")"
ledger_uv sync --quiet --project "$installed_mcp" --frozen --no-dev

swarm() {
  local module="$1"
  shift
  ledger_uv run --quiet --project "$installed_mcp" --frozen --no-dev \
    python -m "swarm_ledger.$module" --repo "$(win "$host")" "$@"
}
launch() { swarm launch "$@"; }

# Setup runs before the commit: it edits .claude/settings.local.json, and a tracked file that
# changes during the run shows up in the Coders' git status checks.
swarm setup
if [ -n "${SMOKE_SETTINGS:-}" ]; then
  ledger_uv run --quiet --project "$installed_mcp" --frozen --no-dev \
    python "$(win "$root/scripts/host_settings.py")" --repo "$(win "$host")" \
    --override "$(win "$SMOKE_SETTINGS")"
fi

git add -A
git commit -q -m init

case "$mode" in
  headless)
    # The transcript stays outside host/ so the swarm never sees it in git status or a Glob.
    launch --headless --transcript "$(win "$run_dir/transcript.jsonl")" "$prompt" \
      2> "$run_dir/stderr.txt" || echo "the launcher exited with status $?" >&2
    report_results
    teardown_after_run
    ;;
  bg)
    # The launcher refuses an untrusted repo and prints the one command that trusts it.
    launch --bg "$prompt"
    echo "When it finishes: bash scripts/smoke.sh --results"
    ;;
  *)
    launch "$prompt" || echo "the launcher exited with status $?" >&2
    report_results
    teardown_after_run
    ;;
esac
