"""The vector-store half must answer the gate `scyllarate` refuses to start without.

The harness empties the keyspace before every concurrency level and then blocks
until this endpoint reports the *new* index at count 0 and status SERVING. Each
failure below produces a complete, plausible, wrong harness ceiling rather than
an error: a count answered for an index nobody created lets a misdirected run
pass its own gate; a count that survives a DROP lets level 2 inherit level 1's
documents; a cumulative counter zeroed by that DROP loses the run's own summary.
"""
import asyncio
import json
import socket
import struct

import pytest
import requests

from ftsbench import (cql_wire, null_sink, null_sink_cql, null_sink_vstore,
                      samplers)
from ftsbench.sink_counters import AcceptedWork
from ftsbench.sink_index import (BUILDING, SERVING, IndexStatus,
                                 ModelledIndex)

KEYSPACE = "wiki"
INDEX = "articles_body_fts"
STATUS_PATH = f"/api/v1/indexes/{KEYSPACE}/{INDEX}/status"

RESET_CQL = (
    "DROP KEYSPACE IF EXISTS wiki",
    "CREATE KEYSPACE wiki WITH replication = "
    "{'class': 'NetworkTopologyStrategy', 'replication_factor': 1}",
    "CREATE TABLE wiki.articles (article_id uuid PRIMARY KEY, page_id bigint, "
    "title text, body text)",
    "CREATE CUSTOM INDEX articles_body_fts ON wiki.articles(body) "
    "USING 'fulltext_index'",
)


def routes(work: AcceptedWork, index: ModelledIndex) -> null_sink_vstore.Routes:
    return null_sink_vstore.Routes(work, index, KEYSPACE, INDEX)


def get(route: null_sink_vstore.Routes, path: str) -> tuple[int, dict]:
    status, body = route.respond(
        null_sink_vstore.Request("GET", path, b""))
    return status, json.loads(body)


def a_serving_index() -> ModelledIndex:
    index = ModelledIndex()
    index.create()
    return index


def apply_reset(index: ModelledIndex) -> None:
    for statement in RESET_CQL:
        null_sink_cql.answer_ddl(null_sink_cql.parse_ddl(statement), index)


def test_status_reports_what_the_cql_half_accepted():
    index = a_serving_index()
    index.add(270269)
    status, body = get(routes(AcceptedWork(), index), STATUS_PATH)
    assert status == 200
    assert body == {"count": 270269, "status": SERVING}


def test_an_index_nobody_created_is_404():
    status, _ = get(routes(AcceptedWork(), ModelledIndex()), STATUS_PATH)
    assert status == 404


def test_a_dropped_index_goes_back_to_404():
    index = a_serving_index()
    index.add(5)
    index.drop()
    status, _ = get(routes(AcceptedWork(), index), STATUS_PATH)
    assert status == 404


def test_another_index_is_refused_and_recorded():
    """A harness pointed at the wrong index must fail its gate, not sail
    through it on another index's count."""
    work = AcceptedWork()
    status, _ = get(routes(work, a_serving_index()),
                    "/api/v1/indexes/wiki/some_other_index/status")
    assert status == 404
    assert dict(work.unexpected) == {
        "GET /api/v1/indexes/wiki/some_other_index/status": 1}


def test_another_keyspace_is_refused_and_recorded():
    work = AcceptedWork()
    status, _ = get(routes(work, a_serving_index()),
                    f"/api/v1/indexes/other/{INDEX}/status")
    assert status == 404
    assert work.unexpected


def test_info_answers_the_version_a_run_header_records():
    status, body = get(routes(AcceptedWork(), ModelledIndex()),
                       "/api/v1/info")
    assert status == 200
    assert body["version"] == null_sink_vstore.VERSION


def test_an_unknown_route_is_refused_and_recorded():
    work = AcceptedWork()
    status, _ = get(routes(work, a_serving_index()), "/api/v1/indexes")
    assert status == 404
    assert dict(work.unexpected) == {"GET /api/v1/indexes": 1}


def test_a_serving_delay_holds_the_index_at_building():
    """The gate is only proven to work against a sink that can fail it once."""
    clock = iter([0.0, 0.1, 0.9]).__next__
    index = ModelledIndex(serving_delay_s=0.5, clock=clock)
    index.create()
    assert index.status().status == BUILDING
    assert index.status().status == SERVING


def test_documents_arriving_before_the_index_exists_are_recorded():
    index = ModelledIndex()
    index.add(4)
    assert index.adds_while_absent == 4
    assert index.count == 0


def test_the_reset_cycle_leaves_a_serving_index_at_zero():
    """What `scyllarate` does before every level, end to end through the DDL
    path the driver actually sends."""
    index = a_serving_index()
    index.add(270269)
    apply_reset(index)
    status, body = get(routes(AcceptedWork(), index), STATUS_PATH)
    assert status == 200
    assert body == {"count": 0, "status": SERVING}


