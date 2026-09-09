#!/usr/bin/env bash
# Phase 0 — what can the CLIENT do, with nothing in the way?
#
# Two constants the whole build-rate campaign rests on are currently estimates
# taken from a client that no longer exists (BUILD-RATE-MATRIX-PLAN.md, Phase 0):
#
#   N_max = 4                    extrapolated from a per-process ceiling of
#                                ~9.8k docs/s (ScyllaDB) / ~11.4k (OpenSearch)
#                                measured with the OLD thread-per-operation
#                                client
#   LOADER_CORE_BOUND_AT = 0.70  anchored to one pre-rewrite loader sitting at
#                                ~0.80 of a core
#
# and the ScyllaDB anchor at one document per operation does not exist at all:
# every ScyllaDB figure in the repo was taken at batch 500 or 1,000, where one
# operation covered hundreds of documents.
#
# Neither can be measured against a real engine. At ~11.7k docs/s the engine
# saturates first, so the number that comes back describes the engine. So this
# runs the REAL loaders against ftsbench.null_sink — accept and discard — over
# synthetic documents at enwiki's ~3.95 KB average, with ftsbench.generator_probe
# watching the harness box. No engines, no images, no corpus staging.
#
# It ends by CONSTRUCTING both answers the generator gate can give: a run with
# nothing in the way (client-bound, must FAIL) and the same run against a
# deliberately delayed sink (not client-bound, must PASS). The gate has never
# seen a positive example, and a constructed one is the only way to test a gate
# whose job is to catch a condition we hope not to meet.
#
# TWO THINGS THAT WILL BITE. The sink sets TCP_QUICKACK because the CQL driver
# leaves Nagle on, and without it a c=8 point measured 404 docs/s against 7,845
# — a 40 ms kernel timer reported as the client's ceiling (ftsbench.sink_tcp
# carries the measurement). And a point has to outlast a few probe intervals or
# it carries no CPU figure at all: keep DOCS large enough that every rung,
# including the top of the worker ladder, runs for several seconds, or
# loader_core_bound_at comes back unmeasured and the gate refuses.
#
# WHERE IT RUNS. The loaders must run on the box whose ceiling the campaign
# depends on — fts-harness — because per-process throughput is a function of
# that box's per-core speed. A laptop pass gives the SHAPE of the N scaling and
# not the value; do not put a laptop figure in the --ceilings JSON the campaign
# reads. On the fleet the sink belongs on fts-sut, so the private network's RTT
# is in the measurement rather than loopback's: start it there by hand
# (python3 -m ftsbench.null_sink --mode http --port 9200) and run this with
# START_SINK=0 SINK_HOST=<fts-sut>.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/client-calibration-$(date -u +%Y-%m-%d)}"
LOG_DIR="${LOG_DIR:-$OUT_DIR/logs}"
CORPUS_DIR="${CORPUS_DIR:-$OUT_DIR/corpus}"
MEASURED_ON="${MEASURED_ON:-$(hostname)}"
ENGINES="${ENGINES:-opensearch scylladb}"

# OpenSearch sweeps the batch axis because that is the axis the campaign sweeps.
# ScyllaDB has no batch axis and its loader takes no batch flag: one operation
# is one prepared INSERT. The 1 below is the point key, not a knob — it keeps a
# ScyllaDB point addressable by the same (batch, concurrency, workers, rep)
# tuple every other point uses.
OS_BATCHES="${OS_BATCHES:-16 64 128 256 512}"
SCYLLA_BATCHES="1"
CONC_LADDER="${CONC_LADDER:-16 64 256}"
REPS="${REPS:-3}"
DOCS="${DOCS:-200000}"

# The worker ladder is what makes LOADER_CORE_BOUND_AT a measurement: it is the
# box's CPU fraction at the point where another loader process stops buying
# throughput, and one process cannot see that. Set WORKER_LADDER="" to skip it,
# and the ceilings JSON omits the constant rather than inventing one.
WORKER_LADDER="${WORKER_LADDER:-1 2 4}"
WORKER_CONC="${WORKER_CONC:-64}"
OS_WORKER_BATCH="${OS_WORKER_BATCH:-512}"
SCYLLA_WORKER_BATCH="1"
# The ladder's own repetitions are numbered above the single-process pass so a
# ladder point at N=1 cannot land on the same (batch, concurrency, workers, rep)
# key as a ceiling point and be averaged into it.
LADDER_REP_BASE=100

