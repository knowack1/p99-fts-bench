# charts — the images a local build-rate run ends in

Three renderers for `HARNESS-LOCAL-RUNBOOK.md`, reading the CSVs the binaries in
`../scylla` and `../opensearch` already write. Nothing here is a deck chart.

| Script | Question | X | Y |
|---|---|---|---|
| [`rate_vs_concurrency.py`](rate_vs_concurrency.py) | what did the client offer, and what did the engine (or the sink modelling one) index | concurrency (log2) | docs/s |
| [`rate_vs_offered.py`](rate_vs_offered.py) | at a rate the client was **told** to send, what did the engine accept and make searchable | offered docs/s (linear) | docs/s |
| [`rate_vs_index_size.py`](rate_vs_index_size.py) | what did one build do while it was happening | documents in the index | documents indexed per second |

**The first two read different CSVs and each refuses the other's.** A rate-ladder
row has one concurrency for every rung — its cap — so on the concurrency axis
every point would stack on one x and draw a plausible chart that is wrong; a
concurrency-ladder row has no offered rate to put on x at all. Each renderer
names the other in its refusal rather than guessing. A CSV written before the
rate columns existed is still a valid concurrency-ladder CSV and renders
unchanged.

```bash
.venv/bin/python3 build-rate/charts/rate_vs_concurrency.py \
    --scylla     "$R/scylla/points/scylla-rep*.csv" \
    --opensearch "$R/opensearch/points/os-b*-rep*.csv" \
    --output "$R/build-rate-vs-concurrency.png" --table "$R/build-rate-vs-concurrency.csv"

.venv/bin/python3 build-rate/charts/rate_vs_index_size.py \
    --scylla     "$R/scylla/samples/*/c32-*.csv" \
    --opensearch "$R/opensearch/samples/*/c32-b*-*.csv" \
    --output "$R/build-rate-vs-index-size.png" --table "$R/build-rate-vs-index-size.csv"
```

Every chart writes a `--table` twin, because the chart is for looking and the
table is for reading, and a plateau nobody can get the numbers out of is not
evidence.

## Why these are here and not in `../../tools`

`tools/plot_harness_grid.py` and `tools/plot_build_growth.py` are the
**null-sink** charts `../HARNESS-AWS-RUNBOOK.md` ends in: no engine runs
there, so the submit rate is the whole story and a series is named by its
harness alone. These two are the **engine** charts. They ask different questions
of the same columns and they would have had to grow flags that change what the
AWS runbook's images mean.

So they are separate scripts that **import** the originals rather than copy
them. `rate_vs_concurrency.py` takes its series naming from
`plot_harness_grid.series_of`, so a row lands on the same line in both charts.
`rate_vs_index_size.py` subclasses `plot_build_growth.Level` and reuses its
timeline, its riser floor, its grid and its drawing, so a build's rate means the
same thing on both. Neither file in `tools/` changed.

## What each adds over its sibling

**`rate_vs_concurrency.py` draws every configuration twice.** Solid with a
filled marker is `docs_per_s`, what the client got the engine to accept; dashed
with a hollow one is `index_docs_per_s`, what the engine made searchable — same
colour, because they are one configuration seen twice and the gap between them
is the chart. A blank `index_docs_per_s` is a level that ran unwatched and is
dropped, never read as a zero that would draw an engine indexing nothing.
`--submitted-only` gives back the sibling's single family.

**`rate_vs_offered.py` draws the diagonal as its reference.** `y = x` is
everything offered arriving and indexed; where a solid line leaves it is the rate
the engine stopped accepting everything, and where the dashed line leaves it is
where the *index* stopped keeping up. A hollow ring marks a saturated rung — and
a ringed point is read against `in_flight_peak` in the table twin before it is
read as an engine result, because a peak sitting at the `--concurrency` cap means
the harness was the limit and the point is void. Everything that is not the x
axis — series naming, repetitions, colours, the table twin — is imported from
`rate_vs_concurrency.py`, so an arm keeps its line and its colour across both.

**`--series 'LABEL=GLOB'` names a line by where its rows came from.** The
engine-flag naming reads a series off the row — engine, and batch size where
there is one — which is all the CSV carries. Arms that differ by an engine knob
(a writer buffer, a commit interval, a refresh interval) write identical rows
and would collapse onto one line. Repeatable; named series are drawn first in
the order given, ahead of anything `--scylla`/`--opensearch` collected, and a
label is never parsed for a batch size. `INDEX-RATE-MATRIX-PLAN.md` is the
campaign it exists for, and carries the full eight-arm command.

**`rate_vs_index_size.py` puts the engine on the series name.** Both halves
write `c32-1.csv` into their own samples directory, so globbed onto one axis
without a prefix the ScyllaDB build and the OpenSearch one group together and
are drawn as two repetitions of a line that is neither. Which builds appear is
chosen by the glob — `c32-*` takes the `c=32` slice of every ladder — because
one line per concurrency per batch stops being readable somewhere around a
dozen.

ScyllaDB keeps the pinned `#2b6cb0` and leads the order on **both** charts, so a
reader who learned that colour on one finds it on the other.

## Two ways a run goes quiet rather than wrong

- **A build too fast for its poll interval leaves under three readings** and
  `rate_vs_index_size.py` skips it by name in the footer. At `--index-interval
  1.0` a level that finishes in two seconds is gone. The runbook polls at
  `0.25 s` for this reason.
- **A level under three seconds is not a measurement**, and
  `rate_vs_concurrency.py` names every one of them in its footer. Raise
  `--max-docs` for those levels and re-run them rather than reading the shape
  they make.

## Tests

```bash
.venv/bin/python3 -m pytest build-rate/charts/tests -q
```

No endpoint and no engine needed. They pin the two silent failures above plus
the merges that would halve either chart without erroring.
