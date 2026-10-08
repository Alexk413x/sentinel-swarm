#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/bench.sh --prd <name|heldout/name> [--trials K] [--model-pin ROLE=MODEL]...
                             [--effort ROLE=LEVEL]... [--settings FILE] [--label NAME] [--dry-run]

Runs K headless smoke trials of one PRD in a row, grades each against its acceptance tests,
and writes trials.jsonl and summary.json to runs/bench/<timestamp>[-<label>]/.

  --prd NAME          hello, a dev PRD in scripts/prds/, or heldout/<name> from scripts/bench/heldout/
  --trials K          number of trials (default 1)
  --model-pin R=M     approve only model M for role R (oracle, manager, lead, coder, driver, or all)
  --effort R=L        run role R at effort L (low, medium, high, xhigh, max)
  --settings FILE     a JSON object merged into the host's .claude/sentinel-swarm.local.md
  --label NAME        a name for this variant, added to the output folder
  --dry-run           print the plan and the settings override, and run nothing
EOF
}

root="$(cd "$(dirname "$0")/.." && pwd)"
win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

prd="" trials=1 settings="" label="" dry=0
pins=() efforts=()
while [ $# -gt 0 ]; do
  case "$1" in
    --prd) prd="${2:?--prd needs a name}"; shift 2 ;;
    --trials) trials="${2:?--trials needs a number}"; shift 2 ;;
    --model-pin) pins+=(--model-pin "${2:?--model-pin needs role=model}"); shift 2 ;;
    --effort) efforts+=(--effort "${2:?--effort needs role=level}"); shift 2 ;;
    --settings) settings="${2:?--settings needs a file}"; shift 2 ;;
    --label) label="${2:?--label needs a name}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[ -n "$prd" ] || { echo "--prd is required" >&2; usage >&2; exit 2; }
case "$trials" in '' | *[!0-9]* | 0) echo "--trials takes a positive whole number" >&2; exit 2 ;; esac
case "$label" in *[!A-Za-z0-9._-]*) echo "--label takes letters, digits, '.', '_', '-'" >&2; exit 2 ;; esac
[ -z "$settings" ] || [ -f "$settings" ] || { echo "no settings file at $settings" >&2; exit 2; }

case "$prd" in
  hello)
    prd_file=""
    acceptance="$root/scripts/bench/acceptance/hello"
    ;;
  heldout/*)
    prd_file="$root/scripts/bench/$prd/prd.md"
    acceptance="$root/scripts/bench/$prd/acceptance"
    ;;
  *)
    prd_file="$root/scripts/prds/$prd.md"
    acceptance="$root/scripts/bench/acceptance/$prd"
    ;;
esac
[ -z "$prd_file" ] || [ -f "$prd_file" ] || { echo "no PRD at $prd_file" >&2; exit 2; }
[ -d "$acceptance" ] || { echo "no acceptance tests at $acceptance" >&2; exit 2; }

bench() { python "$(win "$root/scripts/bench/bench.py")" "$@"; }
override_args=(--prd "$prd" --label "$label" ${pins[@]+"${pins[@]}"} ${efforts[@]+"${efforts[@]}"})
[ -z "$settings" ] || override_args+=(--settings "$(win "$settings")")

if [ "$dry" = 1 ]; then
  echo "PRD:        ${prd_file:-the default hello prompt of smoke.sh}"
  echo "Acceptance: $acceptance"
  echo "Trials:     $trials"
  echo "Each trial: SMOKE_PRD_FILE=${prd_file:-<unset>} SMOKE_SETTINGS=<out>/override.json bash scripts/smoke.sh --headless"
  echo "Variant:"
  bench override "${override_args[@]}"
  exit 0
fi

out="$root/runs/bench/$(date +%Y%m%d-%H%M%S)${label:+-$label}"
mkdir -p "$out"
bench override "${override_args[@]}" --out "$(win "$out/override.json")" > "$out/variant.json"

host="$root/runs/hello/host"
for i in $(seq 1 "$trials"); do
  trial="$out/trial-$i"
  mkdir -p "$trial"
  echo "trial $i of $trials: $trial"
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  status=0
  SMOKE_PRD_FILE="$prd_file" SMOKE_SETTINGS="$out/override.json" \
    bash "$root/scripts/smoke.sh" --headless > "$trial/smoke.log" 2>&1 || status=$?
  [ "$status" = 0 ] || echo "smoke.sh exited with status $status" >> "$trial/smoke.log"
  # The ledger server exits a few seconds after the Oracle's session ends.
  for _ in $(seq 1 30); do
    [ -f "$host/.sentinel-swarm/server.json" ] || break
    sleep 2
  done
  for name in transcript.jsonl stderr.txt; do
    [ ! -f "$root/runs/hello/$name" ] || cp "$root/runs/hello/$name" "$trial/"
  done
  [ ! -f "$host/.sentinel-swarm/report.md" ] || cp "$host/.sentinel-swarm/report.md" "$trial/"
  python -I -S "$(win "$root/mcp/launch/ledger.py")" checklist --json --repo "$(win "$host")" \
    > "$trial/checklist.json" || true
  bench grade --host "$(win "$host")" --acceptance "$(win "$acceptance")" \
    --checklist "$(win "$trial/checklist.json")" --work "$(win "$trial")" \
    --case "$prd" --trial "$i" --label "$label" --since "$since" >> "$out/trials.jsonl"
done

bench summary --trials "$(win "$out/trials.jsonl")" --variant "$(win "$out/variant.json")" \
  --out "$(win "$out/summary.json")"
echo "Results: $out"