START_SINK="${START_SINK:-1}"
SINK_HOST="${SINK_HOST:-127.0.0.1}"
HTTP_PORT="${HTTP_PORT:-9200}"
CQL_PORT="${CQL_PORT:-9042}"
PROBE_INTERVAL="${PROBE_INTERVAL:-1}"
SINK_READY_TRIES="${SINK_READY_TRIES:-150}"

# The gate examples are constructed rather than measured, so they are always
# self-hosted: mixing a fleet sink and a synthetic one in one artifact set would
# leave the example's provenance ambiguous.
EXAMPLE_HOST="127.0.0.1"
EXAMPLE_HTTP_PORT="${EXAMPLE_HTTP_PORT:-19200}"
EXAMPLE_CQL_PORT="${EXAMPLE_CQL_PORT:-19042}"
EXAMPLE_CONC="${EXAMPLE_CONC:-64}"
EXAMPLE_DOCS="${EXAMPLE_DOCS:-40000}"
# The delayed sink is aimed this many times below the measured ceiling, so the
# gate's >=2x margin clears with room. Derived from the ceiling rather than
# picked, because a hand-picked delay would prove only that it was big enough.
EXAMPLE_MARGIN="${EXAMPLE_MARGIN:-4}"
# The examples run at the engine's SMALLEST batch level. That is the tight side
# of the gate — the level whose operation rate comes closest to the client's
# ceiling — and it is also the level whose delay stays small enough for
# EXAMPLE_DOCS to reach steady state: at batch 512 and c=64 a quarter-ceiling
# delay is seconds long, and the example would time its own ramp.
EXAMPLE_BATCH="${EXAMPLE_BATCH:-}"

PYTHON=.venv/bin/python3
SINK_PID=""
PROBE_PID=""
LOADER_ARGV=()

log() { printf '\n######## [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

engine_module() {
  case "$1" in
    opensearch) echo ftsbench.opensearch_load ;;
    scylladb) echo ftsbench.scylla_load ;;
    *) log "unknown engine: $1"; return 1 ;;
  esac
}

engine_batches() {
  case "$1" in
    opensearch) echo "$OS_BATCHES" ;;
    scylladb) echo "$SCYLLA_BATCHES" ;;
  esac
}

engine_worker_batch() {
  case "$1" in
    opensearch) echo "$OS_WORKER_BATCH" ;;
    scylladb) echo "$SCYLLA_WORKER_BATCH" ;;
  esac
}

sink_mode() {
  case "$1" in
    opensearch) echo http ;;
    scylladb) echo cql ;;
  esac
}

sink_port() {
  case "$1" in
    opensearch) echo "$HTTP_PORT" ;;
    scylladb) echo "$CQL_PORT" ;;
  esac
}

example_port() {
  case "$1" in
    opensearch) echo "$EXAMPLE_HTTP_PORT" ;;
    scylladb) echo "$EXAMPLE_CQL_PORT" ;;
  esac
}

engine_endpoint_args() {  # engine host port
  case "$1" in
    opensearch) echo "--url http://$2:$3" ;;
    scylladb) echo "--hosts $2 --port $3" ;;
  esac
}

synth_shards() {  # stem docs shards
  local stem=$1 docs=$2 shards=$3
  if [[ -s "$stem-0.jsonl" ]]; then
    return 0
  fi
  $PYTHON -m ftsbench.synth_corpus --output "$stem.jsonl" --docs "$docs" \
    --shards "$shards" --stats-out "$stem-stats.json"
  if [[ "$shards" == 1 ]]; then
    mv "$stem.jsonl" "$stem-0.jsonl"
  fi
}

generate_corpora() {
  mkdir -p "$CORPUS_DIR"
  for workers in 1 $WORKER_LADDER; do
    log "synthetic corpus: $DOCS docs across $workers shard(s)"
    synth_shards "$CORPUS_DIR/corpus-w$workers" "$DOCS" "$workers"
  done
  log "synthetic corpus for the gate examples: $EXAMPLE_DOCS docs"
  synth_shards "$CORPUS_DIR/example" "$EXAMPLE_DOCS" 1
}

await_sink() {  # logfile
  local tries=0
  until grep -q "null sink ready" "$1" 2>/dev/null; do
    sleep 0.2
    tries=$((tries + 1))
    if (( tries > SINK_READY_TRIES )); then
      log "the sink never announced itself — see $1"
      return 1
    fi
  done
}

