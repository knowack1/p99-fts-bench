#!/usr/bin/env bash
# Serialized C1-C8 campaign over the frozen simplewiki corpus on one laptop.
#
# Four configurations, N repetitions each, one engine running at a time. Nothing
# here is publishable: bench/docker/.env disqualifies it in its own header. What
# this produces is the full chart set with real shapes, so the deck can be built
# and reviewed before any AWS money is spent.
#
#   tools/campaign_laptop.sh --smoke        # 20k docs, N=1 — the Phase 2 gate
#   tools/campaign_laptop.sh                # full corpus, N=5 — hours
#   tools/campaign_laptop.sh --configs opensearch --reps 2
#
# Two rules the script enforces rather than trusts:
#
#   * A repetition that fails a gate is ABORTED, not averaged in. A short series
#     is a truncated run, not a fast one, and a partially-indexed corpus is the
#     documented failure mode of the vector-store at its memory limit.
#   * C1 and C3 are separate ingest runs. C1 wants the ceiling (unpaced); C3
#     wants a fixed sustainable rate, because a loader running flat out records
#     its own backlog and reports it as engine latency. One run cannot be both.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
BENCH_DIR="$PWD"
# Absolute, and on the path, so that a helper invoked from anywhere still finds
# both the interpreter and the ftsbench package.
export PYTHONPATH="$BENCH_DIR${PYTHONPATH:+:$PYTHONPATH}"

