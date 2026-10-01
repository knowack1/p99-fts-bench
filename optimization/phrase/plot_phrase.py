"""Charts for the phrase optimization case study.

Two figures:
  k-sweep   — latency against LIMIT for OpenSearch / control / patched. The
              shape is the argument: flat means no pruning.
  per-class — p50 and p99 by query class, three arms.

Usage: python3 plot_phrase.py [outdir]
"""
import json
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
MEAS = HERE / "measurements"

OS_COLOR = "#0b6fa4"
V0_COLOR = "#b3402f"
V1_COLOR = "#1d7a4c"

FOOTER = ("simplewiki 270,269 docs · laptop-simulation sizing (docker/.env) · "
          "closed-loop, 20 iterations/query · relative comparison only, not "
          "quotable as absolute performance")


def k_sweep(outdir: pathlib.Path) -> None:
    v0 = json.load(open(MEAS / "v0-control-probe.json"))["k_sweep"]
    v1 = json.load(open(MEAS / "v1-prune-probe.json"))["k_sweep"]
    ks = [row["k"] for row in v0]

    fig, ax = plt.subplots(figsize=(9, 5.2))
    ax.plot(ks, [r["os_ms"] for r in v0], "o-", color=OS_COLOR, lw=2,
            label="OpenSearch (prunes: scales with K)")
    ax.plot(ks, [r["vs_ms"] for r in v0], "s-", color=V0_COLOR, lw=2,
            label="ScyllaDB FTS before (flat = no pruning)")
    ax.plot(ks, [r["vs_ms"] for r in v1], "^-", color=V1_COLOR, lw=2.4,
            label="ScyllaDB FTS after (pruning)")

    ax.set_xscale("log")
    ax.set_xticks(ks)
    ax.set_xticklabels([str(k) for k in ks])
    ax.set_xlabel("LIMIT (top-K requested)")
    ax.set_ylabel("median latency (ms)")
    ax.set_title('Sweeping K exposes the missing optimization\n'
                 'query: "you can" — 185,015 matching documents', loc="left")
    ax.grid(alpha=0.3, ls=":")
    ax.legend(frameon=False)
    ax.annotate("flat in K:\nsame work however\nlittle you ask for",
                xy=(10, v0[1]["vs_ms"]), xytext=(1.4, 22),
                color=V0_COLOR, fontsize=9,
                arrowprops=dict(arrowstyle="->", color=V0_COLOR, lw=1.2))
    fig.text(0.01, 0.015, FOOTER, fontsize=6.5, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out = outdir / "phrase-k-sweep.png"
    fig.savefig(out, dpi=170)
    print(f"wrote {out}")


def load_ab() -> dict:
    """Median per (variant, class, stat) across repetitions of the A/B ledger."""
    import csv
    import statistics
    rows = list(csv.DictReader(open(MEAS / "ab-results.csv")))
    grouped: dict = {}
    for r in rows:
        key = (r["variant"], r["class"])
        grouped.setdefault(key, {"p50_ms": [], "p99_ms": []})
        grouped[key]["p50_ms"].append(float(r["p50_ms"]))
        grouped[key]["p99_ms"].append(float(r["p99_ms"]))
    return {k: {s: statistics.median(v) for s, v in stats.items()}
            for k, stats in grouped.items()}


def per_class(outdir: pathlib.Path) -> None:
    ab = load_ab()
    arms = [
        ("OpenSearch", "opensearch", OS_COLOR),
        ("FTS before", "p99-v0-control", V0_COLOR),
        ("FTS after", "p99-v1-prune", V1_COLOR),
    ]
    classes = ["phrase", "common_term", "rare_term"]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    for ax, stat, title in zip(axes, ("p50_ms", "p99_ms"),
                               ("median (p50)", "tail (p99)")):
        width = 0.26
        for i, (label, variant, color) in enumerate(arms):
            xs = [x + (i - 1) * width for x in range(len(classes))]
            ys = [ab[(variant, c)][stat] for c in classes]
            bars = ax.bar(xs, ys, width, label=label, color=color)
            ax.bar_label(bars, fmt="%.1f", fontsize=7, padding=1)
        ax.set_xticks(range(len(classes)))
        ax.set_xticklabels(classes)
        ax.set_ylabel("ms")
        ax.set_title(title, loc="left")
        ax.grid(axis="y", alpha=0.3, ls=":")
        ax.set_axisbelow(True)
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle("Phrase pruning: 3.8x on the tail, other classes' p50 "
                 "untouched  (medians of N=3)", x=0.01, ha="left")
    fig.text(0.01, 0.015, FOOTER, fontsize=6.5, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    out = outdir / "phrase-per-class.png"
    fig.savefig(out, dpi=170)
    print(f"wrote {out}")


def main() -> int:
    outdir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "charts"
    outdir.mkdir(parents=True, exist_ok=True)
    k_sweep(outdir)
    per_class(outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
