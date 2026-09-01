"""Render C1-C8 and assemble the results tree from one declarative table.

Eight `plot_c*` invocations and one `results_tree` invocation have to agree on
every glob, or a chart silently draws the wrong thing. Three of the defects this
campaign has already paid for came from a glob written twice and edited once:

- `data/c1-opensearch-*.jsonl` also matches every `c1-opensearch-refresh30-*`
  file, so the refresh=30s repetitions were folded into plain OpenSearch and
  compared against a mixture containing themselves;
- C7's artifacts are written as `.json`, not `.jsonl` (`C7_OS_OUT` in the
  Makefile), so the extension documented in `plot_c7`'s own docstring matches
  nothing at all — and a config that matches nothing is drawn as a labelled
  absence, which reads as a finding about the engine;
- C2 has no artifact of its own. It is the C1 series read for
  time-to-searchable, so it must name C1's files.

So the globs are built here, once, from the config name — never typed per chart.

    python3 -m ftsbench.render_results --run-name laptop-simplewiki-2026-08
    python3 -m ftsbench.render_results --run-name x --dry-run   # print, run nothing
"""
import argparse
import subprocess
import sys
from dataclasses import dataclass, field

OPENSEARCH = "opensearch"
OPENSEARCH_REFRESH30 = "opensearch-refresh30"
SCYLLA_BOOTSTRAP = "scylla-bootstrap"
SCYLLA_CDC = "scylla-cdc"

EVERY_CONFIG = (OPENSEARCH, OPENSEARCH_REFRESH30, SCYLLA_BOOTSTRAP, SCYLLA_CDC)
# C3 measures the write tail during ingest, which on the ScyllaDB side is the
# base-table write plus the CDC hop. The bootstrap path indexes an already-loaded
# table, so it has no ingest window to measure and no c3 artifact.
INGEST_CONFIGS = (OPENSEARCH, OPENSEARCH_REFRESH30, SCYLLA_CDC)

HEADLINE_CLASS = "rare_term"
DEFAULT_RESULTS_ROOT = "results"
DEFAULT_DATA_DIR = "data"
DEFAULT_FOOTER = ("laptop simulation values (bench/docker/.env) — not benchmark "
                  "tuning; these numbers are not quotable")
RESULTS_TREE = "ftsbench.results_tree"


@dataclass(frozen=True)
class Chart:
    chart_id: str
    module: str
    configs: tuple[str, ...]
    series_prefix: str
    extension: str = "jsonl"
    class_infix: str = ""
    manifest_prefix: str = "manifest"
    extra: tuple[str, ...] = field(default_factory=tuple)


CHARTS = (
    Chart("C1", "ftsbench.plot_c1", EVERY_CONFIG, "c1"),
    # C2 is the C1 series read for time-to-searchable, not a measurement of its own.
    Chart("C2", "ftsbench.plot_c2", EVERY_CONFIG, "c1"),
    Chart("C3", "ftsbench.plot_c3", INGEST_CONFIGS, "c3",
          manifest_prefix="manifest-c3", extra=("--bucket-s", "5")),
    Chart("C4", "ftsbench.plot_c4", EVERY_CONFIG, "c4"),
    Chart("C5", "ftsbench.plot_c5", EVERY_CONFIG, "c5",
          class_infix=HEADLINE_CLASS, extra=("--query-class", HEADLINE_CLASS)),
    Chart("C6", "ftsbench.plot_c6", EVERY_CONFIG, "c6"),
    # The sweep writes JSONL under a .json name; the extension is the file's, not
    # the format's.
    Chart("C7", "ftsbench.plot_c7", EVERY_CONFIG, "c7", extension="json",
          extra=("--sla-ms", "50")),
    Chart("C8", "ftsbench.plot_c8", EVERY_CONFIG, "c8"),
)


def rep_glob(config: str) -> str:
    """`opensearch-*` also matches `opensearch-refresh30-*`; the repetition
    number does not, so the plain configuration globs on digits."""
    return "[0-9]*" if config == OPENSEARCH else "*"


def artifact_glob(chart: Chart, config: str, data_dir: str) -> str:
    infix = f"-{chart.class_infix}" if chart.class_infix else ""
    return (f"{data_dir}/{chart.series_prefix}-{config}{infix}-"
            f"{rep_glob(config)}.{chart.extension}")


def manifest_glob(chart: Chart, config: str, data_dir: str) -> str:
    return f"{data_dir}/{chart.manifest_prefix}-{config}-{rep_glob(config)}.json"


def png_path(chart: Chart, results_root: str) -> str:
    return f"{results_root}/{chart.chart_id.lower()}.png"


def sidecar_path(chart: Chart, results_root: str) -> str:
    return f"{results_root}/{chart.chart_id.lower()}.json"


def config_flags(chart: Chart, data_dir: str) -> list[str]:
    flags = []
    for config in chart.configs:
        flags += ["--config", f"{config}:{artifact_glob(chart, config, data_dir)}"]
    return flags


def plot_command(chart: Chart, args: argparse.Namespace) -> list[str]:
    return [sys.executable, "-m", chart.module,
            *config_flags(chart, args.data_dir),
            "--output", png_path(chart, args.results_root),
            "--footer-extra", args.footer_extra,
            *chart.extra]


def chart_spec(chart: Chart, args: argparse.Namespace) -> str:
    artifacts = "|".join(artifact_glob(chart, config, args.data_dir)
                         for config in chart.configs)
    manifests = "|".join(manifest_glob(chart, config, args.data_dir)
                         for config in chart.configs)
    return (f"id={chart.chart_id},"
            f"sidecar={sidecar_path(chart, args.results_root)},"
            f"png={png_path(chart, args.results_root)},"
            f"artifacts={artifacts},manifests={manifests}")


def tree_command(args: argparse.Namespace) -> list[str]:
    specs = []
    for chart in CHARTS:
        specs += ["--chart", chart_spec(chart, args)]
    copy_raw = ["--copy-raw"] if args.copy_raw else []
    return [sys.executable, "-m", RESULTS_TREE, "--run-name", args.run_name,
            "--results-root", args.results_root, *specs, *copy_raw]


def commands(args: argparse.Namespace) -> list[list[str]]:
    return [plot_command(chart, args) for chart in CHARTS] + [tree_command(args)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-name", required=True,
                        help="results/<run-name>/ directory to assemble")
    parser.add_argument("--results-root", default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--footer-extra", default=DEFAULT_FOOTER)
    parser.add_argument("--copy-raw", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="copy the series and manifests each chart was drawn "
                             "from into <chart>/raw/")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands and run none of them")
    return parser.parse_args()


def run(command: list[str]) -> int:
    print("+ " + " ".join(command), file=sys.stderr)
    return subprocess.run(command).returncode


def render(args: argparse.Namespace) -> int:
    """A chart that failed to draw does not stop the rest: the tree is more
    useful with seven charts and a named failure than with none. The exit status
    still reports it."""
    failed = []
    for command in commands(args):
        if run(command) != 0:
            failed.append(command[command.index("-m") + 1])
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    args = parse_args()
    if args.dry_run:
        for command in commands(args):
            print(" ".join(command))
        return 0
    return render(args)


if __name__ == "__main__":
    sys.exit(main())
