#!/usr/bin/env bash
# A/B one vector-store ingest variant against stock, at the saturating
# concurrency, on a document budget (deck S11-S15 build rate; B2 in the
# inline-dispatch loop plan).
#
# Why an A/B script and not another rung of sweep_build_rate.sh: the two arms
# differ by ONE environment variable on the SAME image, and they must run in
# the same session against the same stack. The 2026-08-27 bottleneck matrix is
# the cautionary tale -- it ranked 14 variants measured across sessions and its
# own README had to disclaim the ranking, because identical configurations
# returned 16,804 and 6,341 docs/s. Arms interleaved rep-major here so a drift
# in the box shows up as noise in BOTH arms rather than as an effect in one.
#
# Every rep is bracketed by resource_probe on both containers of the ScyllaDB
# side: docs/s alone cannot distinguish "removed overhead" from "removed a
# serialisation point", and CPU can. RSS is a correctness gate, not decoration
# -- the vector-store stops adding documents at its memory budget and keeps
# answering queries, so a breach is silent document skipping.
set -euo pipefail
cd "$(dirname "$0")/.."

VARIANT_ENV="${VARIANT_ENV:-VS_FTS_INLINE_INGEST}"
VARIANT_VALUE="${VARIANT_VALUE:-1}"
OUT_DIR="${OUT_DIR:-data/inline-ab}"
REPS="${REPS:-3}"
CONC="${CONC:-64}"
DOCS="${DOCS:-1000000}"
MAX_SECONDS="${MAX_SECONDS:-2400}"
IDLE_TIMEOUT="${IDLE_TIMEOUT:-90}"
CQLSH="${CQLSH:-docker exec -i fts-bench-scylla cqlsh}"

# Iteration corpus, NOT the frozen FREEZE.md one. Both arms read the same file,
# so the A/B ratio is sound; absolute docs/s from it is not comparable to the
# published ceilings and must never reach a slide. See BUILD-RATE-LOOP.md.
CORPUS="${CORPUS:-/mnt/nvme/data/corpus-ab.jsonl}"

mkdir -p "$OUT_DIR"
log() { printf '\n=== [%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# An instrument, not a variable under test, so it is set identically for both
# arms: the vector-store's own received/added/lock-wait counters are what
# separate "delivery got faster" from "the actor stopped being the constraint".
# Setting it on one arm only would make its small cost look like an effect.
export VS_FTS_METRICS_INTERVAL="${VS_FTS_METRICS_INTERVAL:-1s}"

stop_stack() { make scylla-down >/dev/null 2>&1 || true; }
trap stop_stack EXIT

reset_index() {
  $CQLSH <<'CQL'
DROP INDEX IF EXISTS wiki.articles_body_fts;
DROP TABLE IF EXISTS wiki.articles;
DROP KEYSPACE IF EXISTS wiki;
CQL
  make scylla-schema scylla-index scylla-serving
}

# The whole point of the A/B: arm "off" must leave the variable UNSET, not set
# to 0, so it exercises the same code path a stock image would.
apply_arm() {
  case "$1" in
    off) unset "$VARIANT_ENV" ;;
    on)  export "$VARIANT_ENV=$VARIANT_VALUE" ;;
  esac
}

# Read the tuning back off the vector-store's own startup line rather than
# trusting that the environment arrived. A knob the image ignores looks exactly
# like a knob that does not help.
assert_arm_active() {
  local arm="$1" want line
  case "$arm" in
    off) want="dispatch=worker-pool" ;;
    on)  want="dispatch=inline" ;;
  esac
  line=$(docker logs fts-bench-vector-store 2>&1 | grep -m1 'fts: ingest tuning' || true)
  [ -n "$line" ] || { log "FATAL: no 'fts: ingest tuning' line in vector-store log"; return 1; }
  log "tuning: $line"
  case "$line" in
    *"$want"*) ;;
    *) log "FATAL: arm '$arm' expected '$want' but the engine reported otherwise"; return 1 ;;
  esac
  docker logs fts-bench-vector-store 2>&1 | grep -m1 'index writer using' >&2 || true
}

run_rep() {
  local arm="$1" rep="$2"
  local tag="$arm-$rep"
  local series="$OUT_DIR/c1-$tag.jsonl"

  apply_arm "$arm"
  make scylla-down >/dev/null 2>&1 || true
  make scylla-up scylla-wait
  reset_index
  assert_arm_active "$arm" || return 1

  # The probe reads /sys/fs/cgroup on whatever machine it runs on, so on the
  # fleet it must execute on the SUT -- DOCKER_HOST=ssh:// cannot carry that.
  # Running it here would silently record the harness's idle cgroups and the
  # CPU/RSS half of this measurement would be quietly worthless.
  tools/sut_probe.sh start "$OUT_DIR/cpu-$tag.jsonl" \
    --engine scylladb \
    --containers fts-bench-scylla:scylladb \
    --containers fts-bench-vector-store:vector-store \
    --vs-url http://localhost:16080 --keyspace wiki \
    --vs-index articles_body_fts \
    --interval 1 --duration 0 --label "$tag"

  local rc=0
  make c1-scylla-cdc \
    "CORPUS=$CORPUS" \
    "C1_MAX_SECONDS=$MAX_SECONDS" "C1_IDLE_TIMEOUT=$IDLE_TIMEOUT" \
    "MAX_DOCS=$DOCS" "C1_UNTIL_DOCS=$DOCS" \
    "INGEST_CONCURRENCY=$CONC" "REP=$rep" \
    "CACHE_STATE=warm-container-fresh-index" \
    "LABEL=inline-ab arm=$arm conc=$CONC" \
    "C1_SCYLLA_CDC_SERIES=$series" \
    "C1_SCYLLA_CDC_MANIFEST=$OUT_DIR/manifest-$tag.json" \
    >"$OUT_DIR/load-$tag.log" 2>&1 || rc=1

  tools/sut_probe.sh stop "$OUT_DIR/cpu-$tag.jsonl" || true
  docker logs fts-bench-vector-store >"$OUT_DIR/vslog-$tag.log" 2>&1
  [ -s "$series" ] || rc=1
  [ $rc -eq 0 ] || log "arm=$arm rep=$rep FAILED (rc=$rc)"
  return $rc
}

# Rep-major so box drift lands on both arms rather than on whichever ran later.
for rep in $(seq 1 "$REPS"); do
  for arm in off on; do
    log "arm=$arm rep=$rep ($VARIANT_ENV, conc=$CONC, docs=$DOCS)"
    set +e; run_rep "$arm" "$rep"; set -e
  done
done

log "A/B complete -- $OUT_DIR"
