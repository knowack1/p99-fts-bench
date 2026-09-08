"""A small asyncio HTTP/1.1 client: N connections, one request outstanding each.

`requests` is blocking, so the ingest driver used to hold one OS thread per
in-flight `_bulk`. At the ladder's `--concurrency 256` that is 256 threads on an
8-core box, and the GIL then serialises every JSON encode and every response
parse — which is why the concurrency axis stopped measuring the engine somewhere
around c=32 (`results/client-model-2026-09-08/README.md`).

No aiohttp or httpx in this venv, and adding one would change what the published
runs depend on mid-campaign, so this speaks HTTP/1.1 directly. It is deliberately
small: POST, keep-alive, `Content-Length` or `chunked` responses, and nothing
else. `tools/os_async_single_doc.py` proved the approach before it was wired in.

**One outstanding request per connection, no pipelining.** That is not a
simplification, it is the point: the CQL driver multiplexes N in-flight
statements over a few connections, and matching that shape is what makes
`--concurrency` mean the same thing on both engines. Pipelining here would give
the HTTP side a form of concurrency the CQL side is not being given.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

CRLF = b"\r\n"


class HTTPError(RuntimeError):
    """A non-2xx response. Carries the body, because OpenSearch puts the reason
    a bulk was rejected in it and a bare status code is not actionable."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}: {body[:512].decode(errors='replace')}")
        self.status = status
        self.body = body


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, int | None, bool]:
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("server closed the connection before responding")
    status = int(status_line.split(b" ")[1])
    length: int | None = None
    chunked = False
    while True:
        line = await reader.readline()
        if line in (b"", CRLF, b"\n"):
            break
        lowered = line.lower()
        if lowered.startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
        elif lowered.startswith(b"transfer-encoding:") and b"chunked" in lowered:
            chunked = True
    return status, length, chunked


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    chunks = []
    while True:
        size = int((await reader.readline()).strip() or b"0", 16)
        if size == 0:
            await reader.readline()
            return b"".join(chunks)
        chunks.append(await reader.readexactly(size))
        await reader.readexactly(2)


async def _read_body(reader: asyncio.StreamReader, length: int | None,
                     chunked: bool) -> bytes:
    if chunked:
        return await _read_chunked(reader)
    if length:
        return await reader.readexactly(length)
    return b""


class Connection:
    """One keep-alive socket. Never shared while a request is outstanding —
    `Pool` hands it to exactly one caller at a time."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_open(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port)

    async def post(self, path: str, body: bytes, content_type: str) -> bytes:
        """Send one request and read its whole response.

        Any failure closes the socket rather than returning it to the pool: a
        half-read response would desynchronise the stream, and the next caller
        would parse this request's leftovers as its own answer — a silent
        cross-talk that looks like data corruption, not like an error.
        """
        try:
            await self._ensure_open()
            head = (f"POST {path} HTTP/1.1\r\nHost: {self._host}:{self._port}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: keep-alive\r\n\r\n").encode()
            self._writer.write(head + body)
            await self._writer.drain()
            status, length, chunked = await _read_headers(self._reader)
            payload = await _read_body(self._reader, length, chunked)
        except Exception:
            await self.close()
            raise
        if status >= 300:
            raise HTTPError(status, payload)
        return payload

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class Pool:
    """A fixed set of connections, acquired one caller at a time.

    Fixed rather than growing: the pool size IS the client's concurrency, and a
    pool that quietly opened more sockets under load would make `--concurrency`
    describe something other than what the engine was asked to do at once.
    """

    def __init__(self, host: str, port: int, size: int) -> None:
        self._free: asyncio.LifoQueue = asyncio.LifoQueue()
        for _ in range(max(size, 1)):
            self._free.put_nowait(Connection(host, port))

    @asynccontextmanager
    async def acquire(self):
        connection = await self._free.get()
        try:
            yield connection
        finally:
            self._free.put_nowait(connection)

    async def close(self) -> None:
        while not self._free.empty():
            await self._free.get_nowait().close()
