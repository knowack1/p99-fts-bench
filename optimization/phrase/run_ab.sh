#!/usr/bin/env bash
# A/B the vector-store phrase path across N repetitions.
#
# One repetition = swap the image, let the in-RAM FTS index rebuild from the
# base table, record the segment count it happened to settle on, then measure.
# The segment count is recorded per row rather than controlled, because a
# rebuild picks its own: a claim that survives the spread is worth more than
# one taken at a single lucky layout.
#
# Usage: run_ab.sh [reps] [out.csv]
set -euo pipefail

BENCH=/home/karolnowacki/Projects/Scylla/p99/bench
REPS="${1:-3}"
OUT="${2:-$BENCH/optimization/phrase/measurements/ab-results.csv}"
QUERIES="${QUERIES:-$BENCH/optimization/phrase/measurements/queries-3class-simplewiki.json}"
VARIANTS="${VARIANTS:-p99-v0-control p99-v1-prune}"
PYTHON="$BENCH/.venv/bin/python3"
VS_STATUS=http://127.0.0.1:16080/api/v1/indexes/wiki/articles_body_fts/status
EXPECT_DOCS="${EXPECT_DOCS:-270269}"

# Readiness is "answers a search", not "says SERVING".
#
# Right after a restart the status endpoint can still report the *previous*
# index as SERVING at full count while the new container is still scanning —
# so gating on status alone returns instantly and the benchmark then dies on
# `503 Service Unavailable` from the bm25 endpoint. Gate on the endpoint the
# measurement actually uses, and require the count as well.
wait_ready() {
    local bm25="http://127.0.0.1:16080/api/v1/indexes/wiki/articles_body_fts/bm25"
    for _ in $(seq 1 180); do
        if curl -fsS "$VS_STATUS" 2>/dev/null | grep -q "\"count\":$EXPECT_DOCS" \
           && curl -fsS -X POST "$bm25" -H 'Content-Type: application/json' \
                -d '{"query":"readiness","limit":1}' >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
    done
    echo "index did not become answerable at $EXPECT_DOCS docs" >&2
    return 1
}

segment_count() {
    curl -fsS http://127.0.0.1:16080/metrics 2>/dev/null \
        | awk '/^fts_segment_count/ {print $2}'
}

echo "rep,variant,segments,class,p50_ms,p95_ms,p99_ms" > "$OUT"

for rep in $(seq 1 "$REPS"); do
    for variant in $VARIANTS; do
        echo "[rep $rep] $variant: swapping image" >&2
        cd "$BENCH"
        VECTOR_STORE_IMAGE="scylladb/vector-store:$variant" \
            docker compose -f docker/docker-compose.scylla.yml \
            --env-file docker/.env up -d >/dev/null 2>&1
        # `up -d` is a no-op when the image tag did not change, so force the
        # rebuild that makes every repetition start from the same place.
        VECTOR_STORE_IMAGE="scylladb/vector-store:$variant" \
            docker compose -f docker/docker-compose.scylla.yml \
            --env-file docker/.env restart vector-store >/dev/null 2>&1
        wait_ready
        segments=$(segment_count)
        echo "[rep $rep] $variant: $segments segments, measuring" >&2
        tmp=$(mktemp)
        "$PYTHON" -m ftsbench.query_bench --engine vector-store \
            --queries "$QUERIES" --output "$tmp" \
            --limit 10 --warmup 3 --iterations 20 >/dev/null
        "$PYTHON" "$BENCH/optimization/phrase/summarize_report.py" \
            "$tmp" "$rep" "$variant" "$segments" >> "$OUT"
        rm -f "$tmp"
    done
done

echo "wrote $OUT" >&2