REPS=5
MAX_DOCS=0
# Below this many samples in the build window the C1 line is too coarse to read
# a shape off, which every chart drawn from it has to disclose. It is NOT a pass
# condition: a complete 270k-document OpenSearch build finishes in fourteen
# one-second samples, and failing that is failing an engine for being fast. See
# assert_series_complete.
MIN_BUILD_SAMPLES=20
CONFIGS="opensearch opensearch-refresh30 scylla-bootstrap scylla-cdc"
REP_LIST=""
# Honours the environment as well as --dry-run: DRY_RUN=1 in front of the
# command reads as a dry run to anyone, and swallowing it once started a real
# full-corpus campaign that had to be killed by hand.
DRY_RUN="${DRY_RUN:-0}"
SMOKE=0
# Write-path campaign (WRITE-PATH-TEST-PLAN.md): ingest + its gates only, no
# freshness, no query phase, no paced C3 run.
INGEST_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    # Repetition 99, not 1: a smoke pass writes into the same data/ directory
    # under the same names as the campaign, and on 2026-08-20 it overwrote four
    # full-corpus repetitions with 20,000-document ones. They were recoverable
    # from data/superseded, which is luck, not a design.
    --smoke) REP_LIST="99"; MAX_DOCS=20000; SMOKE=1; shift ;;
    --reps) REPS="$2"; shift 2 ;;
    # Re-running two repetitions of one configuration after a defect cost us
    # those two: --reps only ever means 1..N.
    --rep-list) REP_LIST="${2//,/ }"; shift 2 ;;
    --max-docs) MAX_DOCS="$2"; shift 2 ;;
    # Greedy, so that both `--configs "a b"` and `--configs a b` work. The
    # second is what gets typed and what REPAIR-PLAN.md documents, and the first
    # attempt at re-running the two OpenSearch configurations died on
    # `unknown argument: opensearch-refresh30`.
    --configs)
      shift; CONFIGS=""
      while [[ $# -gt 0 && "$1" != --* ]]; do CONFIGS="${CONFIGS:+$CONFIGS }$1"; shift; done
      [[ -n "$CONFIGS" ]] || { echo "--configs needs at least one name" >&2; exit 2; } ;;
    --ingest-only) INGEST_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REP_LIST="${REP_LIST:-$(seq 1 "$REPS")}"
REP_COUNT="$(printf '%s\n' $REP_LIST | wc -l)"

LOG_DIR="data/campaign-logs"
mkdir -p "$LOG_DIR"

# --smoke asks one question: does every path produce a well-formed artifact. It
# is not a short measurement, so the query windows are cut to whatever proves the
# plumbing. Leaving them at campaign length would turn a 10-minute gate into an
# hour of numbers nobody may quote anyway.
SMOKE_OVERRIDES=()
if [[ "$SMOKE" == 1 ]]; then
  SMOKE_OVERRIDES=(QUERY_DURATION=10 QUERY_WARMUP=3 C7_RATE_MAX=100
                   FRESHNESS_REPS=3 C3_RATE=500)
  MIN_BUILD_SAMPLES=3
fi

say() { printf '\n=== %s ===\n' "$*" >&2; }
run() {
  if [[ "$DRY_RUN" == 1 ]]; then echo "+ $*" >&2; return 0; fi
  "$@"
}

# --- Preflight -------------------------------------------------------------

corpus_docs() {
  wc -l < data/corpus.jsonl
}

expected_docs() {
  if [[ "$MAX_DOCS" == 0 ]]; then corpus_docs; else echo "$MAX_DOCS"; fi
}

assert_corpus_present() {
  [[ -s data/corpus.jsonl ]] || {
    echo "no data/corpus.jsonl — run 'make download corpus' first" >&2; exit 1; }
  [[ -s data/queries.json ]] || {
    echo "no data/queries.json — run 'make queries' first (see QUERY-SET.md)" >&2
    exit 1; }
}

# Both stacks down before anything starts. Two engines sharing this host would
# compete for the same cores and the same page cache, which is the one condition
# that makes every chart in the set uninterpretable at once.
assert_no_engine_running() {
  local running
  [[ "$DRY_RUN" == 1 ]] && return 0
  running="$(docker ps --format '{{.Names}}' | grep -c '^fts-bench-' || true)"
  [[ "$running" == 0 ]] || {
    echo "fts-bench containers still up; run 'make os-down scylla-down'" >&2
    exit 1; }
}

warn_about_competing_containers() {
  local others
  others="$(docker ps --format '{{.Names}}' | grep -v '^fts-bench-' || true)"
  [[ -z "$others" ]] || {
    echo "WARNING: other containers are running and will steal CPU and RAM:" >&2
    echo "$others" | sed 's/^/  /' >&2
    echo "  stop them for a clean run: docker stop $(echo $others | tr '\n' ' ')" >&2
  }
}

# Chart artifacts are named by config and repetition, so a second campaign
# overwrites some of the first one's files and leaves the rest in place. The
# plot globs cannot tell the two apart: a 20k-doc smoke series and a 270k-doc
# campaign series both match c1-opensearch-*.jsonl, and mixing them puts two
# different runs on one line. Nothing is deleted — the old set is moved aside
# with a timestamp, so a superseded run is still there to be looked at.
# Scoped to the configurations this invocation is about to run, and only those.
# A campaign-wide sweep would archive artifacts that are being kept on purpose:
# re-running the two OpenSearch configurations must leave the fifteen valid
# ScyllaDB artifacts where they are. The selection is a filename parse rather
# than a glob because data/c5-opensearch-* also matches
# c5-opensearch-refresh30-*, which has already cost this harness one chart —
# see ftsbench.archive_artifacts.
archive_previous_artifacts() {
  local archive args=()
  local config rep
  archive="data/superseded/$(date -u +%Y%m%dT%H%M%SZ)"
  for config in $CONFIGS; do args+=(--config "$config"); done
  # Scoped to the repetitions too, or re-running cdc 1 and 3 would archive the
  # query and freshness logs of 2, 4 and 5 — which nothing is about to rewrite.
  for rep in $REP_LIST; do args+=(--rep "$rep"); done
  [[ "$DRY_RUN" == 1 ]] && args+=(--dry-run)
  "$PYTHON" -m ftsbench.archive_artifacts --source data --dest "$archive" \
    "${args[@]}"
}

preflight() {
  say "preflight"
  assert_corpus_present
  assert_no_engine_running
  warn_about_competing_containers
  archive_previous_artifacts
  echo "corpus: $(corpus_docs) docs, measuring $(expected_docs)" >&2
  echo "reps: $REP_LIST   configs: $CONFIGS" >&2
}

# --- Gates -----------------------------------------------------------------

# Every gate outcome is recorded in the repetition's manifest, pass or fail.
# ftsbench.results_tree builds each chart's "Gates that passed" section from that
# record; a gate that only printed to a terminal leaves behind an artifact
# indistinguishable from a run nobody checked. gate_log exits non-zero on a
# failure, so recording and aborting are the same step and a failure cannot be
# recorded while the campaign carries on.
PYTHON="$(if [[ -x "$BENCH_DIR/.venv/bin/python3" ]]; \
  then echo "$BENCH_DIR/.venv/bin/python3"; else echo python3; fi)"
CONFIG=""

manifest_path() { echo "data/manifest-$CONFIG-$REP.json"; }

gate() {
  local name="$1"; shift
  local observed status=pass
  if [[ "$DRY_RUN" == 1 ]]; then echo "+ gate $name ($*)" >&2; return 0; fi
  observed="$("$@" 2>&1)" || status=fail
  "$PYTHON" -m ftsbench.gate_log --manifest "$(manifest_path)" --name "$name" \
    --status "$status" --observed "${observed:-no detail reported}" || {
      echo "GATE FAILED [$name]: $observed" >&2
      echo "repetition $REP of $CONFIG ABORTED — a failed gate is a truncated " \
           "run, not a fast one, and is never averaged in" >&2
      exit 1; }
}

# Each gate prints what it observed on stdout whether it passes or fails: the
# observed value is the part a reviewer needs in order to disagree with the
# verdict, and it is what gate_log stores.
assert_not_oom_killed() {
  local container="$1" state
  state="$(docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' \
    "$container" 2>/dev/null || echo "unknown")"
  echo "$container OOMKilled/ExitCode: $state (want 'false 0')"
  [[ "$state" == "false 0" ]]
}

assert_opensearch_doc_count() {
  local want actual
  want="$(expected_docs)"
  actual="$(curl -fsS "${OS_URL:-http://localhost:9200}/wiki-articles/_count" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')"
  echo "OpenSearch holds $actual docs, expected $want"
  [[ "$actual" == "$want" ]]
}

# The vector-store stops adding documents when it hits its memory limit, logs an
# error and keeps serving queries (SIZING.md). Without this gate that reads as a
# fast, complete build.
assert_scylla_index_complete() {
  local want status count allocation_errors
  want="$(expected_docs)"
  read -r count status <<<"$(curl -fsS \
    "${VS_URL:-http://localhost:16080}/api/v1/indexes/wiki/articles_body_fts/status" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("count",-1), d.get("status",""))')"
  allocation_errors="$(vector_store_allocation_errors)"
  echo "index count=$count status=$status (want $want/SERVING), " \
       "vector-store allocation errors=$allocation_errors (want 0)"
  [[ "$count" == "$want" && "$status" == "SERVING" && "$allocation_errors" == 0 ]]
}

# Only ERROR-level lines count. The vector-store announces its own limit at
# startup — "INFO Memory limit set to 2147483648 bytes" — and a bare search for
# "memory limit" matches that banner on every healthy run, which made this gate
# fail identically whether or not a single document had been skipped. A gate that
# always fails is worth no more than one that never does.
VECTOR_STORE_ALLOCATION_ERROR='ERROR.*([Mm]emory|[Aa]llocat)'

vector_store_allocation_errors() {
  docker logs fts-bench-vector-store 2>&1 \
    | grep -Ec "$VECTOR_STORE_ALLOCATION_ERROR" || true
}

# What makes a build series unusable is that the build did not finish, and the
# way to see that is the document count the last sample reports — not how many
# samples there are. The first campaign asserted a floor of twenty samples and
# aborted all ten OpenSearch repetitions, each of which had indexed every one of
# the 270,269 documents in thirteen to sixteen seconds. Sample count is reported
# as a resolution notice instead, which the chart README then has to carry.
assert_series_complete() {
  "$PYTHON" - "$1" "$2" "$MIN_BUILD_SAMPLES" <<'PYGATE'
import sys

from ftsbench.build_report import build_window
from ftsbench.runmeta import read_jsonl

# build_window needs a first and a last sample to bound itself. The floor is
# deliberately this low: what makes a series unusable is the build not finishing,
# which the document count below decides. A --smoke build of 20k documents lasts
# two or three samples and is a complete build.
WINDOW_FLOOR = 2

path, want_docs, coarse_below = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
head, records = read_jsonl(path)
window, _ = build_window(records)
indexed = int((records[-1] if records else {}).get("docs_indexed") or 0)

detail = (f"{path}: header={'present' if head else 'MISSING'}, "
          f"docs_indexed={indexed}/{want_docs}, "
          f"{len(records)} samples ({len(window)} in the build window)")
if len(window) < coarse_below:
    detail += (f" | NOTE coarse time resolution: fewer than {coarse_below} "
               "samples in the build window, so the C1 line resolves shape "
               "only at whole-second granularity")
print(detail)

sys.exit(0 if head and indexed >= want_docs and len(window) >= WINDOW_FLOOR
         else 1)
PYGATE
}

# --- Phases ----------------------------------------------------------------

MAKE_ARGS=()
LABEL=""

set_make_args() {
  LABEL="$1"
  MAKE_ARGS=("MAX_DOCS=$MAX_DOCS" "REP=$REP" "CACHE_STATE=cold" "LABEL=$LABEL")
}

# A make target that fails is not a slower repetition, it is a repetition with a
# hole in it. In cdc repetition 1 of the first campaign the loader exited
# non-zero, c1_run propagated it and make reported `Error 1` — and the campaign
# walked straight on to the gates. It was caught only because that particular
# loss showed up in an index count; the same failure in the C5 phase would have
# left a half-written latency log and no complaint at all. Recorded through
# gate_log so results_tree shows it beside the real gates.
make_step() {
  local target="${*: -1}"
  if [[ "$DRY_RUN" == 1 ]]; then echo "+ $*" >&2; return 0; fi
  "$@" && return 0
  "$PYTHON" -m ftsbench.gate_log --manifest "$(manifest_path)" \
    --name "make_${target}_succeeded" --status fail \
    --observed "make $target exited non-zero — see this repetition's log" || true
  echo "repetition $REP of $CONFIG ABORTED — make $target failed, and a " \
       "repetition with a failed step is not a repetition" >&2
  exit 1
}

mk() { make_step make "${MAKE_ARGS[@]}" "${SMOKE_OVERRIDES[@]+"${SMOKE_OVERRIDES[@]}"}" "$@"; }

# The query phase runs against a stack that has just ingested the whole corpus,
# so its caches are warm and cache_state must say so: the header is what a chart
# footer quotes, and calling a warm run cold is the kind of error nobody catches
# once the artifact is the only surviving record.
mk_warm() {
  make_step make "${MAKE_ARGS[@]/CACHE_STATE=cold/CACHE_STATE=warm}" \
    "${SMOKE_OVERRIDES[@]+"${SMOKE_OVERRIDES[@]}"}" "$@"
}

opensearch_cold_stack() {
  mk os-reset
  mk os-up
  mk os-wait
  mk os-index OS_REFRESH="$OS_REFRESH"
  mk os-verify-analyzer
}

scylla_cold_stack() {
  mk scylla-reset
  mk scylla-up
  mk scylla-wait
  mk scylla-schema
}

# C4 samples the peak, which is during ingest, so the probe brackets the ingest
# run rather than the whole session.
#
# On the fleet (SUT_IP set — tools/fleet_env.sh) the probe cannot run here:
# it reads /sys/fs/cgroup on the machine it runs on, and the containers are on
# the SUT. tools/sut_probe.sh runs it there and copies the series back on
# stop, so gates and plots read the same file either way. The probe's engine
# URLs are localhost in that case — its own box.
FLEET_PROBE_OUT=""

fleet_probe_args() {
  local target="$1"
  case "$target" in
    c4-os) echo "--engine opensearch --containers fts-bench-opensearch:opensearch --os-url http://localhost:9200 --os-index wiki-articles" ;;
    c4-scylla) echo "--engine scylladb --containers fts-bench-scylla:scylladb --containers fts-bench-vector-store:vector-store --vs-url http://localhost:16080 --keyspace wiki --vs-index articles_body_fts" ;;
    *) echo "unknown probe target: $target" >&2; return 2 ;;
  esac
}

