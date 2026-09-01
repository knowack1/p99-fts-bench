"""Shared plumbing for the C1-C8 chart modules.

``plot_c1.py`` predates this module and carries its own copies of the
config-spec parsing, median-run selection, footer and sidecar logic. Every
convention here is lifted from it unchanged, so a later commit can migrate
``plot_c1`` onto this module without altering a single rendered chart:

- the **median** repetition is plotted, never the mean: averaging repetitions
  smears a sawtooth into a slope and destroys the finding a chart exists for;
- the per-repetition spread goes into a sidecar JSON beside the PNG, so a
  reader can see whether the repetitions agreed;
- the footer names engine versions, cache state, N and the corpus;
- configs arrive as repeatable ``--config NAME:GLOB`` specs.

Two disclosures ride in the footer rather than in a slide footnote, per
COMPARABILITY.md and the header of ``docker/.env``: the write-path asymmetry,
and the fact that nothing measured on this host may be quoted.
"""
import argparse
import glob
import json
import math
import os
import statistics
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)
from matplotlib.figure import Figure  # noqa: E402

from .runmeta import read_jsonl  # noqa: E402
from .stats import is_stable, is_supported, min_samples_for, percentile  # noqa: E402

PRELIMINARY_STAMP = "PRELIMINARY — laptop, simplewiki, not quotable"
WRITE_PATH_DISCLOSURE = (
    "Write paths are not the same shape: the ScyllaDB side performs a durable "
    "base-table write plus a CDC hop plus an in-RAM Tantivy index build; the "
    "OpenSearch side performs a translog write plus segment build and merges "
    "(COMPARABILITY.md)."
)
SCYLLA_RATE_NOTE = (
    "ScyllaDB rates are overall only (docs / build wall time): the vector-store "
    "count advances in ~10k-document bursts, so its instantaneous and median "
    "rates are counter granularity, not engine behaviour (PROGRESS.md)."
)
MEDIAN_RUN_SELECTION = (
    "median repetition by the chart's own metric; repetitions are never "
    "averaged, which would smooth away the shape the chart exists to show"
)
CLAIM_UNASSESSED = (
    "not assessed automatically — read the numbers below and say whether they "
    "support or refute the claim"
)

QUERY_CLASSES = ("rare_term", "common_term", "phrase",
                 "bool_and", "bool_not", "bool_mixed")

CHART_CLAIMS = {
    "C1": "ScyllaDB holds a steady index-build rate; OpenSearch sawtooths as "
          "Lucene merges segments.",
    "C2": "One system reaches a fully searchable index sooner than the other.",
    "C3": "During ingest the JVM side shows periodic write-latency spikes at "
          "p99/p999 while the LSM side stays flat.",
    "C4": "ScyllaDB plus vector-store costs more RAM today because the Tantivy "
          "index is in RAM; the other resources are comparable.",
    "C5": "The two engines are close at p50 for the headline query and separate "
          "after p99, where the JVM tail bends upward.",
    "C6": "Per query class there is a winner, including classes OpenSearch wins "
          "(phrase is the likely candidate).",
    "C7": "Under a fixed p99 SLA each engine sustains a different maximum "
          "offered QPS, with a visible knee.",
    "C8": "CDC write-to-searchable freshness is seconds, comparable to "
          "OpenSearch, whose freshness depends on how refresh_interval was tuned.",
}

CONFIDENCE_TIERS = {
    "C1": "Tier 2 — shape informative, absolutes will move a lot "
          "(CPU- and loader-bound on this host)",
    "C2": "Tier 1 — ordering likely survives AWS (structural)",
    "C3": "Tier 3 — produce for layout, do not believe the numbers "
          "(tail measurement, swapping laptop, co-resident generator)",
    "C4": "Tier 1 — ordering likely survives AWS (design-governed)",
    "C5": "Tier 3 at p99.9/p99.99; Tier 2 below that",
    "C6": "Tier 1 at p50, Tier 2 at p95, Tier 3 at p99",
    "C7": "Tier 3 — the knee on this host may be the generator, not the engine",
    "C8": "Tier 1 — ordering likely survives AWS (governed by refresh "
          "interval and CDC design)",
}

