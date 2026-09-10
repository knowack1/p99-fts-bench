#!/usr/bin/env bash
# One arm of the loader-capability campaign: one engine at one worker count,
# over the concurrency ladder, N repetitions.
#
#   tools/loader_capability_sweep.sh opensearch 4 3
#   DRY_RUN=1 tools/loader_capability_sweep.sh scylladb 8 1
#
# What one point runs is written out in `run_point` below — the probe, the N
# loaders, and the gates — with every flag visible. The campaign that decides
# which arms exist is tools/loader_capability_campaign.sh.
#
# WHY N SEPARATE LOADER PROCESSES RATHER THAN `--workers N`. ftsbench.mp_load
# spawns its workers, and a spawned child's argv is
# `python -c 'from multiprocessing.spawn import spawn_main; ...'` — no `-m`, no
# module name. generator_probe.LoaderFinder._is_loader matches on `-m <module>`
# adjacency, so against mp_load it finds only the parent, which after
# notify_all() sits in future.result() using no CPU. Every CPU number would be
# an idle process's and the gates below would pass on it. Forking N loaders is
# what tools/client_calibration.sh already does, it is what P0 measured with,
# and the probe's default matches see it. (The same blindness applies to
# build_rate_point.sh --workers 4; that is a separate repair.)
#
# TWO THINGS THAT WILL BITE. The sink is one asyncio loop in one process
# (ftsbench/sink_counters.py): P0's N=4 OpenSearch point already pushed
# ~400 MB/s of _bulk bodies through one, so the loaders are spread round-robin
# across two sink instances per mode — no code change, every loader takes its
# own endpoint. And the sink needs TCP_QUICKACK, which it sets: without it a
# c=8 point measured 404 docs/s against 7,845, a 40 ms kernel timer reported as
# the client's ceiling (ftsbench/sink_tcp.py carries it).
set -euo pipefail
cd "$(dirname "$0")/.."

ENGINE="${1:?usage: $0 <opensearch|scylladb> <workers> [reps]}"
WORKERS="${2:?usage: $0 <opensearch|scylladb> <workers> [reps]}"
REPS="${3:-3}"

ROOT="${ROOT:-data/loader-cap-$(date -u +%Y-%m-%d)}"
OUT_DIR="${OUT_DIR:-$ROOT/points}"
# The warm-up lands here rather than beside the measured points:
# client_ceilings.POINT_RE matches r0 as readily as r1, and a discarded
# repetition folded into a median is a warm-up published.
WARMUP_DIR="${WARMUP_DIR:-$OUT_DIR/warmup}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
CORPUS_DIR="${CORPUS_DIR:-$ROOT/corpus}"

# Multiples of lcm(4,6,8) = 24, so every worker count divides every rung
# exactly -- see the campaign for why an unbalanced rung is a spurious slope.
LADDER="${LADDER:-24 48 96 192 384}"
WARMUP="${WARMUP:-1}"
OS_BATCH="${OS_BATCH:-512}"
SCYLLA_BATCH=1
DOCS_OS="${DOCS_OS:-1200000}"
DOCS_SCYLLA="${DOCS_SCYLLA:-400000}"
CORPUS_DOCS="${CORPUS_DOCS:-$DOCS_OS}"
PROBE_INTERVAL="${PROBE_INTERVAL:-0.5}"

SINK_HOST="${SINK_HOST:-127.0.0.1}"
HTTP_PORTS="${HTTP_PORTS:-9200 9201}"
CQL_PORTS="${CQL_PORTS:-9042 9043}"

PYTHON="${PYTHON:-.venv/bin/python3}"
DRY_RUN="${DRY_RUN:-0}"
# G5 refuses a point whose swap grew at all, because on a dedicated box any
# growth means the measurement includes reclaim. A shared workstation is not a
# dedicated box: with zram in the mix SwapFree moves a page in both directions
# while nothing at all is under pressure, so a local ladder needs a floor under
# which movement is background rather than this point's. Zero here keeps the
# fleet's behaviour exactly as it was.
MAX_SWAP_GROWTH_BYTES="${MAX_SWAP_GROWTH_BYTES:-0}"
export MAX_SWAP_GROWTH_BYTES

PROBE_PID=""
FIRST_EXITED=""
FAILED=0
LOADER_ARGV=()

