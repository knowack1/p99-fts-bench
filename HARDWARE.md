# Benchmark hardware — what to provision, and why each box exists

This is the machine list for the run whose numbers may be **quoted on a
slide**. It supersedes nothing in `SIZING.md` — that document derives the
*size* of the engine boxes from measurement; this one derives the *count* and
the roles from the fairness rules in `p99-conf-fts-talk.md` §7, the framing
decision in `COMPARABILITY.md`, and the defects found in the preliminary
laptop run (`PROGRESS.md`, "Preliminary C1 run").

Nothing measured on the laptop is quotable. `docker/.env` says so in its own
header, and the preliminary run confirmed why: the measurement was bound by
the load generator, not by either engine.

## The list — 3 machines

Framing A (ScyllaDB+vector-store vs OpenSearch alone), single-node, RF=1.

| # | Role | Instance | Why this box exists |
|---|---|---|---|
| 1 | ScyllaDB + vector-store (colocated) | `im4gn.4xlarge` | 16 vCPU / 64 GiB / 7.5 TB NVMe. Sized in `SIZING.md`: ~46 GB working set for full enwiki (13 GB Tantivy structure + 26 GB writer/merge/allocator + 16 GB ScyllaDB + 4 GB OS), ~60 GB with headroom. |
| 2 | OpenSearch | `im4gn.4xlarge` | Identical type is the §7 hardware-parity commitment. Not sized independently — parity is the constraint, not OpenSearch's appetite. |
| 3 | Load generator + control node | `im4gn.4xlarge` | **The single most important addition.** See below. |

All three `linux/arm64`. Verified 2026-08-19 that every pinned image
publishes an arm64 manifest: `scylladb/scylla:2026.3.0-rc2`,
`scylladb/vector-store:1.10.0`, `opensearchproject/opensearch:3.8.0`.

### Why the load generator must be its own machine

The preliminary run's headline finding was that we measured the client, not
the engines:

- OpenSearch throughput rose 27% (7,709 -> 9,798 docs/s) purely from raising
  the loader's batch size 500 -> 2000. The engine's ceiling was never found.
- OpenSearch's `write` thread pool sat at 0.50-0.77 of 3 threads active with
  an **empty queue** throughout. The engine was starved.
- That starvation is also the most likely reason C1's claimed
  merge-sawtooth did not appear: merge pressure cannot show up in a
  throughput line that the client is capping.

Co-locating the loader with an engine would reproduce exactly that defect,
and worse — it would contend for the CPU under measurement, and the
contention would differ between the two sides. Charts C5/C6/C7 make this
non-negotiable: an open-loop, coordinated-omission-safe generator must have
CPU headroom to stay ahead of offered load, or the p99 numbers are the
client's queueing delay rather than the engine's.

This box also holds the prepared corpus and drives the runs, so it needs the
NVMe: ~40 GB of enwiki bz2 shards plus a ~35 GB `corpus.jsonl`.
`prepare_corpus.py` streams the bz2 shards, so there is no ~236 GB
decompressed intermediate.

### Placement

- **One region, one AZ, one cluster placement group.** Cross-AZ RTT is
  ~0.5-1 ms, which is the same order as the query latencies C5/C6/C7 are
  trying to separate. It would swamp the result.
- Nothing else running on any of the three boxes.

## Colocating vector-store with ScyllaDB is deliberate

`SIZING.md` sizes box 1 for both processes on one machine. Giving the
ScyllaDB side a separate vector-store box would hand it 32 vCPU against
OpenSearch's 16 and break hardware parity. Colocated, ScyllaDB and
vector-store contend for the same 16 vCPU while OpenSearch has 16 to itself
— that is the conservative choice, and it is the one to state on the
methodology slide when someone asks.

Per `CLAUDE.md`: this is still **two clusters**, colocated on one box for the
benchmark. It is not "one cluster", and the box count must never be used to
imply it is.

## Corpus: enwiki, not simplewiki

Recommended, for three reasons:

1. §7 already promises the audience "English Wikipedia snapshot — millions of
   articles".
2. simplewiki's entire OpenSearch build takes **30 seconds**. That is far too
   short for C1, C2, or C3 to have anything to show — it is part of why the
   merge sawtooth had no room to appear.
3. `SIZING.md`'s box sizing is already derived for enwiki.

**Open risk, cheapest to close first:** the 0.42x index/text ratio was
measured on simplewiki and has never been validated against enwiki text
(`SIZING.md` caveat 2). Run `tantivy-ram` over **one** enwiki shard on box 3
before committing to the run. If the extrapolated index lands above ~20 GB,
both engine boxes move to `im4gn.8xlarge` (32 vCPU / 128 GiB) — and they move
together, or parity breaks.

