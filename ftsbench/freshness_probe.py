"""Write-to-searchable lag — the samples behind chart C8.

Writes a document carrying a random marker token, then polls a search for that
token every 50 ms until the document comes back, and records the gap:

    python3 -m ftsbench.freshness_probe --engine opensearch \\
        --refresh-interval 1s --reps 20 --output data/c8-opensearch-1.jsonl

    python3 -m ftsbench.freshness_probe --engine scylladb --path cdc \\
        --port 19042 --reps 20 --output data/c8-scylla-cdc-1.jsonl

Why it is built this way:

- **A marker token, not a doc-id lookup.** Searchability is the question, so the
  probe has to go through the same analyzer and index the benchmark's queries
  go through. A GET by id would answer a different, easier question.

- **50 ms polling, recorded in every record.** The resolution of a lag
  measurement is its poll interval: a 1 s poll cannot resolve a 1 s refresh
  interval, and a C8 chart whose artifact does not state the interval cannot be
  audited. `lag_s` is an upper bound — the document became searchable somewhere
  inside the last poll gap — and `poll_interval_s` is how wide that bound is.

- **The clock starts when the write is acknowledged**, not when it was sent, so
  the number answers "my write returned; how long until I can find it". The
  write's own duration is recorded as `write_ms`, so the other convention stays
  derivable from the artifact.

- **The searches are never forced to see the write.** OpenSearch indexing is
  issued without `?refresh`, because forcing a refresh would make the write
  searchable by construction and measure nothing. On the ScyllaDB side the query
  is the fixed M1 shape, unmodified, inherited from `engines.ScyllaEngine`:
  `SELECT ... WHERE BM25(col,'marker') > 0 ORDER BY BM25(col,'marker') LIMIT n`,
  LIMIT mandatory and <= 1000, no other WHERE clause.

- **A timeout is a result.** A probe that never becomes searchable is written out
  with `timed_out: true` and its poll/error counts. It is a finding about the
  configuration — the engine accepted a write it will not serve — not a run to
  discard, and dropping it would turn that into an unmeasured silence.

- **N reps with a jittered settle gap.** One sample says only where in the
  refresh cycle that one write happened to land. Repeated at an exact constant
  spacing, every write would land at the same phase of a fixed cycle, and the
  range C8 plots would be an artifact of the probe's own period rather than the
  engine's behaviour; `--jitter` breaks that phase lock, and `--seed` keeps it
  reproducible.

Two operational notes. The probe **leaves `--reps` extra documents behind**, so
run it after the corpus-count gate or expect the count to exceed the corpus by
that many — those gates exist because `vector-store/src/memory.rs` stops adding
documents at its memory limit while still answering queries (SIZING.md), and a
freshness run must not be what makes the count ambiguous. And the ScyllaDB side
of FTS is two clusters, ScyllaDB plus the vector-store holding the index: the
lag measured here is the built-in sync pipeline's, which is the honest
differentiator, not any claim about cluster count.
"""
import argparse
import random
import secrets
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from . import runmeta
from .engines import OpenSearchEngine, ScyllaEngine, add_connection_args
from .samplers import OpenSearchSampler, ScyllaSampler

MARKER_PREFIX = "ftsfresh"
MARKER_HEX_BYTES = 6
PROBE_PAGE_ID_BASE = -1_000_000
DEFAULT_POLL_INTERVAL_S = 0.05
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_REPS = 20
DEFAULT_SETTLE_S = 2.0
DEFAULT_JITTER_S = 1.0
DEFAULT_SEED = 20260819
DEFAULT_LIMIT = 10
MAX_M1_LIMIT = 1000
WRITE_TIMEOUT_S = 30
SETTINGS_TIMEOUT_S = 30


@dataclass(frozen=True)
class PollSettings:
    interval_s: float
    timeout_s: float


@dataclass(frozen=True)
class PollOutcome:
    t_searchable_s: float | None
    polls: int
    errors: int
    last_error: str | None


@dataclass(frozen=True)
class ProbeResult:
    i: int
    marker: str
    doc_key: str
    t_write_s: float
    write_ms: float
    outcome: PollOutcome


@dataclass(frozen=True)
class ProbeConfig:
    engine: str
    refresh_interval: str
    poll_interval_s: float
    origin_s: float


class FreshnessTarget(Protocol):
    def write_marker(self, marker: str, i: int) -> str:
        ...

    def is_searchable(self, marker: str, doc_key: str) -> bool:
        ...

    def close(self) -> None:
        ...


