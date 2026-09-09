#!/usr/bin/env bash
# Every run of the build-rate (write-path) campaign, spelled out.
#
# The runs are the nine lines under "the runs" below. Nothing computes them:
# reading this file IS reading the campaign, and adding or dropping a run means
# adding or dropping a line.
#
#   tools/build_rate_campaign.sh dry-run     # print every command, touch nothing
#   tools/build_rate_campaign.sh run         # execute them in order
#   tools/build_rate_campaign.sh run 2>&1 | tee campaign.log     # keep the log
#
# `set -e` stops the campaign at the first arm that fails, which is what should
# happen: an arm aborts when its knobs did not take effect, and that invalidates
# every run after it too — the artifacts would be complete, plausible and
# wrongly labelled.
#
# There is no resume. A row already measured is re-measured if it is run again,
# so run the lines you need — each one is a complete command you can paste.
#
# What each line runs is tools/sweep_build_rate.sh (one arm's ladder), and what
# one point of that runs is tools/build_rate_point.sh (--dry-run prints it).
#
# Provenance, all from BUILD-RATE-MATRIX-PLAN.md: the arms and their knob deltas
# from "The run table", the rungs, reps and the 1M cap from "Decisions locked",
# the batch levels from "The batch-size axis", and CSAT from "Measured on the
# fleet" — R4 c_sat=8 (ceiling 12,913 docs/s) and R5 c_sat=8 (13,997 docs/s),
# both at N=3 on the 1M cap.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="${ROOT:-data/build-rate-$(date -u +%Y-%m-%d)}"
KNOB_RUNGS="4 8 16 32 64 96 128"
BATCH_LEVELS="16 64 128 256 512"
CSAT=8
# "Decisions locked" says OpenSearch 512, and it has to be said out loud: with
# OS_BATCH_SIZE unset the ladder falls back to the Makefile's historical
# BATCH_SIZE=500, so the knob matrix would run 500 under a plan that says 512
# — and 500 is not comparable with the batch axis's top level. The ScyllaDB arms
# take no batch flag at all: one operation is one prepared INSERT.
OS_BATCH=512

# Locked for every run, so they are set once here rather than repeated per line.
export SWEEP_DOCS=1000000
export WARMUP=1
# Cleared rather than inherited: a BATCHES left in the caller's environment
# would turn a concurrency ladder into a batch sweep, and WORKERS would change
# the client under a label that says nothing about it.
unset BATCHES WORKERS

case "${1:-}" in
  run)     ;;
  dry-run) export DRY_RUN=1 ;;
  *) echo "usage: $0 {run|dry-run}   (the runs themselves are in this file)" >&2
     exit 2 ;;
esac

# --- the runs --------------------------------------------------------------

# P3, the five-arm knob matrix, one arm per line at N=3 over seven rungs.
# R1 first on purpose: it prices the writer buffer, and if R2 does not beat it
# by roughly the 1.42x BUILD-RATE-LOOP.md measured, the generator is binding and
# the rest of the matrix is measuring the client, not the engine.
OUT_DIR="$ROOT/knobs" LADDER="$KNOB_RUNGS" tools/sweep_build_rate.sh --scylladb-cdc-buf15             3
OUT_DIR="$ROOT/knobs" LADDER="$KNOB_RUNGS" tools/sweep_build_rate.sh --scylladb-cdc-buf376            3
OUT_DIR="$ROOT/knobs" LADDER="$KNOB_RUNGS" tools/sweep_build_rate.sh --scylladb-cdc-buf376-commit30   3
OUT_DIR="$ROOT/knobs" LADDER="$KNOB_RUNGS" OS_BATCH_SIZE="$OS_BATCH" tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh3    3
OUT_DIR="$ROOT/knobs" LADDER="$KNOB_RUNGS" OS_BATCH_SIZE="$OS_BATCH" tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh30   3

# P2, the batch axis. OpenSearch only: there a batch is a wire batch the engine
# sees, while on the CQL path every row is its own prepared statement, so a
# level would be a dispatch window inside the client and the curve would measure
# ftsbench. Each arm runs at its own measured c_sat, then one rep at 2 x c_sat
# as a pin probe — offered document pressure is c x batch, so the c_sat found at
# batch 512 can sit below the c_sat at batch 16, and pinning one concurrency
# would under-report the small levels by exactly the amount that confirms
# "a bigger batch is faster".
OUT_DIR="$ROOT/batch" BATCHES="$BATCH_LEVELS" LADDER="$CSAT"           tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh3    3
OUT_DIR="$ROOT/batch" BATCHES="$BATCH_LEVELS" LADDER="$((CSAT * 2))"   tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh3    1
OUT_DIR="$ROOT/batch" BATCHES="$BATCH_LEVELS" LADDER="$CSAT"           tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh30   3
OUT_DIR="$ROOT/batch" BATCHES="$BATCH_LEVELS" LADDER="$((CSAT * 2))"   tools/sweep_build_rate.sh --opensearch-ram-nostore-refresh30   1

# --- what turns the points into numbers ------------------------------------

[[ "${DRY_RUN:-0}" == 1 ]] && exit 0

cat <<NEXT

campaign finished. Next, per group directory:

  .venv/bin/python3 -m ftsbench.sweep_build_rate --data-dir $ROOT/knobs \\
      --output-csv $ROOT/knobs/summary.csv --output-png $ROOT/knobs/s12.png
  .venv/bin/python3 -m ftsbench.verify_cpu_usage --data-dir $ROOT/knobs
      a plateau without CPU saturation is not an engine ceiling
  .venv/bin/python3 -m ftsbench.verify_generator --cpu-series <gen-...jsonl> \\
      --batch-size N --concurrency N --achieved-docs-per-s X \\
      --ceilings <ceilings.json>
      a point within 2x of the client's own ceiling is a lower bound, not a
      ceiling; the ceilings come from tools/client_calibration.sh
  .venv/bin/python3 tools/plot_batch_ceiling.py --data-dir $ROOT/batch \\
      --c-sat opensearch-ramindex:$CSAT --c-sat opensearch-ramindex-refresh30:$CSAT
      the batch curve, backup slide B6
NEXT
