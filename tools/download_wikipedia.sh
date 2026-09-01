#!/usr/bin/env bash
# Download a frozen Wikimedia cirrus_search_index content dump for a wiki.
#
# The legacy cirrussearch dumps (single .json.gz per wiki) are deprecated and
# no longer produced by Wikimedia — see
# https://dumps.wikimedia.org/other/cirrussearch/DEPRECATED.txt. This script
# targets the replacement, cirrus_search_index, which shards each wiki's
# content into one or more numbered .json.bz2 files under a dated directory.
#
# Usage: download_wikipedia.sh [wiki] [out_dir] [dump_date]
#   e.g. simplewiki (default) or enwiki; dump_date defaults to the frozen
#   snapshot recorded in bench/data/MANIFEST.md (20260816) — do not bump this
#   without updating the manifest, it is what every chart is reproducible
#   against.
set -euo pipefail

WIKI="${1:-simplewiki}"
OUT_DIR="${2:-data}"
DUMP_DATE="${3:-20260816}"
BASE_URL="https://dumps.wikimedia.org/other/cirrus_search_index"
INDEX_DIR="index_name=${WIKI}_content"
DUMP_URL="${BASE_URL}/${DUMP_DATE}/${INDEX_DIR}"
TARGET_DIR="${OUT_DIR}/${DUMP_DATE}/${INDEX_DIR}"

mkdir -p "$TARGET_DIR"

echo "Verifying dump completeness: ${DUMP_URL}/_SUCCESS" >&2
if ! curl -fsSL -o /dev/null "${DUMP_URL}/_SUCCESS"; then
  echo "error: no _SUCCESS marker for '${WIKI}' at ${DUMP_URL}" >&2
  echo "       (dump date frozen wrong, or Wikimedia hasn't finished publishing it)" >&2
  exit 1
fi

echo "Listing shards for '${WIKI}' at ${DUMP_URL}..." >&2
shard_files="$(curl -fsSL "${DUMP_URL}/" \
  | grep -oE "${WIKI}_content-${DUMP_DATE}-[0-9]{5}\.json\.bz2" \
  | sort -u)"

if [[ -z "$shard_files" ]]; then
  echo "error: no .json.bz2 shards found for '${WIKI}' at ${DUMP_URL}" >&2
  exit 1
fi

shard_count="$(wc -l <<<"$shard_files")"
echo "Found ${shard_count} shard(s) for '${WIKI}-${DUMP_DATE}'" >&2

manifest="${TARGET_DIR}/${WIKI}_content-${DUMP_DATE}.sha256"
: > "$manifest"

while IFS= read -r shard_file; do
  target="${TARGET_DIR}/${shard_file}"
  echo "Downloading ${DUMP_URL}/${shard_file}" >&2
  if command -v wget >/dev/null; then
    wget -c -O "$target" "${DUMP_URL}/${shard_file}"
  else
    curl -fL --retry 3 -C - -o "$target" "${DUMP_URL}/${shard_file}"
  fi
  (cd "$TARGET_DIR" && sha256sum "$shard_file") >> "$manifest"
done <<<"$shard_files"

echo "Ready: ${shard_count} shard(s) in ${TARGET_DIR}" >&2
echo "Checksums recorded: ${manifest}" >&2
echo "Verify with: (cd ${TARGET_DIR} && sha256sum -c $(basename "$manifest"))" >&2