def new_marker(i: int) -> str:
    """A token that cannot occur in the corpus, and cannot be seeded.

    Unseeded entropy is deliberate: a reproducible marker would repeat across
    runs, and a leftover probe document from an earlier run would make the next
    run's write look searchable before it was written — the one failure mode
    that would silently produce a lag of zero.

    The shape matters too. Only `[a-z0-9]`, so neither analyzer's punctuation
    splitting can break it into pieces that match something else; and it always
    carries the rep index as a digit, so it can be no English word and
    therefore no stop word either.
    """
    return f"{MARKER_PREFIX}{i}{secrets.token_hex(MARKER_HEX_BYTES)}"


def probe_page_id(i: int) -> int:
    """Negative, so a probe document can never be confused with corpus content:
    Wikipedia page ids are positive."""
    return PROBE_PAGE_ID_BASE - i


def probe_document(marker: str, i: int) -> dict[str, Any]:
    return {"page_id": probe_page_id(i), "title": marker, "body": marker}


class OpenSearchFreshness:
    """Indexes without `?refresh` and searches through `engines.OpenSearchEngine`,
    so the read path is the same one the latency charts measure."""

    def __init__(self, url: str, index: str, field: str = "body",
                 default_operator: str = "OR", limit: int = DEFAULT_LIMIT) -> None:
        self._url = url.rstrip("/")
        self._index = index
        self._limit = limit
        self._session = requests.Session()
        self._engine = OpenSearchEngine(url, index, field=field,
                                        default_operator=default_operator)

    def write_marker(self, marker: str, i: int) -> str:
        response = self._session.put(
            f"{self._url}/{self._index}/_doc/{marker}",
            json=probe_document(marker, i), timeout=WRITE_TIMEOUT_S)
        response.raise_for_status()
        return marker

    def is_searchable(self, marker: str, doc_key: str) -> bool:
        return doc_key in self._engine.search(marker, limit=self._limit)

    def close(self) -> None:
        self._session.close()


class ScyllaFreshness(ScyllaEngine):
    """Writes the base-table row; searches with the inherited M1 query shape.

    Subclassed rather than reimplemented so the BM25 query is single-sourced:
    the M1 shape is fixed (LIMIT mandatory and <= 1000, no WHERE clause besides
    BM25) and a second copy of it here would be free to drift out of parity with
    the one every other read-path tool uses.
    """

    def __init__(self, hosts: list[str], port: int, keyspace: str, table: str,
                 column: str, limit: int = DEFAULT_LIMIT) -> None:
        super().__init__(hosts, port=port, keyspace=keyspace, table=table,
                         column=column)
        self._limit = limit
        self._insert = self._session.prepare(
            f"INSERT INTO {table} (article_id, page_id, title, body) "
            "VALUES (?, ?, ?, ?)")

    def write_marker(self, marker: str, i: int) -> str:
        document = probe_document(marker, i)
        article_id = uuid.uuid4()
        self._session.execute(self._insert, (article_id, document["page_id"],
                                            document["title"],
                                            document["body"]))
        return str(article_id)

    def is_searchable(self, marker: str, doc_key: str) -> bool:
        """The written row id must be among the hits, not merely some hit: the
        marker is unique, but requiring the identity closes the gap between
        "something matched" and "my write is visible"."""
        return doc_key in [str(row_id)
                           for row_id in self.search(marker, limit=self._limit)]

    def close(self) -> None:
        self._cluster.shutdown()


def searchable_or_error(target: FreshnessTarget, marker: str,
                        doc_key: str) -> tuple[bool, str | None]:
    """A failed poll means "not yet", not "abort": the vector-store answers 503
    until its index reaches SERVING, and probing across that transition is part
    of the measurement. The text is still kept, so a probe that timed out
    because every poll errored stays distinguishable from one that timed out
    because the write never appeared."""
    try:
        return target.is_searchable(marker, doc_key), None
    except Exception as err:
        return False, f"{type(err).__name__}: {err}"


def poll_until_searchable(target: FreshnessTarget, marker: str, doc_key: str,
                          poll: PollSettings) -> PollOutcome:
    deadline = time.perf_counter() + poll.timeout_s
    polls, errors, last_error = 0, 0, None
    while time.perf_counter() < deadline:
        found, error = searchable_or_error(target, marker, doc_key)
        polls += 1
        if error is not None:
            errors, last_error = errors + 1, error
        if found:
            return PollOutcome(time.perf_counter(), polls, errors, last_error)
        time.sleep(poll.interval_s)
    return PollOutcome(None, polls, errors, last_error)


