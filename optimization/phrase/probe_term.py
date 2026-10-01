"""Probe the single-term path the way probe_phrase.py probed the phrase path.

Term queries already prune (Tantivy implements `for_each_pruning` for
`TermWeight`), and the vector-store already beats OpenSearch on them — so the
question here is not "why are we slow" but "what is the remaining floor made
of". Three cuts:

  floor    — a term that matches nothing: HTTP, parse, and per-segment setup
             with no posting list to walk and no document to fetch
  K sweep  — how much of the cost is per-returned-document (stored-field
             fetch) rather than matching
  spread   — latency against match count across the common_term class

Usage: probe_term.py [reps] [out.json]
"""
import json
import statistics
import sys

import requests

from probe_phrase import OS_URL, os_total_hits, time_os, time_vs

NO_MATCH = "zzqqxxnomatchterm"


def load_terms(path: str, klass: str, count: int) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["classes"][klass][:count]


def main() -> int:
    session = requests.Session()
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/probe-term.json"
    queries = "/home/karolnowacki/Projects/Scylla/p99/bench/data/queries.json"
    terms = load_terms(queries, "common_term", 12)

    print("=== fixed overhead: a term that matches nothing ===")
    floor_os = time_os(session, NO_MATCH, 10, reps)
    floor_vs = time_vs(session, NO_MATCH, 10, reps)
    print(f"{'no-match':24} {'':>10} {floor_os:8.2f} {floor_vs:8.2f}")

    print()
    print("=== per term (limit=10) ===")
    print(f"{'term':24} {'matches':>10} {'OS ms':>8} {'VS ms':>8} "
          f"{'VS-floor':>9}")
    rows = []
    for term in terms:
        total = os_total_hits(session, term)
        o = time_os(session, term, 10, reps)
        v = time_vs(session, term, 10, reps)
        rows.append({"term": term, "matches": total, "os_ms": o, "vs_ms": v})
        print(f"{term:24} {total:10} {o:8.2f} {v:8.2f} {v - floor_vs:9.2f}")

    print()
    print("=== K sweep on the most common term ===")
    probe = max(rows, key=lambda r: r["matches"])["term"]
    print(f"term {probe!r} ({max(r['matches'] for r in rows)} matches)")
    print(f"{'K':>6} {'OS ms':>8} {'VS ms':>8} {'VS per +doc µs':>16}")
    ksweep = []
    prev_v = None
    prev_k = None
    for k in [1, 10, 100, 1000]:
        o = time_os(session, probe, k, reps)
        v = time_vs(session, probe, k, reps)
        per_doc = ""
        if prev_v is not None:
            per_doc = f"{(v - prev_v) * 1000 / (k - prev_k):16.1f}"
        ksweep.append({"k": k, "os_ms": o, "vs_ms": v})
        print(f"{k:6} {o:8.2f} {v:8.2f} {per_doc:>16}")
        prev_v, prev_k = v, k

    median_vs = statistics.median(r["vs_ms"] for r in rows)
    print()
    print(f"median common_term VS latency : {median_vs:.2f} ms")
    print(f"of which fixed floor          : {floor_vs:.2f} ms "
          f"({100 * floor_vs / median_vs:.0f}%)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"floor_os_ms": floor_os, "floor_vs_ms": floor_vs,
                   "per_term": rows, "k_sweep": ksweep, "reps": reps}, f,
                  indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
