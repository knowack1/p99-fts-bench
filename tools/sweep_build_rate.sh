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
# Batch size is per-engine, and only OpenSearch has an axis to sweep (campaign
# decision 2026-09-08). There --batch-size is a wire batch: N documents, one
# _bulk, one request the engine actually sees. On ScyllaDB every row goes as its
# own prepared statement, so --batch-size was only ever a dispatch window inside
# the client; it is pinned at 1 so --concurrency is exactly the number of
# INSERTs in flight, and sweeping it would measure ftsbench rather than the
# engine.
#
# BATCHES sweeps the OpenSearch levels as an INNER loop, so the container stays
# warm across all of them. One invocation per level would pay the cold-JVM
# artifact above once per level and land it directly on the axis being measured.
#
# Each level's artifacts go to OUT_DIR/b<batch>/ with UNCHANGED filenames, so
# every summary, chart and CPU verdict is taken over a directory that is
# internally single-batch and no reader needs a new filename token to parse.
# With BATCHES empty there is one pass at the engine's own default, written
# straight to OUT_DIR exactly as before.
#
# The batch value is resolved here and passed to make rather than left to the
# Makefile's default, because the same number has to reach the point label, the
# series header and the manifest at once — and a value this script does not know
# cannot go in the label.
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
BATCHES="${BATCHES:-}"
# DRY_RUN=1 prints the plan and the exact commands a point runs, then exits
# without touching an engine — same spelling as tools/campaign_laptop.sh.
# DRY_RUN_DETAIL is how many points get the full expansion before the rest
# collapse to one line each; the commands differ between points only in the
# concurrency, batch and rep they carry.
DRY_RUN="${DRY_RUN:-0}"
DRY_RUN_DETAIL="${DRY_RUN_DETAIL:-1}"

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

# Same default as the Makefile's OS_BATCH_SIZE, including the historical shared
# BATCH_SIZE=500 that OpenSearch still falls back to, so an invocation with
# BATCHES unset asks make for the value make would have chosen on its own. The
# ScyllaDB arm has no batch to name: one operation is one prepared INSERT.
engine_batch_default() {
  case "$ENGINE" in
    opensearch) echo "${OS_BATCH_SIZE:-${BATCH_SIZE:-500}}" ;;
    scylla-cdc) echo "1" ;;
  esac
}