This matters more than a capacity note. Per `SIZING.md`, an undersized
vector-store **silently skips documents** and keeps answering queries, which
yields plausible-looking recall and latency numbers that are wrong. Every
load asserts index `count` == corpus doc count before any number is trusted.

## What it costs

On-demand list prices, `us-east-1`, checked 2026-08-19. They vary by region
(Frankfurt and Ireland typically run 5-15% higher) and they change — verify
before committing budget.

| Instance | vCPU | RAM | NVMe | On-demand | Spot | Spot reclaim |
|---|---|---|---|---|---|---|
| `im4gn.4xlarge` | 16 | 64 GiB | 7.5 TB | **$1.455/h** | $0.602 | 15-20% |
| `im4gn.8xlarge` | 32 | 128 GiB | 2 x 7.5 TB | $2.910/h | $1.121 | >20% |
| `i4i.4xlarge` (x86) | 16 | 128 GiB | 3.75 TB | $1.373/h | $0.657 | 15-20% |
| `i3en.3xlarge` (x86) | 12 | 96 GiB | 7.5 TB | $1.356/h | $0.543 | 5-10% |

**The three-box fleet is $4.37/hour — $105/day, $735/week.**

### How many hours the run needs

Scaled from the measured simplewiki build walls in `PROGRESS.md` by body-text
bytes: enwiki's ~31 GB against simplewiki's 0.422 GB is **73x**.

| Config | measured, simplewiki | linear-by-bytes, enwiki |
|---|---|---|
| OpenSearch | 30.2 s | ~37 min |
| ScyllaDB bootstrap scan | 120.3 s | ~2.5 h, plus the base-table load |
| ScyllaDB CDC tail | 52.2 s | ~1.1 h |

Linear is the central estimate, not a floor. OpenSearch merge cost grows worse
than linearly with corpus size; the laptop numbers were taken with 9 GB of swap
in use, which cuts the other way.

| Phase | Fleet hours | Cost |
|---|---|---|
| Bring-up, corpus download + prepare, ratio validation | 6-9 | $26-39 |
| C1/C2/C3/C4 ingest runs — N=5, three configs | ~26 | $114 |
| Reruns for failed gates (2 of 3 CDC reps truncated last time) | +14 | $61 |
| C5/C6/C7/C8 query runs — needs a built index resident | 12-18 | $52-79 |
| **Total measurement** | **~60-70** | **~$260-300** |

Call it **$300-500 including the debugging that always happens on unfamiliar
hardware.** That is the cost of the measurement, not the cost of the project.

Runs are serialized — box 3 drives one engine at a time, because a generator
splitting 16 vCPU across two targets reintroduces exactly the contention box 3
exists to remove. That leaves one engine box idle and billed through much of
the schedule, for about $38 total. Not worth trading methodology for.

### The number that actually decides the bill

**Write the harness on the laptop; rent AWS only to measure.** That was the
difference between roughly $400 and roughly $3,000, and it has now been
banked: all eight charts have a runnable target and all eight rendered from a
laptop campaign, so no fleet hour is spent writing code. Loader parity, the
open-loop generator and the C3/C4/C8 probes were all built and gate-tested
against simplewiki locally, for $0.

The same rule still applies to what is left. Every harness change the enwiki
run needs — a faster `prepare_corpus`, a coarser vector-store sampling window,
any new gate — is cheaper to write and test on simplewiki first. Bring the
boxes up to measure a harness that already works, not to debug one.

Second lever, worth ~3 h of the setup budget: `prepare_corpus.py` streams the
65 bz2 shards single-threaded, and decompressing ~236 GB at bz2 speeds takes
3-4 h. One shard per core across 16 cores is ~15 min.

### Stopping the boxes, and the trap

`im4gn` NVMe is **instance store**: stopping an instance destroys it, including
the ~35 GB prepared `corpus.jsonl`. Keep the corpus in S3 in the same region
(~75 GB, **$1.73/month**; S3-to-EC2 in-region transfer is free) and re-stage on
start. Then stopping between sessions is safe, and the standing cost is only
the EBS root volumes — 3 x 100 GB gp3, **$24/month**, billed while stopped.

Everything else rounds to zero: the Wikimedia download is inbound traffic
(free), the single-AZ rule means no cross-AZ transfer, and the results are
kilobytes.

Spot at 59% off is tempting and mostly wrong here. A reclaim mid-run voids a
2.5-hour bootstrap rep *and* the instance-store corpus, to save about $2. Use
on-demand for measurement; spot is fine for a download/prepare pass that
checkpoints to S3.

### Worth reconsidering: `i4i.4xlarge` is cheaper *and* larger

