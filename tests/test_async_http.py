"""The async HTTP client the ingest path now depends on.

It replaced `requests` plus a thread per in-flight bulk, so a defect here is a
defect in every OpenSearch write number. Two failure modes are worth more than
the rest and are tested directly: a half-read response left on a pooled socket,
which would make the next operation parse someone else's answer, and a pool that
quietly grows, which would make `--concurrency` describe something other than
what the engine was asked at once.

Tested against a real asyncio server rather than a mocked reader, because the
things that break here are wire-level: header casing, chunked framing, and
whether the socket is safe to hand to the next caller.
"""
import asyncio

import pytest

from ftsbench import async_http


class FakeServer:
    """Speaks just enough HTTP/1.1 to answer the client, and records what it
    saw so connection reuse can be asserted rather than assumed."""

    def __init__(self, responder, hang_up_after: int = 0) -> None:
        self._responder = responder
        self._hang_up_after = hang_up_after
        self.requests: list[bytes] = []
        self.connections = 0
        self._server = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        try:
            while True:
                length = 0
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                body = await reader.readexactly(length) if length else b""
                self.requests.append(body)
                writer.write(self._responder(len(self.requests)))
                await writer.drain()
                if self._hang_up_after and len(self.requests) >= self._hang_up_after:
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            # Awaited, not just closed: `Server.wait_closed()` waits for every
            # handler's connection to finish closing, so a writer left
            # half-closed here hangs the test rather than the client.
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass


def plain(body: bytes, status: int = 200):
    return (f"HTTP/1.1 {status} OK\r\nContent-Length: {len(body)}\r\n"
            f"\r\n").encode() + body


def chunked(body: bytes):
    return (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")


def serve(responder, hang_up_after: int = 0):
    """Run one scenario against a live server, returning (result, server)."""
    async def runner(scenario):
        server = FakeServer(responder, hang_up_after)
        port = await server.start()
        try:
            return await scenario(port), server
        finally:
            await server.stop()
    return runner


def test_a_content_length_response_is_read_whole():
    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        try:
            return await connection.post("/_bulk", b"payload", "application/x-ndjson")
        finally:
            await connection.close()

    body, _ = asyncio.run(serve(lambda n: plain(b'{"errors":false}'))(scenario))
    assert body == b'{"errors":false}'


def test_a_chunked_response_is_reassembled():
    """OpenSearch chunks larger bulk responses, and a client that stopped at the
    first chunk would read a truncated body as valid JSON or as no errors."""
    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        try:
            return await connection.post("/_bulk", b"x", "application/x-ndjson")
        finally:
            await connection.close()

    body, _ = asyncio.run(serve(lambda n: chunked(b'{"took":5}'))(scenario))
    assert body == b'{"took":5}'


def test_the_request_body_arrives_intact():
    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        try:
            await connection.post("/_bulk", b'{"index":{}}\n{"a":1}\n',
                                  "application/x-ndjson")
        finally:
            await connection.close()

    _, server = asyncio.run(serve(lambda n: plain(b"{}"))(scenario))
    assert server.requests == [b'{"index":{}}\n{"a":1}\n']


def test_a_connection_is_reused_across_requests():
    """Keep-alive is the difference between one TCP handshake and one per
    operation; without it the client's cost would scale with the ladder."""
    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        try:
            for _ in range(3):
                await connection.post("/_bulk", b"x", "application/x-ndjson")
        finally:
            await connection.close()

    _, server = asyncio.run(serve(lambda n: plain(b"{}"))(scenario))
    assert len(server.requests) == 3
    assert server.connections == 1


def test_a_non_2xx_raises_with_the_body_attached():
    """A bare status code is not actionable: OpenSearch puts the reason a bulk
    was rejected — a 429, a closed index — in the body."""
    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        try:
            with pytest.raises(async_http.HTTPError) as caught:
                await connection.post("/_bulk", b"x", "application/x-ndjson")
            return caught.value
        finally:
            await connection.close()

    error, _ = asyncio.run(
        serve(lambda n: plain(b'{"reason":"too many requests"}', 429))(scenario))
    assert error.status == 429
    assert b"too many requests" in error.body


def test_a_failed_request_does_not_leave_a_half_read_socket_in_the_pool():
    """The cross-talk failure. If a connection whose response was cut short were
    reused, the next operation would parse this one's leftovers as its own
    answer — a bulk would appear to succeed on someone else's 200.
    """
    def responder(n):
        # First response is truncated mid-body, then the server hangs up.
        return b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"

    async def scenario(port):
        connection = async_http.Connection("127.0.0.1", port)
        with pytest.raises(Exception):
            await connection.post("/_bulk", b"x", "application/x-ndjson")
        # A closed socket is the contract: the next post reconnects rather than
        # reading the previous response's remainder.
        return connection._writer is None

    reconnects, _ = asyncio.run(serve(responder, hang_up_after=1)(scenario))
    assert reconnects, "a half-read connection was kept for the next caller"


def test_the_pool_hands_one_connection_to_one_caller_at_a_time():
    async def scenario():
        pool = async_http.Pool("127.0.0.1", 1, size=1)
        held = []

        async def take(tag):
            async with pool.acquire():
                held.append(tag)
                await asyncio.sleep(0.02)
                held.append(f"{tag}-done")

        await asyncio.gather(take("a"), take("b"))
        return held

    order = asyncio.run(scenario())
    assert order in (["a", "a-done", "b", "b-done"],
                     ["b", "b-done", "a", "a-done"]), \
        f"connection was shared while a request was outstanding: {order}"


def test_the_pool_size_is_the_clients_concurrency():
    """Fixed, not growing. A pool that opened more sockets under load would make
    --concurrency describe something other than the offered concurrency."""
    async def scenario():
        pool = async_http.Pool("127.0.0.1", 1, size=3)
        first = [await pool._free.get() for _ in range(3)]
        return len(first), pool._free.empty()

    count, exhausted = asyncio.run(scenario())
    assert (count, exhausted) == (3, True)


def test_a_pool_is_never_empty_even_when_asked_for_none():
    """--concurrency 0 would otherwise deadlock on the first acquire rather than
    failing, and a deadlocked loader looks exactly like a slow engine."""
    async def scenario():
        pool = async_http.Pool("127.0.0.1", 1, size=0)
        async with pool.acquire() as connection:
            return connection is not None

    assert asyncio.run(scenario())