AWS_DELTAS = {
    "C1": "16 dedicated vCPU and a saturating loader change the absolutes "
          "entirely; the merge-sawtooth hypothesis must be retested, not "
          "compared (PROGRESS.md records it as unconfirmed at this scale).",
    "C2": "Absolute times scale with corpus size and core count; the ordering "
          "is expected to hold.",
    "C3": "Regenerate from scratch rather than compare: co-resident generator, "
          "7 GB of swap in use and 2.5 GHz efficiency cores are this chart's "
          "p99 on a laptop.",
    "C4": "RAM and index size scale with the corpus (~0.42x source text for "
          "Tantivy, unvalidated on enwiki); CPU changes with core count.",
    "C5": "p99.9 and p99.99 must be re-measured on a dedicated generator box; "
          "only the p50/p90 shape is expected to carry over.",
    "C6": "Per-class ordering is expected to hold at p50; p95/p99 need "
          "dedicated hardware.",
    "C7": "Must be rerun with the generator off-box; a laptop knee is a "
          "generator ceiling until proven otherwise.",
    "C8": "Refresh-interval-governed numbers should carry over; CDC lag needs "
          "confirmation at enwiki scale and on N=5.",
}

CONFIG_STYLES = {
    "opensearch": {"color": "#c2432b", "linestyle": "-"},
    "opensearch-refresh3": {"color": "#c2432b", "linestyle": "-"},
    "opensearch-refresh30": {"color": "#dd8452", "linestyle": ":"},
    "scylla-bootstrap": {"color": "#2b6cb0", "linestyle": "-"},
    "scylla-cdc": {"color": "#2f855a", "linestyle": "--"},
    "scylladb": {"color": "#2b6cb0", "linestyle": "-"},
}
FALLBACK_COLORS = ["#6b46c1", "#b7791f", "#2c7a7b", "#97266d"]
ROLE_COLORS = {
    "opensearch": "#c2432b",
    "scylladb": "#2b6cb0",
    "vector-store": "#2f855a",
    "total": "#4a5568",
}
UNRESOLVED = math.inf


@dataclass(frozen=True)
class Run:
    """One repetition: its artifact path, its header, and its data records."""

    path: str
    header: dict[str, Any]
    records: list[dict[str, Any]]

    def field(self, name: str, default: Any = "") -> Any:
        value = self.header.get(name)
        return default if value in (None, "") else value

    @property
    def engine(self) -> str:
        return str(self.field("engine", "unknown"))

    @property
    def engine_version(self) -> str:
        return str(self.field("engine_version", "unknown"))

    @property
    def cache_state(self) -> str:
        return str(self.field("cache_state", "unspecified"))

    @property
    def corpus(self) -> str:
        return str(self.field("corpus", "unrecorded"))


@dataclass(frozen=True)
class ConfigSeries:
    """Every repetition of one config, plus which one is being drawn."""

    name: str
    runs: list[Run]
    metrics: list[float]
    chosen_index: int

    @property
    def chosen(self) -> Run:
        return self.runs[self.chosen_index]

    @property
    def repetitions(self) -> int:
        return len(self.runs)

    @property
    def paths(self) -> list[str]:
        return [run.path for run in self.runs]

    @property
    def chosen_metric(self) -> float:
        return self.metrics[self.chosen_index]


def parse_config_spec(spec: str) -> tuple[str, str]:
    name, separator, pattern = spec.partition(":")
    if not separator or not name.strip() or not pattern.strip():
        raise ValueError(f"--config expects NAME:GLOB, got {spec!r}")
    return name.strip(), pattern.strip()


def expand(pattern: str) -> list[str]:
    return sorted(glob.glob(os.path.expanduser(pattern)))


