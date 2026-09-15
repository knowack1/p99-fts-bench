//! Acknowledge at once, and never hold a reply back, so the sink cannot invent
//! a stall the engine would not.
//!
//! Two socket options, each for a delay the loader would otherwise record as
//! its own ceiling.
//!
//! **`TCP_QUICKACK`, after every read.** The CQL driver leaves Nagle ON:
//! `cassandra/cluster.py:927` documents `sockopts = [(IPPROTO_TCP,
//! TCP_NODELAY, 1)]` as something a user may try, and the loaders do not pass
//! it. So the client holds the trailing partial segment of a burst until its
//! previous bytes are acknowledged — and against an accept-and-discard sink
//! there is almost no return traffic to carry an ACK, so Linux delays it by up
//! to 40 ms and the burst completes a quantum late. Measured against the Python
//! sink this replaced, batch 1, 8,000 synthetic documents:
//!
//! ```text
//! concurrency   without quickack        with quickack
//!           8      404 docs/s            7,845 docs/s   p99 85.1 -> 2.6 ms
//!          16      944 docs/s            8,996 docs/s   p99 47.8 -> 4.4 ms
//!          32   10,265 docs/s           10,231 docs/s   unchanged
//! ```
//!
//! At and above c=32 the burst is large enough that the receiver acknowledges
//! on its own and the effect disappears — which is exactly what makes it
//! dangerous: the low rungs would have reported a 40 ms TCP timer as the
//! CLIENT's ceiling, in the one measurement whose whole purpose is to stop a
//! guess reaching the deck. It is set after every read rather than once at
//! accept because the flag is not sticky: the kernel clears it as the
//! connection's ACK policy evolves. One `setsockopt` per read, never per
//! document.
//!
//! **`TCP_NODELAY`, once at accept.** This one is here because the port is to
//! Rust. The Python sink never asked for it and never needed to: asyncio
//! disables Nagle on every TCP transport it creates
//! (`asyncio/selector_events.py`, `_set_nodelay` in the transport's
//! constructor), so every number this instrument has ever produced was produced
//! with Nagle off on the sink's side. Tokio does not, and a sink that left it
//! on would hold small replies back behind the very timer the paragraph above
//! exists to remove — at the low rungs, in the same invisible way.
use socket2::SockRef;
use tokio::net::TcpStream;

pub const QUICKACK: &str = "TCP_QUICKACK set after every read";
pub const QUICKACK_UNAVAILABLE: &str =
    "TCP_QUICKACK UNAVAILABLE on this platform — low-concurrency points may \
     carry a delayed-ACK artifact; see engine_mock::tcp";
pub const HAS_QUICKACK: bool = cfg!(any(
    target_os = "android",
    target_os = "fuchsia",
    target_os = "linux"
));

/// Best effort on both options: a platform without them still measures
/// something, it just measures it with the artifacts above, and the run header
/// says so rather than the sink failing.
pub fn accepted(stream: &TcpStream) {
    let _ = stream.set_nodelay(true);
    acknowledge_now(stream);
}

#[cfg(any(target_os = "android", target_os = "fuchsia", target_os = "linux"))]
pub fn acknowledge_now(stream: &TcpStream) {
    let _ = SockRef::from(stream).set_tcp_quickack(true);
}

#[cfg(not(any(target_os = "android", target_os = "fuchsia", target_os = "linux")))]
pub fn acknowledge_now(_stream: &TcpStream) {}

pub fn quickack_note() -> &'static str {
    if HAS_QUICKACK {
        QUICKACK
    } else {
        QUICKACK_UNAVAILABLE
    }
}

#[cfg(test)]
#[path = "tcp_tests.rs"]
mod tests;
