"""Acknowledge at once, so the sink cannot invent a stall the engine would not.

The CQL driver leaves Nagle ON: `cassandra/cluster.py:927` documents
`sockopts = [(IPPROTO_TCP, TCP_NODELAY, 1)]` as something a user may try, and
`scylla_load` does not pass it. So the loader holds the trailing partial segment
of a burst until its previous bytes are acknowledged — and against an
accept-and-discard sink there is almost no return traffic to carry an ACK, so
Linux delays it by up to 40 ms and the burst completes a quantum late.

Measured on this laptop against `ftsbench.null_sink_cql`, batch 1, 8,000
synthetic documents:

    concurrency   without quickack        with quickack
              8      404 docs/s            7,845 docs/s   p99 85.1 -> 2.6 ms
             16      944 docs/s            8,996 docs/s   p99 47.8 -> 4.4 ms
             32   10,265 docs/s           10,231 docs/s   unchanged

At and above c=32 the burst is large enough that the receiver acknowledges on
its own and the effect disappears — which is exactly what makes it dangerous:
the low rungs would have reported a 40 ms TCP timer as the CLIENT's ceiling, in
the one measurement whose whole purpose is to stop a guess reaching the deck.

Set after every read rather than once at accept, because `TCP_QUICKACK` is not
sticky — the kernel clears it as the connection's ACK policy evolves. One
`setsockopt` per read, not per document.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

HAS_QUICKACK = hasattr(socket, "TCP_QUICKACK")


def accepted_socket(writer: asyncio.StreamWriter) -> Any | None:
    """The connection's socket handle, whatever asyncio hands back.

    Duck-typed on `setsockopt`, NOT on `isinstance(socket.socket)`: asyncio
    returns an `asyncio.trsock.TransportSocket`, which proxies `setsockopt` and
    is not a `socket.socket` subclass. An isinstance check here silently
    returned `None`, quickack was never set, and a c=16 ScyllaDB point went
    straight back to 964 docs/s — the artifact this module exists to remove,
    with every unit test still green.
    """
    handle = writer.get_extra_info("socket")
    return handle if hasattr(handle, "setsockopt") else None


def acknowledge_now(handle: Any | None) -> None:
    """Best effort: a platform without `TCP_QUICKACK` still measures something,
    it just measures it with the artifact above, and the sink says so rather
    than failing the run."""
    if handle is None or not HAS_QUICKACK:
        return
    try:
        handle.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
    except OSError:
        pass


def quickack_note() -> str:
    if HAS_QUICKACK:
        return "TCP_QUICKACK set after every read"
    return ("TCP_QUICKACK UNAVAILABLE on this platform — low-concurrency "
            "points may carry a delayed-ACK artifact; see ftsbench.sink_tcp")
