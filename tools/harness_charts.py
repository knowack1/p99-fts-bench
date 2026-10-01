"""What the harness charts share: reading their CSVs, and the drawing both do.

These are diagnostic charts off the Rust harnesses' CSV output, not deck charts
off run JSONL, which is why they sit here rather than on `ftsbench.plotlib` —
see the module header of `plot_harness_grid.py` for the palette half of that
argument. What they do share with each other is small and worth having in one
place: a reader that knows a harness CSV carries a `#` preamble, an ordered
colour ramp, direct labels at the right edge, the footer block, and a table
writer, because a chart nobody can get the numbers out of is not evidence.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

MARKERS = ["s", "o", "v", "^", "D", "P", "X", "*", "<", ">", "h"]
RAMP = "plasma"


def read_csv_rows(path: Path | str) -> list[dict]:
    """Rows of a harness CSV, without its `# key=value` preamble.

    The preamble precedes the column header, so dropping the `#` lines leaves
    the header exactly where `csv.DictReader` expects it.
    """
    with Path(path).open() as handle:
        return list(csv.DictReader(
            line for line in handle if not line.startswith("#")))


def read_preamble(path: Path | str) -> dict[str, str]:
    """The `# key=value` facts a harness writes above the column header.

    The rows alone do not say what a latency column MEANS -- whether a request
    is one document or 1,024 of them, and whether it was timed from when it was
    sent or from when it was due. Both live here, and a chart that puts two
    different answers on one axis needs to be able to read them to refuse.
    """
    facts: dict[str, str] = {}
    with Path(path).open() as handle:
        for line in handle:
            if not line.startswith("#"):
                break
            key, seam, value = line.lstrip("#").strip().partition("=")
            if seam:
                facts[key.strip()] = value.strip()
    return facts


def colours(count: int, ramp: str = RAMP) -> list:
    # matplotlib.colormaps rather than cm.get_cmap: the latter was removed in
    # 3.11, which is what bench/.venv carries.
    import matplotlib
    scale = matplotlib.colormaps[ramp]
    if count <= 1:
        return [scale(0.5)]
    return [scale(0.08 + 0.80 * i / (count - 1)) for i in range(count)]


def label_right_edge(axes: Any, ends: Sequence[tuple], log_y: bool = False) -> None:
    """Direct labels at each line's right end, nudged apart so they stay legible.

    Lines bunch up wherever the knob has stopped buying anything — which is
    usually the finding — so their end labels land on top of each other and
    render as a smear. Placing them in ascending order with a minimum vertical
    gap keeps every series readable without giving up the direct label, which
    is what carries identity when a ramp step cannot.
    """
    low, high = axes.get_ylim()
    scale = (lambda y: math.log10(y)) if log_y else (lambda y: y)
    unscale = (lambda y: 10.0 ** y) if log_y else (lambda y: y)
    gap = (scale(high) - scale(low)) * 0.045
    placed: list[float] = []
    for x, y, name, colour in sorted(ends, key=lambda end: end[1]):
        target = scale(y)
        for taken in placed:
            if abs(target - taken) < gap:
                target = taken + gap
        placed.append(target)
        axes.annotate(f" {name}", xy=(x, y), xytext=(x, unscale(target)),
                      color=colour, fontsize=8, va="center", ha="left",
                      annotation_clip=False,
                      arrowprops=None if abs(target - scale(y)) < gap / 2 else
                      dict(arrowstyle="-", color=colour, linewidth=0.5, alpha=0.6))


def draw_footer(figure: Any, lines: Sequence[str]) -> None:
    figure.text(0.02, 0.02, "\n".join(lines), fontsize=7, va="bottom", wrap=True)


def write_table(path: Path | str, header: Sequence[str],
                rows: Iterable[Sequence]) -> None:
    with Path(path).open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
