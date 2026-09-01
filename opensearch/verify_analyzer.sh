#!/usr/bin/env bash
# Assert the m1_parity analyzer reproduces the vector-store M1 analyzer exactly.
#
# The expected token streams below are ground truth: they are the output of
# vector-store's own `build_standard_analyzer()` (SimpleTokenizer + LowerCaser +
# StopWordFilter(English), see fts_index/tantivy.rs) run over these probes.
#
# Probes are chosen to cover the cases where a UAX#29 `standard` tokenizer and
# Tantivy's SimpleTokenizer DISAGREE — apostrophes, dotted abbreviations,
# decimals, underscores, hostnames, addresses, CJK runs. An earlier version of
# this script only probed case folding, stop words and hyphen splitting, all of
# which the two tokenizers agree on, so it passed while 70% of corpus documents
# tokenized differently. Keep every divergence class represented.
#
# Positions are asserted too, not just token text: M1 supports exact phrases,
# so a stop word must leave the same position gap on both sides.
#
# Usage: OS_URL=http://localhost:9200 verify_analyzer.sh [index]
set -euo pipefail

OS_URL="${OS_URL:-http://localhost:9200}"
INDEX="${1:-wiki-articles}"

# probe <TAB> expected "position:token" stream
PROBES=$(cat <<'EOF'
PHOTOSYNTHESIS	0:photosynthesis
the database	1:database
wide-column database built for high-throughput, low-latency workloads	0:wide 1:column 2:database 3:built 5:high 6:throughput 7:low 8:latency 9:workloads
run running runs	0:run 1:running 2:runs
Theory of Relativity	0:theory 2:relativity
The U.S. Army in Washington D.C.	1:u 2:s 3:army 5:washington 6:d 7:c
don't stop the world's music	0:don 1:t 2:stop 4:world 5:s 6:music
version 3.14 was released	0:version 1:3 2:14 4:released
foo_bar and snake_case names	0:foo 1:bar 3:snake 4:case 5:names
see www.fifa.com for details	0:see 1:www 2:fifa 3:com 5:details
e-mail user@example.org now	0:e 1:mail 2:user 3:example 4:org 5:now
Tokyo 東京都 and 日本語 text	0:tokyo 1:東京都 3:日本語 4:text
AT&T 100% CO2 naïve café	1:t 2:100 3:co2 4:naïve 5:café
EOF
)

analyze() {
  python3 - "$OS_URL" "$INDEX" "$1" <<'PY'
import json, sys, urllib.request
url, index, text = sys.argv[1:4]
request = urllib.request.Request(
    f"{url}/{index}/_analyze",
    data=json.dumps({"analyzer": "m1_parity", "text": text}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request) as response:
    tokens = json.load(response)["tokens"]
print(" ".join(f'{t["position"]}:{t["token"]}' for t in tokens))
PY
}

echo "m1_parity vs vector-store M1 analyzer (index '$INDEX' at $OS_URL):"
failures=0
while IFS=$'\t' read -r probe expected; do
  [[ -z "$probe" ]] && continue
  actual="$(analyze "$probe")"
  if [[ "$actual" == "$expected" ]]; then
    printf '  ok    %-56s %s\n' "$probe" "$actual"
  else
    failures=$((failures + 1))
    printf '  FAIL  %-56s\n          expected: %s\n          actual:   %s\n' \
      "$probe" "$expected" "$actual"
  fi
done <<< "$PROBES"

if (( failures > 0 )); then
  cat >&2 <<EOF

$failures probe(s) FAILED: analyzer parity is broken. Recall, ranking and
BM25 comparisons are not trustworthy until this passes. Fix
opensearch/index-config.json (and index-config-ramindex.json) and recreate
the index — a running index cannot have its analyzer changed in place.
EOF
  exit 1
fi

cat >&2 <<'EOF'

All probes passed: OpenSearch and the vector-store tokenize these identically,
positions included.

Known residual (not covered by a probe, and not fixable on the OpenSearch
side): Turkish dotted capital I, U+0130. Rust's char::to_lowercase applies the
Unicode full lowercase mapping and yields "i" + U+0307; Java's
Character.toLowerCase yields plain "i". Measured on 3,000 simplewiki docs:
12 divergent tokens out of 533,388 (0.002%), in 5 documents, all Turkish
proper nouns. Char-filter workarounds do not help — char filters run before
the tokenizer, so an injected U+0307 splits the token instead.
EOF
