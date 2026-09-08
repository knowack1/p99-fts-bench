#!/usr/bin/env bash
# Does one loader process still bind the ScyllaDB write path?
#
# BUILD-RATE-LOOP.md measured, on this fleet: 376 MB buffer with one loader =
# 9,650 docs/s, with two disjoint shards = 12,228. The cause was named — all of
# scylla_load's encoding ran on one thread — and commit 295c726 moved encoding
# off the dispatcher thread when it unified the two loaders. Nobody has measured
# it since.
#
# It has to be settled BEFORE the knob matrix, not after. sweep_build_rate.sh
# runs one loader per point, so if the cap is still there, R2 and R3 flat-top on
# the client: the writer buffer reads as roughly +7% when the same change
# measured +42%, and every conclusion about the knob under test is wrong while
# every artifact looks complete.
#
# Measured on the buf376 arm because that is where the engine outruns the client
# — at R1's stock 15 MB floor the vector-store is CPU-pinned at 3.98/4 cores and
# one loader is provably enough, so calibrating there would prove nothing.
set -euo pipefail
cd "$(dirname "$0")/.."

REPS="${1:-3}"
ARM="${ARM:---scylladb-cdc-buf376}"
CONC="${CONC:-64}"
OUT_DIR="${OUT_DIR:-data/calibration-$(date -u +%Y-%m-%d)}"
SWEEP_DOCS="${SWEEP_DOCS:-1000000}"
SHARD_GLOB="${SHARD_GLOB:-/mnt/nvme/data/corpus-ab-?.jsonl}"

# Both arms must differ ONLY in the generator. sweep_build_rate.sh exports the
# arm's knobs itself; sharded_build_rate.sh predates the registry and would
# inherit whatever .env.sut happens to say, so the knobs are exported here for
# both. Without this the comparison confounds the loader count with the writer
# buffer and the metrics interval.
eval "$(.venv/bin/python3 -m ftsbench.target "$ARM" --shell)"
export VS_FTS_COMMIT_THRESHOLD VS_FTS_METRICS_INTERVAL VS_FTS_WRITER_MEMORY_MB \
       VS_FTS_COMMIT_INTERVAL

echo "calibration: $ARM at c=$CONC, $SWEEP_DOCS docs, N=$REPS" >&2
echo "  knobs: buffer=${VS_FTS_WRITER_MEMORY_MB:-<tantivy floor>} threshold=${VS_FTS_COMMIT_THRESHOLD} metrics=${VS_FTS_METRICS_INTERVAL}" >&2
echo "  arm A: one loader   (what sweep_build_rate.sh does today)" >&2
echo "  arm B: two shards   (what sharded_build_rate.sh does)" >&2
echo "  shards: $SHARD_GLOB" >&2

mkdir -p "$OUT_DIR"

# Arm A — the sweep's own driver, one rung, so the comparison is against the
# exact code path the matrix will use rather than a reimplementation of it.
OUT_DIR="$OUT_DIR/single" LADDER="$CONC" SWEEP_DOCS="$SWEEP_DOCS" WARMUP=0 \
  tools/sweep_build_rate.sh "$ARM" "$REPS"

# Arm B — the sharded generator at the same concurrency PER SHARD, so each
# process offers what one process offered in arm A and the engine sees twice the
# supply. Comparing equal total concurrency instead would change two things at
# once and answer nothing.
OUT_DIR="$OUT_DIR/sharded" CONC="$CONC" SHARD_GLOB="$SHARD_GLOB" \
  TOTAL_DOCS="$SWEEP_DOCS" \
  tools/sharded_build_rate.sh scylla-cdc "$REPS"

echo >&2
echo "=== verdict ===" >&2
.venv/bin/python3 - "$OUT_DIR" <<'PY'
import glob, json, statistics, sys

def rates(pattern):
    out = []
    for path in sorted(glob.glob(pattern)):
        last = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)
        docs = last.get("docs_indexed") or 0
        secs = last.get("t_elapsed_s") or 0
        if docs and secs:
            out.append(docs / secs)
    return out

root = sys.argv[1]
single = rates(f"{root}/single/c1-*.jsonl")
sharded = rates(f"{root}/sharded/c1-*.jsonl")
if not single or not sharded:
    print(f"incomplete: {len(single)} single, {len(sharded)} sharded reps")
    raise SystemExit(2)

a, b = statistics.median(single), statistics.median(sharded)
print(f"one loader : {a:8.0f} docs/s  {[round(v) for v in single]}")
print(f"two shards : {b:8.0f} docs/s  {[round(v) for v in sharded]}")
print(f"ratio      : {b / a:.3f}x")
print()
# 5% is above the ~3.5% worst-case median-of-3 error measured across the
# existing 5-rep ladder, so a difference this size is a real one.
if b / a > 1.05:
    print("THE CLIENT STILL BINDS. Shard the sweep before running the matrix,")
    print("or R2/R3 will measure the loader and understate the writer buffer.")
else:
    print("One loader is not the constraint at this arm and concurrency.")
    print("sweep_build_rate.sh may run single-process as it stands.")
PY
