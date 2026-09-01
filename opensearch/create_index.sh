#!/usr/bin/env bash
# Create the analyzer-parity index from index-config.json.
# Usage: OS_URL=http://localhost:9200 create_index.sh [index] [--recreate]
#
# OS_REFRESH_INTERVAL overrides the refresh_interval in the config. The campaign
# runs OpenSearch twice, at 1s and at 30s, because 30s is a real throughput
# tuning and picking it silently would be choosing the flattering setting for
# one engine. It is applied at creation rather than by a later _settings PUT so
# there is no window in which documents were indexed under the other value.
set -euo pipefail

OS_URL="${OS_URL:-http://localhost:9200}"
INDEX="${1:-wiki-articles}"
RECREATE="${2:-}"
OS_REFRESH_INTERVAL="${OS_REFRESH_INTERVAL:-}"
# OS_INDEX_CONFIG selects the mapping. index-config-ramindex.json is the
# ScyllaDB-parity variant: same analyzer, but _source disabled so the index
# carries no document text, matching Tantivy's schema.
CONFIG_FILE="$(cd "$(dirname "$0")" && pwd)/${OS_INDEX_CONFIG:-index-config.json}"

index_exists() {
  curl -fso /dev/null "$OS_URL/$INDEX"
}

if index_exists; then
  if [[ "$RECREATE" == "--recreate" ]]; then
    curl -fsS -X DELETE "$OS_URL/$INDEX" >/dev/null
    echo "dropped existing index '$INDEX'" >&2
  else
    echo "error: index '$INDEX' already exists at $OS_URL — pass --recreate to drop it first" >&2
    exit 1
  fi
fi

payload() {
  if [[ -z "$OS_REFRESH_INTERVAL" ]]; then
    cat "$CONFIG_FILE"
    return
  fi
  OS_REFRESH_INTERVAL="$OS_REFRESH_INTERVAL" python3 -c '
import json, os, sys
config = json.load(open(sys.argv[1]))
config["settings"]["index"]["refresh_interval"] = os.environ["OS_REFRESH_INTERVAL"]
json.dump(config, sys.stdout)
' "$CONFIG_FILE"
}

payload | curl -fsS -X PUT "$OS_URL/$INDEX" \
  -H 'Content-Type: application/json' \
  --data-binary @-
echo
echo "created index '$INDEX' at $OS_URL (refresh_interval=${OS_REFRESH_INTERVAL:-from index-config.json})" >&2
