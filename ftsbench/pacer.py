"""Fixed-schedule dispatch, so latency is measured from intended start.

The defect this exists to prevent: a generator that sends the next request only
after the previous one returns measures a *slower* system as *faster*, because
while the system is stalled the generator stops asking it questions. The stall
vanishes from the sample instead of dominating it. That is coordinated omission,
and it is the single most common way a latency chart lies.

The schedule here is absolute and computed up front from the run's start:
operation i is *intended* to start at `start + i / rate`, whether or not the
generator managed to send it then. Latency is reported against that intended
time, so a stall shows up at full size in every operation queued behind it.

The generator's own lateness is recorded separately as `queue_ms`. That turns
"we avoided coordinated omission" from a claim into a number a reader can check:
if `queue_ms` is a material fraction of `latency_ms`, the chart is measuring the
generator, not the engine.
"""
import time
from dataclasses import dataclass
from typing import Iterator

MIN_SLEEP_S = 0.0005


@dataclass(frozen=True)
class Op:
    """One scheduled operation: its index and when it was *meant* to start."""
    i: int
    t_intended_s: float


def schedule(rate_per_s: float, count: int) -> list[float]:
    """Intended offsets, in seconds from run start. Open-loop and absolute:
    offsets never drift with how long operations actually take."""
    if rate_per_s <= 0:
        raise ValueError("rate_per_s must be positive")
    if count < 0:
        raise ValueError("count must not be negative")
    interval = 1.0 / rate_per_s
    return [i * interval for i in range(count)]


def unpaced(count: int) -> Iterator[Op]:
    """Closed-loop dispatch: intended time is now, so latency == service time.
    Correct for maximum-throughput runs (C1 ingest) where the question is
    "how fast can it go", not "what is the tail at rate X". Never use this
    for a percentile that will be published as a latency SLA."""
    for i in range(count):
        yield Op(i=i, t_intended_s=time.perf_counter())


def paced(rate_per_s: float, count: int, origin_s: float | None = None) -> Iterator[Op]:
    """Dispatch on the fixed schedule, sleeping only when ahead of it.

    When behind schedule, this yields immediately and does not skip work: the
    backlog is the finding. Callers must record `latency_ms` against
    `t_intended_s` and not against the moment the send actually began.
    """
    origin = time.perf_counter() if origin_s is None else origin_s
    for i, offset in enumerate(schedule(rate_per_s, count)):
        intended = origin + offset
        sleep_for = intended - time.perf_counter()
        if sleep_for > MIN_SLEEP_S:
            time.sleep(sleep_for)
        yield Op(i=i, t_intended_s=intended)


def paced_for_duration(rate_per_s: float, duration_s: float,
                       origin_s: float | None = None) -> Iterator[Op]:
    """Same schedule, bounded by wall time rather than a count. Used by the C7
    ladder, where each rung runs for a fixed duration so points are comparable."""
    origin = time.perf_counter() if origin_s is None else origin_s
    if rate_per_s <= 0:
        raise ValueError("rate_per_s must be positive")
    interval = 1.0 / rate_per_s
    i = 0
    while True:
        intended = origin + i * interval
        if intended - origin >= duration_s:
            return
        sleep_for = intended - time.perf_counter()
        if sleep_for > MIN_SLEEP_S:
            time.sleep(sleep_for)
        yield Op(i=i, t_intended_s=intended)
        i += 1


def latencies_ms(t_intended_s: float, t_start_s: float,
                 t_end_s: float) -> tuple[float, float, float]:
    """(latency_ms, service_ms, queue_ms) for one operation — see SCHEMAS.md.

    queue_ms is clamped at zero: an operation dispatched marginally early by
    clock granularity is not negative queueing, and a negative value here would
    silently subtract from the coordinated-omission accounting.
    """
    latency_ms = (t_end_s - t_intended_s) * 1000.0
    service_ms = (t_end_s - t_start_s) * 1000.0
    return latency_ms, service_ms, max(0.0, latency_ms - service_ms)
