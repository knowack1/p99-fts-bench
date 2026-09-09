#!/usr/bin/env bash
# Every run the build-rate (write-path) campaign intends to execute, as a table.
#
# `list` prints the table and how much of it is already on disk; `run` executes
# what is left. The roster used to be split between tools/knob_matrix.sh (the
# five-arm concurrency matrix) and tools/batch_matrix.sh (the OpenSearch
# batch-size axis), with the rungs in one, the levels in the other and the
# saturating concurrencies passed in as environment variables with no defaults.
# One table is what makes "what are we running, and what is left" answerable
# without reading three scripts.
#
#   tools/build_rate_campaign.sh              # = list
#   tools/build_rate_campaign.sh list
#   tools/build_rate_campaign.sh run          # every row that is not complete
#   tools/build_rate_campaign.sh run R2 R3    # named rows only
#   tools/build_rate_campaign.sh run --dry-run
#
# What each row runs is tools/sweep_build_rate.sh (the ladder), and what one
# point of that runs is tools/build_rate_point.sh. Those two files hold the
# mechanism; this one holds the intent.
#
# Provenance for the rows is BUILD-RATE-MATRIX-PLAN.md: the arms and their knob
# deltas from "The run table", the rungs and reps from "Decisions locked", the
# batch levels from "The batch-size axis", and the pinned concurrencies from
# "Measured on the fleet" — R4 c_sat=8 (ceiling 12,913 docs/s) and R5 c_sat=8
# (13,997 docs/s), both at N=3 on the 1M cap. A row's concurrency is written
# here WITH the run that measured it, rather than defaulted, because a default
# would be a guess about where the ceiling is inside the campaign that finds it.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python3}"
ROOT="${ROOT:-data/build-rate-$(date -u +%Y-%m-%d)}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
REPS_OVERRIDE="${REPS:-}"
# The median of 3 is the middle value, which one cold repetition CAN move — the
# median of 5 is the third, which it cannot. That is what the 2026-09-01
# no-warm-up decision rested on, so N=3 buys the warm-up point back.
WARMUP="${WARMUP:-1}"
# Manifest-to-manifest marginal at the 1M cap, measured in P1: 76-85 s of build
# wall against a ~82 s point-to-point cadence. Only ever an estimate, and it is
# printed as one.
SECONDS_PER_POINT="${SECONDS_PER_POINT:-82}"

# id | group | arm | ladder | batches | reps | workers
#
# group  the subdirectory, so a concurrency ladder and a batch axis never share
#        one directory: the summariser and plot_batch_ceiling each expect a tree
#        that is internally one shape.
# reps   N=3 for a measurement, N=1 for a probe that only has to answer
#        "does this beat the median by more than the spread".
CAMPAIGN=(
  "R1 |knobs|--scylladb-cdc-buf15              |4 8 16 32 64 96 128|                   |3|"
  "R2 |knobs|--scylladb-cdc-buf376             |4 8 16 32 64 96 128|                   |3|"
  "R3 |knobs|--scylladb-cdc-buf376-commit30    |4 8 16 32 64 96 128|                   |3|"
  "R4 |knobs|--opensearch-ram-nostore-refresh3 |4 8 16 32 64 96 128|                   |3|"
  "R5 |knobs|--opensearch-ram-nostore-refresh30|4 8 16 32 64 96 128|                   |3|"
  "R4b|batch|--opensearch-ram-nostore-refresh3 |8                  |16 64 128 256 512  |3|"
  "R4p|batch|--opensearch-ram-nostore-refresh3 |16                 |16 64 128 256 512  |1|"
  "R5b|batch|--opensearch-ram-nostore-refresh30|8                  |16 64 128 256 512  |3|"
  "R5p|batch|--opensearch-ram-nostore-refresh30|16                 |16 64 128 256 512  |1|"
)

field() { awk -F'|' -v n="$2" '{gsub(/^ +| +$/, "", $n); print $n}' <<<"$1"; }

row_id()      { field "$1" 1; }
row_group()   { field "$1" 2; }
row_arm()     { field "$1" 3; }
row_ladder()  { field "$1" 4; }
row_batches() { field "$1" 5; }
row_reps()    { echo "${REPS_OVERRIDE:-$(field "$1" 6)}"; }
row_workers() { field "$1" 7; }

row_config() { $PYTHON -m ftsbench.target "$(row_arm "$1")"; }
row_knobs() {
  $PYTHON -c '
import sys
from ftsbench import target
print(target.format_env(target.by_flag(sys.argv[1])) or "(inherited from the env file)")' \
    "$(row_arm "$1")"
}

row_out_dir() { echo "$ROOT/$(row_group "$1")"; }

# Every series this row intends to write. The naming rule is
# build_rate_point.sh's, and it is reproduced here for one reason only: to count
# what already exists without starting an engine. A point whose series was set
# aside carries a .failed suffix and is deliberately NOT counted as done.
row_series() {
  local row="$1" config out_dir batches
  config="$(row_config "$row")"
  out_dir="$(row_out_dir "$row")"
  batches="$(row_batches "$row")"
  [[ -n "$batches" ]] || batches=""
  local rep conc batch dir
  for rep in $(seq 1 "$(row_reps "$row")"); do
    for batch in ${batches:-_}; do
      dir="$out_dir"
      [[ "$batch" == _ ]] || dir="$out_dir/b$batch"
      for conc in $(row_ladder "$row"); do
        echo "$dir/c1-$config-c$conc-$rep.jsonl"
      done
    done
  done
}

