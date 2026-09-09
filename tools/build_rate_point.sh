#!/usr/bin/env bash
# ONE build-rate point: empty the index, start the probes, build, gate.
#
# This is the file to open to see what the write-path campaign actually runs.
# The same point used to be assembled from `make c1-os` — three commands
# produced by ~90 lines of `?=` layering (OS_LOAD -> TASKSET -> GEN_CPUSET,
# C1_MONITOR_OS + C1_MONITOR_COMMON) and two `define` blocks — and every one of
# those recipe lines is `@`-prefixed, so the campaign's own logs recorded the
# ladder's progress and not one loader invocation. Here the commands are
# written out, echoed as they run, and `--dry-run` prints them without a stack.
#
# It is also hand-runnable, which the make path was not in practice: a point set
# aside by the completeness gate can be re-run by copying the line the ladder
# logged, instead of reconstructing twelve make overrides.
#
# `make` still owns the stack and the index DDL — os-up, os-reindex,
# os-verify-analyzer, scylla-schema, scylla-index, scylla-serving. Those targets
# are shared with campaign_laptop.sh and every other chart, and a second
# definition of "bring OpenSearch up" is how the OS_RAM_INDEX-on-up-but-off-down
# hazard gets realised. Only the measurement left make.
#
# The Makefile's c1-os / c1-scylla-cdc stay for the nine diagnostic scripts that
# drive them, so the measured commands now exist in two places.
# tests/test_build_rate_point.py compares them argv by argv and is what forbids
# them from drifting.
#
# Fleet mode: source tools/fleet_env.sh first. DOCKER_HOST carries the compose,
# exec and reset calls to the SUT; the resource probe reads local cgroups, so on
# the fleet it runs ON the SUT via tools/sut_probe.sh.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  cat >&2 <<'USAGE'
usage: tools/build_rate_point.sh --arm <flag> --concurrency N --rep N [options]

  --arm FLAG            arm from ftsbench/target.py, e.g. --scylladb-cdc-buf376
                        (or --config <name> for a bare configuration label)
  --concurrency N        outstanding requests to the engine
  --rep N                repetition number; 0 is the discarded warm-up
  --batch N              documents per _bulk (OpenSearch); must be 1 on ScyllaDB
  --cap N                stop after this many documents (--max-docs/--until-docs)
  --out-dir DIR          where this point's five artifacts land
  --interval S           sampling interval for the monitor and both probes
  --idle-timeout S       stop when the doc count has not moved for this long
  --settle-timeout S     how long written-but-not-searchable may take
  --max-seconds S        wall-clock backstop
  --cache-state STATE    recorded in every artifact header
  --label TEXT           overrides the computed point label
  --workers N|auto       loader processes (ftsbench.mp_load); off when unset
  --n-max N              the measured process ceiling --workers auto needs
  --dry-run              print the commands, touch nothing
  --print-artifacts      print this point's five artifact paths and exit; the
                         ladder asks for them when it sets a point aside, so
                         the naming rule lives here only
USAGE
}

# Same names as the Makefile's, so `source tools/fleet_env.sh` redirects this
# script at the fleet exactly as it redirects make. GEN_CPUSET uses ${VAR-...}
# rather than ${VAR:-...}: fleet_env.sh exports it EMPTY on purpose, because the
# generator is isolated by being on another machine, and `taskset -c ""` fails.
CORPUS="${CORPUS:-data/corpus.jsonl}"
OS_URL="${OS_URL:-http://localhost:9200}"
OS_INDEX="${OS_INDEX:-wiki-articles}"
VS_URL="${VS_URL:-http://localhost:16080}"
SCYLLA_HOSTS="${SCYLLA_HOSTS:-127.0.0.1}"
SCYLLA_PORT="${SCYLLA_PORT:-19042}"
KEYSPACE="${KEYSPACE:-wiki}"
VS_INDEX="${VS_INDEX:-articles_body_fts}"
COMPOSE_ENV="${COMPOSE_ENV:-docker/.env}"
PYTHON="${PYTHON:-$( [ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3 )}"
CQLSH="${CQLSH:-docker exec -i fts-bench-scylla cqlsh}"
GEN_CPUSET="${GEN_CPUSET-12-19}"
OS_CONTAINER="${OS_CONTAINER:-fts-bench-opensearch}"
SCYLLA_CONTAINER="${SCYLLA_CONTAINER:-fts-bench-scylla}"
VS_CONTAINER="${VS_CONTAINER:-fts-bench-vector-store}"

