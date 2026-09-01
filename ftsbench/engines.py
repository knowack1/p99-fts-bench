"""Query clients for the two engines.

Both accept the same query text: ScyllaDB passes it to BM25() (Tantivy query
parser), OpenSearch to a `query_string` query (Lucene syntax). Single terms,
"quoted phrases", AND / OR / NOT and (grouping) mean the same thing in both.
Fairness mirroring: `track_total_hits: false` because ScyllaDB reports no
result totals, and both clients project the same thing.

**What "the same thing" is, is a measurement decision.** With
`fetch_documents=False` both return identity only (`SELECT article_id` vs.
`_source: false`); that measures the index lookup. With `fetch_documents=True`
both additionally return `title` and `body`, which is what an application
actually does — and it is a different measurement, because the two engines get
the document text from different places. OpenSearch reads stored fields out of
the same segment files it just searched; ScyllaDB takes the hit list from the
vector-store back to the coordinator and reads the projected columns from
SSTables. A latency chart must say which mode it was taken in: the two are not
interchangeable and must never be mixed in one comparison.

Both clients are used from a worker pool by `load_gen`, so both must be able to
hold as many connections open as there are workers. urllib3's default pool of 10
would otherwise cap OpenSearch concurrency at 10 while the ScyllaDB driver opened
as many as it wanted — a client-side asymmetry that would land on the chart as an
engine difference.
"""
import argparse

import requests

DEFAULT_LIMIT = 10
SEARCH_TIMEOUT_S = 30
DEFAULT_OS_URL = "http://localhost:9200"
DEFAULT_OS_INDEX = "wiki-articles"
DEFAULT_SCYLLA_HOSTS = "127.0.0.1"
# The bench stack publishes ScyllaDB on 19042; 9042 stays the default so this
# matches scylla_load.py, and every caller passes --port explicitly.
DEFAULT_SCYLLA_PORT = 9042
# Projected on both sides when fetch_documents is on. `title` and `body` exist
# under these names in the ScyllaDB table and in the OpenSearch mapping, so the
# two engines return the same bytes and the comparison stays symmetric.
DOCUMENT_FIELDS = ("title", "body")


class OpenSearchEngine:
    def __init__(self, url: str, index: str, field: str = "body",
                 default_operator: str = "OR", pool_maxsize: int = 1,
                 fetch_documents: bool = False):
        self._url = url.rstrip("/")
        self._index = index
        self._field = field
        self._default_operator = default_operator
        self._session = pooled_session(pool_maxsize)
        self._source = DOCUMENT_FIELDS if fetch_documents else False
        self.bytes_fetched = 0

    def search(self, query_text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        payload = {
            "size": limit,
            "_source": self._source,
            "track_total_hits": False,
            "query": {
                "query_string": {
                    "query": query_text,
                    "default_field": self._field,
                    "default_operator": self._default_operator,
                }
            },
        }
        response = self._session.post(
            f"{self._url}/{self._index}/_search",
            json=payload,
            timeout=SEARCH_TIMEOUT_S,
        )
        response.raise_for_status()
        hits = response.json()["hits"]["hits"]
        self.bytes_fetched = document_bytes(hit.get("_source") for hit in hits)
        return [hit["_id"] for hit in hits]


class ScyllaEngine:
    def __init__(self, hosts: list[str], port: int = DEFAULT_SCYLLA_PORT,
                 keyspace: str = "wiki", table: str = "articles",
                 column: str = "body", fetch_documents: bool = False):
        from cassandra.cluster import Cluster

        self._cluster = Cluster(hosts, port=port)
        self._session = self._cluster.connect(keyspace)
        self._table = table
        self._column = column
        self._projection = ("article_id, " + ", ".join(DOCUMENT_FIELDS)
                            if fetch_documents else "article_id")
        self.bytes_fetched = 0

    def search(self, query_text: str, limit: int = DEFAULT_LIMIT) -> list:
        rows = list(self._session.execute(self._build_query(query_text, limit)))
        self.bytes_fetched = document_bytes(rows)
        return [row.article_id for row in rows]

    def _build_query(self, query_text: str, limit: int) -> str:
        # Built as a literal because the M1 rule "identical term in WHERE and
        # ORDER BY" is unverified for bound parameters; escaping is plain CQL
        # single-quote doubling.
        escaped = query_text.replace("'", "''")
        bm25 = f"BM25({self._column}, '{escaped}')"
        return (
            f"SELECT {self._projection} FROM {self._table} "
            f"WHERE {bm25} > 0 ORDER BY {bm25} LIMIT {limit}"
        )


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Connection flags shared by every read-path tool. Centralised because the
    per-tool copies drifted: the Makefile passed --port to query_bench, whose
    parser had never had one, so `make bench-scylla` failed outright."""
    parser.add_argument("--url", default=DEFAULT_OS_URL)
    parser.add_argument("--index", default=DEFAULT_OS_INDEX)
    parser.add_argument("--default-operator", default="OR")
    parser.add_argument("--hosts", default=DEFAULT_SCYLLA_HOSTS)
    parser.add_argument("--port", type=int, default=DEFAULT_SCYLLA_PORT)
    parser.add_argument("--keyspace", default="wiki")
    parser.add_argument("--table", default="articles")
    parser.add_argument("--column", default="body")
    parser.add_argument("--fetch-documents", action="store_true",
                        help="project title and body, not just the id — what an "
                             "application does. Changes what is measured; see "
                             "the module docstring.")


def document_bytes(records) -> int:
    """Forces the projected text to actually be read.

    Without this the row objects can come back without their text ever being
    touched, and the measurement would time a transfer the client never paid
    for. It is also the evidence that documents were fetched at all, which the
    chart footer has to be able to state.
    """
    total = 0
    for record in records:
        if record is None:
            continue
        for field in DOCUMENT_FIELDS:
            value = record.get(field) if isinstance(record, dict) else getattr(record, field, None)
            if value:
                total += len(value)
    return total


def pooled_session(pool_maxsize: int) -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=pool_maxsize,
                                            pool_maxsize=pool_maxsize)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def build_engine(args: argparse.Namespace, pool_maxsize: int = 1):
    if args.engine == "opensearch":
        return OpenSearchEngine(args.url, args.index,
                                default_operator=args.default_operator,
                                pool_maxsize=pool_maxsize,
                                fetch_documents=fetch_documents(args))
    return ScyllaEngine(args.hosts.split(","), port=args.port,
                        keyspace=args.keyspace,
                        table=args.table, column=args.column,
                        fetch_documents=fetch_documents(args))


def fetch_documents(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "fetch_documents", False))