start_resource_probe() {
  local target="$1"
  [[ "$DRY_RUN" == 1 ]] && { echo "+ make $target (background)" >&2; return 0; }
  if [[ -n "${SUT_IP:-}" ]]; then
    FLEET_PROBE_OUT="data/c4-$CONFIG-$REP.jsonl"
    tools/sut_probe.sh start "$FLEET_PROBE_OUT" $(fleet_probe_args "$target")       --interval 1 --duration 0 --cache-state cold --corpus data/corpus.jsonl       --label "$LABEL"
    return 0
  fi
  make "${MAKE_ARGS[@]}" "${SMOKE_OVERRIDES[@]+"${SMOKE_OVERRIDES[@]}"}" "$target" &
  RESOURCE_PROBE_PID=$!
}

stop_resource_probe() {
  [[ "$DRY_RUN" == 1 ]] && return 0
  if [[ -n "${SUT_IP:-}" && -n "$FLEET_PROBE_OUT" ]]; then
    tools/sut_probe.sh stop "$FLEET_PROBE_OUT"
    FLEET_PROBE_OUT=""
    return 0
  fi
  [[ -n "${RESOURCE_PROBE_PID:-}" ]] || return 0
  kill -TERM "$RESOURCE_PROBE_PID" 2>/dev/null || true
  wait "$RESOURCE_PROBE_PID" 2>/dev/null || true
  RESOURCE_PROBE_PID=""
}