read -r -a BATCH_LEVELS <<<"$BATCHES"
if [[ ${#BATCH_LEVELS[@]} -eq 0 ]]; then
  BATCH_LEVELS=("$(engine_batch_default)")
  PER_BATCH_DIRS=0
else
  PER_BATCH_DIRS=1
fi

for level in "${BATCH_LEVELS[@]}"; do
  [[ "$level" =~ ^[1-9][0-9]*$ ]] || {
    echo "BATCHES must be a space-separated list of positive integers; got '$BATCHES'" >&2
    exit 2
  }
done

if [[ "$ENGINE" == scylla-cdc && ${#BATCH_LEVELS[@]} -gt 1 ]]; then
  cat >&2 <<EOF
BATCHES lists ${#BATCH_LEVELS[@]} levels (${BATCH_LEVELS[*]}) for the ScyllaDB arm $CONFIG.
There is no ScyllaDB batch axis: every row goes as its own prepared statement,
so the loader takes no batch flag at all and one operation is one INSERT. The
ceiling on this arm is found by raising concurrency alone. Pass no level.
EOF
  exit 2
fi

# The warm-up pays the cold-JVM cost once per invocation, so it is spent at the
# largest level — the one the primary matrix is expected to adopt.
largest_batch_level() {
  local best=0 level
  for level in "${BATCH_LEVELS[@]}"; do
    if [[ "$level" -gt "$best" ]]; then best="$level"; fi
  done
  echo "$best"
}
WARMUP_BATCH="$(largest_batch_level)"

resolve_point_dir() {
  local batch="$1"
  if [[ "$PER_BATCH_DIRS" == 1 ]]; then
    echo "$OUT_DIR/b$batch"
  else
    echo "$OUT_DIR"
  fi
}

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

[[ "$DRY_RUN" == 1 ]] || mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-points.log"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
say() { printf '    + %s\n' "$*"; }
say_cmd() { printf '    + '; printf '%q ' "$@"; printf '\n'; }

log "arm $CONFIG ($TARGET_VARIANT) via $TARGET_FLAG"
log "knobs: $($PYTHON -c '
import sys
from ftsbench import target
print(target.format_env(target.by_config(sys.argv[1])) or "(inherited from the env file)")' "$TARGET_CONFIG")"
log "cap=$SWEEP_DOCS reps=$REPS warmup=$WARMUP idle=$C1_IDLE_TIMEOUT settle=$SETTLE_TIMEOUT"
if [[ "$PER_BATCH_DIRS" == 1 ]]; then
  log "batch levels: ${BATCH_LEVELS[*]} — one subdirectory per level under $OUT_DIR"
else
  log "batch size: ${BATCH_LEVELS[*]} — engine default, artifacts directly in $OUT_DIR"
fi

stack_up_targets() {
  case "$ENGINE" in
    opensearch) echo "os-up os-wait os-relax-watermarks" ;;
    scylla-cdc) echo "scylla-up scylla-wait" ;;
  esac
}

start_stack() {
  if [[ "$DRY_RUN" == 1 ]]; then say "make $(stack_up_targets)"; return 0; fi
  make $(stack_up_targets)
}

# One engine at a time is the campaign's central premise; a sweep that leaves
# its stack up hands the next engine a neighbour competing for the box.
stop_stack() {
  gen_probe_stop || true
  probe_stop || true
  make os-down >/dev/null 2>&1 || true
  make scylla-down >/dev/null 2>&1 || true
}
[[ "$DRY_RUN" == 1 ]] || trap stop_stack EXIT

# Every point starts from an empty index. For OpenSearch that is a drop and
# recreate; for ScyllaDB the base table has to go too, or the next load would
# write the corpus into a table that already holds it.
RESET_CQL='DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;'

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
      printf '%s\n' "$RESET_CQL" | $CQLSH
      make scylla-schema scylla-index scylla-serving
      ;;
  esac
}

# Sampled alongside every point, because "did it saturate?" and "did it
# saturate the CPU it was given?" are different questions and the build rate
# alone answers only the first. The ScyllaDB side is two containers and a probe
# that watched one would understate the side by exactly the index's share.
PROBE_PID=""
GEN_PROBE_PID=""
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
  local -a cmd
  if [[ -n "${SUT_IP:-}" ]]; then
    cmd=(tools/sut_probe.sh start "$out" $(probe_args)
         --interval 1 --duration 0 --cache-state warm-container-fresh-index
         --label "$label")
    if [[ "$DRY_RUN" == 1 ]]; then say_cmd "${cmd[@]}"; return 0; fi
    FLEET_PROBE_OUT="$out"
    "${cmd[@]}"
  else
    cmd=($PYTHON -m ftsbench.resource_probe $(probe_args)
         --output "$out" --interval 1 --duration 0
         --cache-state warm-container-fresh-index
         --label "$label")
    if [[ "$DRY_RUN" == 1 ]]; then say_cmd "${cmd[@]}" "&"; return 0; fi
    "${cmd[@]}" >/dev/null 2>&1 &
    PROBE_PID=$!
  fi
}

# The SUT probe's counterpart on this box. G1 asks whether a point was
# client-bound, and until now nothing recorded the generator at all:
# --containers has only ever named the three SUT containers, so every
# "the client was the bottleneck" reading was inferred from the shape of a
# throughput curve. The series carries the POINT's label, which is what lets
# ftsbench.verify_generator be applied per point -- it refuses a label with no
# concurrency= rather than judging one point with another's series.
gen_probe_start() {
  local out="$1" label="$2"
  local module
  case "$ENGINE" in
    opensearch)  module=ftsbench.opensearch_load ;;
    scylla-cdc)  module=ftsbench.scylla_load ;;
    *)           module="" ;;
  esac
  [[ -n "$module" ]] || return 0
  local -a cmd=($PYTHON -m ftsbench.generator_probe --output "$out"
    --interval 1 --duration 0 --match "$module"
    --engine "$TARGET_ENGINE" --cache-state warm-container-fresh-index
    --label "$label")
  if [[ "$DRY_RUN" == 1 ]]; then say_cmd "${cmd[@]}" "&"; return 0; fi
  "${cmd[@]}" >/dev/null 2>&1 &
  GEN_PROBE_PID=$!
}

