#!/usr/bin/env bash
# How much of OpenSearch's index-build rate is its indexing buffer?
#
# tantivy's memory_budget_per_thread is PER THREAD; Lucene's
# indices.memory.index_buffer_size is a NODE total shared by active shards. So
# "both at 64 MB" has two readings and both are measured here:
#   - total parity: tantivy 64 MB x 6 threads = 384 MB  -> OS at 384mb
#   - literal parity: OS at 64mb
# The default (~410 MB, 10% of a 4 GB heap) is the baseline every earlier
# OpenSearch run in this repo used.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/os-buffer}"
REPS="${REPS:-3}"
CONC="${CONC:-16}"
PYTHON="${PYTHON:-.venv/bin/python3}"
mkdir -p "$OUT_DIR"

# name|buffer
VARIANTS=(
  "OS1-default-410mb|"
  "OS2-384mb|384mb"
  "OS3-128mb|128mb"
  "OS4-64mb|64mb"
)

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
step() { local w="$1"; shift; if ! "$@" >/dev/null 2>&1; then log "step '$w' failed"; return 1; fi; }

run_point() {
  local name="$1" rep="$2"
  local series="$OUT_DIR/c1-$name-$rep.jsonl"
  # down -v, not down: the buffer is a node setting applied at startup, and a
  # surviving index would also carry the previous run's segments.
  make os-reset >/dev/null 2>&1 || true
  step "up" make os-up || return 1
  until curl -fsS "http://localhost:9200" >/dev/null 2>&1; do sleep 3; done
  step "watermarks" make os-relax-watermarks || return 1
  step "index" make os-index OS_REFRESH=3s || return 1

  local rc=0
  make c1-os \
    "C1_MAX_SECONDS=2400" "C1_IDLE_TIMEOUT=90" "OS_REFRESH=3s" \
    "OS_CONFIG=opensearch-refresh3" "INGEST_CONCURRENCY=$CONC" "BATCH_SIZE=500" \
    "REP=$rep" "CACHE_STATE=cold" "LABEL=$name buffer=${OS_INDEX_BUFFER:-default} conc=$CONC" \
    "C1_OS_SERIES=$series" "C1_OS_MANIFEST=$OUT_DIR/manifest-$name-$rep.json" \
    >"$OUT_DIR/load-$name-$rep.log" 2>&1 || rc=1

  curl -s "http://localhost:9200/wiki-articles/_stats/merges,segments,store" \
    > "$OUT_DIR/stats-$name-$rep.json" 2>/dev/null || true
  [ -s "$series" ] || return 1
  return $rc
}

for rep in $(seq 1 "$REPS"); do
  for spec in "${VARIANTS[@]}"; do
    IFS='|' read -r name buf <<<"$spec"
    export OS_INDEX_BUFFER="$buf"
    log "$name rep=$rep (indices.memory.index_buffer_size=${buf:-default ~410mb} conc=$CONC)"
    set +e; run_point "$name" "$rep"; rc=$?; set -e
    [ $rc -ne 0 ] && log "$name rep=$rep FAILED (rc=$rc)"
  done
done
log "OpenSearch buffer matrix complete — $OUT_DIR"