# The pacer must be open-loop before C5 or C7 mean anything. A closed-loop
# generator sends the next request only after the previous one returns, so it
# never observes the queueing it caused, and it reports a p99 that flatters
# whichever engine was slower. Checked once per configuration against the live,
# populated index, at a rate deliberately above capacity.
co_gate() {
  local engine="$1"
  [[ "$REP" == 1 ]] || return 0
  if [[ "$DRY_RUN" == 1 ]]; then echo "+ co_gate $engine" >&2; return 0; fi
  if [[ "$SMOKE" == 1 ]]; then
    "$PYTHON" -m ftsbench.gate_log --manifest "$(manifest_path)" \
      --name coordinated_omission_open_loop --status skipped \
      --observed "smoke run: a 20k-doc index answers the check rate without \
queueing, so a pass here would not distinguish an open-loop pacer from an idle \
engine; the gate runs for real in the full campaign"
    return 0
  fi
  gate coordinated_omission_open_loop make "co-check-$engine"
}

# C5's headline series is not produced separately: c6-* runs the generator once
# per class, and C5 is the rare_term artifact from that sweep. Measuring it twice
# would be two chances for the two runs to disagree.
#
# Provenance for this phase is the C1 manifest written earlier in the same stack
# session — same containers, same image pins, same reported engine versions.
query_phase() {
  local engine="$1"
  mk_warm "calibrate-$engine"
  co_gate "$engine"
  mk_warm "c6-$engine"
  mk_warm "c7-$engine"
}

