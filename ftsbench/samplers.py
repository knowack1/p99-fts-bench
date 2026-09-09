"""Engine pollers for index-build progress (chart C1).

Each sampler exposes ``sample()`` returning a flat dict of counters read from
the engine at that instant. The monitor turns consecutive samples into
instantaneous rates, so samplers must return monotonically-increasing
counters, never rates.

OpenSearch progress is read from ``indexing.index_total`` rather than
``_count`` because ``_count`` only sees refreshed documents: with
``refresh_interval`` tuned up (or disabled during load) it would report a
flat line while indexing is in fact proceeding.

The OpenSearch ``write`` thread pool is read beside that progress because it
is the only reading that tells a starved engine from a saturated one, and that
distinction founded this campaign: at ``--batch-size`` 500/1000/2000 the pool
sat at 0.77/0.75/0.50 active of 3 with a permanently empty queue while
throughput rose 27%, so the published figure was the loader's ceiling and not
the engine's (`TUNING.md` section 3). ``write_active``, ``write_queue`` and
``write_pool_size`` are instantaneous levels rather than counters -- the
monitor derives rates from ``docs_indexed`` alone -- and every write-pool
field is ``null``, never 0, when the endpoint cannot be read: an idle pool
beside an empty queue is exactly what a starved engine looks like, so a
synthesised 0 would read as the finding itself.

That reading is **opt-in**, because it is a second node-scoped request to the
system under test on every tick and it costs the producers that never asked
for it. ``build_monitor`` (C1) is the series the fields exist for and asks;
``resource_probe`` (C4) calls ``sample()`` for ``store_size_bytes`` alone and
does not. Unread, the fields are ABSENT from the sample rather than null --
every consumer reads them through ``.get()``, and a null would claim the
endpoint was read. And a pool endpoint that fails is not asked again for the
life of the sampler -- per endpoint, since the levels and the configured pool
size come from two that fail independently: at ``POOL_STATS_TIMEOUT_S``
against a 1 s interval, retrying every tick stretches the cadence to 6 s,
which is the cadence ``build_report.summarize`` computes
``docs_per_s_median``, p10, p90, ``throughput_variability`` and
``stall_fraction`` from.
"""
import sys

import requests

STATS_TIMEOUT_S = 30
# The pool reading annotates the series, so it must not be able to stretch the
# sampling interval it is annotating: a node-stats endpoint that hangs for the
# 30s budget above would cost the chart its resolution exactly where a stall
# is interesting.
POOL_STATS_TIMEOUT_S = 5

WRITE_POOL_FIELDS = ("write_active", "write_queue", "write_rejected",
                     "write_pool_size")


def unavailable_write_pool() -> dict:
    return dict.fromkeys(WRITE_POOL_FIELDS)


def write_pools(stats: dict) -> list[dict]:
    nodes = stats.get("nodes") or {}
    pools = ((node.get("thread_pool") or {}).get("write")
             for node in nodes.values() if isinstance(node, dict))
    return [pool for pool in pools if isinstance(pool, dict)]


def summed(pools: list[dict], key: str) -> int | None:
    """Cluster-wide, matching the index figures beside it: those come from
    ``_all.total``, and the campaign's cluster is one node either way."""
    values = [pool[key] for pool in pools if pool.get(key) is not None]
    return sum(values) if values else None


