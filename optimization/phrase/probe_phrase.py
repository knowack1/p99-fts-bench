"""Probe: does phrase latency scale with total matches, and does K (limit) matter?

If Tantivy has no block-max pruning for phrase queries it must fully evaluate
every matching document, so its latency is flat in K and linear in match count.
Lucene prunes with block-max WAND, so its latency rises with K.
"""
import json
import statistics
import sys
import time

import requests

OS_URL = "http://127.0.0.1:9200"
VS_URL = "http://127.0.0.1:16080"
INDEX = "wiki-articles"
VS_INDEX = "articles_body_fts"

SLOW = ['"can help"', '"you can"', '"article about"', '"help wikipedia"',
        '"made longer"', '"short article"', '"archived from"']
FAST = ['"new york"', '"united kingdom"', '"census bureau"', '"dead link"',
        '"wayback machine"', '"permanent dead"']


def os_total_hits(session, query):
    payload = {"size": 0, "track_total_hits": True,
               "query": {"query_string": {"query": query, "default_field": "body",
                                          "default_operator": "OR"}}}
    r = session.post(f"{OS_URL}/{INDEX}/_search", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["hits"]["total"]["value"]


def time_os(session, query, limit, reps):
    payload = {"size": limit, "_source": False, "track_total_hits": False,
               "query": {"query_string": {"query": query, "default_field": "body",
                                          "default_operator": "OR"}}}
    for _ in range(3):
        session.post(f"{OS_URL}/{INDEX}/_search", json=payload, timeout=30)
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        r = session.post(f"{OS_URL}/{INDEX}/_search", json=payload, timeout=30)
        r.raise_for_status()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


def time_vs(session, query, limit, reps):
    url = f"{VS_URL}/api/v1/indexes/wiki/{VS_INDEX}/bm25"
    payload = {"query": query, "limit": limit}
    for _ in range(3):
        session.post(url, json=payload, timeout=30)
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        r = session.post(url, json=payload, timeout=30)
        r.raise_for_status()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


def main():
    session = requests.Session()
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 15

    print("=== match counts vs latency (limit=10) ===")
    print(f"{'query':24} {'matches':>10} {'OS ms':>8} {'VS ms':>8} {'ratio':>7}")
    rows = []
    for q in SLOW + FAST:
        total = os_total_hits(session, q)
        o = time_os(session, q, 10, reps)
        v = time_vs(session, q, 10, reps)
        rows.append({"query": q, "matches": total, "os_ms": o, "vs_ms": v})
        print(f"{q:24} {total:10} {o:8.2f} {v:8.2f} {v / o:7.2f}x")

    print()
    print("=== latency vs limit K (does pruning exist?) ===")
    probe = '"you can"'
    print(f"query {probe}")
    print(f"{'K':>6} {'OS ms':>8} {'VS ms':>8} {'ratio':>7}")
    ksweep = []
    for k in [1, 10, 100, 1000]:
        o = time_os(session, probe, k, reps)
        v = time_vs(session, probe, k, reps)
        ksweep.append({"k": k, "os_ms": o, "vs_ms": v})
        print(f"{k:6} {o:8.2f} {v:8.2f} {v / o:7.2f}x")

    out = {"match_counts": rows, "k_sweep": ksweep, "reps": reps}
    with open(sys.argv[2] if len(sys.argv) > 2 else "/tmp/probe.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