# --- Configurations --------------------------------------------------------

# A gate aborts the repetition by exiting, which skips the config's closing
# os-down / scylla-down. The next config's reset would eventually clear it, but
# in between two engines are up at once — and the campaign's central premise is
# that only one is. Each repetition runs in its own subshell (main pipes it to
# tee), so an EXIT trap here is scoped to that repetition.
all_stacks_down() {
  [[ "$DRY_RUN" == 1 ]] && { echo "+ all_stacks_down" >&2; return 0; }
  make os-down >/dev/null 2>&1 || true
  make scylla-down >/dev/null 2>&1 || true
}

leave_no_engine_running() {
  stop_resource_probe
  all_stacks_down
}

run_opensearch() {
  trap leave_no_engine_running EXIT
  local config="$1" refresh="$2"
  CONFIG="$config"
  OS_REFRESH="$refresh"
  set_make_args "${CAMPAIGN_LABEL:-C1-C8 laptop simplewiki}, $config, refresh=$refresh"
  # OS_CONFIG names every OpenSearch artifact. Without it the refresh=30s
  # configuration writes over the refresh=1s one and the chart draws one
  # configuration twice.
  MAKE_ARGS+=("OS_CONFIG=$config" "OS_REFRESH=$refresh")

  say "$config rep $REP: ingest ceiling (C1, C2, C4)"
  opensearch_cold_stack
  start_resource_probe c4-os
  mk c1-os C1_UNTIL_DOCS="$(expected_docs)"
  stop_resource_probe
  gate opensearch_doc_count assert_opensearch_doc_count
  gate opensearch_not_oom_killed assert_not_oom_killed fts-bench-opensearch
  gate c1_series_complete assert_series_complete "data/c1-$config-$REP.jsonl" "$(expected_docs)"

  if [[ "$INGEST_ONLY" == 0 ]]; then
    say "$config rep $REP: freshness (C8) and queries (C5, C6, C7)"
    mk_warm c8-os OS_REFRESH="$refresh"
    query_phase os

    say "$config rep $REP: paced ingest (C3)"
    opensearch_cold_stack
    mk c3-os
    gate c3_opensearch_not_oom_killed assert_not_oom_killed fts-bench-opensearch
  fi

  mk os-down
}

