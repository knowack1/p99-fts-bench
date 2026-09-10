"""One asyncio queue, N worker tasks, one measurement point per concurrency level.

In-flight is exactly N: each worker pulls one document and awaits its INSERT
before pulling the next. The queue is bounded so the producer cannot pull the
whole corpus into memory ahead of the workers.
"""
import asyncio
import contextlib
import time
from collections.abc import Callable, Iterator

from cassandra.cluster import Session

from .corpus import InsertParams
from .report import PointResult, note, percentile

PROGRESS_INTERVAL_S = 1.0
QUEUE_DEPTH_PER_WORKER = 2
STOP = object()

SourceFactory = Callable[[], Iterator[InsertParams]]


class Counters:
    __slots__ = ("ok", "errors", "latencies_ms", "first_error")

    def __init__(self) -> None:
        self.ok = 0
        self.errors = 0
        self.latencies_ms: list[float] = []
        self.first_error: str | None = None

    def record_ok(self, latency_ms: float) -> None:
        self.ok += 1
        self.latencies_ms.append(latency_ms)

    def record_error(self, exc: BaseException) -> None:
        self.errors += 1
        if self.first_error is None:
            self.first_error = f"{type(exc).__name__}: {exc}"

    @property
    def done(self) -> int:
        return self.ok + self.errors


def _awaitable(response_future) -> asyncio.Future:
    """Bridge one driver `ResponseFuture` onto this event loop.

    The driver's callbacks fire on its own libev reactor thread, so the result
    has to cross with `call_soon_threadsafe`; settling the Future directly from
    that thread would corrupt or lose completions under load.
    """
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()

    def settle(setter, value) -> None:
        if not waiter.done():
            setter(value)

    response_future.add_callbacks(
        lambda result: loop.call_soon_threadsafe(settle, waiter.set_result, result),
        lambda exc: loop.call_soon_threadsafe(settle, waiter.set_exception, exc))
    return waiter


async def _fill_queue(queue: asyncio.Queue, source: Iterator[InsertParams],
                     workers: int) -> None:
    for params in source:
        await queue.put(params)
    for _ in range(workers):
        await queue.put(STOP)


async def _drain_queue(queue: asyncio.Queue, session: Session, statement,
                      counters: Counters) -> None:
    while True:
        params = await queue.get()
        if params is STOP:
            return
        await _insert_one(session, statement, params, counters)


async def _insert_one(session: Session, statement, params: InsertParams,
                     counters: Counters) -> None:
    started = time.perf_counter()
    try:
        await _awaitable(session.execute_async(statement, params))
    except Exception as exc:
        counters.record_error(exc)
        return
    counters.record_ok((time.perf_counter() - started) * 1000.0)


async def _follow_progress(counters: Counters, concurrency: int) -> None:
    previous = 0
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        done = counters.done
        note(f"  c={concurrency} {done - previous} docs/s (total {done})")
        previous = done


async def _run_point(session: Session, statement, source: Iterator[InsertParams],
                    concurrency: int) -> PointResult:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH_PER_WORKER * concurrency)
    counters = Counters()
    producer = asyncio.create_task(_fill_queue(queue, source, concurrency))
    workers = [asyncio.create_task(_drain_queue(queue, session, statement, counters))
               for _ in range(concurrency)]
    reporter = asyncio.create_task(_follow_progress(counters, concurrency))
    started = time.perf_counter()
    await asyncio.gather(producer, *workers)
    wall_s = time.perf_counter() - started
    await _cancel(reporter)
    _warn_about_errors(counters)
    return _summarize(concurrency, counters, wall_s)


def _warn_about_errors(counters: Counters) -> None:
    if counters.first_error is not None:
        note(f"  !! {counters.errors} failed inserts, first was {counters.first_error}")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _summarize(concurrency: int, counters: Counters, wall_s: float) -> PointResult:
    latencies = sorted(counters.latencies_ms)
    return PointResult(
        concurrency=concurrency,
        docs=counters.ok,
        errors=counters.errors,
        wall_s=wall_s,
        docs_per_s=counters.ok / wall_s if wall_s > 0 else 0.0,
        p50_ms=percentile(latencies, 0.50),
        p99_ms=percentile(latencies, 0.99))


async def run_sweep(session: Session, statement, source_factory: SourceFactory,
                    levels: list[int]) -> list[PointResult]:
    results = []
    for position, concurrency in enumerate(levels, start=1):
        note(f"[{position}/{len(levels)}] concurrency={concurrency}")
        result = await _run_point(session, statement, source_factory(), concurrency)
        _announce(result)
        results.append(result)
    return results


def _announce(result: PointResult) -> None:
    note(f"  -> {result.docs} docs in {result.wall_s:.2f}s = "
         f"{result.docs_per_s:.1f} docs/s, p99 {result.p99_ms:.2f} ms, "
         f"{result.errors} errors")
