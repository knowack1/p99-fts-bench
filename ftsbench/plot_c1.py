"""Render chart C1 — index build throughput (docs/s vs. wall time).

Consumes the JSONL time series written by ftsbench.build_monitor and draws one
line per config, plus a machine-readable sidecar JSON describing exactly what
was plotted.

    python3 -m ftsbench.plot_c1 \\
        --config opensearch:data/c1-opensearch-*.jsonl \\
        --config scylla-bootstrap:data/c1-scylla-bootstrap-*.jsonl \\
        --config scylla-cdc:data/c1-scylla-cdc-*.jsonl \\
        --output results/c1.png --footer-extra "repro: github.com/.../bench"

Three decisions this chart depends on:

- **One representative run per config, never an average of runs.** Repetitions
  differ in length and in *when* each merge lands; averaging them would smear
  the sawtooth into a smooth curve, destroying the finding. The median run by
  overall docs/s is plotted instead, and the spread across repetitions is
  reported in the sidecar so nothing is hidden.
- **Build window only** (ftsbench.build_report.build_window). Samples before
  the loader starts and after it finishes are idle by construction; drawn,
  they become flat zero tails that read as engine stalls.
- **Both engines are resampled onto one common window.** The vector-store's
  status ``count`` advances in quanta of roughly ten thousand documents, so at
  the 1 s sampling interval two thirds of ScyllaDB's samples report no progress
  and the rest report a whole quantum: unsmoothed, its line swings between zero
  and ~28,000 docs/s every second and reads as an unstable engine rather than as
  a reporting artifact. Resampling only ScyllaDB would repair the picture by
  giving the two engines different treatment, which is the asymmetry the
  fairness commitments exist to forbid — so both are resampled, the window is
  named in the subtitle and the sidecar, and each rate is computed from the
  *cumulative* document count over the window rather than by averaging the
  instantaneous samples. The raw 1 s trace stays as a faint underlay so nothing
  is hidden and the merge dips remain visible.
- **Merges are marked, not asserted.** Samples with ``merges_current > 0`` get
  their own markers, so a viewer can check that the dips coincide with Lucene
  merges instead of taking the claim on trust. Configs without merge counters
  (ScyllaDB) simply get no markers.

The loading, median selection, footer and sidecar are `ftsbench.plotlib`'s, the
same as C2-C8: the claim under test, the confidence tier and the AWS delta are
part of that document, and a chart whose write-up cannot state its own tier is
the one most likely to be quoted as if it had none.
"""
import argparse
import sys
from typing import Any

from . import plotlib
from .build_report import build_window, summarize

CHART = "C1"
RECORD = "sample"
DEFAULT_OUTPUT = "results/c1.png"
DEFAULT_TITLE = "Index build throughput"
DEFAULT_SUBTITLE = "docs/s over the build window"
METRIC_NAME = "docs_per_s_overall"
X_AXIS = "wall time since first indexed document (s)"
Y_AXIS = "docs/s"
DEFAULT_RESAMPLE_S = 5.0
RAW_TRACE_ALPHA = 0.22
RESAMPLE_NOTE = ("both engines resampled onto one common {seconds:g}s window, each "
                 "rate computed from the cumulative document count over that "
                 "window; the vector-store reports its count in ~10,000-document "
                 "quanta, so a 1s line would show a sampling artifact rather than "
                 "engine behaviour. The faint trace is the raw 1s sampling.")
RUN_SELECTION = ("median repetition by overall docs/s over the build window; "
                 "runs are never averaged, which would smooth the merge sawtooth")
WINDOW_NOTE = ("build window only (first document indexed .. last document added) "
               "— the idle samples either side would read as engine stalls")

MERGE_MARKER_COLOR = "#7b1d0f"

SIDECAR_SUMMARY_KEYS = (
    "docs_total", "build_wall_seconds", "samples_in_build", "docs_per_s_overall",
    "docs_per_s_median", "docs_per_s_p10", "docs_per_s_p90", "docs_per_s_max",
    "throughput_variability", "stall_fraction", "time_to_serving_s",
)
MERGE_KEYS = ("merges_total", "merges_total_docs", "merge_time_s",
              "merge_time_fraction_of_build", "samples_with_active_merge",
              "segments_final", "segments_max", "store_size_bytes_final")


def parse_args() -> argparse.Namespace:
    parser = plotlib.build_parser(CHART, __doc__, DEFAULT_OUTPUT, DEFAULT_TITLE,
                                  DEFAULT_SUBTITLE, disclose_write_path=True)
    parser.add_argument("--resample-window", type=float,
                        default=DEFAULT_RESAMPLE_S,
                        help="seconds per resampling bucket, applied to every "
                             "config equally (0 disables and plots raw samples)")
    return parser.parse_args()


def run_summary(run: plotlib.Run) -> dict[str, Any]:
    return summarize(run.header, run.records)


def unusable_reason(run: plotlib.Run) -> str:
    return str(run_summary(run).get("error", ""))


