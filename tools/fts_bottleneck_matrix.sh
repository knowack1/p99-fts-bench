#!/usr/bin/env bash
# Locate the vector-store's index-ingest ceiling by configuration.
#
# Every variant is one env set against a single build
# (scylladb/vector-store:1.10.0-p99-tunable), so nothing is rebuilt mid-matrix
# and no variant differs from another by anything but the named variables.
#
# The loader runs in UNLOGGED BATCH mode throughout. That is the documented
# anti-pattern and unfit for a published write number, but the per-row path is
# GIL-bound at ~11k docs/s and would itself become the constraint the moment a
# variant lifted the index above it. Headroom, not a result.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/bottleneck}"
REPS="${REPS:-2}"
LOAD_BATCH_ROWS="${LOAD_BATCH_ROWS:-30}"
LOAD_CONCURRENCY="${LOAD_CONCURRENCY:-32}"
mkdir -p "$OUT_DIR"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# variant|commit_interval|commit_threshold|add_lock|cdc_fine_sleep|cdc_fine_safety|vs_cpus|path
VARIANTS=(
  "V1-stock|3s|10000|exclusive|||6|cdc"
  "V2-nothreshold|3s|0|exclusive|||6|cdc"
  "V3-interval1s|1s|0|exclusive|||6|cdc"
  "V4-interval10s|10s|0|exclusive|||6|cdc"
  "V5-interval30s|30s|0|exclusive|||6|cdc"
  "V6-shared-lock|3s|0|shared|||6|cdc"
  "V7-shared-threshold|3s|10000|shared|||6|cdc"
  "V8-shared-fastcdc|3s|0|shared|50ms|10ms|6|cdc"
  "V9-excl-fastcdc|3s|0|exclusive|50ms|10ms|6|cdc"
  "V10-shared-cpu2|3s|0|shared|||2|cdc"
  "V11-shared-cpu12|3s|0|shared|||12|cdc"
  "V12-shared-bootstrap|3s|0|shared|||6|bootstrap"
  # The tail, not the bulk rate, is what depresses docs/s. Stragglers arrive on
  # the schedule of the *wide* CDC reader (30s safety, 10s sleep by default),
  # so these two shorten it. VS_CDC_SLEEP/SAFETY are the wide reader's knobs;
  # the columns named cdc_fine_* above are the fine reader's.
  "V13-wide-cdc-fast|3s|0|shared|||6|cdc-widefast"
  "V14-wide-and-fine-fast|3s|0|shared|50ms|10ms|6|cdc-widefast"
)

reset_keyspace() {
  docker exec -i fts-bench-scylla cqlsh >/dev/null 2>&1 <<'CQL'
DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;
CQL
}

# Recreated rather than restarted: compose only picks up changed environment by
# recreating the container, and a variant that silently kept the previous env
# would be indistinguishable from a real null result.
restart_vector_store() {
  make scylla-up >/dev/null 2>&1
  make scylla-wait >/dev/null 2>&1
}