def run_one_probe(target: FreshnessTarget, i: int,
                  poll: PollSettings) -> ProbeResult:
    marker = new_marker(i)
    sent_at = time.perf_counter()
    doc_key = target.write_marker(marker, i)
    acked_at = time.perf_counter()
    return ProbeResult(
        i=i, marker=marker, doc_key=doc_key, t_write_s=acked_at,
        write_ms=(acked_at - sent_at) * 1000.0,
        outcome=poll_until_searchable(target, marker, doc_key, poll))


def elapsed(absolute_s: float | None, origin_s: float) -> float | None:
    return None if absolute_s is None else round(absolute_s - origin_s, 6)


def lag_s(result: ProbeResult) -> float | None:
    searchable = result.outcome.t_searchable_s
    return None if searchable is None else round(searchable - result.t_write_s, 6)


def build_record(result: ProbeResult, config: ProbeConfig) -> dict[str, Any]:
    return {
        "record": "freshness_probe",
        "i": result.i,
        "marker": result.marker,
        "doc_key": result.doc_key,
        "t_write_s": elapsed(result.t_write_s, config.origin_s),
        "t_searchable_s": elapsed(result.outcome.t_searchable_s, config.origin_s),
        "lag_s": lag_s(result),
        "write_ms": round(result.write_ms, 3),
        "poll_interval_s": config.poll_interval_s,
        "polls": result.outcome.polls,
        "poll_errors": result.outcome.errors,
        "last_poll_error": result.outcome.last_error,
        "engine": config.engine,
        "refresh_interval": config.refresh_interval,
        "timed_out": result.outcome.t_searchable_s is None,
    }


def settle_gap_s(settle_s: float, jitter_s: float, rng: random.Random) -> float:
    """Jittered so successive writes do not all land at the same phase of a
    fixed refresh cycle, which would make C8's range the probe's period rather
    than the engine's."""
    return settle_s + rng.uniform(0.0, jitter_s)


def print_probe(record: dict[str, Any]) -> None:
    outcome = ("TIMED OUT" if record["timed_out"]
               else f"lag={record['lag_s']:.3f}s")
    print(f"probe {record['i']:>3}  {outcome}  polls={record['polls']}"
          f"  errors={record['poll_errors']}", file=sys.stderr)


def print_summary(records: list[dict[str, Any]]) -> None:
    lags = [record["lag_s"] for record in records if not record["timed_out"]]
    timeouts = sum(1 for record in records if record["timed_out"])
    if not lags:
        print(f"every one of {timeouts} probes timed out", file=sys.stderr)
        return
    print(f"n={len(lags)}  median={statistics.median(lags):.3f}s  "
          f"min={min(lags):.3f}s  max={max(lags):.3f}s  timeouts={timeouts}",
          file=sys.stderr)


def refresh_label(args: argparse.Namespace) -> str:
    """`refresh_interval` carries the OpenSearch setting, or the equivalent
    ScyllaDB ingest path — the two configurations C8 charts on that side are the
    bootstrap scan and the CDC tail."""
    return args.refresh_interval if args.engine == "opensearch" else args.path


def observed_refresh_interval(url: str, index: str) -> str | None:
    """Read back rather than trusted. C8's configurations differ only by this
    setting, so a mislabelled artifact is unfalsifiable from the file alone.
    None means the index never set it and OpenSearch's 1s default applies."""
    response = requests.get(f"{url.rstrip('/')}/{index}/_settings",
                            timeout=SETTINGS_TIMEOUT_S)
    response.raise_for_status()
    settings = response.json()[index]["settings"]["index"]
    return settings.get("refresh_interval")


def observed_or_none(args: argparse.Namespace) -> str | None:
    if args.engine != "opensearch":
        return None
    try:
        return observed_refresh_interval(args.url, args.index)
    except Exception as err:
        print(f"could not read refresh_interval back: {err}", file=sys.stderr)
        return None


def warn_on_refresh_mismatch(label: str, observed: str | None) -> None:
    if observed is not None and observed != label:
        print(f"WARNING: --refresh-interval {label!r} but the index reports "
              f"{observed!r}; the artifact would be mislabelled",
              file=sys.stderr)