def run_docs_per_s(run: plotlib.Run) -> float:
    return float(run_summary(run).get("docs_per_s_overall") or 0.0)


def window_of(run: plotlib.Run) -> list[dict[str, Any]]:
    window, _ = build_window(run.records)
    return window


def series_points(window: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    origin = window[0].get("t_elapsed_s", 0.0)
    return ([sample.get("t_elapsed_s", 0.0) - origin for sample in window],
            [sample.get("docs_per_s", 0.0) for sample in window])


def resampled_points(window: list[dict[str, Any]],
                     seconds: float) -> tuple[list[float], list[float]]:
    origin = window[0].get("t_elapsed_s", 0.0)
    times, rates = [], []
    bucket_t = origin
    bucket_docs = window[0].get("docs_indexed") or 0
    for sample in window[1:]:
        now = sample.get("t_elapsed_s", 0.0)
        if now - bucket_t < seconds:
            continue
        docs = sample.get("docs_indexed") or 0
        times.append(now - origin)
        rates.append((docs - bucket_docs) / (now - bucket_t))
        bucket_t, bucket_docs = now, docs
    return times, rates


def merge_points(window: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    origin = window[0].get("t_elapsed_s", 0.0)
    active = [sample for sample in window if (sample.get("merges_current") or 0) > 0]
    return ([sample.get("t_elapsed_s", 0.0) - origin for sample in active],
            [sample.get("docs_per_s", 0.0) for sample in active])


def legend_label(config: plotlib.ConfigSeries) -> str:
    detail = (f"median of N={config.repetitions}" if config.repetitions > 1
              else "single run")
    return f"{config.name} — {config.chosen_metric:,.0f} docs/s overall ({detail})"


def draw_config(axes: Any, config: plotlib.ConfigSeries, index: int,
                resample_s: float) -> int:
    window = window_of(config.chosen)
    raw_times, raw_rates = series_points(window)
    style = plotlib.style_for(config.name, index)
    if resample_s > 0:
        axes.plot(raw_times, raw_rates, linewidth=0.8,
                  alpha=RAW_TRACE_ALPHA, **style)
        times, rates = resampled_points(window, resample_s)
    else:
        times, rates = raw_times, raw_rates
    axes.plot(times, rates, label=legend_label(config), linewidth=1.4, alpha=0.9,
              **style)

    merge_times, merge_rates = merge_points(window)
    if merge_times:
        axes.scatter(merge_times, merge_rates, s=26, marker="v", zorder=3,
                     facecolors="none", edgecolors=MERGE_MARKER_COLOR,
                     linewidths=1.0,
                     label=f"{config.name}: segment merge in progress "
                           f"({len(merge_times)} samples)")
    return len(merge_times)


def plot(args: argparse.Namespace,
         configs: list[plotlib.ConfigSeries]) -> tuple[Any, dict[str, Any]]:
    figure, axes = plotlib.plt.subplots(figsize=(args.width, args.height))
    marked = {config.name: draw_config(axes, config, index,
                                       args.resample_window)
              for index, config in enumerate(configs)}
    plotlib.frame(axes, args, X_AXIS, Y_AXIS)
    axes.set_xlim(left=0)
    axes.legend(loc="best", fontsize=8, framealpha=0.92)
    plotlib.title_and_subtitle(axes, args)
    return figure, extras(configs, marked, args.resample_window)


def merge_sidecar(summary: dict[str, Any], marked: int) -> dict[str, Any]:
    merges = {key: summary[key] for key in MERGE_KEYS if key in summary}
    if not merges:
        return {}
    return {"merges": {**merges, "merge_marked_samples": marked}}


def config_extras(config: plotlib.ConfigSeries, marked: int) -> dict[str, Any]:
    summary = run_summary(config.chosen)
    reported = {key: summary[key] for key in SIDECAR_SUMMARY_KEYS
                if summary.get(key) is not None}
    return {**reported, **merge_sidecar(summary, marked)}


def resample_note(seconds: float) -> str:
    if seconds <= 0:
        return "raw 1s samples, no resampling"
    return RESAMPLE_NOTE.format(seconds=seconds)


def extras(configs: list[plotlib.ConfigSeries], marked: dict[str, int],
           resample_s: float) -> dict[str, Any]:
    note = resample_note(resample_s)
    return {
        "metric_name": METRIC_NAME,
        "run_selection": RUN_SELECTION,
        "x_axis": X_AXIS,
        "y_axis": Y_AXIS,
        "window": WINDOW_NOTE,
        "resample_window_s": resample_s,
        "resampling": note,
        "notes": [WINDOW_NOTE, note],
        "per_config": {config.name: config_extras(config, marked[config.name])
                       for config in configs},
    }


def main() -> int:
    args = parse_args()
    return plotlib.emit(args, RECORD, run_docs_per_s, plot,
                        usable=unusable_reason)


if __name__ == "__main__":
    sys.exit(main())
