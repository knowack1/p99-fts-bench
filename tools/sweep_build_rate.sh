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

ARM="${1:?usage: sweep_build_rate.sh <target-flag|opensearch|scylla-cdc> [reps]}"
REPS="${2:-5}"
LADDER="${LADDER:-8 16 32 64 96 128 192 256}"
OUT_DIR="${OUT_DIR:-data/sweep}"
VS_URL="${VS_URL:-http://localhost:16080}"
OS_URL="${OS_URL:-http://localhost:9200}"
PYTHON="${PYTHON:-.venv/bin/python3}"
CQLSH="${CQLSH:-docker exec -i fts-bench-scylla cqlsh}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
WARMUP="${WARMUP:-0}"

# The arm comes from ftsbench.target, never from a `case` block here. A driver
# that restates the deployment set is how `vector-store-direct` came to exist in
# tools/read_sweep.sh and in no Python list at all, and how `opensearch-ramindex`
# spent a campaign as a label with no way to select it. Bare engine names still
# work because a dozen callers pass them.
case "$ARM" in
  --*)         ARM_SELECTOR=("$ARM") ;;
  opensearch)  ARM_SELECTOR=(--config "${SWEEP_CONFIG:-opensearch-refresh3}") ;;
  scylla-cdc)  ARM_SELECTOR=(--config "${SWEEP_CONFIG:-scylla-cdc}") ;;
  *)           ARM_SELECTOR=(--config "$ARM") ;;
esac
eval "$($PYTHON -m ftsbench.target "${ARM_SELECTOR[@]}" --shell)"

CONFIG="${SWEEP_CONFIG:-$TARGET_CONFIG}"
case "$TARGET_ENGINE" in
  opensearch) ENGINE=opensearch ;;
  scylladb)   ENGINE=scylla-cdc ;;
  *) echo "sweep_build_rate.sh cannot drive engine '$TARGET_ENGINE'" >&2; exit 2 ;;
esac
# OS_REFRESH is the arm's, exported by the eval above; an explicit environment
# value still wins so a one-off sensitivity run needs no new registry entry.
OS_REFRESH="${OS_REFRESH:-3s}"

# The monitor decides a run is over by watching the engine's own document
# count, and on the ScyllaDB side that count only moves at a commit
# (samplers.py: the vector-store `/status` count IS the committed count). So a
# slow commit cadence looks exactly like a stalled build: at
# VS_FTS_COMMIT_INTERVAL=30s, fleet_env.sh's C1_IDLE_TIMEOUT=60 is two cycles,
# and its own comment sized it on the assumption of "a pure 3 s on both sides".
# Both timeouts are floored at several cadences so a quiet gap between commits
# is never read as a dead loader.
cadence_seconds() {
  local raw="${1:-3s}"
  echo "${raw%s}"
}
SLOWEST_CADENCE=$(cadence_seconds "${VS_FTS_COMMIT_INTERVAL:-${OS_REFRESH}}")
IDLE_FLOOR=$((SLOWEST_CADENCE * 4))
C1_IDLE_TIMEOUT="${C1_IDLE_TIMEOUT:-60}"
[ "$C1_IDLE_TIMEOUT" -ge "$IDLE_FLOOR" ] || C1_IDLE_TIMEOUT="$IDLE_FLOOR"
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-120}"
[ "$SETTLE_TIMEOUT" -ge "$IDLE_FLOOR" ] || SETTLE_TIMEOUT="$IDLE_FLOOR"
export C1_IDLE_TIMEOUT

# A concurrency-8 build is far slower than the growth runs' saturating
# concurrency, and the monitor is what decides a run is over. Too low and the
# low rungs are cut off mid-build and read as a low ceiling.
MAX_SECONDS="${MAX_SECONDS:-2400}"