row_counts() {
  local row="$1" planned=0 done_count=0 series
  while read -r series; do
    planned=$((planned + 1))
    [[ -f "$series" ]] && done_count=$((done_count + 1))
  done < <(row_series "$row")
  echo "$planned $done_count"
}

human_hours() {
  awk -v secs="$1" 'BEGIN { printf "%.1f h", secs / 3600 }'
}

list_campaign() {
  local total=0 remaining=0
  printf '\nbuild-rate campaign — %s docs per point, artifacts under %s\n\n' \
    "$SWEEP_DOCS" "$ROOT"
  printf '  %-4s %-6s %-34s %-21s %-19s %-4s %6s %6s\n' \
    id group arm rungs batches reps points done
  local row counts planned complete
  for row in "${CAMPAIGN[@]}"; do
    counts="$(row_counts "$row")"
    planned="${counts% *}"
    complete="${counts#* }"
    total=$((total + planned))
    remaining=$((remaining + planned - complete))
    printf '  %-4s %-6s %-34s %-21s %-19s %-4s %6s %6s%s\n' \
      "$(row_id "$row")" "$(row_group "$row")" "$(row_config "$row")" \
      "$(row_ladder "$row")" "$(row_batches "$row")" "$(row_reps "$row")" \
      "$planned" "$complete" \
      "$([[ "$planned" == "$complete" ]] && echo '  complete')"
    printf '  %-4s %-6s %s\n' "" "" "$(row_knobs "$row")"
  done
  printf '\n  %d points planned, %d done, %d left (~%s at the measured %ss cadence)\n' \
    "$total" "$((total - remaining))" "$remaining" \
    "$(human_hours $((remaining * SECONDS_PER_POINT)))" "$SECONDS_PER_POINT"
  printf '  plus one discarded warm-up per row (WARMUP=%s)\n\n' "$WARMUP"
  cat <<NEXT
  run it with:   tools/build_rate_campaign.sh run
  see a point:   tools/build_rate_point.sh --arm <flag> --concurrency 8 --rep 1 --dry-run

  Afterwards — the numbers, then the two gates that say whose ceiling it was:
    $PYTHON -m ftsbench.sweep_build_rate --data-dir $ROOT/knobs \\
        --output-csv $ROOT/knobs/summary.csv --output-png $ROOT/knobs/s12.png
    $PYTHON -m ftsbench.verify_cpu_usage --data-dir $ROOT/knobs
        a plateau without CPU saturation is not an engine ceiling
    $PYTHON -m ftsbench.verify_generator --cpu-series <gen-…jsonl> \\
        --batch-size N --concurrency N --achieved-docs-per-s X \\
        --ceilings <ceilings.json>
        a point within 2x of the client's own ceiling is a lower bound, not a
        ceiling; the ceilings come from tools/client_calibration.sh
    $PYTHON tools/plot_batch_ceiling.py --data-dir $ROOT/batch \\
        --c-sat opensearch-ramindex:8 --c-sat opensearch-ramindex-refresh30:8
        the batch curve, backup slide B6
NEXT
}

log() { printf '\n######## [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

run_row() {
  local row="$1" dry="$2"
  local id out_dir log_dir
  id="$(row_id "$row")"
  out_dir="$(row_out_dir "$row")"
  log_dir="$out_dir/logs"
  log "ROW $id — $(row_config "$row") rungs='$(row_ladder "$row")'" \
      "batches='$(row_batches "$row")' reps=$(row_reps "$row") cap=$SWEEP_DOCS"

  local -a env=(
    "OUT_DIR=$out_dir" "LADDER=$(row_ladder "$row")"
    "BATCHES=$(row_batches "$row")" "SWEEP_DOCS=$SWEEP_DOCS"
    "WARMUP=$WARMUP" "WORKERS=$(row_workers "$row")"
  )
  if [[ "$dry" == 1 ]]; then
    env+=("DRY_RUN=1")
    env "${env[@]}" tools/sweep_build_rate.sh "$(row_arm "$row")" "$(row_reps "$row")"
    return 0
  fi
  mkdir -p "$log_dir"
  # An arm that aborts did so because its knobs did not take effect, which
  # invalidates every row after it too: continuing would fill the matrix with
  # plausible, wrongly-labelled ladders.
  if ! env "${env[@]}" tools/sweep_build_rate.sh \
        "$(row_arm "$row")" "$(row_reps "$row")" 2>&1 \
        | tee "$log_dir/$id.log"; then
    log "ROW FAILED: $id — see $log_dir/$id.log; campaign stopped"
    exit 1
  fi
}

run_campaign() {
  local dry=0
  local -a wanted=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry=1; shift ;;
      *) wanted+=("$1"); shift ;;
    esac
  done

  local row id counts
  for row in "${CAMPAIGN[@]}"; do
    id="$(row_id "$row")"
    if [[ ${#wanted[@]} -gt 0 ]] && ! printf '%s\n' "${wanted[@]}" | grep -qx "$id"; then
      continue
    fi
    counts="$(row_counts "$row")"
    if [[ "$dry" == 0 && "${counts% *}" == "${counts#* }" ]]; then
      log "ROW $id already complete — skipping (delete its series to re-run)"
      continue
    fi
    run_row "$row" "$dry"
  done
  [[ "$dry" == 1 ]] || log "campaign finished — summarise with: tools/build_rate_campaign.sh list"
}

case "${1:-list}" in
  list) list_campaign ;;
  run)  shift; run_campaign "$@" ;;
  *) echo "usage: $0 [list|run [--dry-run] [id...]]" >&2; exit 2 ;;
esac
