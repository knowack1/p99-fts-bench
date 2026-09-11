"""HTTP/1.1 framing for the null sinks, with no opinion about what it answers.

Two sinks now speak HTTP and they are shaped by two different engines: the
OpenSearch-shaped one in `null_sink_http` and the vector-store-shaped one in
`null_sink_vstore`. The wire between them is the same — read a head, read
`Content-Length` bytes, write a reply, keep the connection alive — so it lives
here rather than once per sink, and a routing table is whatever object answers
`respond(request) -> (status, body)`.

Keep-alive and one write per request are not incidental. The loaders hold many
requests outstanding on few connections, and a sink that closed per request
would put a TCP handshake between the client and its own ceiling.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from . import sink_tcp

HEADER_TERMINATOR = b"\r\n\r\n"
MAX_HEADER_BYTES = 1 << 16

STATUS_TEXT = {200: "OK", 404: "Not Found"}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: bytes


class Routes(Protocol):
    def respond(self, request: Request) -> tuple[int, bytes]:
        ...


def http_response(status: int, body: bytes) -> bytes:
    reason = STATUS_TEXT.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: keep-alive\r\n\r\n").encode("ascii")
    return head + body


def content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            return int(value.strip())
    return 0


def request_line(head: bytes) -> tuple[str, str]:
    parts = head.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        raise ValueError(f"malformed request line: {parts!r}")
    return parts[0].decode("ascii"), parts[1].decode("latin-1")


async def read_head(reader: asyncio.StreamReader) -> bytes | None:
    try:
        return await reader.readuntil(HEADER_TERMINATOR)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    except asyncio.LimitOverrunError as overrun:
        raise ValueError(f"request head over {MAX_HEADER_BYTES} bytes "
                         f"({overrun.consumed} consumed)") from overrun


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    head = await read_head(reader)
    if not head:
        return None
    method, path = request_line(head)
    body = await reader.readexactly(content_length(head))
    return Request(method, path, body)


async def serve_connection(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter, routes: Routes,
                           delay_s: float) -> None:
    handle = sink_tcp.accepted_socket(writer)
    try:
        while True:
            request = await read_request(reader)
            if request is None:
                return
            sink_tcp.acknowledge_now(handle)
            status, body = routes.respond(request)
            if delay_s:
                await asyncio.sleep(delay_s)
            writer.write(http_response(status, body))
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        return
    finally:
        writer.close()


async def serve(host: str, port: int, routes: Routes,
                delay_s: float = 0.0) -> asyncio.Server:
    async def client_connected(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, routes, delay_s)

    return await asyncio.start_server(client_connected, host, port,
                                      limit=MAX_HEADER_BYTES)