log() { printf '\n######## [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
show() { printf '+ %s\n' "$(printf '%q ' "$@")" >&2; }

execute() {
  show "$@"
  [[ "$DRY_RUN" == 1 ]] && return 0
  "$@"
}

engine_module() {
  case "$ENGINE" in
    opensearch) echo ftsbench.opensearch_load ;;
    scylladb) echo ftsbench.scylla_load ;;
    *) log "unknown engine: $ENGINE"; exit 2 ;;
  esac
}

engine_batch() {
  case "$ENGINE" in
    opensearch) echo "$OS_BATCH" ;;
    scylladb) echo "$SCYLLA_BATCH" ;;
  esac
}

engine_docs() {
  case "$ENGINE" in
    opensearch) echo "$DOCS_OS" ;;
    scylladb) echo "$DOCS_SCYLLA" ;;
  esac
}

engine_ports() {
  case "$ENGINE" in
    opensearch) echo "$HTTP_PORTS" ;;
    scylladb) echo "$CQL_PORTS" ;;
  esac
}

# Round-robin, so a point's N loaders are spread over the sink instances rather
# than queueing behind one single-threaded event loop.
port_for_shard() {  # shard
  local ports
  read -r -a ports <<< "$(engine_ports)"
  echo "${ports[$(( $1 % ${#ports[@]} ))]}"
}

engine_endpoint_args() {  # port
  case "$ENGINE" in
    opensearch) echo "--url http://$SINK_HOST:$1" ;;
    scylladb) echo "--hosts $SINK_HOST --port $1" ;;
  esac
}

# One part's share of a whole-run budget, remainder included. Used for both the
# concurrency and the document cap, because both are compared against exactly
# afterwards: a plain total/workers leaves up to workers-1 unallocated, which
# for documents makes the completeness gate refuse a point for an arithmetic
# reason and for concurrency means the rung labelled c=24 offered 23.
worker_share() {  # total index
  local total=$1 index=$2
  local share=$((total / WORKERS))
  if (( index < total % WORKERS )); then
    share=$((share + 1))
  fi
  (( share < 1 )) && share=1
  echo "$share"
}

generate_corpus() {
  local stem="$CORPUS_DIR/corpus-w$WORKERS"
  mkdir -p "$CORPUS_DIR"
  [[ -s "$stem-0.jsonl" ]] && return 0
  log "synthetic corpus: $CORPUS_DOCS docs across $WORKERS shard(s), enwiki's mean line"
  execute "$PYTHON" -m ftsbench.synth_corpus --output "$stem.jsonl" \
    --docs "$CORPUS_DOCS" --shards "$WORKERS" --stats-out "$stem-stats.json"
  if [[ "$WORKERS" == 1 && "$DRY_RUN" != 1 ]]; then
    mv "$stem.jsonl" "$stem-0.jsonl"
  fi
}

start_probe() {  # series label
  show "$PYTHON" -m ftsbench.generator_probe --output "$1" \
    --interval "$PROBE_INTERVAL" --engine "$ENGINE" --label "$2" \
    --corpus synthetic
  [[ "$DRY_RUN" == 1 ]] && return 0
  "$PYTHON" -m ftsbench.generator_probe --output "$1" \
    --interval "$PROBE_INTERVAL" --engine "$ENGINE" --label "$2" \
    --corpus synthetic >> "$LOG_DIR/probe.log" 2>&1 &
  PROBE_PID=$!
}

stop_probe() {
  [[ -n "$PROBE_PID" ]] || return 0
  kill -TERM "$PROBE_PID" 2>/dev/null || true
  wait "$PROBE_PID" 2>/dev/null || true
  PROBE_PID=""
}

trap stop_probe EXIT

# A point's measured window is the one where all N loaders are running, and it
# ends at the FIRST exit, not the last. Waiting for every loader before stopping
# the probe leaves the drain inside the series: the trailing ticks count fewer
# than N loaders, which refuses the point at G1 and drags its CPU median toward
# an idle box. WARM_IN_TICKS drops the mirror-image ticks at the head, where the
# loaders are still coming up; this is the same exclusion at the other end.
stop_probe_at_first_exit() {  # pids
  local reaped="" rc=0
  wait -n -p reaped "$@" || rc=1
  FIRST_EXITED="$reaped"
  stop_probe
  return "$rc"
}

await_remaining_loaders() {  # pids
  local pid rc=0
  for pid in "$@"; do
    [[ -n "$FIRST_EXITED" && "$pid" == "$FIRST_EXITED" ]] && continue
    wait "$pid" || rc=1
  done
  return "$rc"
}