run_scylla() {
  trap leave_no_engine_running EXIT
  local config="$1" path="$2"
  CONFIG="$config"
  set_make_args "${CAMPAIGN_LABEL:-C1-C8 laptop simplewiki}, $config"
  # C1, C3 and C8 name the path in the target; C4, C5 and C7 are shared by both
  # paths, so without this the CDC repetitions overwrite the bootstrap ones and
  # the bootstrap path's resource and query data is gone with no error.
  MAKE_ARGS+=("SCYLLA_CONFIG=$config")

  say "$config rep $REP: ingest ceiling (C1, C2, C4)"
  scylla_cold_stack
  start_resource_probe c4-scylla
  if [[ "$path" == bootstrap ]]; then
    mk scylla-load
    mk c1-scylla-bootstrap C1_UNTIL_DOCS="$(expected_docs)"
  else
    mk scylla-index
    mk scylla-serving
    mk c1-scylla-cdc C1_UNTIL_DOCS="$(expected_docs)"
  fi
  stop_resource_probe
  gate scylla_index_complete assert_scylla_index_complete
  gate scylla_not_oom_killed assert_not_oom_killed fts-bench-scylla
  gate vector_store_not_oom_killed assert_not_oom_killed fts-bench-vector-store
  gate c1_series_complete assert_series_complete "data/c1-$config-$REP.jsonl" "$(expected_docs)"

  if [[ "$INGEST_ONLY" == 0 ]]; then
    say "$config rep $REP: freshness (C8) and queries (C5, C6, C7)"
    mk_warm "c8-scylla-$path"
    query_phase scylla

    # C3 is the CDC path only: on the bootstrap path the write being timed is a
    # plain base-table insert with no index attached, which is not what C3 claims.
    if [[ "$path" == cdc ]]; then
      say "$config rep $REP: paced ingest (C3)"
      scylla_cold_stack
      mk scylla-index
      mk scylla-serving
      mk c3-scylla-cdc
      gate c3_vector_store_not_oom_killed assert_not_oom_killed fts-bench-vector-store
    fi
  fi

  mk scylla-down
}

# --- Main ------------------------------------------------------------------

run_config() {
  case "$1" in
    opensearch)           run_opensearch opensearch 1s ;;
    opensearch-refresh30) run_opensearch opensearch-refresh30 30s ;;
    scylla-bootstrap)     run_scylla scylla-bootstrap bootstrap ;;
    scylla-cdc)           run_scylla scylla-cdc cdc ;;
    *) echo "unknown config: $1" >&2; return 2 ;;
  esac
}

# The exit status is the campaign's verdict, not the last command's. A run whose
# every repetition aborted would otherwise print "complete" and exit 0, and the
# next step — plotting — would draw whatever stale artifacts happened to match
# the globs.
FAILED_REPS=()

report_and_exit() {
  local total="$1"
  if [[ ${#FAILED_REPS[@]} -eq 0 ]]; then
    say "campaign complete: $total/$total repetitions passed their gates — " \
        "next: make plots, then write the per-chart READMEs"
    [[ "$SMOKE" == 1 ]] && say "these are --smoke artifacts of $MAX_DOCS docs, " \
        "written as repetition 99 — archive them before plotting; the plots " \
        "refuse a config whose repetitions disagree on corpus size"
    return 0
  fi
  say "campaign INCOMPLETE: ${#FAILED_REPS[@]} of $total repetitions aborted"
  printf '  %s\n' "${FAILED_REPS[@]}" >&2
  echo "  an aborted repetition wrote a truncated artifact; plot only after " \
       "re-running it, or the chart shows a failed run as a fast one" >&2
  return 1
}

main() {
  local attempted=0
  preflight
  # Repetition-major on purpose. A campaign this long can be interrupted — by a
  # suspend, by the machine being needed — and configuration-major leaves the
  # last configurations with no data at all, which is not a chart. This way every
  # interruption leaves every configuration at the same N. It also spreads any
  # drift over the run (thermal, background load) across both engines instead of
  # landing it entirely on whichever one ran last.
  local pass=0
  for REP in $REP_LIST; do
    pass=$((pass + 1))
    for config in $CONFIGS; do
      log="$LOG_DIR/$config-$REP.log"
      attempted=$((attempted + 1))
      say "config $config, repetition $REP — pass $pass of $REP_COUNT (log: $log)"
      if run_config "$config" 2>&1 | tee "$log"; then
        continue
      fi
      echo "REPETITION FAILED: $config rep $REP — see $log. Not averaged in." >&2
      FAILED_REPS+=("$config rep $REP — $log")
    done
  done
  report_and_exit "$attempted"
}

# Sourceable so the tests can exercise the gates and the settings without
# running a campaign.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main
fi
