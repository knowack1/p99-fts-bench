#!/usr/bin/env bash
# ScyllaDB + vector-store index throughput vs loader concurrency, with the
# client ceiling removed.
#
# The per-row ladder in results/build-rate-sweep-2026-08-26 plateaus at ~5k
# docs/s, but that plateau is ftsbench.scylla_load's GIL-bound per-row loop, not
# the engine: the same stack reached 22,481 docs/s once the loader batched. This
# repeats the ladder with UNLOGGED BATCH so the curve is about the engine.
#
# Concurrency here means BATCHES in flight, each of LADDER_BATCH_ROWS rows, so
# rows in flight is concurrency x rows-per-batch. Labelling the axis "rows in
# flight" would be wrong by a factor of 30.
#
# UNLOGGED BATCH over a uuid primary key spans one partition per row: the
# documented anti-pattern, warned about per batch by ScyllaDB, and unfit for a
# published write number. It is here to make the *index* the constraint.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/ladder-batched}"
REPS="${REPS:-3}"
LADDER="${LADDER:-1 2 4 8 16 32 64}"
LADDER_BATCH_ROWS="${LADDER_BATCH_ROWS:-30}"
PYTHON="${PYTHON:-.venv/bin/python3}"
mkdir -p "$OUT_DIR"

# Fixed at the best-known configuration from the bottleneck matrix so the only
# variable across the ladder is concurrency.
export VS_FTS_COMMIT_INTERVAL=3s
export VS_FTS_COMMIT_THRESHOLD=0
export VS_FTS_ADD_LOCK=shared
export VS_FTS_METRICS_INTERVAL=5s
export VS_CDC_FINE_SLEEP=""
export VS_CDC_FINE_SAFETY=""
export VS_CDC_SLEEP=""
export VS_CDC_SAFETY=""
export VS_CPUS=6

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

step() {
  local what="$1"; shift
  if ! "$@" >/dev/null 2>&1; then log "step '$what' failed"; return 1; fi
}

run_point() {
  local conc="$1" rep="$2"
  local series="$OUT_DIR/c1-scylla-cdc-c$conc-$rep.jsonl"
  local probe="$OUT_DIR/cpu-c$conc-$rep.jsonl"
  local loadlog="$OUT_DIR/load-c$conc-$rep.log"
  local vslog="$OUT_DIR/vslog-c$conc-$rep.log"

  step "up" make scylla-up || return 1
  step "wait" make scylla-wait || return 1
  docker exec -i fts-bench-scylla cqlsh >/dev/null 2>&1 <<'CQL' || true
DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;
CQL
  step "schema" make scylla-schema || return 1
  step "index" make scylla-index || return 1
  step "serving" make scylla-serving || return 1

  $PYTHON -m ftsbench.resource_probe --engine scylladb \
    --containers fts-bench-scylla:scylladb \
    --containers fts-bench-vector-store:vector-store \
    --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts \
    --output "$probe" --interval 1 --duration 0 --label "conc=$conc" >/dev/null 2>&1 &
  local probe_pid=$!

  local rc=0
  make c1-scylla-cdc \
    "C1_MAX_SECONDS=2400" "C1_IDLE_TIMEOUT=90" \
    "SCYLLA_UNLOGGED_BATCH_ROWS=$LADDER_BATCH_ROWS" \
    "INGEST_CONCURRENCY=$conc" "BATCH_SIZE=500" \
    "CACHE_STATE=warm-container-fresh-index" "REP=$rep" \
    "LABEL=batched ladder conc=$conc rows_per_batch=$LADDER_BATCH_ROWS" \
    "C1_SCYLLA_CDC_SERIES=$series" \
    "C1_SCYLLA_CDC_MANIFEST=$OUT_DIR/manifest-c$conc-$rep.json" >"$loadlog" 2>&1 || rc=1

  kill -TERM "$probe_pid" 2>/dev/null || true
  wait "$probe_pid" 2>/dev/null || true
  docker logs fts-bench-vector-store >"$vslog" 2>&1
  [ -s "$series" ] || { log "conc=$conc rep=$rep produced no series"; return 1; }
  return $rc
}

for rep in $(seq "${REP_START:-1}" "$REPS"); do
  for conc in $LADDER; do
    log "concurrency=$conc rep=$rep (batched, $LADDER_BATCH_ROWS rows/batch)"
    set +e; run_point "$conc" "$rep"; rc=$?; set -e
    [ $rc -ne 0 ] && log "conc=$conc rep=$rep FAILED (rc=$rc) — continuing"
  done
done
log "ladder complete — $OUT_DIR"