gen_probe_stop() {
  [[ -n "${GEN_PROBE_PID:-}" ]] || return 0
  kill -TERM "$GEN_PROBE_PID" 2>/dev/null || true
  wait "$GEN_PROBE_PID" 2>/dev/null || true
  GEN_PROBE_PID=""
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

explain_reset() {
  case "$ENGINE" in
    opensearch)
      say "make os-relax-watermarks"
      say "make os-reindex OS_REFRESH=$OS_REFRESH"
      say "make os-verify-analyzer"
      ;;
    scylla-cdc)
      say "printf '%s' \"\$RESET_CQL\" | $CQLSH   # $(printf '%s' "$RESET_CQL" | tr '\n' ' ')"
      say "make scylla-schema scylla-index scylla-serving"
      ;;
  esac
}

EXPLAINED=0
explain_point() {
  local conc="$1" rep="$2" batch="$3" series="$4" manifest="$5" probe="$6"
  local genprobe="$7" vslog="$8" label="$9"

  printf '\n  point: %s  concurrency=%s  batch=%s  rep=%s\n' \
    "$CONFIG" "$conc" "$batch" "$rep"
  if [[ "$EXPLAINED" -ge "$DRY_RUN_DETAIL" ]]; then
    printf '    (same commands, artifacts …-c%s-%s.*)\n' "$conc" "$rep"
    return 0
  fi
  EXPLAINED=$((EXPLAINED + 1))

  printf '  1. empty the index (every point builds from zero documents)\n'
  explain_reset
  printf '  2. start the probes (1 Hz, for the whole build)\n'
  probe_start "$probe" "$label"
  gen_probe_start "$genprobe" "$label"
  printf '  3. measure the build — this is the number the campaign reports\n'
  build_point_make "$conc" "$rep" "$batch" "$series" "$manifest" "$label"
  say_cmd make "${POINT_MAKE[@]}"
  printf '     expands to:\n'
  make -n "${POINT_MAKE[@]}" | sed 's/^/       /'
  printf '  4. stop the probes, harvest the engine log\n'
  say "SIGTERM the resource probe and the generator probe"
  [[ "$ENGINE" == scylla-cdc ]] && \
    say "docker logs --since <point start> fts-bench-vector-store > $vslog"
  printf '  5. gates — a point that fails one is set aside, not averaged in\n'
  [[ "$ENGINE" == scylla-cdc ]] && \
    say "$PYTHON -m ftsbench.verify_arm $TARGET_FLAG --log $vslog"
  say "docs_indexed >= $SWEEP_DOCS in $series"
  printf '  6. artifacts\n'
  local f
  for f in "$series" "$manifest" "$probe" "$genprobe" "$vslog"; do
    [[ "$f" == "$vslog" && "$ENGINE" != scylla-cdc ]] && continue
    say "$f"
  done
  return 0
}

