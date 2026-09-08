#!/usr/bin/env bash
# The five-arm build-rate knob matrix (2026-09-08).
#
# Two knobs isolated on the ScyllaDB side — the tantivy writer buffer and the
# commit cadence — against OpenSearch's RAM index at matching refresh cadences:
#
#   R1 scylla-cdc-buf15              threshold off, writer buffer UNSET (15 MB floor)
#   R2 scylla-cdc-buf376             + 376 MB/thread  (writer-budget parity)
#   R3 scylla-cdc-buf376-commit30    + 30 s commit interval
#   R4 opensearch-ramindex           tmpfs segments, _source off, refresh 3 s
#   R5 opensearch-ramindex-refresh30 + refresh 30 s
#
# R2 pairs with R4 (both 3 s) and R3 with R5 (both 30 s). R1 is the reference
# that prices the writer buffer, and it is deliberately first: if R2 does not
# beat it by roughly the 1.42x BUILD-RATE-LOOP.md measured, the generator is
# binding and the rest of the matrix is measuring the client. Run
# tools/generator_calibration.sh before this, not after.
#
# One arm per invocation of sweep_build_rate.sh, so each gets a stack built with
# its own knobs — the vector-store reads them at startup, not per index — and
# the sweep's EXIT trap tears the previous one down. Arms share one OUT_DIR so
# the summariser sees the whole matrix.
set -euo pipefail
cd "$(dirname "$0")/.."

REPS="${1:-3}"
OUT_DIR="${OUT_DIR:-data/sweep-knobs-$(date -u +%Y-%m-%d)}"
LOG_DIR="${LOG_DIR:-$OUT_DIR/logs}"
LADDER="${LADDER:-4 8 16 32 64 96 128 192 256}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
# The median of 3 is the middle value, which one cold repetition CAN move — the
# median of 5 is the third, which it cannot. That is what the 2026-09-01
# no-warm-up decision rested on, so dropping to N=3 buys the warm-up point back.
WARMUP="${WARMUP:-1}"

ARMS=(
  --scylladb-cdc-buf15
  --scylladb-cdc-buf376
  --scylladb-cdc-buf376-commit30
  --opensearch-ram-nostore-refresh3
  --opensearch-ram-nostore-refresh30
)

export OUT_DIR LADDER SWEEP_DOCS WARMUP
mkdir -p "$OUT_DIR" "$LOG_DIR"

log() { printf '\n######## [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

for arm in "${ARMS[@]}"; do
  name="${arm#--}"
  log "ARM $arm  (reps=$REPS, ladder='$LADDER', cap=$SWEEP_DOCS)"
  if ! tools/sweep_build_rate.sh "$arm" "$REPS" 2>&1 | tee "$LOG_DIR/$name.log"; then
    # An arm aborts only when its knobs did not take effect, which invalidates
    # every point after it too. Stopping is the point: continuing would fill the
    # matrix with plausible, wrongly-labelled ladders.
    log "ARM FAILED: $arm — see $LOG_DIR/$name.log; matrix stopped"
    exit 1
  fi
done

log "matrix complete — $(ls "$OUT_DIR"/c1-*.jsonl 2>/dev/null | wc -l) points in $OUT_DIR"
if [[ -s "$OUT_DIR/failed-points.log" ]]; then
  log "SOME POINTS WERE SET ASIDE — re-run them before summarising:"
  cat "$OUT_DIR/failed-points.log" >&2
fi