ARM=""
CONCURRENCY=""
REP=""
BATCH=""
CAP="${CAP:-1000000}"
OUT_DIR="${OUT_DIR:-data/sweep}"
INTERVAL="${INTERVAL:-1}"
IDLE_TIMEOUT="${IDLE_TIMEOUT:-60}"
SETTLE_TIMEOUT="${SETTLE_TIMEOUT:-120}"
MAX_SECONDS="${MAX_SECONDS:-2400}"
CACHE_STATE="${CACHE_STATE:-warm-container-fresh-index}"
LABEL=""
WORKERS="${WORKERS:-}"
N_MAX="${N_MAX:-}"
DRY_RUN=0
PRINT_ARTIFACTS=0

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --arm)             ARM="$2"; shift 2 ;;
      --config)          ARM="--config=$2"; shift 2 ;;
      --concurrency)     CONCURRENCY="$2"; shift 2 ;;
      --rep)             REP="$2"; shift 2 ;;
      --batch)           BATCH="$2"; shift 2 ;;
      --cap)             CAP="$2"; shift 2 ;;
      --out-dir)         OUT_DIR="$2"; shift 2 ;;
      --interval)        INTERVAL="$2"; shift 2 ;;
      --idle-timeout)    IDLE_TIMEOUT="$2"; shift 2 ;;
      --settle-timeout)  SETTLE_TIMEOUT="$2"; shift 2 ;;
      --max-seconds)     MAX_SECONDS="$2"; shift 2 ;;
      --cache-state)     CACHE_STATE="$2"; shift 2 ;;
      --label)           LABEL="$2"; shift 2 ;;
      --workers)         WORKERS="$2"; shift 2 ;;
      --n-max)           N_MAX="$2"; shift 2 ;;
      --dry-run)         DRY_RUN=1; shift ;;
      --print-artifacts) PRINT_ARTIFACTS=1; shift ;;
      -h|--help)         usage; exit 0 ;;
      *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
  done
  [[ -n "$ARM" && -n "$CONCURRENCY" && -n "$REP" ]] \
    || { echo "--arm, --concurrency and --rep are required" >&2; usage; exit 2; }
}

# The arm comes from ftsbench/target.py and nowhere else. A driver with its own
# `case` block is how `vector-store-direct` came to exist in tools/read_sweep.sh
# and in no Python list at all, and how `opensearch-ramindex` spent a campaign
# as a label with no way to select it.
resolve_arm() {
  local selector
  case "$ARM" in
    --config=*) selector=(--config "${ARM#--config=}") ;;
    *)          selector=("$ARM") ;;
  esac
  eval "$($PYTHON -m ftsbench.target "${selector[@]}" --shell)"
  OS_REFRESH="${OS_REFRESH:-3s}"
}

# One operation is one prepared INSERT on the CQL path: there is no wire batch to
# size, so a level other than 1 would name a dispatch window inside the client
# and the artifact would claim an axis the engine never saw.
resolve_batch() {
  case "$TARGET_ENGINE" in
    opensearch) BATCH="${BATCH:-${OS_BATCH_SIZE:-${BATCH_SIZE:-500}}}" ;;
    scylladb)
      BATCH="${BATCH:-1}"
      [[ "$BATCH" == 1 ]] || {
        echo "--batch $BATCH on $TARGET_CONFIG: ScyllaDB has no wire batch, so" \
             "one operation is one INSERT and the only level is 1." >&2
        exit 2
      } ;;
  esac
}

artifact_paths() {
  local stem="$TARGET_CONFIG-c$CONCURRENCY-$REP"
  SERIES="$OUT_DIR/c1-$stem.jsonl"
  MANIFEST="$OUT_DIR/manifest-$stem.json"
  PROBE_OUT="$OUT_DIR/cpu-$stem.jsonl"
  GEN_PROBE_OUT="$OUT_DIR/gen-$stem.jsonl"
  VS_LOG="$OUT_DIR/vslog-$stem.log"
  LABEL="${LABEL:-build-rate sweep, $TARGET_CONFIG, concurrency=$CONCURRENCY batch=$BATCH}"
}

