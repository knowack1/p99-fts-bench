#!/usr/bin/env bash
# The batch-size axis on the two OpenSearch arms (2026-09-08).
#
#   R4 opensearch-ramindex           tmpfs segments, _source off, refresh 3 s
#   R5 opensearch-ramindex-refresh30 + refresh 30 s
#
# Levels: --batch-size 16 64 128 256 512. Only OpenSearch is swept: there a
# batch is a wire batch — N documents, one _bulk, one request the engine sees.
# The ScyllaDB arms run pinned at --batch-size 1 and contribute a sentence
# rather than a series; sweep_build_rate.sh refuses a multi-level BATCHES on
# them for that reason.
#
# Two passes per arm, one invocation of sweep_build_rate.sh each, so the
# container stays warm across all five levels of a pass:
#
#   pass csat  LADDER=<c_sat>      REPS=3  the measurement
#   pass pin   LADDER=<2 x c_sat>  REPS=1  the pin probe
#
# The pin probe exists because offered document pressure is `c x batch`, so the
# c_sat measured at batch 512 can sit BELOW c_sat at batch 16. Pinning one
# concurrency would then under-report the small batches by exactly the amount
# that confirms "a bigger batch is faster". A probe that beats its level's
# median by more than the rep spread means that level has not been shown to be a
# ceiling: escalate it to a three-rung mini-ladder at N=3, or draw it as a lower
# bound with the gate that marked it named.
#
# c_sat comes from the five-arm concurrency matrix (tools/knob_matrix.sh), per
# arm, and is REQUIRED: R4_CSAT and R5_CSAT have no defaults. A default here
# would be a guess about where the ceiling is, inside the run that exists to
# find it. LADDER is set per pass rather than exported, so an inherited LADDER
# cannot silently replace those rungs.
#
# 2 arms x 5 levels x 3 reps = 30, + 2 arms x 5 levels x 1 probe = 10,
# + 4 warm-ups = 44 points, ~1.4 h at the $4.37/h fleet rate.
#
# Both arms share one OUT_DIR; each level's points land in OUT_DIR/b<batch>/,
# which is what keeps every summary, chart and CPU verdict single-batch. The two
# passes of an arm write into the same level directories at different rungs, so
# every measured point composes into a two-rung ladder per level. The only file
# the second pass rewrites is the first pass's rep-0 warm-up, which
# sweep_build_rate's summariser discards by repetition number anyway.
set -euo pipefail
cd "$(dirname "$0")/.."

REPS="${1:-3}"
OUT_DIR="${OUT_DIR:-data/sweep-batch-$(date -u +%Y-%m-%d)}"
LOG_DIR="${LOG_DIR:-$OUT_DIR/logs}"
BATCHES="${BATCHES:-16 64 128 256 512}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
# One discarded point per invocation, at the largest level, absorbs the cold-JVM
# artifact that would otherwise land on whichever level ran first — which is the
# axis under measurement here.
WARMUP="${WARMUP:-1}"
# The pin probe is a check, not a measurement: one rep is what makes it cheap
# enough to run at every level, and its verdict is "does this beat the median by
# more than the spread", which one sample answers.
PIN_REPS=1
# DRY_RUN=1 explains the axis instead of running it: no stack, no
# directories, no log files — see tools/explain_build_rate.sh.
DRY_RUN="${DRY_RUN:-0}"

ARMS=(
  --opensearch-ram-nostore-refresh3
  --opensearch-ram-nostore-refresh30
)
CSAT_VARS=(R4_CSAT R5_CSAT)

export OUT_DIR BATCHES SWEEP_DOCS WARMUP DRY_RUN

log() { printf '\n######## [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

require_csat() {
  local name="$1"
  if [[ ! "${!name:-}" =~ ^[1-9][0-9]*$ ]]; then
    log "MISSING $name: the batch axis runs at each arm's own c_sat from the concurrency matrix."
    log "Set R4_CSAT and R5_CSAT to the saturating concurrency measured for those arms, e.g. R4_CSAT=64 R5_CSAT=64 tools/batch_matrix.sh"
    exit 2
  fi
}

run_pass() {
  local arm="$1" rung="$2" reps="$3" tag="$4"
  local name="${arm#--}"
  log "ARM $arm pass=$tag (c=$rung, reps=$reps, batches='$BATCHES', cap=$SWEEP_DOCS)"
  if [[ "$DRY_RUN" == 1 ]]; then
    LADDER="$rung" tools/sweep_build_rate.sh "$arm" "$reps"
    return 0
  fi
  if ! LADDER="$rung" tools/sweep_build_rate.sh "$arm" "$reps" 2>&1 \
      | tee "$LOG_DIR/$name-$tag.log"; then
    # An arm aborts only when its knobs did not take effect, which invalidates
    # every level after it too. Stopping is the point: continuing would fill the
    # axis with plausible, wrongly-labelled curves.
    log "ARM FAILED: $arm pass=$tag — see $LOG_DIR/$name-$tag.log; matrix stopped"
    exit 1
  fi
}

for var in "${CSAT_VARS[@]}"; do
  require_csat "$var"
done

[[ "$DRY_RUN" == 1 ]] || mkdir -p "$OUT_DIR" "$LOG_DIR"

for i in "${!ARMS[@]}"; do
  arm="${ARMS[$i]}"
  csat="${!CSAT_VARS[$i]}"
  run_pass "$arm" "$csat" "$REPS" csat
  run_pass "$arm" "$((csat * 2))" "$PIN_REPS" pin
done

[[ "$DRY_RUN" == 1 ]] && exit 0

log "batch axis complete — $(ls "$OUT_DIR"/b*/c1-*.jsonl 2>/dev/null | wc -l) series in $OUT_DIR (rep-0 warm-ups included)"
if [[ -s "$OUT_DIR/failed-points.log" ]]; then
  log "SOME POINTS WERE SET ASIDE — re-run them before summarising:"
  cat "$OUT_DIR/failed-points.log" >&2
fi
