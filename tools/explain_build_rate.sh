#!/usr/bin/env bash
# What the build-rate (write-path) campaign actually measures — printed, not run.
#
# The measurement is defined across four layers, and reading it out of the
# Makefile means resolving `?=` defaults, three `define` blocks and two levels of
# variable indirection before the loader's flags become visible. This script
# collapses that: it walks the real drivers with DRY_RUN=1 and prints, per
# measured point, the reset, the probes, the expanded `make` recipe (via
# `make -n`, so it is the recipe make would run rather than a second copy of it),
# the gates and the artifacts.
#
# Nothing here restates the campaign. Arms and their knobs come from
# ftsbench/target.py, ladders/caps/reps from the driver scripts' own defaults,
# and the measured commands from the Makefile itself — so this output goes stale
# only if the campaign changes, in which case it is the output that is right.
#
#   tools/explain_build_rate.sh              # the whole campaign
#   tools/explain_build_rate.sh knobs        # P3 only, the five-arm matrix
#   tools/explain_build_rate.sh batch        # P2 only, the batch-size axis
#   tools/explain_build_rate.sh point        # one arm, one point, full detail
#
# Add `source tools/fleet_env.sh` first to see the fleet's URLs and caps instead
# of localhost: every knob is a plain environment variable, so what this prints
# is what that environment would run.
set -euo pipefail
cd "$(dirname "$0")/.."

WHAT="${1:-all}"
PYTHON="${PYTHON:-.venv/bin/python3}"

# P3's measured saturating concurrencies (BUILD-RATE-MATRIX-PLAN.md, "Measured
# on the fleet": R4 c_sat=8 / 12,913 docs/s, R5 c_sat=8 / 13,997 docs/s). The
# batch axis refuses to run without them, and a default inside batch_matrix.sh
# would be a guess about where the ceiling is inside the run that finds it.
R4_CSAT="${R4_CSAT:-8}"
R5_CSAT="${R5_CSAT:-8}"

heading() {
  printf '\n\n==============================================================\n'
  printf '%s\n' "$*"
  printf '==============================================================\n'
}

explain_arms() {
  heading "The arms — one deployment each, knobs from ftsbench/target.py"
  $PYTHON - <<'PY'
from ftsbench import target
for arm in target.TARGETS:
    if arm.engine == target.VECTOR_STORE or arm.config == "scylla-bootstrap":
        continue
    knobs = target.format_env(arm) or "(inherited from the env file)"
    print(f"  {arm.config:<32} {arm.flag}")
    print(f"  {'':<32} {arm.variant}")
    print(f"  {'':<32} {knobs}\n")
PY
  printf '  The five the build-rate matrix runs are R1 scylla-cdc-buf15,\n'
  printf '  R2 scylla-cdc-buf376, R3 scylla-cdc-buf376-commit30,\n'
  printf '  R4 opensearch-ramindex, R5 opensearch-ramindex-refresh30.\n'
}

explain_point() {
  heading "One point, in full — the unit every pass repeats"
  DRY_RUN=1 LADDER=8 OUT_DIR=data/sweep-knobs-DATE WARMUP=0 \
    tools/sweep_build_rate.sh --scylladb-cdc-buf376 1
}

explain_knobs() {
  heading "P3 — the five-arm concurrency matrix (tools/knob_matrix.sh 3)"
  DRY_RUN=1 tools/knob_matrix.sh 3
}

explain_batch() {
  heading "P2 — the batch-size axis, OpenSearch only (tools/batch_matrix.sh 3)"
  printf '  Run as: R4_CSAT=%s R5_CSAT=%s tools/batch_matrix.sh 3\n' \
    "$R4_CSAT" "$R5_CSAT"
  DRY_RUN=1 R4_CSAT="$R4_CSAT" R5_CSAT="$R5_CSAT" tools/batch_matrix.sh 3
}

explain_after() {
  heading "What turns the points into numbers"
  cat <<'AFTER'
  Per arm, after its points are complete:

    + .venv/bin/python3 -m ftsbench.verify_cpu_usage <OUT_DIR>
        a plateau without CPU saturation is not an engine ceiling
    + .venv/bin/python3 -m ftsbench.verify_generator <gen series> <ceilings.json>
        a point within 2x of the client's own ceiling is a lower bound, not a
        ceiling; the ceilings come from P0, tools/client_calibration.sh
    + tools/plot_batch_ceiling.py            the batch curve (backup slide B6)

  The signals each point records — build_monitor series, vector-store counters,
  resource probe, generator probe, manifest, summary.csv — are tabulated in
  BUILD-RATE-MATRIX-PLAN.md, "Signals recorded per point". The slides these
  feed (S11-S15) are in WRITE-PATH-TEST-PLAN.md.
AFTER
}

case "$WHAT" in
  all)   explain_arms; explain_point; explain_knobs; explain_batch; explain_after ;;
  arms)  explain_arms ;;
  point) explain_point ;;
  knobs) explain_knobs ;;
  batch) explain_batch ;;
  after) explain_after ;;
  *) echo "usage: $0 [all|arms|point|knobs|batch|after]" >&2; exit 2 ;;
esac
