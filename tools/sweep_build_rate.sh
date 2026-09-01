#!/usr/bin/env bash
# Build-rate saturation sweep: docs/s ceiling vs loader concurrency (deck S12).
#
# Unlike C1, which measures one fixed INGEST_CONCURRENCY, this walks a ladder
# and keeps the engine container warm across the whole ladder. The C1 pass shows
# why the container must stay up: its OpenSearch repetition 1 read 14,112 docs/s
# against 22,347 and 22,370 for repetitions 2 and 3 — a cold-JVM artifact. Paid
# once per ladder point instead of once per campaign, that artifact would land
# on every point and be indistinguishable from a concurrency effect.
#
# No discarded warm-up point (decision 2026-09-01, WRITE-PATH-TEST-PLAN.md):
# reps run rep-major, so the cold artifact lands on exactly one sample — rep 1
# of the first rung — and the per-point median of REPS absorbs it. Expect one
# visibly low thin line there and narrate it. WARMUP=1 restores the old
# discarded point if a run ever needs it.
#
# The index is still rebuilt from empty for every point, so no point ever
# measures a build on top of another point's documents.
#
# Every point is capped at SWEEP_DOCS documents, not the full corpus: a
# ceiling is a steady-state rate, and the budget is what makes 40 points per
# engine affordable at enwiki scale. The cap is recorded in every series and
# must reach the chart footer.
#
# Fleet mode: source tools/fleet_env.sh first. DOCKER_HOST carries the
# compose/exec/reset calls to the SUT; the resource probe runs ON the SUT via
# tools/sut_probe.sh because it reads local cgroups. Both engines are probed —
# the CPU chart (S14) and the saturation verdict (verify_cpu_usage) need the
# OpenSearch side too, not only ScyllaDB's two containers.
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${1:?usage: sweep_build_rate.sh <opensearch|scylla-cdc> [reps]}"
REPS="${2:-5}"
LADDER="${LADDER:-8 16 32 64 96 128 192 256}"
OUT_DIR="${OUT_DIR:-data/sweep}"
OS_REFRESH="${OS_REFRESH:-3s}"
VS_URL="${VS_URL:-http://localhost:16080}"
OS_URL="${OS_URL:-http://localhost:9200}"
PYTHON="${PYTHON:-.venv/bin/python3}"
CQLSH="${CQLSH:-docker exec -i fts-bench-scylla cqlsh}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
WARMUP="${WARMUP:-0}"

# A concurrency-8 build is far slower than the growth runs' saturating
# concurrency, and the monitor is what decides a run is over. Too low and the
# low rungs are cut off mid-build and read as a low ceiling.
MAX_SECONDS="${MAX_SECONDS:-2400}"

MAKE_COMMON=(
  "C1_MAX_SECONDS=$MAX_SECONDS"
  "MAX_DOCS=$SWEEP_DOCS"
  "C1_UNTIL_DOCS=$SWEEP_DOCS"
  "CACHE_STATE=warm-container-fresh-index"
)

mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-points.log"

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
      make os-verify-analyzer
      ;;
    scylla-cdc)
      $CQLSH <<'CQL'
DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;
CQL
      make scylla-schema scylla-index scylla-serving
      ;;
  esac
}

# Sampled alongside every point, because "did it saturate?" and "did it
# saturate the CPU it was given?" are different questions and the build rate
# alone answers only the first. The ScyllaDB side is two containers and a probe
# that watched one would understate the side by exactly the index's share.
PROBE_PID=""
FLEET_PROBE_OUT=""

probe_args() {
  case "$ENGINE" in
    opensearch)
      if [[ -n "${SUT_IP:-}" ]]; then
        echo "--engine opensearch --containers fts-bench-opensearch:opensearch \
--os-url http://localhost:9200 --os-index wiki-articles"
      else
        echo "--engine opensearch --containers fts-bench-opensearch:opensearch \
--os-url $OS_URL --os-index wiki-articles"
      fi ;;
    scylla-cdc)
      if [[ -n "${SUT_IP:-}" ]]; then
        echo "--engine scylladb --containers fts-bench-scylla:scylladb \
--containers fts-bench-vector-store:vector-store \
--vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts"
      else
        echo "--engine scylladb --containers fts-bench-scylla:scylladb \
--containers fts-bench-vector-store:vector-store \
--vs-url $VS_URL --keyspace wiki --vs-index articles_body_fts"
      fi ;;
  esac
}

