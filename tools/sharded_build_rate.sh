#!/usr/bin/env bash
# Build rate measured off a load generator that is NOT the bottleneck.
#
# Why this exists: the campaign's single-process loaders have different
# ceilings, and one of them binds. Measured on the fleet, one process reaches
# ~9.8k docs/s against ScyllaDB and ~11.4k against OpenSearch, each pegged at
# ~80-100% of one core -- they are GIL-bound, not engine-bound. OpenSearch's
# engine ceiling (~11.1k) sits BELOW its client ceiling, so its published
# number was a real engine measurement; ScyllaDB's engine capability sits ABOVE
# its client ceiling, so its published 8,992 was a measurement of the client.
# Comparing those two numbers compares a client to an engine.
#
# The read path already solved this with a sharded multi-process runner
# (ftsbench.cell_bench_mp, TUNING.md 6-7). This is the write-path equivalent:
# SHARDS disjoint corpus files, one loader process each, one build_monitor
# watching the engine's own searchable count.
#
# Corpus shards must be DISJOINT. Pointing two loaders at the same prefix makes
# them write identical primary keys: base-table throughput still looks right
# (the write-op count is unchanged) while the index count becomes meaningless.
set -euo pipefail
cd "$(dirname "$0")/.."

ENGINE="${1:?usage: sharded_build_rate.sh <opensearch|scylla-cdc> [reps]}"
REPS="${2:-3}"
SHARD_GLOB="${SHARD_GLOB:-/mnt/nvme/data/corpus-ab-?.jsonl}"
OUT_DIR="${OUT_DIR:-data/sharded-build}"
CONC="${CONC:-64}"
OS_REFRESH="${OS_REFRESH:-3s}"
# Long enough that a drain of the CDC backlog is never mistaken for a stall:
# a premature idle timeout reports a partial count as if it were the final one,
# which cost one voided measurement already (BUILD-RATE-LOOP.md iteration 2).
IDLE_TIMEOUT="${IDLE_TIMEOUT:-240}"
MAX_SECONDS="${MAX_SECONDS:-1200}"
PYTHON="${PYTHON:-.venv/bin/python3}"

shopt -s nullglob
SHARDS=($SHARD_GLOB)
shopt -u nullglob
[ ${#SHARDS[@]} -ge 2 ] || { echo "need >=2 disjoint corpus shards ($SHARD_GLOB)" >&2; exit 2; }
TOTAL_DOCS="${TOTAL_DOCS:-$(cat "${SHARDS[@]}" | wc -l)}"

mkdir -p "$OUT_DIR"
log() { printf '\n=== [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

stop_stack() { make os-down >/dev/null 2>&1 || true; make scylla-down >/dev/null 2>&1 || true; }
trap stop_stack EXIT

start_stack() {
  case "$ENGINE" in
    opensearch) make os-up os-wait os-relax-watermarks >/dev/null 2>&1 ;;
    scylla-cdc) make scylla-up scylla-wait >/dev/null 2>&1 ;;
  esac
}

reset_index() {
  case "$ENGINE" in
    opensearch)
      make os-relax-watermarks >/dev/null 2>&1
      make os-reindex "OS_REFRESH=$OS_REFRESH" >/dev/null 2>&1
      make os-verify-analyzer >/dev/null 2>&1
      ;;
    scylla-cdc)
      docker exec -i fts-bench-scylla cqlsh -e "
        DROP INDEX IF EXISTS wiki.articles_body_fts;
        DROP TABLE IF EXISTS wiki.articles;
        DROP KEYSPACE IF EXISTS wiki;" >/dev/null 2>&1 || true
      make scylla-schema scylla-index scylla-serving >/dev/null 2>&1
      ;;
  esac
}

monitor_args() {
  case "$ENGINE" in
    opensearch) echo "--engine opensearch --url $OS_URL --index wiki-articles" ;;
    scylla-cdc) echo "--engine scylladb --vs-url $VS_URL --keyspace wiki --vs-index articles_body_fts" ;;
  esac
}

loader_for() {
  local shard="$1" label="$2" out="$3"
  case "$ENGINE" in
    opensearch)
      $PYTHON -m ftsbench.opensearch_load --corpus "$shard" --url "$OS_URL" \
        --index wiki-articles --batch-size 500 --concurrency "$CONC" \
        --label "$label" >"$out" 2>&1
      ;;
    scylla-cdc)
      $PYTHON -m ftsbench.scylla_load --corpus "$shard" --hosts "$SCYLLA_HOSTS" \
        --port "$SCYLLA_PORT" --batch-size 1000 --concurrency "$CONC" \
        --label "$label" >"$out" 2>&1
      ;;
  esac
}

run_rep() {
  local rep="$1" tag="$ENGINE-$rep"
  # Cold repetitions when COLD_REPS=1 (required for full-corpus runs, per
  # WRITE-PATH-TEST-PLAN.md Step 3). Resetting only the index is not enough:
  # the vector-store does NOT release its in-RAM index on DROP, leaving ~12.4
  # GiB resident after an 8.97M-doc build, so successive warm reps start higher
  # and higher until they breach VECTOR_STORE_MEMORY_LIMIT and silently drop
  # documents. Measured 2026-09-07: rep RSS started 0.25 / 12.36 / 12.38 GiB
  # and rep 3 lost 14,917 documents. Warm reps stay the default for capped
  # ladder points, where the index is small and a cold engine would cost a
  # startup artifact on every point.
  if [ "${COLD_REPS:-0}" = "1" ]; then
    stop_stack
    start_stack
  fi
  reset_index

  tools/sut_probe.sh start "$OUT_DIR/cpu-$tag.jsonl" \
    $(case "$ENGINE" in
        opensearch) echo "--engine opensearch --containers fts-bench-opensearch:opensearch --os-url http://localhost:9200 --os-index wiki-articles" ;;
        scylla-cdc) echo "--engine scylladb --containers fts-bench-scylla:scylladb --containers fts-bench-vector-store:vector-store --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts" ;;
      esac) \
    --interval 1 --duration 0 --label "$tag"

  $PYTHON -m ftsbench.build_monitor $(monitor_args) \
    --output "$OUT_DIR/c1-$tag.jsonl" --until-docs "$TOTAL_DOCS" \
    --idle-timeout "$IDLE_TIMEOUT" --max-seconds "$MAX_SECONDS" \
    --label "sharded x${#SHARDS[@]} conc=$CONC" \
    --cache-state warm-container-fresh-index >"$OUT_DIR/mon-$tag.log" 2>&1 &
  local mon=$!
  sleep 2

  local i=0
  for shard in "${SHARDS[@]}"; do
    i=$((i + 1))
    loader_for "$shard" "shard$i" "$OUT_DIR/load-$tag-$i.log" &
  done
  wait $(jobs -p | grep -v "^$mon$") 2>/dev/null || true
  wait "$mon" 2>/dev/null || true

  tools/sut_probe.sh stop "$OUT_DIR/cpu-$tag.jsonl" || true
  [ "$ENGINE" = "scylla-cdc" ] && docker logs fts-bench-vector-store >"$OUT_DIR/vslog-$tag.log" 2>&1
  return 0
}

log "$ENGINE: ${#SHARDS[@]} shards, $TOTAL_DOCS docs, conc=$CONC, N=$REPS"
start_stack
for rep in $(seq 1 "$REPS"); do
  log "$ENGINE rep=$rep"
  run_rep "$rep"
done
log "sharded build-rate complete -- $OUT_DIR"
