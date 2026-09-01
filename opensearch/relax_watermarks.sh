#!/usr/bin/env bash
# Move OpenSearch's disk watermarks, then wait until index creation is actually
# permitted again.
#
# The watermarks are percentages, so a large disk trips the default 90% high
# watermark with tens of GB still free. DiskThresholdMonitor then applies an
# index-create block, and every os-index fails with a bare 403. Two properties
# of that block make it awkward to clear:
#
#   - it is applied at runtime, so it does NOT appear in _cluster/settings and
#     cannot be deleted by writing null to cluster.blocks.create_index;
#   - the monitor only releases it on its next evaluation cycle, ~30s later.
#
# So raising the thresholds is necessary but not sufficient: a caller that
# creates an index immediately afterwards still lands inside the release window.
# This script therefore probes until a create succeeds and fails loudly if it
# never does, rather than leaving the 403 for an unrelated target to hit.
#
# This is a deviation from stock OpenSearch. Any run that needed it has to say
# so; see TUNING.md.
set -euo pipefail

OS_URL="${OS_URL:-http://localhost:9200}"
LOW="${OS_WATERMARK_LOW:-97%}"
HIGH="${OS_WATERMARK_HIGH:-98%}"
FLOOD="${OS_WATERMARK_FLOOD:-99%}"
PROBE_INDEX="${OS_WATERMARK_PROBE_INDEX:-watermark-probe}"
TIMEOUT_S="${OS_WATERMARK_TIMEOUT_S:-180}"

settings_payload() {
  LOW="$LOW" HIGH="$HIGH" FLOOD="$FLOOD" python3 -c '
import json, os
print(json.dumps({"persistent": {
    "cluster.routing.allocation.disk.watermark.low": os.environ["LOW"],
    "cluster.routing.allocation.disk.watermark.high": os.environ["HIGH"],
    "cluster.routing.allocation.disk.watermark.flood_stage": os.environ["FLOOD"],
    "cluster.blocks.create_index": None,
}}))'
}

apply_settings() {
  curl -fsS -X PUT "$OS_URL/_cluster/settings" \
    -H 'Content-Type: application/json' -d "$(settings_payload)" >/dev/null
}

create_allowed() {
  curl -fsS -X PUT "$OS_URL/$PROBE_INDEX" >/dev/null 2>&1 || return 1
  curl -fsS -X DELETE "$OS_URL/$PROBE_INDEX" >/dev/null 2>&1 || true
  return 0
}

apply_settings

deadline=$(( SECONDS + TIMEOUT_S ))
until create_allowed; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "index creation still blocked after ${TIMEOUT_S}s at low=$LOW high=$HIGH" >&2
    curl -fsS "$OS_URL/_cat/allocation?v" >&2 || true
    exit 1
  fi
  echo "waiting for the disk-watermark index-create block to clear..." >&2
  sleep 5
done

echo "watermarks: low=$LOW high=$HIGH flood=$FLOOD; index creation permitted" >&2