def load_runs(name: str, pattern: str, record: str) -> list[Run]:
    """Load every repetition of one config, dropping artifacts with no data."""
    runs: list[Run] = []
    for path in expand(pattern):
        header, records = read_jsonl(path)
        kept = [item for item in records if item.get("record") == record]
        if not kept:
            print(f"WARNING {name}: {path} holds no {record!r} records, skipping",
                  file=sys.stderr)
            continue
        runs.append(Run(path=path, header=header, records=kept))
    if not runs:
        print(f"WARNING {name}: no usable {record!r} artifact matched {pattern!r}, "
              f"skipping config", file=sys.stderr)
    return runs


def median_index(metrics: Sequence[float]) -> int:
    """Index of the median repetition.

    The lower-middle element is taken for even counts so the drawn series is
    always a real measured run rather than an interpolation between two.
    """
    order = sorted(range(len(metrics)), key=lambda i: metrics[i])
    return order[(len(order) - 1) // 2]


def keep_usable(name: str, runs: list[Run],
                usable: Callable[[Run], str]) -> list[Run]:
    """Drop repetitions a chart cannot measure, loudly.

    Holding the right record type is not the same as holding a measurable run:
    a C1 artifact whose loader never indexed a document has samples but no build
    window. Silently scoring such a run zero would make it a candidate for the
    median and could put a failed repetition on the chart.
    """
    kept = []
    for run in runs:
        reason = usable(run)
        if reason:
            print(f"WARNING {name}: skipping {run.path}: {reason}", file=sys.stderr)
            continue
        kept.append(run)
    if not kept:
        print(f"WARNING {name}: no measurable repetition left, skipping config",
              file=sys.stderr)
    return kept


UNRECORDED = (None, 0, "", "unrecorded")


def grouped_by_value(runs: Sequence[Run],
                     of: Callable[[Run], Any]) -> dict[Any, list[str]]:
    """Runs bucketed by one header value, ignoring the ones that never recorded
    it. An older artifact that predates a header field must not be read as
    disagreeing with a newer one."""
    groups: dict[Any, list[str]] = {}
    for run in runs:
        value = of(run)
        if value in UNRECORDED:
            continue
        groups.setdefault(value, []).append(run.path)
    return groups


def assert_one_measurement(name: str, runs: Sequence[Run]) -> None:
    """Every repetition of a config must have measured the same thing.

    The median of repetitions that indexed different corpora describes neither,
    and the sidecar reports only the chosen run's corpus, so the mixture is
    invisible in the output. This is not hypothetical: a `--smoke` pass writes
    20,000-document artifacts into the same `data/` directory, under the same
    names, as a 270,269-document campaign.
    """
    for field_name, of in (("corpus", lambda run: os.path.basename(run.corpus)),
                           ("max_docs", lambda run: run.header.get("max_docs"))):
        groups = grouped_by_value(runs, of)
        if len(groups) > 1:
            detail = "; ".join(f"{value} -> {', '.join(sorted(paths))}"
                               for value, paths in sorted(groups.items(), key=str))
            raise SystemExit(
                f"{name}: repetitions disagree on {field_name} ({detail}). "
                "They did not measure the same thing, so the median of them "
                "describes neither run — archive the odd ones out (a --smoke "
                "repetition is the usual cause) and plot again.")


def collect_configs(specs: list[str], record: str,
                    metric: Callable[[Run], float],
                    usable: Callable[[Run], str] | None = None) -> list[ConfigSeries]:
    configs: list[ConfigSeries] = []
    for spec in specs:
        name, pattern = parse_config_spec(spec)
        runs = load_runs(name, pattern, record)
        if usable is not None:
            runs = keep_usable(name, runs, usable)
        if not runs:
            continue
        assert_one_measurement(name, runs)
        metrics = [metric(run) for run in runs]
        configs.append(ConfigSeries(name=name, runs=runs, metrics=metrics,
                                    chosen_index=median_index(metrics)))
    return configs


def spread(values: Sequence[float]) -> dict[str, float]:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return {}
    return {"min": min(finite), "max": max(finite),
            "median": statistics.median(finite)}


def is_scylla(name: str) -> bool:
    return "scylla" in name.lower()


def style_for(name: str, index: int) -> dict[str, str]:
    if name in CONFIG_STYLES:
        return dict(CONFIG_STYLES[name])
    return {"color": FALLBACK_COLORS[index % len(FALLBACK_COLORS)],
            "linestyle": "-"}


def supported_percentiles(count: int, candidates: Sequence[float]) -> list[float]:
    """The candidates `count` samples can carry. `stats.is_supported` owns the
    floor; a chart's job is to omit the rest and say that it did."""
    return [pct for pct in candidates if is_supported(count, pct)]


def refusal_note(name: str, count: int, refused: Sequence[float]) -> str:
    needs = ", ".join(f"{percentile_label(pct)} needs {min_samples_for(pct):,}"
                      for pct in refused)
    return (f"{name}: {count:,} samples cannot support "
            f"{', '.join(percentile_label(pct) for pct in refused)} ({needs}) "
            f"— not drawn")


def percentiles(values: Sequence[float], pcts: Sequence[float]) -> dict[float, float]:
    ordered = sorted(values)
    return {pct: percentile(ordered, pct) for pct in pcts}


def percentile_label(pct: float) -> str:
    return f"p{pct:g}"


def latency_ms_values(records: Sequence[dict[str, Any]]) -> list[float]:
    """Only successful operations: a failed op has no latency to rank, and
    counting it as one would turn a failing engine into a fast one. The error
    count is reported separately by every chart that uses this."""
    return [float(item["latency_ms"]) for item in records
            if item.get("ok", True) and item.get("latency_ms") is not None]


def error_count(records: Sequence[dict[str, Any]]) -> int:
    return sum(1 for item in records if not item.get("ok", True))


def _add_io_args(parser: argparse.ArgumentParser, default_output: str) -> None:
    parser.add_argument("--config", action="append", required=True, metavar="NAME:GLOB",
                        help="config name and a glob of its repetition artifacts, "
                             "e.g. opensearch:data/c3-opensearch-*.jsonl (repeatable)")
    parser.add_argument("--output", default=default_output, help="PNG path")
    parser.add_argument("--sidecar", default="",
                        help="sidecar JSON path (default: --output with .json)")


def _add_text_args(parser: argparse.ArgumentParser, default_title: str,
                   default_subtitle: str) -> None:
    parser.add_argument("--title", default=default_title)
    parser.add_argument("--subtitle", default=default_subtitle)
    parser.add_argument("--footer-extra", default="",
                        help="appended to the provenance footer, e.g. the repo URL")


def _add_disclosure_args(parser: argparse.ArgumentParser,
                         disclose_write_path: bool) -> None:
    parser.add_argument("--no-preliminary-stamp", dest="stamp", action="store_false",
                        help="drop the PRELIMINARY stamp; only legitimate for a run "
                             "on benchmark hardware with published tuning")
    parser.add_argument("--write-path-disclosure", action=argparse.BooleanOptionalAction,
                        default=disclose_write_path,
                        help="state the ScyllaDB base-table + CDC write asymmetry in "
                             "the footer (required for C1-C4 by COMPARABILITY.md)")


def _add_figure_args(parser: argparse.ArgumentParser, width: float,
                     height: float) -> None:
    parser.add_argument("--width", type=float, default=width,
                        help="figure width, inches")
    parser.add_argument("--height", type=float, default=height,
                        help="figure height, inches")
    parser.add_argument("--dpi", type=int, default=160)


def build_parser(chart: str, description: str, default_output: str,
                 default_title: str, default_subtitle: str,
                 disclose_write_path: bool = False,
                 width: float = 11.0, height: float = 6.0) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_io_args(parser, default_output)
    _add_text_args(parser, default_title, default_subtitle)
    _add_disclosure_args(parser, disclose_write_path)
    _add_figure_args(parser, width, height)
    parser.set_defaults(chart=chart)
    return parser


def provenance_parts(configs: Sequence[ConfigSeries]) -> list[str]:
    parts = []
    for config in configs:
        run = config.chosen
        parts.append(f"{config.name}: {run.engine} {run.engine_version}, "
                     f"cache={run.cache_state}, N={config.repetitions}, "
                     f"corpus={os.path.basename(run.corpus)}")
    return parts


def footer_text(configs: Sequence[ConfigSeries], args: argparse.Namespace,
                notes: Sequence[str] = ()) -> str:
    lines = ["  |  ".join(provenance_parts(configs))]
    if args.write_path_disclosure:
        lines.append(WRITE_PATH_DISCLOSURE)
    lines.extend(notes)
    if args.footer_extra:
        lines.append(args.footer_extra)
    return "\n".join(line for line in lines if line)


def stamp(figure: Figure) -> None:
    figure.text(0.99, 0.985, PRELIMINARY_STAMP, fontsize=9.5, weight="bold",
                color="#8c1d13", ha="right", va="top",
                bbox={"boxstyle": "round,pad=0.35", "facecolor": "#fdecea",
                      "edgecolor": "#8c1d13", "linewidth": 0.8})


def frame(axes: Any, args: argparse.Namespace, xlabel: str, ylabel: str) -> None:
    axes.set_xlabel(xlabel)
    axes.set_ylabel(ylabel)
    axes.grid(True, which="major", alpha=0.25, linewidth=0.6)
    axes.set_axisbelow(True)
    title_and_subtitle(axes, args)


def title_and_subtitle(axes: Any, args: argparse.Namespace) -> None:
    axes.set_title(args.title, fontsize=14, loc="left", pad=18)
    axes.text(0.0, 1.02, args.subtitle, transform=axes.transAxes, fontsize=9,
              color="#444444")


def figure_title(figure: Figure, args: argparse.Namespace) -> None:
    figure.text(0.01, 0.965, args.title, fontsize=14, ha="left", va="top")
    figure.text(0.01, 0.925, args.subtitle, fontsize=9, color="#444444",
                ha="left", va="top")


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def sidecar_path(args: argparse.Namespace) -> str:
    return args.sidecar or f"{os.path.splitext(args.output)[0]}.json"


def config_sidecar(config: ConfigSeries, metric_name: str) -> dict[str, Any]:
    run = config.chosen
    record = {
        "chosen_file": run.path,
        "repetitions": config.repetitions,
        "repetition_files": config.paths,
        "metric": metric_name,
        "repetition_metric_values": config.metrics,
        "repetition_metric_spread": spread(config.metrics),
        "engine": run.engine,
        "engine_version": run.engine_version,
        "label": run.field("label", "UNRECORDED"),
        "cache_state": run.cache_state,
        "corpus": run.corpus,
        "max_docs": run.header.get("max_docs"),
        "git_commit": run.field("git_commit", "UNRECORDED"),
        "host": run.header.get("host", {}),
        "env": run.header.get("env", {}),
    }
    return record


def _document_head(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "chart": args.chart,
        "title": args.title,
        "subtitle": args.subtitle,
        "png": args.output,
        "command": " ".join(sys.argv),
        "claim": CHART_CLAIMS[args.chart],
        "claim_status": CLAIM_UNASSESSED,
        "confidence_tier": CONFIDENCE_TIERS[args.chart],
        "aws_delta": AWS_DELTAS[args.chart],
        "preliminary": bool(args.stamp),
        "preliminary_stamp": PRELIMINARY_STAMP if args.stamp else "",
        "write_path_disclosure": (WRITE_PATH_DISCLOSURE
                                  if args.write_path_disclosure else ""),
    }


def _document_configs(configs: Sequence[ConfigSeries], metric_name: str,
                      per_config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {config.name: {**config_sidecar(config, metric_name),
                          **per_config.get(config.name, {})}
            for config in configs}


def sidecar_document(args: argparse.Namespace, configs: Sequence[ConfigSeries],
                     extras: dict[str, Any]) -> dict[str, Any]:
    body = dict(extras)
    metric_name = body.pop("metric_name", "unnamed")
    per_config = body.pop("per_config", {})
    document = _document_head(args)
    document["run_selection"] = body.pop("run_selection", MEDIAN_RUN_SELECTION)
    document["chart_notes"] = list(body.pop("notes", []))
    document.update(body)
    document["configs"] = _document_configs(configs, metric_name, per_config)
    return document


def write_sidecar(path: str, document: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as out:
        json.dump(document, out, indent=2, ensure_ascii=False, default=str)
        out.write("\n")


FOOTER_FONT_SIZE = 7
FOOTER_LINE_INCHES = 0.135
CHARS_PER_INCH = 16


def wrap_footer(text: str, width_inches: float) -> str:
    """A footer that runs off the canvas discloses nothing, so every line is
    wrapped to the figure width rather than clipped by it."""
    limit = max(60, int(width_inches * CHARS_PER_INCH))
    return "\n".join(textwrap.fill(line, limit) for line in text.split("\n"))


def footer_reserve(footer: str, height_inches: float) -> float:
    lines = footer.count("\n") + 1
    return min(0.45, (lines * FOOTER_LINE_INCHES + 0.12) / height_inches)


DEFAULT_LAYOUT_TOP = 0.955


def data_artist_count(figure: Figure) -> int:
    """Drawn data across every axes: lines, filled areas, and bars."""
    return sum(len(axes.lines) + len(axes.collections) + len(axes.patches)
               for axes in figure.axes)


def assert_something_was_drawn(figure: Figure, output: str) -> None:
    """A chart whose axes are empty is a finding about the harness, and it must
    not be written.

    C3's laptop png was axes, a title, an honest footer, and a legend reading
    "0/28 buckets" six times. Every individual refusal was correct; nothing
    refused the *chart*, so it sat in the curated results tree for four days
    being referenced by a slide."""
    if data_artist_count(figure) == 0:
        raise SystemExit(
            f"refusing to write {output}: nothing was drawn on it. Every series "
            "was empty or every point was refused for want of samples — fix the "
            "measurement, not the chart")


def finish_figure(figure: Figure, args: argparse.Namespace,
                  configs: Sequence[ConfigSeries], notes: Sequence[str] = (),
                  layout_top: float = DEFAULT_LAYOUT_TOP) -> None:
    footer = wrap_footer(footer_text(configs, args, notes), args.width)
    figure.tight_layout(rect=(0, footer_reserve(footer, args.height), 1, layout_top))
    figure.text(0.01, 0.008, footer, fontsize=FOOTER_FONT_SIZE, color="#555555",
                va="bottom", ha="left")
    if args.stamp:
        stamp(figure)
    assert_something_was_drawn(figure, args.output)
    figure.savefig(args.output, dpi=args.dpi)
    plt.close(figure)


def print_summary(configs: Sequence[ConfigSeries], metric_name: str) -> None:
    for config in configs:
        print(f"{config.name:<22} N={config.repetitions}  "
              f"median run={config.chosen.path}  "
              f"{metric_name}={config.chosen_metric:,.3f}", file=sys.stderr)


Plotter = Callable[[argparse.Namespace, list[ConfigSeries]],
                   tuple[Figure, dict[str, Any]]]


def emit(args: argparse.Namespace, record: str, metric: Callable[[Run], float],
         plot: Plotter, usable: Callable[[Run], str] | None = None) -> int:
    """Load, choose the median repetition, render, and write the sidecar.

    `usable` returns an empty string for a repetition this chart can measure and
    a reason to skip it otherwise — see keep_usable.
    """
    try:
        configs = collect_configs(args.config, record, metric, usable)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if not configs:
        print("error: no config produced usable data; nothing to plot", file=sys.stderr)
        return 1
    ensure_parent_dir(args.output)
    figure, extras = plot(args, configs)
    finish_figure(figure, args, configs, extras.get("notes", ()),
                  extras.pop("layout_top", DEFAULT_LAYOUT_TOP))
    write_sidecar(sidecar_path(args), sidecar_document(args, configs, extras))
    print_summary(configs, extras.get("metric_name", "metric"))
    print(f"wrote {args.output} and {sidecar_path(args)}", file=sys.stderr)
    return 0
