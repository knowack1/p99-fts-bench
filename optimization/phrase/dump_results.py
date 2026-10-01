"""Dump the vector-store's top-K primary keys and scores, per query.

Pruning is only a valid optimization if it returns what the full walk would
have returned. Run this against the control build and against the patched
build, then `--diff` the two files: any query whose key set or score list
moved is a correctness bug, not a speedup.

Usage:
  python3 dump_results.py --queries q.json --output v0.json [--limit 10]
  python3 dump_results.py --diff v0.json v1.json
"""
import argparse
import json
import sys

sys.path.insert(0, "/home/karolnowacki/Projects/Scylla/p99/bench")

from ftsbench.engines import VectorStoreEngine  # noqa: E402

DEFAULT_VS_URL = "http://127.0.0.1:16080"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries")
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--vs-url", default=DEFAULT_VS_URL)
    parser.add_argument("--keyspace", default="wiki")
    parser.add_argument("--vs-index", default="articles_body_fts")
    parser.add_argument("--classes", nargs="*", default=None)
    parser.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    return parser.parse_args()


def dump(args: argparse.Namespace) -> int:
    engine = VectorStoreEngine(args.vs_url, keyspace=args.keyspace,
                               index=args.vs_index)
    with open(args.queries, encoding="utf-8") as f:
        query_set = json.load(f)
    names = args.classes if args.classes else list(query_set["classes"].keys())
    results = {}
    for name in names:
        for text in query_set["classes"][name]:
            keys = engine.search(text, args.limit)
            results[f"{name}\t{text}"] = [list(map(str, k)) for k in keys]
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump({"limit": args.limit, "results": results}, out, indent=1)
    print(f"dumped {len(results)} queries to {args.output}")
    return 0


def diff(before_path: str, after_path: str) -> int:
    before = json.load(open(before_path, encoding="utf-8"))["results"]
    after = json.load(open(after_path, encoding="utf-8"))["results"]
    shared = sorted(set(before) & set(after))
    identical_ordered = 0
    same_set_reordered = []
    changed_set = []
    for key in shared:
        b, a = before[key], after[key]
        if b == a:
            identical_ordered += 1
            continue
        bset = {tuple(x) for x in b}
        aset = {tuple(x) for x in a}
        if bset == aset:
            same_set_reordered.append(key)
        else:
            changed_set.append((key, len(bset & aset), len(bset)))
    print(f"queries compared          : {len(shared)}")
    print(f"identical (order included): {identical_ordered}")
    print(f"same set, different order : {len(same_set_reordered)}")
    print(f"different result set      : {len(changed_set)}")
    for key, overlap, total in changed_set[:15]:
        cls, text = key.split("\t", 1)
        print(f"   [{cls}] {text}  kept {overlap}/{total}")
    return 1 if changed_set else 0


def main() -> int:
    args = parse_args()
    if args.diff:
        return diff(args.diff[0], args.diff[1])
    if not args.queries or not args.output:
        raise SystemExit("--queries and --output are required unless --diff")
    return dump(args)


if __name__ == "__main__":
    sys.exit(main())