probe_start() {
  local out="$1" label="$2"
  if [[ -n "${SUT_IP:-}" ]]; then
    FLEET_PROBE_OUT="$out"
    tools/sut_probe.sh start "$out" $(probe_args) \
      --interval 1 --duration 0 --cache-state warm-container-fresh-index \
      --label "$label"
  else
    $PYTHON -m ftsbench.resource_probe $(probe_args) \
      --output "$out" --interval 1 --duration 0 \
      --cache-state warm-container-fresh-index \
      --label "$label" >/dev/null 2>&1 &
    PROBE_PID=$!
  fi
}

probe_stop() {
  if [[ -n "${SUT_IP:-}" && -n "$FLEET_PROBE_OUT" ]]; then
    tools/sut_probe.sh stop "$FLEET_PROBE_OUT"
    FLEET_PROBE_OUT=""
    return 0
  fi
  [[ -n "$PROBE_PID" ]] || return 0
  kill -TERM "$PROBE_PID" 2>/dev/null || true
  wait "$PROBE_PID" 2>/dev/null || true
  PROBE_PID=""
}

# A truncated point is not a slow point — it silently lowers a rung of the
# median. Checked per point; a failure is logged and the point's files renamed
# aside so the summariser never mixes it in.
point_complete() {
  local series="$1"
  $PYTHON - "$series" "$SWEEP_DOCS" <<'PYGATE'
import json, sys
path, want = sys.argv[1], int(sys.argv[2])
last = {}
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            last = json.loads(line)
indexed = int(last.get("docs_indexed") or 0)
print(f"{path}: docs_indexed={indexed}/{want}")
sys.exit(0 if indexed >= want else 1)
PYGATE
}

run_point() {
  local conc="$1" rep="$2"
  local series="$OUT_DIR/c1-$CONFIG-c$conc-$rep.jsonl"
  local manifest="$OUT_DIR/manifest-$CONFIG-c$conc-$rep.json"
  local probe="$OUT_DIR/cpu-$CONFIG-c$conc-$rep.jsonl"
  local label="build-rate sweep, $CONFIG, concurrency=$conc"

  reset_index
  probe_start "$probe" "$label"
  local status=0
  case "$ENGINE" in
    opensearch)
      make c1-os "${MAKE_COMMON[@]}" \
        "OS_REFRESH=$OS_REFRESH" "OS_CONFIG=$CONFIG" \
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label" \
        "C1_OS_SERIES=$series" "C1_OS_MANIFEST=$manifest" || status=$?
      ;;
    scylla-cdc)
      make c1-scylla-cdc "${MAKE_COMMON[@]}" \
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label" \
        "C1_SCYLLA_CDC_SERIES=$series" "C1_SCYLLA_CDC_MANIFEST=$manifest" || status=$?
      ;;
  esac
  probe_stop
  if [[ $status -ne 0 ]] || ! point_complete "$series"; then
    log "POINT FAILED: $CONFIG c=$conc rep=$rep (make=$status) — set aside"
    echo "$(date -u +%FT%TZ) $CONFIG c=$conc rep=$rep make=$status" >> "$FAILURES"
    for f in "$series" "$manifest" "$probe"; do
      [[ -f "$f" ]] && mv "$f" "$f.failed"
    done
    return 0
  fi
}

start_stack

if [[ "$WARMUP" == 1 ]]; then
  log "warmup (discarded) — $CONFIG"
  run_point 8 0 || log "warmup failed — continuing, the measured points gate themselves"
fi

for rep in $(seq 1 "$REPS"); do
  for conc in $LADDER; do
    log "$CONFIG concurrency=$conc rep=$rep"
    run_point "$conc" "$rep"
  done
done

if [[ -s "$FAILURES" ]]; then
  log "sweep finished WITH FAILED POINTS — see $FAILURES; re-run those points before summarising"
else
  log "done — series in $OUT_DIR"
fi
