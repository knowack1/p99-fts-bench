"""The sink must answer correctly, count honestly, and refuse in the open.

The end-to-end proof that the real loaders and the real cassandra-driver connect
to it lives in `tools/client_calibration.sh`; these are the pieces that would
fail silently. A miscounted `_bulk`, a Rows result the driver cannot parse, or a
route answered 404 without a record would each produce a complete, plausible,
wrong Phase 0 number.
"""
import asyncio
import json
import struct
import uuid

import pytest

from ftsbench import cql_wire, null_sink_cql, null_sink_http
from ftsbench.sink_counters import AcceptedWork


def bulk_body(index: str, ids: list[int]) -> bytes:
    lines = []
    for page_id in ids:
        lines.append(json.dumps({"index": {"_index": index,
                                           "_id": str(page_id)}}))
        lines.append(json.dumps({"page_id": page_id, "title": "t",
                                 "body": "b"}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_bulk_action_count_matches_the_documents_offered():
    assert null_sink_http.bulk_action_count(bulk_body("i", [1, 2, 3])) == 3


def test_bulk_action_count_counts_a_delete_that_carries_no_source_line():
    payload = (b'{"delete":{"_index":"i","_id":"1"}}\n'
               + bulk_body("i", [2]))
    assert null_sink_http.bulk_action_count(payload) == 2


def test_bulk_action_count_ignores_a_trailing_blank_line():
    assert null_sink_http.bulk_action_count(bulk_body("i", [1]) + b"\n") == 1


def test_bulk_reply_carries_one_item_per_action():
    work = AcceptedWork()
    routes = null_sink_http.Routes(work)
    status, body = routes.respond(
        null_sink_http.Request("POST", "/_bulk", bulk_body("i", [1, 2])))
    reply = json.loads(body)
    assert status == 200
    assert reply["errors"] is False
    assert len(reply["items"]) == 2
    assert work.docs == 2 and work.ops == 1


def test_count_reports_what_the_sink_accepted():
    work = AcceptedWork()
    routes = null_sink_http.Routes(work)
    routes.respond(null_sink_http.Request("POST", "/_bulk",
                                          bulk_body("i", [1, 2, 3])))
    _, body = routes.respond(
        null_sink_http.Request("GET", "/wiki-articles/_count", b""))
    assert json.loads(body)["count"] == 3


def test_stats_answers_every_field_the_sampler_indexes_into():
    from ftsbench import samplers

    work = AcceptedWork()
    work.add(ops=1, docs=7)
    _, body = null_sink_http.Routes(work).respond(
        null_sink_http.Request("GET", "/wiki-articles/_stats", b""))
    total = json.loads(body)["_all"]["total"]
    assert total["indexing"]["index_total"] == 7
    # The sampler reads these by subscript, so a missing key is a crash rather
    # than a null column.
    assert samplers.OpenSearchSampler is not None
    for key in ("docs", "indexing", "segments", "merges", "refresh", "store"):
        assert key in total


def test_an_unknown_route_is_404_and_recorded():
    work = AcceptedWork()
    status, _ = null_sink_http.Routes(work).respond(
        null_sink_http.Request("GET", "/_cat/indices", b""))
    assert status == 404
    assert work.unexpected["GET /_cat/indices"] == 1


def test_settings_and_index_create_are_acknowledged():
    routes = null_sink_http.Routes(AcceptedWork())
    for request in (null_sink_http.Request("PUT", "/wiki-articles", b"{}"),
                    null_sink_http.Request("PUT", "/wiki-articles/_settings",
                                           b"{}"),
                    null_sink_http.Request("POST", "/wiki-articles/_refresh",
                                           b"")):
        status, body = routes.respond(request)
        assert status == 200
        assert json.loads(body)


def request_frame(opcode: int, stream: int, body: bytes,
                  version: int = cql_wire.REQUEST_VERSION) -> cql_wire.Frame:
    return cql_wire.Frame(version, 0, stream, opcode, body)


def wire_request(opcode: int, stream: int, body: bytes) -> bytes:
    """A REQUEST on the wire. `cql_wire.frame` stamps the response version, so
    using it here would test the version refusal instead of the frame walk."""
    return cql_wire.HEADER.pack(cql_wire.REQUEST_VERSION, 0, stream, opcode,
                                len(body)) + body


def handler(work: AcceptedWork | None = None) -> null_sink_cql.Handler:
    return null_sink_cql.Handler(null_sink_cql.new_identity(), "127.0.0.1",
                                 work or AcceptedWork())


def parsed(response: bytes) -> tuple[int, int, bytes]:
    version, _, stream, opcode, length = cql_wire.HEADER.unpack_from(response)
    assert version == cql_wire.RESPONSE_VERSION
    return opcode, stream, response[cql_wire.HEADER.size:
                                    cql_wire.HEADER.size + length]


def test_a_v5_request_is_refused_with_the_words_the_driver_downgrades_on():
    opcode, _, body = parsed(handler().answer(
        request_frame(cql_wire.OPCODE_OPTIONS, 1, b"", version=5)))
    assert opcode == cql_wire.OPCODE_ERROR
    assert "unsupported protocol version" in body.decode("utf-8", "replace")


def test_options_advertises_no_compression():
    opcode, _, body = parsed(handler().answer(
        request_frame(cql_wire.OPCODE_OPTIONS, 1, b"")))
    assert opcode == cql_wire.OPCODE_SUPPORTED
    assert b"COMPRESSION" in body
    # One entry with a zero-length value list, so the driver finds no overlap.
    assert b"snappy" not in body and b"lz4" not in body


def test_startup_and_register_are_ready():
    for opcode in (cql_wire.OPCODE_STARTUP, cql_wire.OPCODE_REGISTER):
        answered, stream, body = parsed(
            handler().answer(request_frame(opcode, 3, b"")))
        assert (answered, stream, body) == (cql_wire.OPCODE_READY, 3, b"")


def test_each_execute_is_one_document():
    work = AcceptedWork()
    sink = handler(work)
    for stream in range(5):
        opcode, _, body = parsed(sink.answer(
            request_frame(cql_wire.OPCODE_EXECUTE, stream, b"\x00\x00")))
        assert opcode == cql_wire.OPCODE_RESULT
        assert body == cql_wire.void_result()
    assert (work.ops, work.docs) == (5, 5)


def test_a_batch_frame_is_counted_by_its_statement_count():
    work = AcceptedWork()
    body = struct.pack(">BH", 1, 40) + b"rest of the batch"
    handler(work).answer(request_frame(cql_wire.OPCODE_BATCH, 1, body))
    assert (work.ops, work.docs) == (1, 40)


def test_an_unanswered_opcode_errors_and_is_recorded():
    work = AcceptedWork()
    opcode, _, _ = parsed(handler(work).answer(request_frame(0x0F, 1, b"")))
    assert opcode == cql_wire.OPCODE_ERROR
    assert work.unexpected["cql opcode 0x0f"] == 1


def test_use_returns_a_set_keyspace_result():
    body = cql_wire.long_string('USE "wiki"') + b"\x00\x00\x00"
    _, _, answer = parsed(handler().answer(
        request_frame(cql_wire.OPCODE_QUERY, 1, body)))
    assert struct.unpack_from(">i", answer)[0] == cql_wire.RESULT_SET_KEYSPACE
    assert answer.endswith(b"wiki")


@pytest.mark.parametrize("query", [
    "SELECT release_version FROM system.local",
    "SELECT broadcast_address, cluster_name, data_center, host_id, "
    "listen_address, partitioner, rack, release_version, rpc_address, "
    "schema_version, tokens FROM system.local WHERE key='local'",
    "SELECT schema_version FROM system.local WHERE key='local'",
    "SELECT * FROM system.local WHERE key='local'",
])
def test_a_local_answer_declares_exactly_the_columns_asked_for(query):
    identity = null_sink_cql.new_identity()
    values = null_sink_cql.local_row_values(identity, "10.0.0.1")
    columns, rows = null_sink_cql.table_rows(query, values, present=True)
    wanted = null_sink_cql.selected_names(query, list(values))
    assert [column.name for column in columns] == wanted
    assert len(rows) == 1 and len(rows[0]) == len(wanted)
    assert all(value is not None for value in rows[0])


def test_a_peers_answer_has_columns_but_no_rows():
    columns, rows = null_sink_cql.table_rows(
        "SELECT * FROM system.peers_v2",
        {name: (option, b"") for name, option
         in null_sink_cql.PEERS_COLUMN_TYPES.items()}, present=False)
    assert columns and rows == []


def test_an_unmodelled_select_still_declares_a_column():
    """A zero-column Rows result makes the driver fall back to the statement's
    cached metadata, which for a one-off SELECT is None — and its row parser
    raises rather than reading "no rows"."""
    answer = null_sink_cql.empty_rows_result(
        "SELECT * FROM system_schema.keyspaces")
    columns = struct.unpack_from(">i", answer, 8)[0]
    assert struct.unpack_from(">i", answer)[0] == cql_wire.RESULT_ROWS
    assert columns >= 1


def test_prepare_binds_the_campaign_table_types():
    columns = null_sink_cql.bind_columns(
        "INSERT INTO articles (article_id, page_id, title, body) "
        "VALUES (?, ?, ?, ?)")
    assert [column.name for column in columns] == ["article_id", "page_id",
                                                   "title", "body"]
    assert [column.type_option for column in columns] == [
        cql_wire.TYPE_UUID, cql_wire.TYPE_BIGINT, cql_wire.TYPE_VARCHAR,
        cql_wire.TYPE_VARCHAR]


def test_prepare_binds_a_delete_predicate():
    columns = null_sink_cql.bind_columns(
        "DELETE FROM articles WHERE article_id = ?")
    assert [column.name for column in columns] == ["article_id"]
    assert columns[0].type_option == cql_wire.TYPE_UUID


def test_prepare_never_returns_fewer_columns_than_markers():
    columns = null_sink_cql.bind_columns("SELECT * FROM t WHERE token(x) > ?")
    assert len(columns) == 1


def test_a_prepared_id_is_stable_for_the_same_statement():
    query = "INSERT INTO articles (article_id) VALUES (?)"
    assert null_sink_cql.query_id(query) == null_sink_cql.query_id(query)
    assert len(null_sink_cql.query_id(query)) == 16


def test_take_frame_keeps_a_partial_frame_for_the_next_read():
    whole = cql_wire.frame(cql_wire.OPCODE_EXECUTE, 1, b"abc")
    frame, cursor = cql_wire.take_frame(whole[:-1], 0)
    assert frame is None and cursor == 0
    frame, cursor = cql_wire.take_frame(whole, 0)
    assert frame is not None and cursor == len(whole)


def test_answers_for_answers_every_whole_frame_and_keeps_the_remainder():
    work = AcceptedWork()
    frames = b"".join(wire_request(cql_wire.OPCODE_EXECUTE, stream, b"xy")
                      for stream in range(3))
    buffer = bytearray(frames + frames[:4])
    payload = null_sink_cql.answers_for(buffer, handler(work))
    assert work.docs == 3
    assert len(payload) == 3 * (cql_wire.HEADER.size + 4)
    assert len(buffer) == 4


def test_a_uuid_cell_round_trips_through_the_wire_encoding():
    value = uuid.uuid4()
    assert cql_wire.cell(cql_wire.uuid_value(value))[4:] == value.bytes


def test_a_null_cell_is_not_an_empty_one():
    assert cql_wire.cell(None) == struct.pack(">i", -1)
    assert cql_wire.cell(b"") == struct.pack(">i", 0)


def free_port() -> int:
    import socket

    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def test_the_http_sink_serves_a_real_socket():
    async def exercise() -> tuple[int, int]:
        work = AcceptedWork()
        port = free_port()
        server = await null_sink_http.serve("127.0.0.1", port, work)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = bulk_body("i", [1, 2, 3, 4])
        writer.write(f"POST /_bulk HTTP/1.1\r\nHost: x\r\n"
                     f"Content-Length: {len(payload)}\r\n\r\n".encode()
                     + payload)
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        writer.close()
        server.close()
        await server.wait_closed()
        return status, work.docs

    assert asyncio.run(exercise()) == (200, 4)


def test_the_sink_finds_the_socket_asyncio_actually_hands_back():
    """asyncio returns an `asyncio.trsock.TransportSocket`, which proxies
    `setsockopt` and is NOT a `socket.socket`. An isinstance check here returned
    None, quickack went unset, and a c=16 ScyllaDB point fell back to 964 docs/s
    with every other test still green — so the lookup is asserted against a real
    accepted connection rather than a constructed one."""
    from ftsbench import sink_tcp

    async def exercise() -> bool:
        found: list[bool] = []

        async def accepted(_reader, writer):
            handle = sink_tcp.accepted_socket(writer)
            found.append(handle is not None)
            sink_tcp.acknowledge_now(handle)
            writer.close()

        port = free_port()
        server = await asyncio.start_server(accepted, "127.0.0.1", port)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if found:
                break
        writer.close()
        server.close()
        await server.wait_closed()
        return bool(found) and found[0]

    assert asyncio.run(exercise()) is True
