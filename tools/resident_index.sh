#!/usr/bin/env bash
# R0 of READ-PATH-TEST-PLAN.md: build the resident index and LEAVE IT UP.
#
#   tools/resident_index.sh <opensearch|scylla-cdc>
#
# Cold stack, full-corpus load at the write-path saturating concurrency,
# wait until every document is searchable, gate on the exact count — then
# stop, with the engine still running. The ingest campaign always tears its
# stack down (one engine at a time is its premise); the read cube needs the
# opposite, which is the whole reason this script exists.
#
# Fleet mode: source tools/fleet_env.sh first (compose and gates travel over
# DOCKER_HOST; the loader streams from this box).
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${1:?usage: resident_index.sh <opensearch|scylla-cdc>}"
CONCURRENCY="${INGEST_CONCURRENCY:-64}"
OS_URL="${OS_URL:-http://localhost:9200}"
VS_URL="${VS_URL:-http://localhost:16080}"
PYTHON="${PYTHON:-.venv/bin/python3}"
EXPECTED_DOCS="${EXPECTED_DOCS:-$(wc -l < data/corpus.jsonl)}"
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-300}"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

count_now() {
  case "$ENGINE" in
    opensearch)
      curl -fsS "$OS_URL/wiki-articles/_count" \
        | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["count"])' ;;
    scylla-cdc)
      curl -fsS "$VS_URL/api/v1/indexes/wiki/articles_body_fts/status" \
        | $PYTHON -c 'import json,sys; print(json.load(sys.stdin).get("count",-1))' ;;
  esac
}

wait_searchable() {
  local deadline=$((SECONDS + SETTLE_TIMEOUT)) count
  while :; do
    count="$(count_now || echo -1)"
    [[ "$count" == "$EXPECTED_DOCS" ]] && { log "searchable: $count docs"; return 0; }
    [[ $SECONDS -lt $deadline ]] || {
      echo "settle timeout: $count of $EXPECTED_DOCS searchable" >&2; return 1; }
    echo "settling: $count / $EXPECTED_DOCS" >&2
    sleep 5
  done
}

assert_not_oom() {
  local state
  for c in "$@"; do
    state="$(docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' "$c")"
    [[ "$state" == "false 0" ]] || { echo "$c OOM/exit: $state" >&2; return 1; }
  done
  echo "no OOM kills" >&2
}

case "$ENGINE" in
  opensearch)
    log "cold OpenSearch stack"
    make os-reset os-up os-wait os-relax-watermarks
    make os-reindex OS_REFRESH=3s
    make os-verify-analyzer
    log "loading $EXPECTED_DOCS docs at concurrency $CONCURRENCY"
    make os-load TARGET_RATE=0 INGEST_CONCURRENCY="$CONCURRENCY" MAX_DOCS=0
    wait_searchable
    assert_not_oom fts-bench-opensearch
    ;;
  scylla-cdc)
    log "cold ScyllaDB stack"
    make scylla-reset scylla-up scylla-wait scylla-schema scylla-index scylla-serving
    log "loading $EXPECTED_DOCS docs at concurrency $CONCURRENCY"
    make scylla-load TARGET_RATE=0 INGEST_CONCURRENCY="$CONCURRENCY" MAX_DOCS=0
    wait_searchable
    assert_not_oom fts-bench-scylla fts-bench-vector-store
    ;;
  *) echo "unknown engine: $ENGINE" >&2; exit 2 ;;
esac

log "resident index ready — stack LEFT UP for the read cube"