MAKE_COMMON=(
  "C1_MAX_SECONDS=$MAX_SECONDS"
  "MAX_DOCS=$SWEEP_DOCS"
  "C1_UNTIL_DOCS=$SWEEP_DOCS"
  "CACHE_STATE=warm-container-fresh-index"
  "C1_IDLE_TIMEOUT=$C1_IDLE_TIMEOUT"
  "C1_SETTLE_TIMEOUT=$SETTLE_TIMEOUT"
)

mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-points.log"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

log "arm $CONFIG ($TARGET_VARIANT) via $TARGET_FLAG"
log "knobs: $($PYTHON -c '
import sys
from ftsbench import target
print(target.format_env(target.by_config(sys.argv[1])) or "(inherited from the env file)")' "$TARGET_CONFIG")"
log "cap=$SWEEP_DOCS reps=$REPS warmup=$WARMUP idle=$C1_IDLE_TIMEOUT settle=$SETTLE_TIMEOUT"

start_stack() {
  case "$ENGINE" in
    opensearch) make os-up os-wait os-relax-watermarks ;;
    scylla-cdc) make scylla-up scylla-wait ;;
  esac
}

# One engine at a time is the campaign's central premise; a sweep that leaves
# its stack up hands the next engine a neighbour competing for the box.
stop_stack() {
  probe_stop || true
  make os-down >/dev/null 2>&1 || true
  make scylla-down >/dev/null 2>&1 || true
}
trap stop_stack EXIT

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

# The vector-store's own ingest counters, which is where `added/s` — documents
# entering the tantivy writer, ungated by commit — is the only throughput signal
# that survives raising the commit interval. The `/status` count the monitor
# watches moves once per commit, so at 30 s it resolves a 100 s build into three
# points; `added/s` resolves it into a hundred. Also carries the startup lines
# that state what tuning the process ACTUALLY took, which is the re-run gate:
# public vector-store 1.10.0 ignores every VS_FTS_* variable silently.
# `--since` because the container is deliberately warm across the whole ladder,
# so an unbounded `docker logs` would hand every point the entire sweep's
# history and make each point's counters unattributable. The 5 s margin absorbs
# clock skew between the harness (where `date` runs) and the SUT (where the
# daemon stamps the lines); both are EC2 under chrony, so the real skew is
# microseconds, and over-reading by 5 s is harmless where under-reading loses
# the startup tuning lines.
harvest_vector_store_log() {
  local out="$1" since="$2"
  [ "$ENGINE" = scylla-cdc ] || return 0
  docker logs --since "$since" fts-bench-vector-store > "$out" 2>&1 || true
}

run_point() {
  local conc="$1" rep="$2"
  local series="$OUT_DIR/c1-$CONFIG-c$conc-$rep.jsonl"
  local manifest="$OUT_DIR/manifest-$CONFIG-c$conc-$rep.json"
  local probe="$OUT_DIR/cpu-$CONFIG-c$conc-$rep.jsonl"
  local vslog="$OUT_DIR/vslog-$CONFIG-c$conc-$rep.log"
  local label="build-rate sweep, $CONFIG, concurrency=$conc"

  reset_index
  local since
  since=$(date -u -d '5 seconds ago' +%Y-%m-%dT%H:%M:%SZ)
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
  harvest_vector_store_log "$vslog" "$since"

  # Fatal, not a set-aside point. A knob that did not take effect is not a bad
  # sample — it means every point of this arm is measuring some other arm, and
  # the artifacts would be complete, plausible and wrongly labelled.
  if [[ "$ENGINE" == scylla-cdc ]]; then
    $PYTHON -m ftsbench.verify_arm "$TARGET_FLAG" --log "$vslog" || {
      log "ABORTING $CONFIG: the vector-store is not running this arm's tuning"
      exit 3
    }
  fi
  if [[ $status -ne 0 ]] || ! point_complete "$series"; then
    log "POINT FAILED: $CONFIG c=$conc rep=$rep (make=$status) — set aside"
    echo "$(date -u +%FT%TZ) $CONFIG c=$conc rep=$rep make=$status" >> "$FAILURES"
    for f in "$series" "$manifest" "$probe" "$vslog"; do
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
