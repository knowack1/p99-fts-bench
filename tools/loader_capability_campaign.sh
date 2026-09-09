#!/usr/bin/env bash
# What can the HARNESS BOX offer, at the worker counts the campaign runs at?
#
# The runs are the six lines under "the runs" below. Nothing computes them:
# reading this file IS reading the campaign, and adding or dropping an arm means
# adding or dropping a line.
#
#   tools/loader_capability_campaign.sh dry-run   # print every command, touch nothing
#   tools/loader_capability_campaign.sh pilot     # the first line only, 1 rep
#   tools/loader_capability_campaign.sh run       # execute the six in order
#   tools/loader_capability_campaign.sh run 2>&1 | tee loader-cap.log
#
# BUILD-RATE-MATRIX-PLAN.md records the gap this closes: verify_generator judges
# the build-rate campaign's four-worker points against ceilings built from N=1
# points, and N_max stands at ">=4, a lower bound" because neither of P0's
# worker ladders stopped scaling. So: the real loaders, run the way a build-rate
# point runs them, with ftsbench.null_sink on the SUT standing in for OpenSearch
# and ScyllaDB. No engines, no images, no corpus staging.
#
# RUN THE PILOT FIRST. P0's own artifacts say the concurrency axis may carry no
# signal: at N=1 the OpenSearch client delivered 26,871 / 27,686 / 27,364 docs/s
# at c=16 / 64 / 256 — +-3%, inside the repetition spread — with
# busiest_thread_cores pinned at 1.009 of a core ALREADY AT c=16, and the same
# flatness across batch 16..512. That is a client bound by per-document work,
# and a bound like that does not move when offered more in flight. Ten minutes
# answers whether the chart's x axis is an axis.
#   results/aws-batch-axis-2026-09-08/phase0/ceilings-opensearch-provenance.json
#
# There is no resume. A row already measured is re-measured if it is run again,
# so run the lines you need — each one is a complete command you can paste.
#
# `set -e` stops the campaign at the first arm that fails.
#
# What each line runs is tools/loader_capability_sweep.sh (one arm's ladder),
# and that file's `run_point` is what one point runs, with every flag written
# out. `DRY_RUN=1` previews it.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="${ROOT:-data/loader-cap-$(date -u +%Y-%m-%d)}"
# One repetition count for every arm, so no line is quietly less certain than
# its neighbours, plus one discarded warm-up per arm — the median of 3 is the
# middle value, which one cold repetition can move.
REPS="${REPS:-3}"
# Every rung is a multiple of lcm(4,6,8) = 24, so all three worker counts
# divide it exactly. A worker's share of the concurrency is remainder-preserving,
# so on an unbalanced rung some workers hold one more operation than others and
# the point's rate is dragged by whichever shard drew the short share — a
# spurious slope on the very axis being plotted. That rules out a rung below 24:
# c=8 at N=6 would be 2,2,1,1,1,1, and c=4 at N=8 would offer 8 because a share
# floors at 1. The cost is that the lowest reachable in-flight per worker is
# 24/8 = 3, so the RTT-bound region below that is not on this chart -- state it
# in the footer rather than leave it to be noticed.
LADDER="${LADDER:-24 48 96 192 384}"
# Sized by the CPU figure, not the throughput one. P0's whole pass ran at
# DOCS=200000, which at N=4 is a two-second point — two probe ticks, both
# discarded by client_ceilings.WARM_IN_TICKS, which is why P0's
# loader_core_bound_at came from its single-process points and its four-worker
# CPU figure is thin. At the rates P0 measured these put every point above ten
# seconds.
DOCS_OS="${DOCS_OS:-1200000}"
DOCS_SCYLLA="${DOCS_SCYLLA:-400000}"
# Below one second doubles the ticks inside a point. Not below 0.5:
# cpu_cores_used differences 100 Hz clock ticks, so a 0.25 s window quantises a
# one-core process to about +-4%.
PROBE_INTERVAL="${PROBE_INTERVAL:-0.5}"
# "Decisions locked" says OpenSearch 512. The ScyllaDB arms take no batch flag
# at all: one operation is one prepared INSERT.
OS_BATCH="${OS_BATCH:-512}"