start_sink() {  # host mode port delay_ms tag
  local host=$1 mode=$2 port=$3 delay=$4 tag=$5
  $PYTHON -m ftsbench.null_sink --mode "$mode" --host "$host" --port "$port" \
    --delay-ms "$delay" --report-interval 30 --label "$tag" \
    --stats-out "$OUT_DIR/sink-$tag.json" > "$LOG_DIR/sink-$tag.log" 2>&1 &
  SINK_PID=$!
  await_sink "$LOG_DIR/sink-$tag.log"
}

stop_sink() {
  [[ -n "$SINK_PID" ]] || return 0
  kill "$SINK_PID" 2>/dev/null || true
  wait "$SINK_PID" 2>/dev/null || true
  SINK_PID=""
}

start_probe() {  # series engine label
  $PYTHON -m ftsbench.generator_probe --output "$1" \
    --interval "$PROBE_INTERVAL" --engine "$2" --label "$3" \
    --corpus synthetic >> "$LOG_DIR/probe.log" 2>&1 &
  PROBE_PID=$!
}

stop_probe() {
  [[ -n "$PROBE_PID" ]] || return 0
  kill -TERM "$PROBE_PID" 2>/dev/null || true
  wait "$PROBE_PID" 2>/dev/null || true
  PROBE_PID=""
}

cleanup() {
  stop_probe || true
  stop_sink || true
}
trap cleanup EXIT

worker_share() {  # total workers index
  local total=$1 workers=$2 index=$3
  local share=$((total / workers))
  if (( index < total % workers )); then
    share=$((share + 1))
  fi
  if (( share < 1 )); then
    share=1
  fi
  echo "$share"
}

loader_argv() {  # engine host port corpus batch conc latency_log -> LOADER_ARGV
  local engine=$1 host=$2 port=$3 corpus=$4 batch=$5 conc=$6 latency=$7
  local module endpoint
  module=$(engine_module "$engine")
  endpoint=$(engine_endpoint_args "$engine" "$host" "$port")
  # The ScyllaDB loader has no --batch-size: one operation is one prepared
  # INSERT, so the flag would be rejected rather than ignored. `batch` stays in
  # the label and the point key, where it reads 1 for that engine.
  local batch_flag=(--batch-size "$batch")
  [[ "$engine" == scylladb ]] && batch_flag=()
  # shellcheck disable=SC2206,SC2207  # $endpoint is two words on purpose
  LOADER_ARGV=("$PYTHON" -m "$module" $endpoint --corpus "$corpus"
    "${batch_flag[@]}" --concurrency "$conc" --latency-log "$latency"
    --cache-state n/a
    --label "phase0 client calibration, batch=$batch, c=$conc")
}

await_loaders() {  # pids...
  local failed=0
  for pid in "$@"; do
    wait "$pid" || failed=1
  done
  return $failed
}

run_point() {  # dir corpus_stem engine host port batch conc workers rep
  local dir=$1 stem=$2 engine=$3 host=$4 port=$5 batch=$6 conc=$7
  local workers=$8 rep=$9
  local name="$engine-b$batch-c$conc-w$workers-r$rep"
  local pids=() status=0
  # The label names the point the way ftsbench.verify_generator checks it: the
  # gate refuses a series whose label does not say which rung it covers,
  # because a probe over a whole pass would otherwise answer for every point.
  start_probe "$dir/gen-$name.jsonl" "$engine" \
    "phase0 $name, concurrency=$conc batch=$batch"
  for (( shard = 0; shard < workers; shard++ )); do
    loader_argv "$engine" "$host" "$port" "$stem-$shard.jsonl" "$batch" \
      "$(worker_share "$conc" "$workers" "$shard")" \
      "$dir/lat-$name-s$shard.jsonl"
    "${LOADER_ARGV[@]}" > "$LOG_DIR/$name-s$shard.log" 2>&1 &
    pids+=("$!")
  done
  await_loaders "${pids[@]}" || status=1
  stop_probe
  if (( status != 0 )); then
    log "POINT FAILED: $name — see $LOG_DIR/$name-s*.log"
    return 1
  fi
}

single_process_pass() {  # engine
  local engine=$1 port
  port=$(sink_port "$engine")
  for (( rep = 1; rep <= REPS; rep++ )); do
    for batch in $(engine_batches "$engine"); do
      for conc in $CONC_LADDER; do
        log "$engine batch=$batch c=$conc rep=$rep/$REPS"
        run_point "$OUT_DIR" "$CORPUS_DIR/corpus-w1" "$engine" "$SINK_HOST" \
          "$port" "$batch" "$conc" 1 "$rep"
      done
    done
  done
}