class OpenSearchSampler:
    engine = "opensearch"

    def __init__(self, url: str, index: str,
                 read_write_pool: bool = False) -> None:
        self._url = url.rstrip("/")
        self._index = index
        self._session = requests.Session()
        self._read_write_pool = read_write_pool
        self._unreadable: set[str] = set()
        self._write_pool_size: int | None = None
        self._warned: set[str] = set()

    def sample(self) -> dict:
        response = self._session.get(
            f"{self._url}/{self._index}/_stats", timeout=STATS_TIMEOUT_S
        )
        response.raise_for_status()
        total = response.json()["_all"]["total"]
        merges = total["merges"]
        segments = total["segments"]
        return {
            "docs_indexed": total["indexing"]["index_total"],
            "docs_searchable": total["docs"]["count"],
            "segments_count": segments["count"],
            "segments_memory_bytes": segments.get("memory_in_bytes", 0),
            "merges_current": merges["current"],
            "merges_current_docs": merges["current_docs"],
            "merges_total": merges["total"],
            "merges_total_docs": merges["total_docs"],
            "merges_total_time_ms": merges["total_time_in_millis"],
            "refresh_total": total["refresh"]["total"],
            "refresh_total_time_ms": total["refresh"]["total_time_in_millis"],
            "store_size_bytes": total["store"]["size_in_bytes"],
            **self.write_pool_fields(),
            "index_status": "n/a",
        }

    def write_pool_fields(self) -> dict:
        """Nothing at all for a producer that did not ask: no request to the
        engine under measurement, and no columns claiming it was read."""
        return self.write_pool() if self._read_write_pool else {}

    def write_pool(self) -> dict:
        stats = self.optional_json("/_nodes/stats/thread_pool",
                                   "write thread pool stats")
        pools = write_pools(stats) if stats else []
        if not pools:
            return unavailable_write_pool()
        return {
            "write_active": summed(pools, "active"),
            "write_queue": summed(pools, "queue"),
            "write_rejected": summed(pools, "rejected"),
            "write_pool_size": self.write_pool_size(),
        }

    def write_pool_size(self) -> int | None:
        """The configured ``thread_pool.write.size``, which is the denominator
        the founding finding counted active threads against.

        The live ``threads`` count in the stats response is not that number: a
        fixed pool starts threads on demand, so a client too slow to fill the
        pool also holds ``threads`` below the configured size -- and a starved
        cell would then be divided by its own symptom.
        """
        if self._write_pool_size is None:
            info = self.optional_json("/_nodes/thread_pool",
                                      "write thread pool size")
            self._write_pool_size = (summed(write_pools(info), "size")
                                     if info else None)
        return self._write_pool_size

    def optional_json(self, path: str, what: str) -> dict | None:
        """One unresponsive endpoint costs one timeout, not one per tick.

        The warning was already once per sampler; the request was not, so a
        node that had stopped answering paid ``POOL_STATS_TIMEOUT_S`` on every
        sample and stretched the very interval this reading annotates. Latched
        per path rather than per sampler: the levels and the configured size
        come from two endpoints that fail independently, and reporting null
        levels because the size endpoint died would draw the starved-engine
        picture out of an endpoint that is still answering.
        """
        if path in self._unreadable:
            return None
        try:
            response = self._session.get(f"{self._url}{path}",
                                         timeout=POOL_STATS_TIMEOUT_S)
            response.raise_for_status()
            return response.json()
        except Exception as err:
            self._unreadable.add(path)
            self.warn_once(what, err)
            return None

    def warn_once(self, what: str, err: Exception) -> None:
        if what in self._warned:
            return
        self._warned.add(what)
        print(f"WARNING: {what} unavailable ({err}): those write-pool columns "
              "record null rather than a level for the rest of this run, and "
              "the endpoint is not asked again", file=sys.stderr)

    def version(self) -> str:
        try:
            response = self._session.get(self._url, timeout=STATS_TIMEOUT_S)
            response.raise_for_status()
            return response.json()["version"]["number"]
        except Exception:
            return "unknown"


class ScyllaSampler:
    """Polls the vector-store index status endpoint.

    ``count`` is the number of documents present in the Tantivy index, which
    is what "index build progress" means on the ScyllaDB side: rows land in
    the base table first, then the vector-store either bootstrap-scans the
    table or tails CDC to catch up. ``status`` is SERVING once the index is
    queryable.
    """

    engine = "scylladb"

    def __init__(self, vs_url: str, keyspace: str, index: str):
        self._url = vs_url.rstrip("/")
        self._keyspace = keyspace
        self._index = index
        self._session = requests.Session()

    def sample(self) -> dict:
        response = self._session.get(
            f"{self._url}/api/v1/indexes/{self._keyspace}/{self._index}/status",
            timeout=STATS_TIMEOUT_S,
        )
        response.raise_for_status()
        body = response.json()
        return {
            "docs_indexed": body.get("count", 0),
            "docs_searchable": body.get("count", 0),
            "index_status": body.get("status", "unknown"),
        }

    def version(self) -> str:
        try:
            response = self._session.get(
                f"{self._url}/api/v1/info", timeout=STATS_TIMEOUT_S
            )
            response.raise_for_status()
            body = response.json()
            return str(body.get("version", body))
        except Exception:
            return "unknown"


def build_sampler(args):
    """`build_monitor`'s own factory, and the one producer that wants the write
    pool: C1 is the series where a starved engine has to be visible."""
    if args.engine == "opensearch":
        return OpenSearchSampler(args.url, args.index, read_write_pool=True)
    return ScyllaSampler(args.vs_url, args.keyspace, args.vs_index)
