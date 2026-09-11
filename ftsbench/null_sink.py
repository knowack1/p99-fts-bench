"""Accept-and-discard sinks for both engine clients — Phase 0's instrument.

The client's own ceiling cannot be measured against a real engine. At the
~11.7k docs/s where the engine saturates, the engine is what the number
describes, which is how `N_max = 4` and `LOADER_CORE_BOUND_AT = 0.70` came to be
extrapolations from a client that no longer exists
(`BUILD-RATE-MATRIX-PLAN.md`, Phase 0). So: a sink that answers correctly,
stores nothing, and cannot be the constraint.

    python3 -m ftsbench.null_sink --mode http --port 9200
    python3 -m ftsbench.null_sink --mode cql  --port 9042

`--delay-ms` is the other half of the instrument. A sink with no delay makes the
loader client-bound by construction, which is the positive example the generator
gate has never had; the same sink with a delay puts the constraint back outside
the client, which is the negative one. A gate whose job is to catch a condition
we hope not to meet cannot be tested any other way.

**What it is not.** No storage, no consistency, no schema, no relevance. A run
against this sink measures the loader and nothing else, and no number taken from
one belongs beside an engine result.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any

from . import (null_sink_cql, null_sink_http, null_sink_vstore, runmeta,
               sink_counters, sink_tcp)
from .sink_counters import AcceptedWork
from .sink_index import ModelledIndex

MODES = ("http", "cql")
DEFAULT_PORTS = {"http": 9200, "cql": 9042}
DEFAULT_REPORT_INTERVAL_S = 5.0
# Served by default with the ScyllaDB-shaped sink, because `scyllarate` gates
# every level on it. NOT with the OpenSearch-shaped one: there is no index to
# report there, and a fixed default would make the second of N http sinks fail
# to bind. An explicit --vs-port is obeyed in either mode.
DEFAULT_VS_PORT = 6080
DEFAULT_VS_KEYSPACE = "wiki"
DEFAULT_VS_INDEX = "articles_body_fts"
MILLISECONDS = 1000.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--vs-port", type=int, default=None,
                        help=f"vector-store index-status endpoint; 0 disables. "
                             f"Defaults to {DEFAULT_VS_PORT} with --mode cql, "
                             f"where scyllarate gates every level on it, and to "
                             f"off with --mode http")
    parser.add_argument("--vs-keyspace", default=DEFAULT_VS_KEYSPACE)
    parser.add_argument("--vs-index", default=DEFAULT_VS_INDEX,
                        help="the one index this sink answers a count for; any "
                             "other is 404 and recorded, so a harness pointed "
                             "at the wrong index cannot pass its own gate")
    parser.add_argument("--vs-serving-delay-ms", type=float, default=0.0,
                        help="hold a freshly created index at BUILDING for this "
                             "long, to exercise a loader's SERVING gate")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address; the fleet runs the sink on fts-sut, "
                             "so a localhost bind would hide the network RTT "
                             "the in-flight count exists to cover")
    parser.add_argument("--port", type=int, default=None,
                        help="default 9200 for http, 9042 for cql")
    parser.add_argument("--delay-ms", type=float, default=0.0,
                        help="delay every response by this much, to construct a "
                             "case where the client is NOT the constraint")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="0 = until terminated")
    parser.add_argument("--report-interval", type=float,
                        default=DEFAULT_REPORT_INTERVAL_S)
    parser.add_argument("--label", default="")
    parser.add_argument("--stats-out", default=None,
                        help="write what the sink accepted as JSON on exit")
    return parser.parse_args(argv)


def port_of(args: argparse.Namespace) -> int:
    return args.port if args.port is not None else DEFAULT_PORTS[args.mode]


def vs_port_of(args: argparse.Namespace) -> int:
    if args.vs_port is not None:
        return args.vs_port
    return DEFAULT_VS_PORT if args.mode == "cql" else 0


def modelled_index(args: argparse.Namespace) -> ModelledIndex:
    """Created up front, because that is the state a loader meets.

    The campaign applies `schema.cql` and `index.cql` before anything writes, so
    a sink that started with no index would answer 404 to a run that never
    issued DDL — and a `--no-reset` ladder would measure an index that, as far
    as this sink was concerned, never existed.
    """
    index = ModelledIndex(args.vs_serving_delay_ms / MILLISECONDS)
    index.create()
    return index


def stats_header(args: argparse.Namespace, port: int) -> dict[str, Any]:
    return runmeta.header(
        producer="null_sink", engine=f"null-sink-{args.mode}",
        engine_version="n/a — accept and discard, nothing is stored",
        label=args.label, cache_state="n/a", corpus="", max_docs=0,
        mode=args.mode, bind_host=args.host, port=port,
        delay_ms=args.delay_ms, tcp_ack=sink_tcp.quickack_note(),
        purpose="Phase 0 client calibration; not an engine measurement")


def announce(args: argparse.Namespace, port: int) -> None:
    delay = f", delay {args.delay_ms} ms" if args.delay_ms else ""
    print(f"null sink ready: {args.mode} on {args.host}:{port}{delay}"
          f"{vector_store_note(args)}", file=sys.stderr, flush=True)


def vector_store_note(args: argparse.Namespace) -> str:
    port = vs_port_of(args)
    if not port:
        return ""
    return (f", vector-store {args.vs_keyspace}/{args.vs_index} on "
            f"{args.host}:{port}")


def install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)


async def await_stop(stop: asyncio.Event, duration_s: float) -> None:
    if not duration_s:
        await stop.wait()
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=duration_s)
    except (asyncio.TimeoutError, TimeoutError):
        return


async def start_servers(args: argparse.Namespace, work: AcceptedWork,
                        index: ModelledIndex) -> list[asyncio.Server]:
    """The engine's own endpoint, and beside it the index that endpoint feeds.

    Both, not one or the other: the CQL half accepts the documents and the DDL,
    and the vector-store half is where a loader reads back what that did. A
    loader that gates on the index cannot be measured against half a sink.
    """
    delay_s = args.delay_ms / MILLISECONDS
    if args.mode == "http":
        engine = await null_sink_http.serve(args.host, port_of(args), work,
                                            delay_s)
    else:
        engine = await null_sink_cql.serve(args.host, port_of(args), work,
                                           index, delay_s)
    port = vs_port_of(args)
    if not port:
        return [engine]
    vector_store = await null_sink_vstore.serve(
        args.host, port, work, index, args.vs_keyspace, args.vs_index, delay_s)
    return [engine, vector_store]


async def run_sink(args: argparse.Namespace, work: AcceptedWork) -> None:
    servers = await start_servers(args, work, modelled_index(args))
    announce(args, port_of(args))
    stop = asyncio.Event()
    install_stop_handlers(stop)
    reporter = asyncio.create_task(
        sink_counters.report_periodically(work, args.report_interval))
    try:
        await await_stop(stop, args.duration)
    finally:
        reporter.cancel()
        for server in servers:
            server.close()
            await server.wait_closed()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work = AcceptedWork()
    asyncio.run(run_sink(args, work))
    print(sink_counters.summary_line(work.snapshot()), file=sys.stderr)
    if work.unexpected:
        print(f"WARNING: the sink was asked for routes it does not answer: "
              f"{dict(work.unexpected)} — a loader or sampler changed, and the "
              f"run may be measuring the error path", file=sys.stderr)
    if args.stats_out:
        sink_counters.write_stats(args.stats_out, work,
                                  stats_header(args, port_of(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