worker_ladder_pass() {  # engine
  local engine=$1 port batch
  port=$(sink_port "$engine")
  batch=$(engine_worker_batch "$engine")
  for (( rep = 1; rep <= REPS; rep++ )); do
    for workers in $WORKER_LADDER; do
      log "$engine worker ladder: N=$workers batch=$batch c=$WORKER_CONC rep=$rep"
      run_point "$OUT_DIR" "$CORPUS_DIR/corpus-w$workers" "$engine" \
        "$SINK_HOST" "$port" "$batch" "$WORKER_CONC" "$workers" \
        "$((LADDER_REP_BASE + rep))"
    done
  done
}

derive_ceilings() {  # engine
  local engine=$1
  $PYTHON -m ftsbench.client_ceilings --data-dir "$OUT_DIR" --engine "$engine" \
    --measured-on "$MEASURED_ON" --out "$OUT_DIR/ceilings-$engine.json" \
    --provenance-out "$OUT_DIR/ceilings-$engine-provenance.json"
}

example_delay_ms() {  # ceilings_json batch conc margin
  $PYTHON - "$1" "$2" "$3" "$4" <<'PY'
import json, sys
path, batch, concurrency, margin = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
with open(path, encoding="utf-8") as handle:
    ceiling = json.load(handle)["ops_per_s"].get(batch)
if not ceiling:
    raise SystemExit(f"no measured ceiling for batch {batch} in {path}")
# `concurrency` operations in flight, each held for d seconds, cannot exceed
# concurrency/d operations per second. Solve for the delay that lands at
# ceiling/margin.
print(round(1000.0 * concurrency * margin / ceiling, 3))
PY
}

achieved_docs_per_s() {  # provenance_json batch
  $PYTHON - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["docs_per_s_ceiling"][sys.argv[2]])
PY
}

expected_verdict() {  # tag
  [[ "$1" == client-bound ]] && echo FAIL || echo PASS
}

verdict_reason() {  # tag
  if [[ "$1" == client-bound ]]; then
    echo "nothing but the client is in the way, so the achieved rate IS the client's ceiling and the >=2x margin cannot clear"
  else
    echo "the sink holds every response, so the client sits far below its own ceiling with the box idle"
  fi
}

write_example_expectation() {  # dir tag engine batch delay achieved
  cat > "$1/expected-verdict.json" <<JSON
{
  "case": "$2",
  "engine": "$3",
  "batch_size": $4,
  "concurrency": $EXAMPLE_CONC,
  "sink_delay_ms": $5,
  "achieved_docs_per_s": $6,
  "expected_verdict": "$(expected_verdict "$2")",
  "why": "$(verdict_reason "$2")",
  "caveat": "constructed on $MEASURED_ON to exercise the gate; not an engine or a campaign measurement"
}
JSON
}

write_gate_command() {  # dir engine batch achieved ceilings_json series
  cat > "$1/gate-command.sh" <<CMD
#!/usr/bin/env bash
# The generator gate on this constructed case. The verdict it MUST give is in
# expected-verdict.json beside this script.
set -euo pipefail
cd "$PWD"
.venv/bin/python3 -m ftsbench.verify_generator \\
  --cpu-series "$6" \\
  --batch-size $3 --concurrency $EXAMPLE_CONC --achieved-docs-per-s $4 \\
  --ceilings "$5"
CMD
  chmod +x "$1/gate-command.sh"
}

gate_agrees() {  # tag exit_status
  # Zero or non-zero only. The gate also has a distinct REFUSAL exit, which
  # reads as non-zero here, so agreement on a FAIL case is necessary and not
  # sufficient — read gate-output.txt to tell a refusal from a verdict.
  if [[ "$(expected_verdict "$1")" == PASS ]]; then
    (( $2 == 0 ))
  else
    (( $2 != 0 ))
  fi
}

run_gate_if_present() {  # dir tag
  local dir=$1 tag=$2 status=0
  if ! $PYTHON -c "import ftsbench.verify_generator" 2>/dev/null; then
    log "ftsbench.verify_generator is not present yet — the $tag example is "\
"staged for it at $dir/gate-command.sh"
    return 0
  fi
  "$dir/gate-command.sh" > "$dir/gate-output.txt" 2>&1 || status=$?
  echo "$status" > "$dir/gate-exit-code.txt"
  if gate_agrees "$tag" "$status"; then
    log "$tag example: gate exited $status, which AGREES with the expected "\
"$(expected_verdict "$tag") — see $dir/gate-output.txt"
    return 0
  fi
  log "GATE DISAGREES on the $tag example: exit $status against an expected "\
"$(expected_verdict "$tag"). This case was constructed to have one right "\
"answer; see $dir/gate-output.txt and $dir/expected-verdict.json"
  return 1
}

