"""Move one campaign's artifacts aside before those repetitions are re-run.

Scoped to the configurations being re-run, because a campaign-wide sweep also
archives the repetitions that are being kept: re-running only the OpenSearch
configurations must not touch fifteen ScyllaDB artifacts that took two hours to
produce and are still valid.

Filenames are parsed, not globbed. `data/c5-opensearch-*` also matches
`c5-opensearch-refresh30-rare_term-1.jsonl`, and that exact class of mistake has
already cost this harness one chart — the refresh=30s repetitions were summarized
twice, once under each configuration. Configurations are matched longest name
first, so `opensearch-refresh30` can never fall through to `opensearch`.

    python3 -m ftsbench.archive_artifacts \\
        --dest data/superseded/20260820T090000Z \\
        --config opensearch --config opensearch-refresh30

`--rep` narrows it further, to the repetitions this invocation is about to
rewrite. Re-running scylla-cdc repetitions 1 and 3 must leave 2, 4 and 5 alone:
their query and freshness logs are valid measurements and nothing is going to
overwrite them.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Sequence

# C3 writes a manifest of its own (Makefile C3_OS_MANIFEST), so `manifest` on
# its own leaves `manifest-c3-<config>-<rep>.json` belonging to no
# configuration: a re-run repetition left its C3 gate records behind, and
# results_tree globs them as part of the fresh run.
CHART_PREFIXES = (tuple(f"c{number}" for number in range(1, 9))
                  + ("manifest-c3", "manifest"))
LOG_DIR = "campaign-logs"

# Every configuration the campaign can produce, not just the ones being archived.
# A filename is parsed against all of them and only then filtered, because
# `c1-opensearch-refresh30-1.jsonl` begins with `c1-opensearch-` and asking only
# about `opensearch` would otherwise claim it. Kept in step with
# run_manifest.CONFIGS by test_archive_artifacts.
KNOWN_CONFIGS = ("opensearch", "opensearch-refresh3", "opensearch-refresh30",
                 "scylla-bootstrap", "scylla-cdc")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data")
    parser.add_argument("--dest", required=True)
    parser.add_argument("--config", action="append", default=[], required=True,
                        help="repeatable; only these configurations are moved")
    parser.add_argument("--rep", action="append", default=[], type=int,
                        help="repeatable; only these repetitions are moved "
                             "(default: every repetition of each config)")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def by_longest_name(configs: Sequence[str]) -> list[str]:
    return sorted(configs, key=len, reverse=True)


def artifact_config(filename: str,
                    configs: Sequence[str] = KNOWN_CONFIGS) -> str | None:
    """`c4-opensearch-refresh30-1.jsonl` belongs to opensearch-refresh30."""
    for config in by_longest_name(configs):
        for prefix in CHART_PREFIXES:
            if filename.startswith(f"{prefix}-{config}-"):
                return config
    return None


def log_config(filename: str,
               configs: Sequence[str] = KNOWN_CONFIGS) -> str | None:
    """A repetition log is named `<config>-<rep>.log` with no chart prefix."""
    if not filename.endswith(".log"):
        return None
    for config in by_longest_name(configs):
        if filename.startswith(f"{config}-"):
            return config
    return None


REP_SUFFIX = re.compile(r"-(\d+)\.[a-z]+$")


def artifact_rep(filename: str) -> int | None:
    """The repetition an artifact belongs to, or None if the name has none.

    Every campaign artifact ends `-<rep>.<ext>`, including the class-split query
    logs (`c5-scylla-cdc-bool_and-2.jsonl`) and the repetition logs.
    """
    found = REP_SUFFIX.search(filename)
    return int(found.group(1)) if found else None


def assert_known(configs: Sequence[str]) -> None:
    """A misspelled configuration would archive nothing and report success,
    which reads exactly like a directory that was already clean."""
    unknown = sorted(set(configs) - set(KNOWN_CONFIGS))
    if unknown:
        raise SystemExit(f"unknown configuration(s): {', '.join(unknown)}; "
                         f"known: {', '.join(KNOWN_CONFIGS)}")


def files_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def in_scope(filename: str, config: str | None, wanted: set[str],
             reps: set[int]) -> bool:
    """An artifact with no parseable repetition is aggregate — a chart built
    from the whole config — so it is in scope whenever its config is: the
    re-run will rewrite it either way."""
    if config not in wanted:
        return False
    rep = artifact_rep(filename)
    return not reps or rep is None or rep in reps


def selected(source: Path, configs: Sequence[str],
             reps: Sequence[int] = ()) -> list[Path]:
    assert_known(configs)
    wanted, want_reps = set(configs), set(reps)
    return ([path for path in files_in(source)
             if in_scope(path.name, artifact_config(path.name), wanted, want_reps)]
            + [path for path in files_in(source / LOG_DIR)
               if in_scope(path.name, log_config(path.name), wanted, want_reps)])


def destination_for(path: Path, source: Path, dest: Path) -> Path:
    return dest / path.relative_to(source)


def move_all(paths: Sequence[Path], source: Path, dest: Path) -> None:
    for path in paths:
        target = destination_for(path, source, dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))


def main() -> int:
    args = parse_args()
    source, dest = Path(args.source), Path(args.dest)
    paths = selected(source, args.config, args.rep)
    verb = "would move" if args.dry_run else "moving"
    scope = (f"repetition(s) {', '.join(str(rep) for rep in sorted(args.rep))} of "
             if args.rep else "")
    print(f"{verb} {len(paths)} artifact(s) of {scope}"
          f"{', '.join(sorted(args.config))} to {dest}", file=sys.stderr)
    for path in paths:
        print(f"  {path}", file=sys.stderr)
    if not args.dry_run:
        move_all(paths, source, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
