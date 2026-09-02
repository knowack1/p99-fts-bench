#!/usr/bin/env bash
# Render every read-path chart (deck S18-S26 + S28) from the cube and churn
# artifacts. One command so the deck refill is reproducible verbatim.
#   tools/render_readpath.sh <cells-dir> <out-dir> [churn-dir]
set -euo pipefail
cd "$(dirname "$0")/.."

CELLS="${1:?usage: render_readpath.sh <cells-dir> <out-dir> [churn-dir]}"
OUT="${2:?usage: render_readpath.sh <cells-dir> <out-dir> [churn-dir]}"
CHURN="${3:-}"
PYTHON="${PYTHON:-.venv/bin/python3}"
FOOTER="${FOOTER:-AWS 2x i8g.2xlarge (SUT-CONFIG.md), enwiki 8,967,625 docs resident, closed-loop sharded client (TUNING.md \\u00a77) — repro: github.com/knowack1/p99-fts-bench}"
OS_GLOB="$CELLS/cell-opensearch-refresh3-*.jsonl"
SC_GLOB="$CELLS/cell-scylla-cdc-*.jsonl"
COMMON=(--config "opensearch:$OS_GLOB" --config "scylla-cdc:$SC_GLOB"
        --no-preliminary-stamp --footer-extra "$FOOTER")
mkdir -p "$OUT"

sweep() { # class limit slide title
  $PYTHON -m ftsbench.plot_readcube --mode sweep --query-class "$1" \
    --limit "$2" "${COMMON[@]}" --title "$4" \
    --output "$OUT/$3.png"
}
sweep rare_term   10   s18-rare-l10    "Latency + throughput: rare term, top-10"
sweep rare_term   100  s19-rare-l100   "Latency + throughput: rare term, top-100"
sweep rare_term   1000 s20-rare-l1000  "Latency + throughput: rare term, top-1000"
sweep common_term 10   s22-common-l10  "Latency + throughput: common, top-10"
sweep phrase      10   s23-phrase-l10  "Latency + throughput: phrase, top-10"
sweep bool_mixed  10   s24-bool-l10    "Latency + throughput: boolean, top-10"

$PYTHON -m ftsbench.plot_readcube --mode heatmap --rows limit \
  --query-class rare_term "${COMMON[@]}" \
  --title "p99 heatmap: top-N x concurrency" --output "$OUT/s21-heatmap-topn.png"
$PYTHON -m ftsbench.plot_readcube --mode heatmap --rows class --cols concurrency \
  --limit 10 "${COMMON[@]}" \
  --title "p99 heatmap: query class x concurrency" --output "$OUT/s25-heatmap-class.png"
$PYTHON -m ftsbench.plot_readcube --mode heatmap --rows class --cols limit \
  --concurrency 64 "${COMMON[@]}" \
  --title "Read path summary: p99 heatmap" --output "$OUT/s26-heatmap-matrix.png"

if [[ -n "$CHURN" ]]; then
  $PYTHON -m ftsbench.plot_readcube --mode heatmap --rows churn \
    --query-class rare_term --limit 10 \
    --config "opensearch:$CHURN/cell-opensearch-refresh3-*.jsonl" \
    --config "scylla-cdc:$CHURN/cell-scylla-cdc-*.jsonl" \
    --no-preliminary-stamp --footer-extra "$FOOTER" \
    --title "p99 across churn and query load" \
    --output "$OUT/s28-heatmap-churn.png"
fi
echo "rendered into $OUT" >&2