loader_argv() {  # port corpus batch conc cap latency_log label
  local endpoint module
  module=$(engine_module)
  endpoint=$(engine_endpoint_args "$1")
  # The ScyllaDB loader has no --batch-size: one operation is one prepared
  # INSERT, so the flag is rejected rather than ignored. The level stays in the
  # point key, where it reads 1 for that engine.
  local batch_flag=(--batch-size "$3")
  [[ "$ENGINE" == scylladb ]] && batch_flag=()
  # shellcheck disable=SC2206,SC2207  # $endpoint is two words on purpose
  LOADER_ARGV=("$PYTHON" -m "$module" $endpoint --corpus "$2"
    "${batch_flag[@]}" --concurrency "$4" --max-docs "$5"
    --latency-log "$6" --cache-state warm-page-cache --label "$7")
}

point_artifacts() {  # dir stem
  local shard
  echo "$1/gen-$2.jsonl"
  for (( shard = 0; shard < WORKERS; shard++ )); do
    echo "$1/lat-$2-s$shard.jsonl"
  done
}

set_aside_point() {  # dir stem reason
  local path
  while read -r path; do
    [[ -e "$path" ]] && mv "$path" "$path.failed"
  done < <(point_artifacts "$1" "$2")
  printf '%s: %s\n' "$2" "$3" >> "$ROOT/failed-points.log"
  FAILED=$((FAILED + 1))
  log "POINT SET ASIDE $2: $3"
}

