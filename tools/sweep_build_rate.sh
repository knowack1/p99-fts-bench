#!/usr/bin/env bash
# Build-rate saturation ladder: docs/s ceiling vs loader concurrency (deck S12).
#
# This file is the LOOP. What one point runs is tools/build_rate_point.sh, and
# that split is the point: the commands a rung executes used to be assembled
# from `make c1-os` — three commands produced by ~90 lines of `?=` layering and
# two `define` blocks, every recipe line `@`-prefixed — so this script's own log
# recorded which rung it was on and nothing about the loader it started.
#
# The container stays warm across the whole ladder. The C1 pass shows why: its
# OpenSearch repetition 1 read 14,112 docs/s against 22,347 and 22,370 for
# repetitions 2 and 3 — a cold-JVM artifact. Paid once per ladder point instead
# of once per campaign, that artifact would land on every point and be
# indistinguishable from a concurrency effect. The index is still rebuilt from
# empty for every point, so no point measures a build on top of another's
# documents.
#
# WARMUP=1 spends one discarded point on that artifact, at the largest batch
# level, so it lands nowhere near the axis under measurement. With WARMUP=0 the
# reps run rep-major and the artifact lands on exactly one sample — rep 1 of the
# first rung — which the per-point median of REPS absorbs; expect one visibly
# low thin line there and narrate it.
#
# Every point is capped at SWEEP_DOCS documents, not the full corpus: a ceiling
# is a steady-state rate, and the budget is what makes 40 points per engine
# affordable at enwiki scale. The cap is recorded in every series and must reach
# the chart footer.
#
# BATCHES sweeps the OpenSearch levels as an INNER loop, so the container stays
# warm across all of them; one invocation per level would pay the cold-JVM
# artifact once per level and land it directly on the axis being measured. Each
# level's artifacts go to OUT_DIR/b<batch>/ with UNCHANGED filenames, so every
# summary, chart and CPU verdict is taken over a directory that is internally
# single-batch and no reader needs a new filename token to parse. With BATCHES
# empty there is one pass at the engine's own default, written straight to
# OUT_DIR.
#
# Fleet mode: source tools/fleet_env.sh first. DOCKER_HOST carries the
# compose/exec/reset calls to the SUT; the point script runs the resource probe
# ON the SUT via tools/sut_probe.sh, because it reads local cgroups.
set -euo pipefail

cd "$(dirname "$0")/.."

ARM="${1:?usage: sweep_build_rate.sh <target-flag|opensearch|scylla-cdc> [reps]}"
# 3, the campaign's repetition count (tools/build_rate_campaign.sh), so a bare
# invocation of this ladder measures what the campaign measures. The deck's
# "5 thin lines" predates the N=3 decision in BUILD-RATE-MATRIX-PLAN.md.
REPS="${2:-3}"
LADDER="${LADDER:-8 16 32 64 96 128 192 256}"
OUT_DIR="${OUT_DIR:-data/sweep}"
PYTHON="${PYTHON:-.venv/bin/python3}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
WARMUP="${WARMUP:-0}"
BATCHES="${BATCHES:-}"
# Loader processes, passed through to ftsbench.mp_load. Off unless asked for:
# with WORKERS unset a point is exactly the single-process run every archived
# artifact was taken with.
WORKERS="${WORKERS:-}"
N_MAX="${N_MAX:-}"
# DRY_RUN=1 prints the ladder and the commands one point runs, then exits
# without touching an engine. Same spelling as tools/campaign_laptop.sh.
DRY_RUN="${DRY_RUN:-0}"

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
# BATCHES unset offers the engine the batch make would have chosen on its own.
# The ScyllaDB arm has no batch to name: one operation is one prepared INSERT.
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
  cat >&2 <<REFUSAL
