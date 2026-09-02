#!/usr/bin/env bash
# S28 grid: p99 of rare-term top-10 under concurrent add/delete churn.
#
#   tools/churn_bench.sh <opensearch|scylla-cdc> [reps]
#
# Per repetition, per churn rate: start the paced churn stream, let it warm
# in, then measure the query cells for every query concurrency while the
# churn keeps running; stop the churn and gate it (achieved >= 95% of
# offered, zero errors) — a row whose churn fell behind is labeled by its
# offered rate and would be a lie, so its cells are set aside with it.
# Runs against the resident index (R0); rate 0 is measured with no churn
# process at all, as the baseline row.
#
# Caveat recorded here because it travels with the artifact: the churn
# writer and the query workers share the harness box. The churn achieved-vs-
# offered gate plus each cell's queue_p99 are the two signals that the
# harness itself stayed out of the numbers.
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${1:?usage: churn_bench.sh <opensearch|scylla-cdc> [reps]}"
REPS="${2:-5}"
CHURN_RATES="${CHURN_RATES:-0 500 1000 2000 4000 8000}"
QCONCS="${QCONCS:-8 16 32 64}"
OUT_DIR="${OUT_DIR:-data/churn}"
WARMIN="${WARMIN:-10}"
DURATION="${DURATION:-30}"
WARMUP="${WARMUP:-5}"
QUERIES="${QUERIES:-data/queries.json}"
CORPUS="${CORPUS:-data/corpus.jsonl}"
PYTHON="${PYTHON:-.venv/bin/python3}"

OS_URL="${OS_URL:-http://localhost:9200}"
SCYLLA_HOSTS="${SCYLLA_HOSTS:-127.0.0.1}"
SCYLLA_PORT="${SCYLLA_PORT:-9042}"

case "$ENGINE" in
  opensearch)
    CONFIG="opensearch-refresh3"
    CELL_CONN=(--engine opensearch --url "$OS_URL" --index wiki-articles)
    CHURN_CONN=(--engine opensearch --url "$OS_URL" --index wiki-articles) ;;
  scylla-cdc)
    CONFIG="scylla-cdc"
    CELL_CONN=(--engine scylladb --hosts "$SCYLLA_HOSTS" --port "$SCYLLA_PORT")
    CHURN_CONN=(--engine scylladb --hosts "$SCYLLA_HOSTS" --port "$SCYLLA_PORT") ;;
  *) echo "unknown engine: $ENGINE" >&2; exit 2 ;;
esac

mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-cells.log"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# The churn must outlive every query cell of its row; anything left over is
# harmless (it is stopped as soon as the row's cells finish).
row_duration() {
  local cells
  cells="$(echo $QCONCS | wc -w)"
  echo $(( WARMIN + cells * (DURATION + WARMUP + 5) + 60 ))
}

CHURN_PID=""
CHURN_OUT=""

churn_start() {
  local rate="$1" rep="$2"
  CHURN_OUT="$OUT_DIR/churn-$CONFIG-$rate-r$rep.jsonl"
  $PYTHON -m ftsbench.churn_load "${CHURN_CONN[@]}" \
    --corpus "$CORPUS" --rate "$rate" --duration "$(row_duration)" \
    --output "$CHURN_OUT" --label "S28 churn, $CONFIG, rate=$rate" \
    >/dev/null 2>&1 &
  CHURN_PID=$!
}

churn_stop_and_gate() {
  local rate="$1" rep="$2" verdict=0
  [[ -n "$CHURN_PID" ]] || return 0
  kill -TERM "$CHURN_PID" 2>/dev/null || true
  wait "$CHURN_PID" 2>/dev/null || true
  CHURN_PID=""
  $PYTHON - "$CHURN_OUT" <<'PYGATE' || verdict=1
import json, sys
summary = {}
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        r = json.loads(line)
        if r.get("record") == "churn_summary":
            summary = r
offered = summary.get("offered_ops_per_s", 0)
achieved = summary.get("achieved_ops_per_s", 0)
errors = summary.get("errors", 1)
print(f"churn offered={offered} achieved={achieved} errors={errors}")
sys.exit(0 if errors == 0 and achieved >= 0.95 * offered else 1)
PYGATE
  if [[ $verdict -ne 0 ]]; then
    log "CHURN ROW FAILED gate: rate=$rate rep=$rep — cells of this row set aside"
    echo "$(date -u +%FT%TZ) $CONFIG churn=$rate rep=$rep churn-gate" >> "$FAILURES"
    for f in "$OUT_DIR"/cell-"$CONFIG"-churn"$rate"-c*-r"$rep".jsonl; do
      [[ -f "$f" ]] && mv "$f" "$f.failed"
    done
  fi
}

run_cell() {
  local rate="$1" qconc="$2" rep="$3"
  local out="$OUT_DIR/cell-$CONFIG-churn$rate-c$qconc-r$rep.jsonl"
  local status=0
  $PYTHON -m ftsbench.cell_bench_mp --processes "${CELL_PROCESSES:-6}" "${CELL_CONN[@]}" \
    --queries "$QUERIES" --query-class rare_term --limit 10 \
    --concurrency "$qconc" --warmup "$WARMUP" --duration "$DURATION" \
    --rep "$rep" --cache-state warm --extra "churn_ops_per_s=$rate" \
    --label "S28, $CONFIG, churn=$rate qconc=$qconc" \
    --output "$out" || status=$?
  if [[ $status -ne 0 ]]; then
    log "CELL FAILED: churn=$rate qconc=$qconc rep=$rep"
    echo "$(date -u +%FT%TZ) $CONFIG churn=$rate qconc=$qconc rep=$rep rc=$status" >> "$FAILURES"
    [[ -f "$out" ]] && mv "$out" "$out.failed"
  fi
}

trap '[[ -n "$CHURN_PID" ]] && kill -TERM "$CHURN_PID" 2>/dev/null || true' EXIT

for rep in $(seq 1 "$REPS"); do
  for rate in $CHURN_RATES; do
    log "$CONFIG churn=$rate ops/s, rep $rep of $REPS"
    if [[ "$rate" != 0 ]]; then
      churn_start "$rate" "$rep"
      sleep "$WARMIN"
    fi
    for qconc in $QCONCS; do
      run_cell "$rate" "$qconc" "$rep"
    done
    [[ "$rate" != 0 ]] && churn_stop_and_gate "$rate" "$rep"
  done
done

if [[ -s "$FAILURES" ]]; then
  log "churn grid finished WITH FAILURES — see $FAILURES"
else
  log "churn grid done — $OUT_DIR"
fi
