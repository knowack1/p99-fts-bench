"""The index-only read arm: BM25 straight against the vector-store.

`scylla-cdc` minus `vector-store-direct` is the only way to attribute a
ScyllaDB-vs-OpenSearch latency gap to the index or to the path around it, so
the two things that make that subtraction valid are what these tests pin: the
returned length is a real hit count, and the arm cannot silently be measured
in a different projection mode from the other two.
"""
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ftsbench.engines import VectorStoreEngine, primary_key_rows

SWEEP = pathlib.Path(__file__).resolve().parents[1] / "tools" / "read_sweep.sh"


def test_primary_key_rows_zips_columns_into_rows():
    rows = primary_key_rows({"article_id": [7, 8, 9]})

    assert rows == [(7,), (8,), (9,)]


def test_primary_key_rows_counts_rows_not_columns_for_composite_keys():
    """The endpoint returns one array per key column. Counting columns, or
    concatenating them, would make len() report the wrong hit count — and
    achieved_qps and the SLA gate are both computed off that number."""
    rows = primary_key_rows({"part": ["a", "b"], "clust": [1, 2]})

    assert len(rows) == 2


def test_primary_key_rows_handles_no_hits():
    assert primary_key_rows({}) == []


def test_fetch_documents_is_refused_not_ignored():
    """BM25 cannot return document text. Accepting the flag and quietly
    returning identities would put the cost of the other engines' document
    fetch on the chart as an engine difference."""
    with pytest.raises(ValueError, match="primary keys only"):
        VectorStoreEngine("http://localhost:1", fetch_documents=True)


class _Bm25Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, json.loads(body)))
        payload = json.dumps({"primary_keys": {"article_id": [1, 2]},
                              "scores": [2.5, 1.5]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def bm25_server():
    server = HTTPServer(("127.0.0.1", 0), _Bm25Handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_search_posts_the_documented_endpoint_and_payload(bm25_server):
    host, port = bm25_server.server_address
    engine = VectorStoreEngine(f"http://{host}:{port}", keyspace="wiki",
                              index="articles_body_fts")

    hits = engine.search("kraken", limit=100)

    assert len(hits) == 2
    path, payload = bm25_server.seen[0]
    assert path == "/api/v1/indexes/wiki/articles_body_fts/bm25"
    assert payload == {"query": "kraken", "limit": 100}


def test_search_reports_no_bytes_fetched(bm25_server):
    """Identity-only by construction, so the footer's fetched-bytes evidence
    must stay zero rather than inherit a stale value from another engine."""
    host, port = bm25_server.server_address
    engine = VectorStoreEngine(f"http://{host}:{port}")

    engine.search("kraken")

    assert engine.bytes_fetched == 0


def test_sweep_driver_never_passes_fetch_documents():
    """The three arms are comparable only in identity-only mode; the moment any
    arm projects documents the cube stops being one measurement."""
    code = [line for line in SWEEP.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]

    assert not any("--fetch-documents" in line for line in code)


def test_sweep_driver_labels_the_arm_distinctly():
    """Artifacts are globbed by filename prefix at render time, so a shared
    label would silently merge the index-only cells into the CQL series."""
    text = SWEEP.read_text(encoding="utf-8")

    assert 'CONFIG="vector-store-direct"' in text