`i4i.4xlarge` costs **$1.373/h against im4gn's $1.455** and carries **128 GiB
against 64 GiB**. It wins on both axes that matter here, because the open
sizing risk in `SIZING.md` is RAM headroom — a ~46 GB working set on a 64 GiB
box, whose failure mode is `vector-store` silently skipping documents while
still answering queries. 128 GiB removes that risk outright, and with it the
"move both engine boxes to `im4gn.8xlarge`" branch at $7.28/h.

What it costs is §7's wording, which says ARM (Graviton). Hardware *parity* —
the actual fairness commitment — is untouched: both engines still get identical
boxes. Only the ARM detail changes, and the talk document's own checklist
already flags that line as needing an update. This is a call for the speakers,
not a change to make silently.

## Alternatives, and what they cost

| Option | Machines | $/hour | When to take it |
|---|---|---|---|
| Baseline: `im4gn.4xlarge` x3 | 3 | $4.37 | The recommendation above. |
| `i4i.4xlarge` x3 (x86, 128 GiB) | 3 | $4.12 | Worth taking on merit rather than only as a Graviton-capacity fallback — cheaper, and doubles the headroom the sizing risk is about. Costs the §7 ARM wording, not hardware parity. |
| `im4gn.8xlarge` engine boxes | 3 | $7.28 | If the enwiki ratio validation puts the index above ~20 GB and the fleet stays ARM. Both engine boxes move together, or parity breaks. |
| Framing B (stack comparison) | 4 | $5.82 | The `COMPARABILITY.md` open decision — a fourth box running ScyllaDB + CDC pipeline + OpenSearch. Isolates the actual thesis, but costs the box **and** the pipeline work. Framing A alone is defensible with the write/resource disclosure. |
| 3-node, RF=3 | 7 | $10.19 | Answers "nobody runs RF=1", but multi-node automation does not exist yet (`README.md`, "Not here yet"). Not recommended for an 18-minute talk. |

## What provisioning does *not* unblock

This section listed the harness as the blocker. It no longer is: every chart
C1-C8 has a runnable target, and all eight were rendered end to end from a
laptop campaign over simplewiki (`results/laptop-simplewiki-2026-08/`). What
provisioning buys is scale and isolation, not code.

Closed since that list was written:

- **Loader parity.** Both loaders now take an explicit in-flight depth and run
  at one shared value (`INGEST_CONCURRENCY ?= 8`, `BATCH_SIZE ?= 500`,
  `Makefile` "Ingest knobs"). The asymmetry that made the earlier ingest
  numbers incomparable — serial `_bulk` against 128-way concurrency — is gone,
  and the setting is published rather than implicit.
- **Open-loop generator** (C5, C7). `ftsbench/load_gen.py` dispatches from
  `pacer` on a fixed schedule into a worker pool, records `t_intended_s`
  alongside service time, and `tools/co_check.py` runs as a gate so a
  closed-loop regression fails the run instead of quietly producing service
  times labelled as latencies.
- **Ingest tail capture** (C3). `c3-os` / `c3-scylla-cdc` run the loader paced
  with per-operation latency logging.
- **Resource and freshness probes** (C4, C8). `ftsbench/resource_probe.py`
  (cgroup v2 anon RSS) and `ftsbench/freshness_probe.py`.

What is genuinely left, and what AWS is for:

- **Corpus scale.** simplewiki is 0.422 GB of body text against enwiki's
  ~31 GB. Every laptop number is disqualified by `docker/.env`'s own header,
  and the merge behaviour C1 is about is a function of corpus size.
- **Generator isolation.** Measured on this laptop, the generator's own ceiling
  is engine-specific — ~1,090 qps against OpenSearch against ~4,368 qps against
  ScyllaDB, a 4x asymmetry — so C7's knee is capped much harder on the
  OpenSearch side by the client than by the engine. Box 3 exists to remove
  exactly this, and until it does, C7 compares two client ceilings.
- **Memory headroom.** The laptop ran the engines under a 12 GiB cgroup cap
  with swap already in use. `SIZING.md`'s open risk — `vector-store` silently
  skipping documents at its memory limit while still answering queries — is not
  testable at simplewiki scale.

Two methodology items also remain open, and neither is unblocked by hardware:

- **Repetition count.** The laptop pass ran N=3 deliberately, to verify the
  tools rather than to produce quotable numbers. The AWS run's N is a separate
  decision — see `AWS-RUN-PLAN.md`, which prices it.
- **Vector-store progress granularity.** Its `count` endpoint advances in
  ~10,000-doc steps, so at 1 s sampling the ScyllaDB C1 line is partly an
  artifact of the sampling interval. Either sample at a multiple of the commit
  interval or state the smoothing window; do not pick whichever looks flatter.

## Access needed

- SSM Session Manager preferred over SSH keys; either works.
- Docker + compose v2, user in the `docker` group, `git`, `python3` + venv.
- No credentials in `bench/docker/.env` — it is tracked in git now.
