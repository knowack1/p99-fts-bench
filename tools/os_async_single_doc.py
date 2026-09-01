"""One document per OpenSearch request, N in flight, from a single event loop.

Isolates protocol cost from Python's threading model. ftsbench.opensearch_load
holds N requests in flight with N blocking threads, which is not what the CQL
driver does: that multiplexes N in-flight statements over a small event loop and
a few connections. Comparing per-document cost across the two engines using a
thread-per-request client on one side measures CPython, not the engines.

Raw asyncio HTTP/1.1 with keep-alive, no aiohttp/httpx in this venv. Pipelining
is deliberately NOT used — one outstanding request per connection, N connections
— because that is what the CQL driver's concurrency does.
"""
import asyncio, json, sys, time

URL_HOST, URL_PORT = "localhost", 9200
INDEX = "wiki-articles"


async def one_connection(queue, host, port, stats):
    reader, writer = await asyncio.open_connection(host, port)
    sent = 0
    try:
        while True:
            doc = await queue.get()
            if doc is None:
                queue.task_done()
                break
            body = (json.dumps({"index": {"_index": INDEX, "_id": str(doc["id"])}}) + "\n"
                    + json.dumps({"page_id": doc["id"], "title": doc["title"],
                                  "body": doc["text"]}) + "\n").encode()
            request = (f"POST /_bulk HTTP/1.1\r\nHost: {host}:{port}\r\n"
                       f"Content-Type: application/x-ndjson\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
                       ).encode() + body
            writer.write(request)
            await writer.drain()
            await read_response(reader)
            sent += 1
            stats[0] += 1
            queue.task_done()
    finally:
        writer.close()
        await writer.wait_closed()
    return sent


async def read_response(reader):
    length, chunked = None, False
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"", b"\n"):
            break
        low = line.lower()
        if low.startswith(b"content-length:"):
            length = int(line.split(b":")[1].strip())
        elif low.startswith(b"transfer-encoding:") and b"chunked" in low:
            chunked = True
    if chunked:
        while True:
            size = int((await reader.readline()).strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            await reader.readexactly(size + 2)
    elif length:
        await reader.readexactly(length)


async def main(corpus, concurrency, max_docs):
    queue = asyncio.Queue(maxsize=concurrency * 4)
    stats = [0]
    workers = [asyncio.create_task(one_connection(queue, URL_HOST, URL_PORT, stats))
               for _ in range(concurrency)]
    started = time.perf_counter()
    with open(corpus) as handle:
        for i, line in enumerate(handle):
            if i >= max_docs:
                break
            await queue.put(json.loads(line))
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers)
    elapsed = time.perf_counter() - started
    print(f"concurrency={concurrency:<4} docs={stats[0]:<7} "
          f"{stats[0]/elapsed:8.0f} docs/s avg  ({elapsed:.1f}s)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