def test_a_drop_zeroes_the_index_but_not_the_runs_own_total():
    """`AcceptedWork` spans the process and feeds the sink's summary and
    --stats-out; only the index resets with the keyspace."""
    work, index = AcceptedWork(), a_serving_index()
    handler = null_sink_cql.Handler(null_sink_cql.new_identity(), "127.0.0.1",
                                    work, index, {})
    handler._accept(100)
    apply_reset(index)
    handler._accept(30)

    assert work.docs == 130
    assert index.count == 30


def read_short_string(body: bytes) -> tuple[str, bytes]:
    """`cql_wire` encodes but does not decode — it is the sink's spelling book,
    not a client — so the assertions read the bytes back themselves."""
    length, = struct.unpack_from(">H", body)
    return body[2:2 + length].decode("utf-8"), body[2 + length:]


def schema_change_fields(body: bytes) -> list[str]:
    kind, = struct.unpack_from(">i", body)
    assert kind == cql_wire.RESULT_SCHEMA_CHANGE
    fields, rest = [], body[4:]
    while rest:
        value, rest = read_short_string(rest)
        fields.append(value)
    return fields


def test_a_dropped_keyspace_is_announced_with_no_name_after_it():
    """A KEYSPACE target carries three fields, not four. A fourth would leave
    the driver reading the next statement's bytes as a name."""
    body = null_sink_cql.answer_ddl(
        null_sink_cql.parse_ddl(RESET_CQL[0]), ModelledIndex())
    assert schema_change_fields(body) == ["DROPPED", "KEYSPACE", "wiki"]


def test_a_created_index_is_announced_as_an_updated_table():
    """v4 has no INDEX target: an index is announced as a change to the table
    it lives on, which is also what the driver will go and re-read."""
    index = ModelledIndex()
    body = null_sink_cql.answer_ddl(null_sink_cql.parse_ddl(RESET_CQL[3]),
                                    index)
    assert schema_change_fields(body) == ["UPDATED", "TABLE", "wiki",
                                          "articles"]
    assert index.status().count == 0


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def sample_through_a_live_sink(index: ModelledIndex) -> dict:
    """One reading by the real `ScyllaSampler` over a real socket.

    Only the listening socket is closed, never awaited through
    `Server.wait_closed()`: that also waits on in-flight connection tasks, and
    `raise_for_status()` raises without reading the body, which leaves the
    connection checked out of the pool where `session.close()` cannot reach it.
    Awaiting it would hang the suite on the 404 case rather than fail it.
    """
    port = free_port()

    async def exercise() -> dict:
        server = await null_sink_vstore.serve(
            "127.0.0.1", port, AcceptedWork(), index, KEYSPACE, INDEX)
        sampler = samplers.ScyllaSampler(f"http://127.0.0.1:{port}", KEYSPACE,
                                         INDEX)
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, sampler.sample)
        finally:
            sampler._session.close()
            server.close()

    return asyncio.run(exercise())


def test_the_real_sampler_reads_the_real_socket():
    """`ScyllaSampler` is what the engine campaign measures build rate with, so
    the sink is proven against it rather than against this file's idea of the
    reply shape."""
    index = a_serving_index()
    index.add(42)
    sample = sample_through_a_live_sink(index)

    assert sample["docs_indexed"] == 42
    assert sample["docs_searchable"] == 42
    assert sample["index_status"] == SERVING


def test_a_missing_index_raises_rather_than_reading_as_zero():
    """404 must not come back as a count of 0. Zero would read as "the index
    exists and is empty" — exactly the state the post-reset gate waits for, so
    a harness would start loading against an index that was never created."""
    with pytest.raises(requests.HTTPError):
        sample_through_a_live_sink(ModelledIndex())


def test_the_cql_sink_serves_the_endpoint_scyllarate_gates_on_by_default():
    assert null_sink.vs_port_of(null_sink.parse_args(["--mode", "cql"])) == \
        null_sink.DEFAULT_VS_PORT


def test_the_http_sink_serves_no_vector_store_by_default():
    """Part B runs N http sinks at once. A fixed default port here would make
    the second of them fail to bind, and there is no index to report on the
    OpenSearch-shaped side anyway."""
    assert null_sink.vs_port_of(null_sink.parse_args(["--mode", "http"])) == 0


def test_an_explicit_port_is_obeyed_in_either_mode():
    for mode in ("cql", "http"):
        args = null_sink.parse_args(["--mode", mode, "--vs-port", "7000"])
        assert null_sink.vs_port_of(args) == 7000


def test_zero_turns_the_vector_store_off_where_it_is_the_default():
    args = null_sink.parse_args(["--mode", "cql", "--vs-port", "0"])
    assert null_sink.vs_port_of(args) == 0
    assert null_sink.vector_store_note(args) == ""


def test_the_index_a_loader_meets_already_exists():
    """The campaign applies schema.cql and index.cql before anything writes, so
    a --no-reset run must find an index rather than a 404."""
    index = null_sink.modelled_index(null_sink.parse_args(["--mode", "cql"]))
    assert index.status() == IndexStatus(0, SERVING)