# The sinks. Two instances per mode because the sink is one asyncio loop in one
# process (ftsbench/sink_counters.py) and P0's N=4 OpenSearch point already
# pushed ~400 MB/s of _bulk bodies through one; the sweep spreads a point's
# loaders across them round-robin. On the fleet they run on fts-sut, so the
# private network's 0.353 ms is in the measurement rather than loopback's:
#
#   source tools/fleet_env.sh
#   ssh -n $SUT_IP 'cd $HOME/p99/bench
#     for p in 9200 9201; do setsid $HOME/venv/bin/python3 -m ftsbench.null_sink \
#       --mode http --port $p --report-interval 5 \
#       --stats-out /tmp/sink-http-$p.json \
#       </dev/null >/tmp/sink-http-$p.log 2>&1 & done
#     for p in 9042 9043; do setsid $HOME/venv/bin/python3 -m ftsbench.null_sink \
#       --mode cql --port $p --report-interval 5 \
#       --stats-out /tmp/sink-cql-$p.json \
#       </dev/null >/tmp/sink-cql-$p.log 2>&1 & done'
#   SINK_HOST=$SUT_IP tools/loader_capability_campaign.sh run
#
# `setsid` and `</dev/null`, not bare `nohup`: an ssh command channel closing
# takes its whole process group with it, so the nohup form leaves nothing
# listening and the first point fails with connection refused. Verify from a
# SECOND ssh that four processes are up and four ports are bound before running
# anything. To clear stale sinks, `pkill -f 'ftsbench[.]null_sink'` — the
# bracket keeps the pattern from matching the ssh command line that carries it,
# which otherwise kills the remote shell mid-command.
#
# and nothing else may be running on the SUT — `docker ps -q` empty — or the
# sink's own CPU figure is the engines'.
SINK_HOST="${SINK_HOST:-127.0.0.1}"

# Locked for every arm, so they are set once here rather than repeated per line.
export ROOT OUT_DIR="$ROOT/points" LOG_DIR="$ROOT/logs" CORPUS_DIR="$ROOT/corpus"
export LADDER DOCS_OS DOCS_SCYLLA PROBE_INTERVAL OS_BATCH SINK_HOST
export WARMUP=1
export HTTP_PORTS="${HTTP_PORTS:-9200 9201}"
export CQL_PORTS="${CQL_PORTS:-9042 9043}"

PILOT=0
case "${1:-}" in
  run)     ;;
  dry-run) export DRY_RUN=1 ;;
  # One repetition and no warm-up: the pilot is asking whether the ladder
  # has a shape, which one repetition shows, not what its value is.
  pilot)   PILOT=1; REPS=1; export WARMUP=0 ;;
  *) echo "usage: $0 {run|dry-run|pilot}   (the runs themselves are in this file)" >&2
     exit 2 ;;
esac

# --- the runs --------------------------------------------------------------

# OpenSearch N=4 first, and it is also the `pilot` line: it is the arm the
# build-rate campaign's own gate needs, and if its ladder is flat then the five
# lines after it are flat too and the chart's x axis is the thing to settle
# before the rest of the session is spent.
tools/loader_capability_sweep.sh opensearch 4 "$REPS"

if (( PILOT == 1 )); then
  echo "pilot done — read the ladder in $ROOT/points before running the rest" >&2
  exit 0
fi

tools/loader_capability_sweep.sh opensearch 6 "$REPS"
tools/loader_capability_sweep.sh opensearch 8 "$REPS"
tools/loader_capability_sweep.sh scylladb   4 "$REPS"
tools/loader_capability_sweep.sh scylladb   6 "$REPS"
tools/loader_capability_sweep.sh scylladb   8 "$REPS"

# --- what turns points into numbers ----------------------------------------

[[ "${DRY_RUN:-0}" == 1 ]] && exit 0

cat <<NEXT

campaign finished. Next:

  .venv/bin/python3 -m ftsbench.client_ceilings --data-dir $ROOT/points \\
      --engine opensearch --measured-on "\$(hostname)" \\
      --out $ROOT/ceilings-opensearch.json \\
      --provenance-out $ROOT/ceilings-opensearch-provenance.json
      and the same for --engine scylladb. The provenance file carries every
      point — that is the grid both charts read, so no reducer of our own.

      Two rules when reading it. Its worker_ceiling is computed over whatever
      ladder it finds, so an N=8 rung that flattened because an 8-vCPU box ran
      out of cores would be published as N_max = 8; and loader_core_bound_at
      belongs to the N=4 arm, because N=8's busiest_thread_cores is
      contention-depressed and a bound taken there would make verify_generator
      refuse points that are fine.

  .venv/bin/python3 -m ftsbench.plot_loader_capability --data-dir $ROOT/points \\
      --output $ROOT/loader-capability.png
  .venv/bin/python3 -m ftsbench.plot_loader_cpu --mode summary \\
      --data-dir $ROOT/points --output $ROOT/loader-cpu-summary.png
  .venv/bin/python3 -m ftsbench.plot_loader_cpu --mode timeline \\
      --data-dir $ROOT/points --output $ROOT/loader-cpu-timeline.png

  and check every sink's unexpected_requests is 0 — a 404'd route means the
  loader was not doing what the chart says.
NEXT
