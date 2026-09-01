#!/usr/bin/env bash
# Read-path cube driver: every (query class x top-N x concurrency) cell,
# closed-loop, N runs, one engine at a time (deck S18-S26; READ-PATH-TEST-PLAN.md).
#
#   tools/read_sweep.sh <opensearch|scylla-cdc> [runs]
#
# Runs are traversal-major: run 1 visits all cells, then run 2 repeats — an
# interrupted campaign leaves every cell at equal N, and host drift spreads
# across the whole cube instead of landing on one class.
#
# Resumable: a cell whose artifact already exists and is complete is skipped,
# so re-invoking after an interruption continues where it stopped. A cell that
# exits non-zero or reports errors>0 is renamed .failed and logged — the
# zero-errors claim on deck S17 is a per-cell gate here, not a hope.
#
# The engine stack is NOT managed here: the cube runs against a resident,
# fully built index (R0 in the plan) and tearing it down between cells would
# measure rebuilds. Bring the stack up and build the index first; the driver
# only verifies the doc count before starting and refuses to run otherwise.
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${1:?usage: read_sweep.sh <opensearch|scylla-cdc> [runs]}"
RUNS="${2:-9}"
CLASSES="${CLASSES:-rare_term common_term phrase bool_and bool_not bool_mixed}"
TOPNS="${TOPNS:-10 100 1000}"
LADDER="${LADDER:-2 4 8 16 32 64 128}"
OUT_DIR="${OUT_DIR:-data/readcube}"
WARMUP="${WARMUP:-5}"
DURATION="${DURATION:-20}"
QUERIES="${QUERIES:-data/queries.json}"
PYTHON="${PYTHON:-.venv/bin/python3}"
EXPECTED_DOCS="${EXPECTED_DOCS:-8967625}"

OS_URL="${OS_URL:-http://localhost:9200}"
VS_URL="${VS_URL:-http://localhost:16080}"
SCYLLA_HOSTS="${SCYLLA_HOSTS:-127.0.0.1}"
SCYLLA_PORT="${SCYLLA_PORT:-9042}"

case "$ENGINE" in
  opensearch)
    CONFIG="opensearch-refresh3"
    CONN=(--engine opensearch --url "$OS_URL" --index wiki-articles) ;;
  scylla-cdc)
    CONFIG="scylla-cdc"
    CONN=(--engine scylladb --hosts "$SCYLLA_HOSTS" --port "$SCYLLA_PORT") ;;
  *) echo "unknown engine: $ENGINE" >&2; exit 2 ;;
esac

mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-cells.log"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

assert_index_resident() {
  local count
  case "$ENGINE" in
    opensearch)
      count="$(curl -fsS "$OS_URL/wiki-articles/_count" \
        | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["count"])')" ;;
    scylla-cdc)
      count="$(curl -fsS "$VS_URL/api/v1/indexes/wiki/articles_body_fts/status" \
        | $PYTHON -c 'import json,sys; print(json.load(sys.stdin).get("count",-1))')" ;;
  esac
  [[ "$count" == "$EXPECTED_DOCS" ]] || {
    echo "index not resident: $count docs, expected $EXPECTED_DOCS — build it first (R0)" >&2
    exit 1; }
  echo "index resident: $count docs" >&2
}

cell_complete() {
  local file="$1"
  [[ -s "$file" ]] || return 1
  $PYTHON - "$file" <<'PYGATE'
import json, sys
ok = False
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        if r.get("record") == "cell_summary":
            ok = r.get("completed", 0) > 0 and r.get("errors", 0) == 0
print(f"complete={ok}")
sys.exit(0 if ok else 1)
PYGATE
}

run_cell() {
  local cls="$1" topn="$2" conc="$3" rep="$4"
  local out="$OUT_DIR/cell-$CONFIG-$cls-l$topn-c$conc-r$rep.jsonl"
  if [[ -f "$out" ]] && cell_complete "$out" >/dev/null 2>&1; then
    return 0
  fi
  local status=0
  $PYTHON -m ftsbench.cell_bench "${CONN[@]}" \
    --queries "$QUERIES" --query-class "$cls" --limit "$topn" \
    --concurrency "$conc" --warmup "$WARMUP" --duration "$DURATION" \
    --rep "$rep" --cache-state warm \
    --label "read cube, $CONFIG, $cls l=$topn c=$conc" \
    --output "$out" || status=$?
  if [[ $status -ne 0 ]] || ! cell_complete "$out" >/dev/null 2>&1; then
    log "CELL FAILED: $CONFIG $cls l=$topn c=$conc rep=$rep (rc=$status)"
    echo "$(date -u +%FT%TZ) $CONFIG $cls l=$topn c=$conc rep=$rep rc=$status" >> "$FAILURES"
    [[ -f "$out" ]] && mv "$out" "$out.failed"
  fi
}

assert_index_resident

for rep in $(seq 1 "$RUNS"); do
  log "$CONFIG cube traversal $rep of $RUNS"
  for cls in $CLASSES; do
    for topn in $TOPNS; do
      for conc in $LADDER; do
        run_cell "$cls" "$topn" "$conc" "$rep"
      done
    done
  done
done

if [[ -s "$FAILURES" ]]; then
  log "cube finished WITH FAILED CELLS — see $FAILURES"
else
  log "cube done — $OUT_DIR"
fi