gate_example() {  # engine tag delay_ms batch
  local engine=$1 tag=$2 delay=$3 batch=$4
  local dir="$OUT_DIR/example-$tag-$engine" mode port achieved
  mode=$(sink_mode "$engine")
  port=$(example_port "$engine")
  mkdir -p "$dir"
  start_sink "$EXAMPLE_HOST" "$mode" "$port" "$delay" "$tag-$engine"
  run_point "$dir" "$CORPUS_DIR/example" "$engine" "$EXAMPLE_HOST" "$port" \
    "$batch" "$EXAMPLE_CONC" 1 1
  stop_sink
  $PYTHON -m ftsbench.client_ceilings --data-dir "$dir" --engine "$engine" \
    --measured-on "$MEASURED_ON ($tag example)" --out "$dir/observed.json" \
    --provenance-out "$dir/provenance.json" --quiet
  achieved=$(achieved_docs_per_s "$dir/provenance.json" "$batch")
  write_example_expectation "$dir" "$tag" "$engine" "$batch" "$delay" "$achieved"
  write_gate_command "$dir" "$engine" "$batch" "$achieved" \
    "$OUT_DIR/ceilings-$engine.json" \
    "$dir/gen-$engine-b$batch-c$EXAMPLE_CONC-w1-r1.jsonl"
  log "$tag example for $engine: $achieved docs/s achieved at ${delay} ms delay"
  # A ceilings document with no loader_core_bound_at makes the gate REFUSE, which
  # is correct and is not a verdict — so judging the case against it would report
  # a refusal as a disagreement about something the gate never got to judge.
  if has_core_bound "$OUT_DIR/ceilings-$engine.json"; then
    run_gate_if_present "$dir" "$tag"
  else
    log "$tag example staged but NOT judged: ceilings-$engine.json carries no "\
"loader_core_bound_at, so the gate can only refuse. Run the worker ladder long "\
"enough to measure it, then $dir/gate-command.sh"
  fi
}

example_batch() {  # engine
  if [[ -n "$EXAMPLE_BATCH" ]]; then
    echo "$EXAMPLE_BATCH"
    return 0
  fi
  local batches
  batches=$(engine_batches "$1")
  echo "${batches%% *}"
}

has_core_bound() {  # ceilings_json
  $PYTHON - "$1" <<'CHECK'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    raise SystemExit(0 if json.load(handle).get("loader_core_bound_at") else 1)
CHECK
}

gate_examples() {  # engine
  local engine=$1 batch delay
  batch=$(example_batch "$engine")
  gate_example "$engine" client-bound 0 "$batch"
  delay=$(example_delay_ms "$OUT_DIR/ceilings-$engine.json" "$batch" \
    "$EXAMPLE_CONC" "$EXAMPLE_MARGIN")
  gate_example "$engine" engine-bound "$delay" "$batch"
}

calibrate() {  # engine
  local engine=$1 mode port
  mode=$(sink_mode "$engine")
  port=$(sink_port "$engine")
  if [[ "$START_SINK" == 1 ]]; then
    start_sink "$SINK_HOST" "$mode" "$port" 0 "$engine"
  else
    log "using the sink already running at $SINK_HOST:$port ($mode)"
  fi
  single_process_pass "$engine"
  if [[ -n "${WORKER_LADDER// /}" && "$WORKER_LADDER" != "1" ]]; then
    worker_ladder_pass "$engine"
  else
    log "WORKER_LADDER is empty or single — loader_core_bound_at will be OMITTED"
  fi
  stop_sink
  derive_ceilings "$engine"
  gate_examples "$engine"
}

mkdir -p "$OUT_DIR" "$LOG_DIR"
log "Phase 0 client calibration into $OUT_DIR"
log "engines='$ENGINES' docs=$DOCS reps=$REPS conc='$CONC_LADDER' workers='$WORKER_LADDER'"
log "loaders run HERE ($MEASURED_ON); the constants are transferable to no other box"
generate_corpora
for engine in $ENGINES; do
  calibrate "$engine"
done

log "done — ceilings in $OUT_DIR/ceilings-*.json, provenance beside them"
log "NOT the campaign's constants unless $MEASURED_ON is the harness box"