step() { printf '\n%s\n' "$*" >&2; }
show() { printf '    + '; printf '%q ' "$@"; printf '\n'; }
note() { printf '      (%s)\n' "$*"; }

# Printed on a dry run, printed AND executed otherwise: a log that records the
# ladder's progress but not the loader invocation is how a recorded point became
# unreconstructable in the first place.
execute() {
  show "$@"
  [[ "$DRY_RUN" == 1 ]] && return 0
  "$@"
}

# --- the point's steps, in the order they run ------------------------------

RESET_CQL='DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;'

# Every point builds from zero documents. The OpenSearch watermarks are
# re-relaxed per point, not once per ladder: the index-create block is applied
# at runtime by DiskThresholdMonitor and can come back mid-sweep, and a point
# that dies on a bare 403 is a hole in the curve. The analyzer gate runs
# immediately after the create, because an analyzer cannot be changed on a live
# index and an index whose analyzer differs from the vector-store's is not a
# comparison.
empty_the_index() {
  step "1. empty the index"
  case "$TARGET_ENGINE" in
    opensearch)
      execute make os-relax-watermarks
      execute make os-reindex "OS_REFRESH=$OS_REFRESH"
      execute make os-verify-analyzer
      ;;
    scylladb)
      printf "    + printf '%%s' <RESET_CQL> | %s\n" "$CQLSH"
      note "$(printf '%s' "$RESET_CQL" | tr '\n' ' ')"
      [[ "$DRY_RUN" == 1 ]] || $CQLSH <<<"$RESET_CQL"
      execute make scylla-schema scylla-index scylla-serving
      ;;
  esac
}

# `docker logs --since` needs a floor, and the container is deliberately warm
# across a whole ladder, so an unbounded read would hand this point the entire
# sweep's counters. The 5 s margin absorbs harness-to-SUT clock skew, which is
# microseconds under chrony — over-reading is harmless where under-reading loses
# the startup tuning lines the arm gate reads.
mark_log_window() {
  SINCE=$(date -u -d '5 seconds ago' +%Y-%m-%dT%H:%M:%SZ)
}

# On the fleet the probe reads the SUT's cgroups, so it runs there and addresses
# the engines on localhost; sut_probe.sh copies the series back.
probe_target_args() {
  local os_url="$OS_URL" vs_url="$VS_URL"
  if [[ -n "${SUT_IP:-}" ]]; then
    os_url="http://localhost:9200"
    vs_url="http://localhost:16080"
  fi
  case "$TARGET_ENGINE" in
    opensearch)
      PROBE_ARGS=(--engine opensearch --containers "$OS_CONTAINER:opensearch"
                  --os-url "$os_url" --os-index "$OS_INDEX") ;;
    scylladb)
      PROBE_ARGS=(--engine scylladb --containers "$SCYLLA_CONTAINER:scylladb"
                  --containers "$VS_CONTAINER:vector-store"
                  --vs-url "$vs_url" --keyspace "$KEYSPACE"
                  --vs-index "$VS_INDEX") ;;
  esac
}

# Two probes, because "did it saturate?" and "did the CLIENT saturate?" are
# different questions and the build rate answers neither on its own. Both carry
# THIS point's label: verify_generator refuses a series whose label has no
# concurrency=, rather than judging one point with another's samples.
start_probes() {
  step "2. start the probes"
  probe_target_args
  local -a probe=("$PYTHON" -m ftsbench.resource_probe "${PROBE_ARGS[@]}"
                  --output "$PROBE_OUT" --interval "$INTERVAL" --duration 0
                  --cache-state "$CACHE_STATE" --label "$LABEL")
  local -a gen=("$PYTHON" -m ftsbench.generator_probe --output "$GEN_PROBE_OUT"
                --interval "$INTERVAL" --duration 0 --match "$(loader_module)"
                --engine "$TARGET_ENGINE" --cache-state "$CACHE_STATE"
                --label "$LABEL")
  start_resource_probe "${probe[@]}"
  start_background_probe GEN_PROBE_PID "${gen[@]}"
}

