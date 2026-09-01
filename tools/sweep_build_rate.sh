#!/usr/bin/env bash
# Build-rate saturation sweep: docs/s ceiling vs loader concurrency.
#
# Unlike C1, which measures one fixed INGEST_CONCURRENCY, this walks a ladder
# and keeps the engine container warm across the whole ladder. The C1 pass shows
# why the container must stay up: its OpenSearch repetition 1 read 14,112 docs/s
# against 22,347 and 22,370 for repetitions 2 and 3 — a cold-JVM artifact. Paid
# once per ladder point instead of once per campaign, that artifact would land
# on every point and be indistinguishable from a concurrency effect.
#
# The index is still rebuilt from empty for every point, so no point ever
# measures a build on top of another point's documents.
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${1:?usage: sweep_build_rate.sh <opensearch|scylla-cdc> [reps]}"
REPS="${2:-3}"
LADDER="${LADDER:-1 2 4 8 16 32 64}"
OUT_DIR="${OUT_DIR:-data/sweep}"
OS_REFRESH="${OS_REFRESH:-3s}"
VS_URL="${VS_URL:-http://localhost:16080}"
PYTHON="${PYTHON:-.venv/bin/python3}"

# A concurrency-1 build is far slower than the C1 default of 8, and the monitor
# is what decides a run is over. Left at the C1 value the low points would be
# cut off mid-build and read as a low ceiling.
MAX_SECONDS="${MAX_SECONDS:-2400}"

MAKE_COMMON=(
  "C1_MAX_SECONDS=$MAX_SECONDS"
  "CACHE_STATE=warm-container-fresh-index"
)

mkdir -p "$OUT_DIR"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

case "$ENGINE" in
  opensearch)  CONFIG="opensearch-refresh${OS_REFRESH%s}" ;;
  scylla-cdc)  CONFIG="scylla-cdc" ;;
  *) echo "unknown engine: $ENGINE" >&2; exit 2 ;;
esac

start_stack() {
  case "$ENGINE" in
    opensearch) make os-up os-wait os-relax-watermarks ;;
    scylla-cdc) make scylla-up scylla-wait ;;
  esac
}

# Every point starts from an empty index. For OpenSearch that is a drop and
# recreate; for ScyllaDB the base table has to go too, or the next load would
# write the corpus into a table that already holds it.
reset_index() {
  case "$ENGINE" in
    opensearch)
      # Re-gated per point, not once per ladder: the index-create block is
      # applied at runtime by DiskThresholdMonitor and can come back mid-sweep,
      # and a point that dies on a 403 is a hole in the curve.
      make os-relax-watermarks
      make os-reindex "OS_REFRESH=$OS_REFRESH"
      ;;
    scylla-cdc)
      docker exec -i fts-bench-scylla cqlsh <<'CQL'
DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;
CQL
      make scylla-schema scylla-index scylla-serving
      ;;
  esac
}

run_point() {
  local conc="$1" rep="$2"
  local series="$OUT_DIR/c1-$CONFIG-c$conc-$rep.jsonl"
  local manifest="$OUT_DIR/manifest-$CONFIG-c$conc-$rep.json"
  local label="build-rate sweep, $CONFIG, concurrency=$conc"

  reset_index
  case "$ENGINE" in
    opensearch)
      make c1-os "${MAKE_COMMON[@]}" \
        "OS_REFRESH=$OS_REFRESH" "OS_CONFIG=$CONFIG" \
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label" \
        "C1_OS_SERIES=$series" "C1_OS_MANIFEST=$manifest"
      ;;
    scylla-cdc)
      # Sampled alongside every point, because "did it saturate?" and "did it
      # saturate the CPU it was given?" are different questions and the build
      # rate alone answers only the first. Both containers are named: the
      # ScyllaDB side is two processes and a probe that watched one would
      # understate the side by exactly the index's share.
      local probe="$OUT_DIR/cpu-$CONFIG-c$conc-$rep.jsonl"
      $PYTHON -m ftsbench.resource_probe --engine scylladb \
        --containers fts-bench-scylla:scylladb \
        --containers fts-bench-vector-store:vector-store \
        --vs-url "$VS_URL" --keyspace wiki --vs-index articles_body_fts \
        --output "$probe" --interval 1 --duration 0 \
        --label "$label" >/dev/null 2>&1 &
      local probe_pid=$!
      make c1-scylla-cdc "${MAKE_COMMON[@]}" \
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label" \
        "C1_SCYLLA_CDC_SERIES=$series" "C1_SCYLLA_CDC_MANIFEST=$manifest"
      kill -TERM "$probe_pid" 2>/dev/null || true
      wait "$probe_pid" 2>/dev/null || true
      ;;
  esac
}

start_stack

# Discarded: this is the run that pays for JIT, page cache and connection setup.
log "warmup (discarded) — $CONFIG"
run_point 8 0 || log "warmup failed — continuing, the measured points gate themselves"

for rep in $(seq 1 "$REPS"); do
  for conc in $LADDER; do
    log "$CONFIG concurrency=$conc rep=$rep"
    run_point "$conc" "$rep"
  done
done

log "done — series in $OUT_DIR"