# The measured work of one point, as a make invocation this script can either
# run or hand to `make -n`. Kept in one place because DRY_RUN's whole value is
# that what it prints is what a real point runs.
POINT_MAKE=()
build_point_make() {
  local conc="$1" rep="$2" batch="$3" series="$4" manifest="$5" label="$6"
  case "$ENGINE" in
    opensearch)
      POINT_MAKE=(c1-os "${MAKE_COMMON[@]}"
        "OS_REFRESH=$OS_REFRESH" "OS_CONFIG=$CONFIG"
        "OS_BATCH_SIZE=$batch"
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label"
        "C1_OS_SERIES=$series" "C1_OS_MANIFEST=$manifest") ;;
    scylla-cdc)
      POINT_MAKE=(c1-scylla-cdc "${MAKE_COMMON[@]}"
        "INGEST_CONCURRENCY=$conc" "REP=$rep" "LABEL=$label"
        "C1_SCYLLA_CDC_SERIES=$series" "C1_SCYLLA_CDC_MANIFEST=$manifest") ;;
  esac
}

run_point() {
  local conc="$1" rep="$2" batch="$3"
  local point_dir
  point_dir="$(resolve_point_dir "$batch")"
  [[ "$DRY_RUN" == 1 ]] || mkdir -p "$point_dir"
  local series="$point_dir/c1-$CONFIG-c$conc-$rep.jsonl"
  local manifest="$point_dir/manifest-$CONFIG-c$conc-$rep.json"
  local probe="$point_dir/cpu-$CONFIG-c$conc-$rep.jsonl"
  local genprobe="$point_dir/gen-$CONFIG-c$conc-$rep.jsonl"
  local vslog="$point_dir/vslog-$CONFIG-c$conc-$rep.log"
  local label="build-rate sweep, $CONFIG, concurrency=$conc batch=$batch"

  if [[ "$DRY_RUN" == 1 ]]; then
    explain_point "$conc" "$rep" "$batch" "$series" "$manifest" "$probe" \
      "$genprobe" "$vslog" "$label"
    return 0
  fi

  reset_index
  local since
  since=$(date -u -d '5 seconds ago' +%Y-%m-%dT%H:%M:%SZ)
  probe_start "$probe" "$label"
  gen_probe_start "$genprobe" "$label"
  local status=0
  build_point_make "$conc" "$rep" "$batch" "$series" "$manifest" "$label"
  make "${POINT_MAKE[@]}" || status=$?
  gen_probe_stop
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
    log "POINT FAILED: $CONFIG c=$conc batch=$batch rep=$rep (make=$status) — set aside"
    echo "$(date -u +%FT%TZ) $CONFIG c=$conc rep=$rep batch=$batch make=$status" >> "$FAILURES"
    for f in "$series" "$manifest" "$probe" "$genprobe" "$vslog"; do
      [[ -f "$f" ]] && mv "$f" "$f.failed"
    done
    return 0
  fi
}

start_stack

if [[ "$WARMUP" == 1 ]]; then
  log "warmup (discarded) — $CONFIG batch=$WARMUP_BATCH"
  run_point 8 0 "$WARMUP_BATCH" || log "warmup failed — continuing, the measured points gate themselves"
fi

for rep in $(seq 1 "$REPS"); do
  for batch in "${BATCH_LEVELS[@]}"; do
    for conc in $LADDER; do
      [[ "$DRY_RUN" == 1 ]] || log "$CONFIG concurrency=$conc batch=$batch rep=$rep"
      run_point "$conc" "$rep" "$batch"
    done
  done
done

if [[ "$DRY_RUN" == 1 ]]; then
  points=0
  for _batch in "${BATCH_LEVELS[@]}"; do
    for _conc in $LADDER; do points=$((points + REPS)); done
  done
  printf '\n  %d measured points for this arm (%d rungs x %d batch levels x %d reps)%s\n' \
    "$points" "$(wc -w <<<"$LADDER")" "${#BATCH_LEVELS[@]}" "$REPS" \
    "$([[ "$WARMUP" == 1 ]] && echo ", plus 1 discarded warm-up")"
  exit 0
fi

if [[ -s "$FAILURES" ]]; then
  log "sweep finished WITH FAILED POINTS — see $FAILURES; re-run those points before summarising"
else
  log "done — series in $OUT_DIR"
fi
