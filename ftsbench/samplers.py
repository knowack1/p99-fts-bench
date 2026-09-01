"""Engine pollers for index-build progress (chart C1).

Each sampler exposes ``sample()`` returning a flat dict of counters read from
the engine at that instant. The monitor turns consecutive samples into
instantaneous rates, so samplers must return monotonically-increasing
counters, never rates.

OpenSearch progress is read from ``indexing.index_total`` rather than
``_count`` because ``_count`` only sees refreshed documents: with
``refresh_interval`` tuned up (or disabled during load) it would report a
flat line while indexing is in fact proceeding.
"""
import requests

STATS_TIMEOUT_S = 30


class OpenSearchSampler:
    engine = "opensearch"

    def __init__(self, url: str, index: str):
        self._url = url.rstrip("/")
        self._index = index
        self._session = requests.Session()

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
            "index_status": "n/a",
        }

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
    if args.engine == "opensearch":
        return OpenSearchSampler(args.url, args.index)
    return ScyllaSampler(args.vs_url, args.keyspace, args.vs_index)