start_resource_probe() {
  if [[ -n "${SUT_IP:-}" ]]; then
    execute tools/sut_probe.sh start "$PROBE_OUT" "${PROBE_ARGS[@]}" \
      --interval "$INTERVAL" --duration 0 --cache-state "$CACHE_STATE" \
      --label "$LABEL"
    FLEET_PROBE=1
    return 0
  fi
  start_background_probe PROBE_PID "$@"
}

start_background_probe() {
  local -n pid_ref="$1"; shift
  show "$@"
  note "backgrounded, sampling until the build ends"
  [[ "$DRY_RUN" == 1 ]] && return 0
  "$@" >/dev/null 2>&1 &
  pid_ref=$!
}

stop_probes() {
  [[ "$DRY_RUN" == 1 ]] && return 0
  stop_background_probe GEN_PROBE_PID
  if [[ "${FLEET_PROBE:-0}" == 1 ]]; then
    tools/sut_probe.sh stop "$PROBE_OUT" || true
    FLEET_PROBE=0
    return 0
  fi
  stop_background_probe PROBE_PID
}

stop_background_probe() {
  local -n pid_ref="$1"
  [[ -n "${pid_ref:-}" ]] || return 0
  kill -TERM "$pid_ref" 2>/dev/null || true
  wait "$pid_ref" 2>/dev/null || true
  pid_ref=""
}

# Recorded BEFORE the measured work, because the version probes need a live
# stack: image tags and engine versions are unrecoverable once it is torn down.
# The `--config` it passes is the arm the registry resolved, which is the one
# thing the make path could not do: c1-scylla-cdc hardcodes the literal
# `scylla-cdc`, so all three knob arms wrote manifests claiming to be the same
# deployment.
record_manifest() {
  step "3. record the manifest, while the stack is still live"
  local -a manifest=("$PYTHON" -m ftsbench.run_manifest --output "$MANIFEST"
    --config "$TARGET_CONFIG" --rep "$REP" --label "$LABEL"
    --cache-state "$CACHE_STATE" --series "$SERIES" --corpus "$CORPUS"
    --max-docs "$CAP" --env-file "$COMPOSE_ENV" --os-url "$OS_URL"
    --vs-url "$VS_URL" --scylla-hosts "$SCYLLA_HOSTS"
    --scylla-port "$SCYLLA_PORT"
    --command "tools/build_rate_point.sh --arm $TARGET_FLAG")
  [[ "$TARGET_ENGINE" == opensearch ]] && manifest+=(--batch-size "$BATCH")
  execute "${manifest[@]}"
}

monitor_command() {
  MONITOR=("$PYTHON" -m ftsbench.build_monitor)
  case "$TARGET_ENGINE" in
    opensearch) MONITOR+=(--engine opensearch --url "$OS_URL"
                          --index "$OS_INDEX" --batch-size "$BATCH") ;;
    scylladb)   MONITOR+=(--engine scylladb --vs-url "$VS_URL"
                          --keyspace "$KEYSPACE" --vs-index "$VS_INDEX") ;;
  esac
  MONITOR+=(--output "$SERIES" --interval "$INTERVAL"
            --idle-timeout "$IDLE_TIMEOUT" --until-docs "$CAP"
            --max-seconds "$MAX_SECONDS" --corpus "$CORPUS"
            --settle-timeout "$SETTLE_TIMEOUT" --label "$LABEL"
            --cache-state "$CACHE_STATE")
}

loader_module() {
  case "$TARGET_ENGINE" in
    opensearch) echo ftsbench.opensearch_load ;;
    scylladb)   echo ftsbench.scylla_load ;;
  esac
}

# --concurrency counts outstanding requests to the engine on BOTH sides. What one
# request carries is what differs — an OpenSearch _bulk of N documents against
# one ScyllaDB INSERT — and that belongs in the chart footer, not in a knob.
loader_command() {
  LOADER=()
  [[ -n "$GEN_CPUSET" ]] && LOADER=(taskset -c "$GEN_CPUSET")
  LOADER+=("$PYTHON" -m "$(loader_module)" --corpus "$CORPUS")
  case "$TARGET_ENGINE" in
    opensearch) LOADER+=(--url "$OS_URL" --index "$OS_INDEX"
                         --max-docs "$CAP" --batch-size "$BATCH") ;;
    scylladb)   LOADER+=(--hosts "$SCYLLA_HOSTS" --port "$SCYLLA_PORT"
                         --max-docs "$CAP") ;;
  esac
  LOADER+=(--concurrency "$CONCURRENCY" --label "$LABEL"
           --cache-state "$CACHE_STATE")
  [[ -n "$WORKERS" ]] && LOADER+=(--workers "$WORKERS")
  [[ -n "$N_MAX" ]] && LOADER+=(--n-max "$N_MAX")
  return 0
}