# The gates, in one place, because a point that fails any of them is not a
# measurement of the client. Each exists because its absence lets a particular
# wrong answer look like a right one.
gate_point() {  # dir stem workers expected_docs
  "$PYTHON" - "$@" <<'GATE'
import json
import os
import sys
from pathlib import Path

directory, stem, workers, expected = sys.argv[1:5]
workers, expected = int(workers), int(expected)
# client_ceilings discards the first two probe ticks: the first has no rate at
# all (it is a difference) and the second still covers connect and prepare.
WARM_IN_TICKS = 2
MIN_TICKS = 5
WALL_SPREAD = 1.1
MAX_STEAL_CORES = 0.05
MAX_SWAP_GROWTH_BYTES = int(os.environ.get("MAX_SWAP_GROWTH_BYTES") or 0)
problems = []


def records(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


shards = [records(Path(directory) / f"lat-{stem}-s{shard}.jsonl")
          for shard in range(workers)]
ticks = [r for r in records(Path(directory) / f"gen-{stem}.jsonl")
         if (r.get("i") or 0) >= WARM_IN_TICKS]
box = [r for r in ticks if r.get("record") == "generator_box_sample"]

# G1 — the probe found the loaders. Without this, "the probe matched nothing"
# and "the loaders used no CPU" are the same picture, and the CPU chart is of an
# idle box.
seen = {r.get("loaders_running") for r in box}
if not box:
    problems.append("G1 loaders_running: no box ticks to judge")
elif seen != {workers}:
    problems.append(f"G1 loaders_running: saw {sorted(seen)}, want {workers} on "
                    f"every tick — the probe is not watching the loaders")

# G2 — every document offered, none refused. Dividing by documents the sink
# rejected reports refused work as delivered throughput.
ops = [r for shard in shards for r in shard if r.get("record") == "latency_op"]
errors = [r for r in ops if not r.get("ok")]
docs = sum(r.get("n_docs") or 0 for r in ops if r.get("ok"))
if errors:
    problems.append(f"G2 errors: {len(errors)} operation(s) failed, "
                    f"first={errors[0].get('error')!r}")
if docs != expected:
    problems.append(f"G2 docs: {docs} delivered against {expected} offered")

# G3 — long enough to carry a CPU figure at all.
if len(box) < MIN_TICKS:
    problems.append(f"G3 duration: {len(box)} usable probe tick(s), need "
                    f"{MIN_TICKS} — too short for a CPU figure")

# G4 — the shards finished together. The run is over when the last loader is,
# so a straggler shard's wall is the whole point's rate.
walls = [max((r.get("t_end_s") or 0.0 for r in shard), default=0.0)
         for shard in shards]
if walls and min(walls) > 0 and max(walls) / min(walls) > WALL_SPREAD:
    problems.append(f"G4 balance: shard walls {min(walls):.1f}..{max(walls):.1f}s "
                    f"spread more than {WALL_SPREAD}x")

# G5 — the box was ours alone. Steal time or growing swap makes a per-core
# figure a figure about the hypervisor.
steal = max((r.get("steal_cores") or 0.0 for r in box), default=0.0)
if steal > MAX_STEAL_CORES:
    problems.append(f"G5 steal: {steal:.3f} cores stolen — a noisy neighbour")
swap = [r["swap_used_bytes"] for r in box if r.get("swap_used_bytes") is not None]
swap_growth = max(swap) - min(swap) if swap else 0
if swap_growth > MAX_SWAP_GROWTH_BYTES:
    problems.append(f"G5 swap: grew {swap_growth} bytes during the point, over "
                    f"the {MAX_SWAP_GROWTH_BYTES} byte tolerance")

# Printed either way: a rate that flattened while the box sat far below its
# cores is the shape to distrust, and the reader needs the number to see it.
peak = max((r.get("cpu_cores_used") or 0.0 for r in box), default=0.0)
cores = max((r.get("cores_available") or 0 for r in box), default=0)
if cores:
    print(f"{stem}: box peak {peak:.2f}/{cores} cores ({peak / cores:.0%}), "
          f"{len(box)} usable ticks, swap moved {swap_growth} bytes",
          file=sys.stderr)

for problem in problems:
    print(f"GATE FAIL {stem}: {problem}", file=sys.stderr)
raise SystemExit(1 if problems else 0)
GATE
}

run_point() {  # dir conc rep
  local dir=$1 conc=$2 rep=$3 batch docs
  batch=$(engine_batch)
  docs=$(engine_docs)
  local stem="$ENGINE-b$batch-c$conc-w$WORKERS-r$rep"
  # The label names the point the way verify_generator checks it: the gate
  # refuses a series whose label does not say which rung it covers, because a
  # probe over a whole pass would otherwise answer for every point. workers= is
  # here too, which the build-rate campaign's label still does not carry.
  local label="loader capability, engine=$ENGINE batch=$batch concurrency=$conc workers=$WORKERS"
  local total=0 cap pids=() status=0 shard

  mkdir -p "$dir"
  start_probe "$dir/gen-$stem.jsonl" "$label"
  for (( shard = 0; shard < WORKERS; shard++ )); do
    cap=$(worker_share "$docs" "$shard")
    total=$((total + cap))
    loader_argv "$(port_for_shard "$shard")" \
      "$CORPUS_DIR/corpus-w$WORKERS-$shard.jsonl" "$batch" \
      "$(worker_share "$conc" "$shard")" "$cap" \
      "$dir/lat-$stem-s$shard.jsonl" "$label"
    show "${LOADER_ARGV[@]}"
    [[ "$DRY_RUN" == 1 ]] && continue
    "${LOADER_ARGV[@]}" > "$LOG_DIR/$stem-s$shard.log" 2>&1 &
    pids+=("$!")
  done
  if [[ "$DRY_RUN" == 1 ]]; then
    stop_probe
    return 0
  fi
  FIRST_EXITED=""
  stop_probe_at_first_exit "${pids[@]}" || status=1
  await_remaining_loaders "${pids[@]}" || status=1
  if (( status != 0 )); then
    set_aside_point "$dir" "$stem" "a loader exited non-zero — see $LOG_DIR/$stem-s*.log"
    return 0
  fi
  gate_point "$dir" "$stem" "$WORKERS" "$total" \
    || set_aside_point "$dir" "$stem" "a gate refused the point"
}

top_rung() {
  local rung last
  for rung in $LADDER; do last=$rung; done
  echo "$last"
}

mkdir -p "$ROOT" "$OUT_DIR" "$LOG_DIR"
log "$ENGINE N=$WORKERS: ladder='$LADDER' reps=$REPS batch=$(engine_batch) docs=$(engine_docs)"
log "sinks at $SINK_HOST — ports $(engine_ports)"
generate_corpus

# One discarded point at the top rung, so repetition 1 does not pay for a cold
# page cache the other two find warm. The median of 3 is the middle value,
# which one cold repetition can move.
if (( WARMUP == 1 )); then
  log "$ENGINE N=$WORKERS: discarded warm-up at c=$(top_rung)"
  run_point "$WARMUP_DIR" "$(top_rung)" 0
fi

for (( rep = 1; rep <= REPS; rep++ )); do
  for conc in $LADDER; do
    log "$ENGINE N=$WORKERS c=$conc rep=$rep/$REPS"
    run_point "$OUT_DIR" "$conc" "$rep"
  done
done

if (( FAILED > 0 )); then
  log "$ENGINE N=$WORKERS: $FAILED point(s) set aside — see $ROOT/failed-points.log"
fi