BATCHES lists ${#BATCH_LEVELS[@]} levels (${BATCH_LEVELS[*]}) for the ScyllaDB arm $CONFIG.
There is no ScyllaDB batch axis: every row goes as its own prepared statement,
so the loader takes no batch flag at all and one operation is one INSERT. The
ceiling on this arm is found by raising concurrency alone. Pass no level.
REFUSAL
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

# The monitor decides a run is over by watching the engine's own document count,
# and on the ScyllaDB side that count only moves at a commit (samplers.py: the
# vector-store `/status` count IS the committed count). So a slow commit cadence
# looks exactly like a stalled build: at VS_FTS_COMMIT_INTERVAL=30s,
# fleet_env.sh's C1_IDLE_TIMEOUT=60 is two cycles, and its own comment sized it
# on the assumption of "a pure 3 s on both sides". Both timeouts are floored at
# several cadences so a quiet gap between commits is never read as a dead
# loader. Per-arm, which is why it lives here and not in the point script.
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

# Disclosed rather than implied: the container is warm across the ladder while
# the index is fresh per point, which is neither a cold start nor a warm cache.
CACHE_STATE=warm-container-fresh-index

[[ "$DRY_RUN" == 1 ]] || mkdir -p "$OUT_DIR"
FAILURES="$OUT_DIR/failed-points.log"

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
say() { printf '    + %s\n' "$*"; }

log "arm $CONFIG ($TARGET_VARIANT) via $TARGET_FLAG"
log "knobs: $($PYTHON -c '
import sys
from ftsbench import target
print(target.format_env(target.by_config(sys.argv[1])) or "(inherited from the env file)")' "$TARGET_CONFIG")"
log "cap=$SWEEP_DOCS reps=$REPS warmup=$WARMUP idle=$C1_IDLE_TIMEOUT settle=$SETTLE_TIMEOUT"
[[ -n "$WORKERS" ]] && log "loader processes: workers=$WORKERS n_max=${N_MAX:-<unset>}"
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

# One engine at a time is the campaign's central premise; a sweep that leaves its
# stack up hands the next engine a neighbour competing for the box. Both are
# taken down regardless of which one this arm brought up.
stop_stack() {
  make os-down >/dev/null 2>&1 || true
  make scylla-down >/dev/null 2>&1 || true
}
[[ "$DRY_RUN" == 1 ]] || trap stop_stack EXIT

point_command() {
  local conc="$1" rep="$2" batch="$3" point_dir="$4"
  POINT=(tools/build_rate_point.sh --arm "$TARGET_FLAG"
         --concurrency "$conc" --rep "$rep" --batch "$batch"
         --cap "$SWEEP_DOCS" --out-dir "$point_dir"
         --idle-timeout "$C1_IDLE_TIMEOUT" --settle-timeout "$SETTLE_TIMEOUT"
         --max-seconds "$MAX_SECONDS" --cache-state "$CACHE_STATE")
  [[ -n "$WORKERS" ]] && POINT+=(--workers "$WORKERS")
  [[ -n "$N_MAX" ]] && POINT+=(--n-max "$N_MAX")
  [[ "$DRY_RUN" == 1 ]] && POINT+=(--dry-run)
  return 0
}

# A truncated point is not a slow point — it silently lowers a rung of the
# median, so its files are moved aside where the summariser cannot mix them in.
# The point script is asked for its own paths rather than having them recomputed
# here: one naming rule, in the file that writes the files.
set_aside_point() {
  local conc="$1" rep="$2" batch="$3" status="$4"
  log "POINT FAILED: $CONFIG c=$conc batch=$batch rep=$rep (status=$status) — set aside"
  echo "$(date -u +%FT%TZ) $CONFIG c=$conc rep=$rep batch=$batch status=$status" >> "$FAILURES"
  local artifact
  while read -r artifact; do
    # An `if` rather than `[[ … ]] && mv`: the vslog exists on the ScyllaDB arm
    # only, so on OpenSearch the last iteration would leave the loop — and this
    # function — with a non-zero status, and `set -e` would end the ladder on a
    # point it had just handled.
    if [[ -f "$artifact" ]]; then mv "$artifact" "$artifact.failed"; fi
  done < <("${POINT[@]}" --print-artifacts)
  return 0
}

run_point() {
  local conc="$1" rep="$2" batch="$3"
  local point_dir
  point_dir="$(resolve_point_dir "$batch")"
  point_command "$conc" "$rep" "$batch" "$point_dir"

  local status=0
  "${POINT[@]}" || status=$?
  case "$status" in
    0) ;;
    # Fatal, not a set-aside point. A knob that did not take effect means every
    # point of this arm measured some other arm, and the artifacts would be
    # complete, plausible and wrongly labelled.
    3) log "ABORTING $CONFIG: the engine is not running this arm's tuning"; exit 3 ;;
    # A set-aside point is a hole in the curve, not a reason to stop: the
    # remaining rungs are still measurable and failed-points.log names what to
    # re-run.
    *) set_aside_point "$conc" "$rep" "$batch" "$status" ;;
  esac
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
  printf '\n  points for this arm = %s rungs x %s batch levels x %s reps = %s%s\n' \
    "$(wc -w <<<"$LADDER")" "${#BATCH_LEVELS[@]}" "$REPS" "$points" \
    "$([[ "$WARMUP" == 1 ]] && echo " (+1 discarded warm-up)")"
  exit 0
fi

if [[ -s "$FAILURES" ]]; then
  log "sweep finished WITH FAILED POINTS — see $FAILURES; re-run those points before summarising"
else
  log "done — series in $OUT_DIR"
fi
