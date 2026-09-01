#!/usr/bin/env bash
# Does tantivy's per-thread writer buffer or its merge-thread count move the
# ~5,300 docs/s index ceiling — and how much of the OpenSearch build-rate lead
# is configuration rather than engine?
#
# The vector-store builds its IndexWriter with only num_worker_threads set, so
# memory_budget_per_thread takes tantivy's *minimum* (15 MB) and num_merge_threads
# its fixed default (4). OpenSearch's equivalents on the same box:
# indices.memory.index_buffer_size = 10% of a 4 GB heap (~410 MB for one shard,
# floor 48 MB), merge scheduler max_thread_count = 2 with auto_throttle on and a
# 16 MB floor_segment. M8 mirrors that shape.
#
# Container memory is held at 8g / 6 GiB for every variant including the
# baseline, so a large buffer is never competing with the limit.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/writer-memory}"
REPS="${REPS:-3}"
CONC="${CONC:-32}"
PYTHON="${PYTHON:-.venv/bin/python3}"
mkdir -p "$OUT_DIR"

export VS_FTS_COMMIT_INTERVAL=3s VS_FTS_COMMIT_THRESHOLD=0 VS_FTS_ADD_LOCK=shared
export VS_FTS_METRICS_INTERVAL=5s
export VS_CDC_FINE_SLEEP="" VS_CDC_FINE_SAFETY="" VS_CDC_SLEEP="" VS_CDC_SAFETY=""
export VS_CPUS=6

# name|writer_mb|merge_threads|vs_cpus
VARIANTS=(
  "M1-stock-15mb-4merge|15|4|6"
  "M2-64mb-4merge|64|4|6"
  "M3-128mb-4merge|128|4|6"
  "M4-256mb-4merge|256|4|6"
  "M5-128mb-2merge|128|2|6"
  "M6-128mb-8merge|128|8|6"
  "M7-128mb-12merge|128|12|12"
  "M8-opensearch-shape|68|2|6"
)

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
step() { local w="$1"; shift; if ! "$@" >/dev/null 2>&1; then log "step '$w' failed"; return 1; fi; }

run_point() {
  local name="$1" rep="$2"
  local series="$OUT_DIR/c1-scylla-cdc-$name-$rep.jsonl"
  make scylla-reset >/dev/null 2>&1 || true
  step "up" make scylla-up || return 1
  step "wait" make scylla-wait || return 1
  step "schema" make scylla-schema || return 1
  step "index" make scylla-index || return 1
  step "serving" make scylla-serving || return 1

  $PYTHON -m ftsbench.resource_probe --engine scylladb \
    --containers fts-bench-scylla:scylladb --containers fts-bench-vector-store:vector-store \
    --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts \
    --output "$OUT_DIR/cpu-$name-$rep.jsonl" --interval 1 --duration 0 --label "$name" >/dev/null 2>&1 &
  local probe=$!
  local rc=0
  make c1-scylla-cdc \
    "C1_MAX_SECONDS=2400" "C1_IDLE_TIMEOUT=90" "SCYLLA_UNLOGGED_BATCH_ROWS=30" \
    "INGEST_CONCURRENCY=$CONC" "BATCH_SIZE=500" "REP=$rep" \
    "CACHE_STATE=warm-container-fresh-index" "LABEL=$name conc=$CONC" \
    "C1_SCYLLA_CDC_SERIES=$series" \
    "C1_SCYLLA_CDC_MANIFEST=$OUT_DIR/manifest-$name-$rep.json" \
    >"$OUT_DIR/load-$name-$rep.log" 2>&1 || rc=1
  kill -TERM "$probe" 2>/dev/null || true; wait "$probe" 2>/dev/null || true
  docker logs fts-bench-vector-store >"$OUT_DIR/vslog-$name-$rep.log" 2>&1
  [ -s "$series" ] || return 1
  return $rc
}

for rep in $(seq 1 "$REPS"); do
  for spec in "${VARIANTS[@]}"; do
    IFS='|' read -r name mb merges cpus <<<"$spec"
    export VS_FTS_WRITER_MEMORY_MB="$mb" VS_FTS_MERGE_THREADS="$merges" VS_CPUS="$cpus"
    log "$name rep=$rep (buffer=${mb}MB merge_threads=$merges VS_CPUS=$cpus conc=$CONC)"
    set +e; run_point "$name" "$rep"; rc=$?; set -e
    [ $rc -ne 0 ] && log "$name rep=$rep FAILED (rc=$rc)"
  done
done
log "writer-memory matrix complete — $OUT_DIR"
