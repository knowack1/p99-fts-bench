#!/usr/bin/env bash
# Is the ~5,300 docs/s index ceiling CPU-bound, and on which side?
#
# The batched ladder showed both containers near saturation at concurrency 32:
# vector-store 83% of 6 cores, ScyllaDB 93% of 3. That is consistent with a CPU
# ceiling but does not identify which side, so each is scaled separately.
#
# ENGINE_CPUSET is widened to 0-17 for EVERY config here, including the
# baseline, so the doubled configs are not simply oversubscribing the 12 cores
# the earlier runs used. The generator moves to 18-21. Re-running the baseline
# under the wider cpuset is what makes the comparison single-variable.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-data/cpu-scaling}"
REPS="${REPS:-3}"
CONC="${CONC:-32}"
PYTHON="${PYTHON:-.venv/bin/python3}"
mkdir -p "$OUT_DIR"

export VS_FTS_COMMIT_INTERVAL=3s VS_FTS_COMMIT_THRESHOLD=0 VS_FTS_ADD_LOCK=shared
export VS_FTS_METRICS_INTERVAL=5s
export VS_CDC_FINE_SLEEP="" VS_CDC_FINE_SAFETY="" VS_CDC_SLEEP="" VS_CDC_SAFETY=""

# name|VS_CPUS|SCYLLA_SMP|SCYLLA_CPUS|SCYLLA_MEMORY|SCYLLA_MEM_LIMIT
CONFIGS=(
  "S1-baseline|6|3|3|2G|4g"
  "S2-vs-doubled|12|3|3|2G|4g"
  "S3-both-doubled|12|6|6|3G|6g"
)

log() { printf '\n=== [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
step() { local w="$1"; shift; if ! "$@" >/dev/null 2>&1; then log "step '$w' failed"; return 1; fi; }

set_env_file() {
  VS_CPUS_V="$1" SMP="$2" SCPUS="$3" SMEM="$4" SLIMIT="$5" $PYTHON - <<'PY'
import os, re
p='docker/.env'
s=open(p).read()
def setk(s,k,v): return re.sub(rf'^{k}=.*$', f'{k}={v}', s, flags=re.M)
s=setk(s,'VS_CPUS',os.environ['VS_CPUS_V'])
s=setk(s,'SCYLLA_SMP',os.environ['SMP'])
s=setk(s,'SCYLLA_CPUS',os.environ['SCPUS'])
s=setk(s,'SCYLLA_MEMORY',os.environ['SMEM'])
s=setk(s,'SCYLLA_MEM_LIMIT',os.environ['SLIMIT'])
s=setk(s,'ENGINE_CPUSET','0-17')
open(p,'w').write(s)
PY
}

run_point() {
  local name="$1" rep="$2"
  local series="$OUT_DIR/c1-scylla-cdc-$name-$rep.jsonl"
  # Full reset per config: a stale stack was what produced the 2.6x spread in
  # the earlier matrix, and this test cannot afford that noise.
  make scylla-reset >/dev/null 2>&1 || true
  step "up" make scylla-up || return 1
  step "wait" make scylla-wait || return 1
  step "schema" make scylla-schema || return 1
  step "index" make scylla-index || return 1
  step "serving" make scylla-serving || return 1

  $PYTHON -m ftsbench.resource_probe --engine scylladb \
    --containers fts-bench-scylla:scylladb --containers fts-bench-vector-store:vector-store \
    --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts \
    --output "$OUT_DIR/cpu-$name-$rep.jsonl" --interval 1 --duration 0 --label "$name" >/dev/null 2>&1 &
  local probe=$!
  local rc=0
  make c1-scylla-cdc GEN_CPUSET=18-21 \
    "C1_MAX_SECONDS=2400" "C1_IDLE_TIMEOUT=90" "SCYLLA_UNLOGGED_BATCH_ROWS=30" \
    "INGEST_CONCURRENCY=$CONC" "BATCH_SIZE=500" "REP=$rep" \
    "CACHE_STATE=warm-container-fresh-index" "LABEL=$name conc=$CONC" \
    "C1_SCYLLA_CDC_SERIES=$series" \
    "C1_SCYLLA_CDC_MANIFEST=$OUT_DIR/manifest-$name-$rep.json" \
    >"$OUT_DIR/load-$name-$rep.log" 2>&1 || rc=1
  kill -TERM "$probe" 2>/dev/null || true; wait "$probe" 2>/dev/null || true
  docker logs fts-bench-vector-store >"$OUT_DIR/vslog-$name-$rep.log" 2>&1
  [ -s "$series" ] || return 1
  return $rc
}

for rep in $(seq 1 "$REPS"); do
  for spec in "${CONFIGS[@]}"; do
    IFS='|' read -r name vs smp scpus smem slimit <<<"$spec"
    set_env_file "$vs" "$smp" "$scpus" "$smem" "$slimit"
    log "$name rep=$rep (VS_CPUS=$vs SMP=$smp SCYLLA_CPUS=$scpus mem=$smem conc=$CONC)"
    set +e; run_point "$name" "$rep"; rc=$?; set -e
    [ $rc -ne 0 ] && log "$name rep=$rep FAILED (rc=$rc)"
  done
done
log "cpu scaling test complete — $OUT_DIR"