# The measured work. The monitor, not the loader, decides when the run is over —
# index DDL returns long before the index has finished building — so it runs in
# the background and the loader in the foreground. LOAD_RC is captured before
# `wait`, which would otherwise overwrite it, and the loader's status is the
# point's status.
measure_build() {
  step "4. measure the build — this is the number the campaign reports"
  monitor_command
  loader_command
  show "${MONITOR[@]}"
  note "backgrounded; MON=\$!"
  show sleep 2
  show "${LOADER[@]}"
  note 'LOAD_RC=$?, captured before wait'
  show wait '$MON'
  [[ "$DRY_RUN" == 1 ]] && { LOAD_RC=0; return 0; }
  "${MONITOR[@]}" &
  local monitor_pid=$!
  sleep 2
  LOAD_RC=0
  "${LOADER[@]}" || LOAD_RC=$?
  wait "$monitor_pid" || true
}

# The vector-store's own counters, where `added/s` — documents entering the
# tantivy writer, ungated by commit — is the only throughput signal that
# survives raising the commit interval. Also carries the startup lines stating
# what tuning the process ACTUALLY took, which is what the arm gate reads.
harvest_vector_store_log() {
  [[ "$TARGET_ENGINE" == scylladb ]] || return 0
  step "5. harvest the engine's own counters and startup tuning lines"
  show docker logs --since "$SINCE" "$VS_CONTAINER" ">$VS_LOG"
  [[ "$DRY_RUN" == 1 ]] && return 0
  docker logs --since "$SINCE" "$VS_CONTAINER" >"$VS_LOG" 2>&1 || true
}

# Fatal, not a set-aside point. A knob that did not take effect is not a bad
# sample: it means every point of this arm measured some other arm, and the
# artifacts would be complete, plausible and wrongly labelled.
gate_arm_knobs() {
  [[ "$TARGET_ENGINE" == scylladb ]] || return 0
  step "6. gate: the engine is running THIS arm's tuning"
  execute "$PYTHON" -m ftsbench.verify_arm "$TARGET_FLAG" --log "$VS_LOG" || {
    echo "ABORTING $TARGET_CONFIG: the vector-store is not running this arm's tuning" >&2
    exit 3
  }
}

# A truncated point is not a slow point — it silently lowers a rung of the
# median. The caller sets it aside; this only decides.
gate_point_complete() {
  step "7. gate: the point reached its document cap"
  show "docs_indexed >= $CAP in $SERIES"
  [[ "$DRY_RUN" == 1 ]] && return 0
  $PYTHON - "$SERIES" "$CAP" <<'PYGATE'
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

# Only on success: the make recipe ended `exit $LOAD_RC`, so make aborted the
# target and never reached build_report for a failed point. Reporting one
# unconditionally would change the stdout of every failed point.
summarise_point() {
  [[ "$LOAD_RC" -eq 0 ]] || return 0
  step "8. summarise"
  execute "$PYTHON" -m ftsbench.build_report "$SERIES"
}

main() {
  parse_args "$@"
  resolve_arm
  resolve_batch
  artifact_paths
  if [[ "$PRINT_ARTIFACTS" == 1 ]]; then
    printf '%s\n' "$SERIES" "$MANIFEST" "$PROBE_OUT" "$GEN_PROBE_OUT" "$VS_LOG"
    exit 0
  fi
  [[ "$DRY_RUN" == 1 ]] || mkdir -p "$OUT_DIR"
  step "point: $TARGET_CONFIG  concurrency=$CONCURRENCY  batch=$BATCH  rep=$REP"
  trap stop_probes EXIT
  empty_the_index
  mark_log_window
  start_probes
  record_manifest
  measure_build
  stop_probes
  harvest_vector_store_log
  gate_arm_knobs
  gate_point_complete || exit 4
  summarise_point
  exit "$LOAD_RC"
}

main "$@"