run_variant() {
  local name="$1" interval="$2" threshold="$3" lock="$4"
  local cdc_sleep="$5" cdc_safety="$6" cpus="$7" path="$8" rep="$9"

  export VS_FTS_COMMIT_INTERVAL="$interval"
  export VS_FTS_COMMIT_THRESHOLD="$threshold"
  export VS_FTS_ADD_LOCK="$lock"
  export VS_FTS_METRICS_INTERVAL="5s"
  export VS_CDC_FINE_SLEEP="$cdc_sleep"
  export VS_CDC_FINE_SAFETY="$cdc_safety"
  export VS_CPUS="$cpus"

  restart_vector_store
  reset_keyspace

  local series="$OUT_DIR/$name-$rep.jsonl"
  local probe="$OUT_DIR/cpu-$name-$rep.jsonl"
  local vslog="$OUT_DIR/vslog-$name-$rep.log"
  local loadlog="$OUT_DIR/load-$name-$rep.log"
  local common=(
    # Comfortably above the largest commit interval in the matrix: with a 30s
    # interval the final partial batch is committed up to 30s after the loader
    # stops, and at the stock 30s idle timeout the monitor gave up first and
    # recorded a build 916 documents short.
    "C1_MAX_SECONDS=2400" "C1_IDLE_TIMEOUT=90"
    "SCYLLA_UNLOGGED_BATCH_ROWS=$LOAD_BATCH_ROWS"
    "INGEST_CONCURRENCY=$LOAD_CONCURRENCY" "BATCH_SIZE=500"
    "CACHE_STATE=warm-container-fresh-index" "REP=$rep"
    "LABEL=$name interval=$interval threshold=$threshold lock=$lock cdc=$cdc_sleep/$cdc_safety wide=${VS_CDC_SLEEP:-default}/${VS_CDC_SAFETY:-default} cpus=$cpus"
  )

  $PYTHON -m ftsbench.resource_probe --engine scylladb \
    --containers fts-bench-scylla:scylladb \
    --containers fts-bench-vector-store:vector-store \
    --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts \
    --output "$probe" --interval 1 --duration 0 --label "$name" >/dev/null 2>&1 &
  local probe_pid=$!

  local status=0
  if [ "$path" = "cdc-widefast" ]; then
    export VS_CDC_SLEEP="1s"
    export VS_CDC_SAFETY="1s"
  else
    export VS_CDC_SLEEP=""
    export VS_CDC_SAFETY=""
  fi

  if [ "$path" = "bootstrap" ]; then
    step "$name" "schema" make scylla-schema || status=1
    [ $status -eq 0 ] && { make scylla-load "${common[@]}" >"$loadlog" 2>&1 || status=1; }
    [ $status -eq 0 ] && { make c1-scylla-bootstrap "${common[@]}" \
      "C1_SCYLLA_BOOTSTRAP_SERIES=$series" \
      "C1_SCYLLA_BOOTSTRAP_MANIFEST=$OUT_DIR/manifest-$name-$rep.json" >>"$loadlog" 2>&1 || status=1; }
  else
    step "$name" "schema" make scylla-schema || status=1
    [ $status -eq 0 ] && { step "$name" "index" make scylla-index || status=1; }
    [ $status -eq 0 ] && { step "$name" "serving" make scylla-serving || status=1; }
    [ $status -eq 0 ] && { make c1-scylla-cdc "${common[@]}" \
      "C1_SCYLLA_CDC_SERIES=$series" \
      "C1_SCYLLA_CDC_MANIFEST=$OUT_DIR/manifest-$name-$rep.json" >"$loadlog" 2>&1 || status=1; }
  fi

  kill -TERM "$probe_pid" 2>/dev/null || true
  wait "$probe_pid" 2>/dev/null || true
  # Per variant, not cumulative: the container is recreated for each one, so its
  # log covers exactly this run and the metric rates are attributable.
  docker logs fts-bench-vector-store >"$vslog" 2>&1

  if [ ! -s "$series" ]; then
    log "$name rep=$rep produced no series — see $loadlog"
    return 1
  fi
  return $status
}

step() {
  local name="$1" what="$2"; shift 2
  if ! "$@" >/dev/null 2>&1; then
    log "$name: step '$what' failed"
    return 1
  fi
}

PYTHON="${PYTHON:-.venv/bin/python3}"
for rep in $(seq 1 "$REPS"); do
  for spec in "${VARIANTS[@]}"; do
    IFS='|' read -r name interval threshold lock cdc_sleep cdc_safety cpus path <<<"$spec"
    log "$name rep=$rep (interval=$interval threshold=$threshold lock=$lock cdc=$cdc_sleep/$cdc_safety cpus=$cpus path=$path)"
    set +e
    run_variant "$name" "$interval" "$threshold" "$lock" "$cdc_sleep" "$cdc_safety" "$cpus" "$path" "$rep"
    rc=$?
    set -e
    [ $rc -ne 0 ] && log "$name rep=$rep FAILED (rc=$rc) — continuing"
  done
done
log "matrix complete — $OUT_DIR"