def engine_version(args: argparse.Namespace) -> str:
    if args.engine == "opensearch":
        return OpenSearchSampler(args.url, args.index).version()
    return ScyllaSampler(args.vs_url, args.keyspace, args.vs_index).version()


def build_target(args: argparse.Namespace) -> FreshnessTarget:
    if args.engine == "opensearch":
        return OpenSearchFreshness(args.url, args.index,
                                   default_operator=args.default_operator,
                                   limit=args.limit)
    return ScyllaFreshness(args.hosts.split(","), args.port, args.keyspace,
                           args.table, args.column, limit=args.limit)


def require_m1_limit(limit: int) -> None:
    """M1 makes LIMIT mandatory and caps it at 1000; a probe that violates the
    fixed query shape is not measuring the engine the talk describes."""
    if not 1 <= limit <= MAX_M1_LIMIT:
        raise SystemExit(f"--limit must be between 1 and {MAX_M1_LIMIT}")


def build_header(args: argparse.Namespace, observed: str | None) -> dict[str, Any]:
    return runmeta.header(
        producer="freshness_probe", engine=args.engine,
        engine_version=engine_version(args), label=args.label,
        cache_state=args.cache_state, corpus=args.corpus,
        refresh_interval=refresh_label(args), refresh_interval_observed=observed,
        poll_interval_s=args.poll_interval, timeout_s=args.timeout,
        reps=args.reps, settle_s=args.settle, jitter_s=args.jitter,
        seed=args.seed, limit=args.limit, marker_prefix=MARKER_PREFIX,
        docs_added=args.reps,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("opensearch", "scylladb"), required=True)
    parser.add_argument("--output", required=True, help="JSONL path")
    parser.add_argument("--reps", type=int, default=DEFAULT_REPS)
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_S,
                        help="seconds between probes")
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER_S,
                        help="uniform random seconds added to the settle gap, so "
                             "probes do not phase-lock to the refresh cycle")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="seeds the settle jitter only; markers always use "
                             "fresh entropy")
    parser.add_argument("--poll-interval", type=float,
                        default=DEFAULT_POLL_INTERVAL_S,
                        help="bounds the resolution of lag_s; recorded per record")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="a probe that exceeds this is recorded as timed_out")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"search LIMIT (M1: mandatory, <= {MAX_M1_LIMIT})")
    parser.add_argument("--refresh-interval", default="unspecified",
                        help="OpenSearch refresh_interval this run is labelled with")
    parser.add_argument("--path", choices=("bootstrap", "cdc"), default="cdc",
                        help="ScyllaDB ingest path this run is labelled with")
    parser.add_argument("--label", default="", help="free-form run label")
    parser.add_argument("--cache-state", default="unspecified")
    parser.add_argument("--corpus", default="", help="corpus path, recorded")
    parser.add_argument("--vs-url", default="http://localhost:16080",
                        help="vector-store URL, for the recorded engine version")
    parser.add_argument("--vs-index", default="articles_body_fts")
    add_connection_args(parser)
    return parser.parse_args()


def run_probes(out: Any, args: argparse.Namespace, target: FreshnessTarget,
               config: ProbeConfig) -> list[dict[str, Any]]:
    rng = random.Random(args.seed)
    poll = PollSettings(interval_s=args.poll_interval, timeout_s=args.timeout)
    records = []
    for i in range(args.reps):
        record = build_record(run_one_probe(target, i, poll), config)
        runmeta.write_record(out, record)
        print_probe(record)
        records.append(record)
        if i + 1 < args.reps:
            time.sleep(settle_gap_s(args.settle, args.jitter, rng))
    return records


def probe_config(args: argparse.Namespace, origin_s: float) -> ProbeConfig:
    return ProbeConfig(engine=args.engine, refresh_interval=refresh_label(args),
                       poll_interval_s=args.poll_interval, origin_s=origin_s)


def main() -> int:
    args = parse_args()
    require_m1_limit(args.limit)
    observed = observed_or_none(args)
    warn_on_refresh_mismatch(refresh_label(args), observed)
    target = build_target(args)
    try:
        with open(args.output, "w", encoding="utf-8") as out:
            runmeta.write_record(out, build_header(args, observed))
            records = run_probes(out, args, target, probe_config(
                args, time.perf_counter()))
    finally:
        target.close()
    print_summary(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
