"""The HTTP wire must answer every pipelined request in one write.

`sink_http_wire` used to handle one request per loop iteration: two awaits to
read it, a write and a drain to answer it. Against `osrate --batch-size 1` that
is a syscall per document, and it capped the sink at ~45,000 docs/s while the
CQL sink -- which drains every whole frame per read and answers them in one
write -- reached ~131,000 on the same box and the same corpus.

These are the pieces that would let that regress silently. A partial request
consumed as if it were whole, a pipelined batch answered out of order, or a
head allowed past `MAX_HEADER_BYTES` would each produce a sink that looks
correct and measures the wrong thing.
"""
import asyncio

import pytest

from ftsbench import sink_http_wire
from ftsbench.sink_http_wire import Request


def request_bytes(path: str, body: bytes = b"", method: str = "POST") -> bytes:
    return (f"{method} {path} HTTP/1.1\r\n"
            f"Host: sink\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body


class EchoRoutes:
    """Answers each request with its own path, so ordering is checkable."""

    def __init__(self) -> None:
        self.seen: list[Request] = []

    def respond(self, request: Request) -> tuple[int, bytes]:
        self.seen.append(request)
        return 200, request.path.encode("ascii")


class ChunkReader:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    async def read(self, _limit: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class CountingWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.drains = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drains += 1

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, _name: str) -> None:
        return None


def serve(reader: ChunkReader, writer: CountingWriter,
          routes: EchoRoutes, delay_s: float = 0.0) -> None:
    asyncio.run(sink_http_wire.serve_connection(reader, writer, routes,
                                                delay_s))


def test_take_request_keeps_a_partial_head_for_the_next_read():
    whole = request_bytes("/a", b"xy")
    head_end = whole.index(b"\r\n\r\n")
    request, cursor = sink_http_wire.take_request(whole[:head_end + 2], 0)
    assert request is None and cursor == 0


def test_take_request_keeps_a_whole_head_whose_body_has_not_arrived():
    whole = request_bytes("/a", b"xyz")
    request, cursor = sink_http_wire.take_request(whole[:-1], 0)
    assert request is None and cursor == 0


def test_take_request_returns_the_request_and_the_offset_after_it():
    whole = request_bytes("/a", b"xyz")
    request, cursor = sink_http_wire.take_request(whole, 0)
    assert request == Request("POST", "/a", b"xyz")
    assert cursor == len(whole)


def test_take_request_reads_the_second_request_from_an_offset():
    buffer = request_bytes("/a", b"x") + request_bytes("/b", b"yy")
    _, cursor = sink_http_wire.take_request(buffer, 0)
    request, cursor = sink_http_wire.take_request(buffer, cursor)
    assert request == Request("POST", "/b", b"yy")
    assert cursor == len(buffer)


def test_a_head_that_never_terminates_is_refused_rather_than_buffered():
    flood = b"GET /a HTTP/1.1\r\nX: " + b"z" * sink_http_wire.MAX_HEADER_BYTES
    with pytest.raises(ValueError):
        sink_http_wire.take_request(flood, 0)


def test_answers_for_answers_every_whole_request_and_keeps_the_remainder():
    routes = EchoRoutes()
    whole = request_bytes("/a", b"x") + request_bytes("/b", b"y")
    partial = request_bytes("/c", b"z")[:10]
    buffer = bytearray(whole + partial)
    payload = sink_http_wire.answers_for(buffer, routes)
    assert [r.path for r in routes.seen] == ["/a", "/b"]
    assert payload.count(b"HTTP/1.1 200 OK") == 2
    assert payload.index(b"/a") < payload.index(b"/b")
    assert len(buffer) == len(partial)


def test_answers_for_leaves_the_buffer_alone_when_nothing_is_whole_yet():
    routes = EchoRoutes()
    buffer = bytearray(request_bytes("/a", b"xyz")[:-1])
    assert sink_http_wire.answers_for(buffer, routes) == b""
    assert routes.seen == []
    assert len(buffer) == len(request_bytes("/a", b"xyz")) - 1


def test_a_pipelined_batch_costs_one_write_and_one_drain():
    routes, writer = EchoRoutes(), CountingWriter()
    batch = b"".join(request_bytes(f"/{i}", b"x") for i in range(64))
    serve(ChunkReader(batch), writer, routes)
    assert len(routes.seen) == 64
    assert len(writer.writes) == 1
    assert writer.drains == 1


def test_every_answer_in_a_batch_is_returned_in_request_order():
    routes, writer = EchoRoutes(), CountingWriter()
    batch = b"".join(request_bytes(f"/p{i}", b"x") for i in range(8))
    serve(ChunkReader(batch), writer, routes)
    answered = writer.writes[0]
    assert [r.path for r in routes.seen] == [f"/p{i}" for i in range(8)]
    offsets = [answered.index(f"/p{i}".encode("ascii")) for i in range(8)]
    assert offsets == sorted(offsets)


def test_a_request_split_across_reads_is_answered_once_it_completes():
    routes, writer = EchoRoutes(), CountingWriter()
    whole = request_bytes("/a", b"xyz")
    serve(ChunkReader(whole[:12], whole[12:]), writer, routes)
    assert [r.path for r in routes.seen] == ["/a"]
    assert len(writer.writes) == 1


def test_a_read_that_completes_no_request_writes_nothing():
    routes, writer = EchoRoutes(), CountingWriter()
    whole = request_bytes("/a", b"xyz")
    serve(ChunkReader(whole[:12]), writer, routes)
    assert routes.seen == []
    assert writer.writes == []
    assert writer.closed


def test_the_connection_stays_open_across_reads_until_the_client_goes():
    routes, writer = EchoRoutes(), CountingWriter()
    serve(ChunkReader(request_bytes("/a"), request_bytes("/b")),
          writer, routes)
    assert [r.path for r in routes.seen] == ["/a", "/b"]
    assert len(writer.writes) == 2
    assert writer.closed
