#!/usr/bin/env bash
# R0 for the vector-store-direct read cube, WITHOUT re-loading the corpus.
#
# The base table survived B4 teardown (compose down, no -v) with the full
# frozen corpus, so the only thing missing is the in-RAM Tantivy index, which
# the vector-store rebuilds by a full base-table scan on container start.
# resident_index.sh cannot be used here: it opens with `make scylla-reset`
# (down -v), which would destroy that base table and reload it over CQL for
# nothing.
#
# SERVING is NOT the gate. A vector-store that hit its memory limit reports
# SERVING while having silently discarded documents, so the doc count is the
# gate and SERVING is only the precondition for asking.
set -euo pipefail
cd "$(dirname "$0")/.."
source tools/fleet_env.sh

EXPECTED_DOCS="${EXPECTED_DOCS:-8967625}"
PYTHON="${PYTHON:-.venv/bin/python3}"

log() { printf "\n=== [%s] %s\n" "$(date -u +%H:%M:%S)" "$*"; }

log "bringing up ScyllaDB + a fresh vector-store container"
make scylla-up scylla-wait

log "base table row count (should already be the full corpus)"
docker exec fts-bench-scylla cqlsh -e \
  "SELECT COUNT(*) FROM wiki.articles;" 2>/dev/null | sed -n "4p" || true

log "ensuring the FTS index exists (IF NOT EXISTS)"
make scylla-index

log "waiting for SERVING, then polling the indexed count"
make scylla-serving

while :; do
  count="$(curl -fsS "$VS_URL/api/v1/indexes/wiki/articles_body_fts/status" \
    | $PYTHON -c "import json,sys; print(json.load(sys.stdin).get(\"count\",-1))")"
  [[ "$count" == "$EXPECTED_DOCS" ]] && { log "index resident: $count docs"; break; }
  printf "  indexing: %s / %s\n" "$count" "$EXPECTED_DOCS"
  sleep 20
done

log "OOM gate"
for c in fts-bench-scylla fts-bench-vector-store; do
  state="$(docker inspect -f "{{.State.OOMKilled}} {{.State.ExitCode}}" "$c")"
  echo "$c: $state"
  [[ "$state" == "false 0" ]] || { echo "OOM/exit on $c" >&2; exit 1; }
done

log "R0 COMPLETE — stack left UP, index resident at $EXPECTED_DOCS docs"
