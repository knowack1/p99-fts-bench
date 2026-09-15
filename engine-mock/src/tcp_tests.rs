use tokio::net::TcpListener;

use super::*;

/// Both ends of a connection the runtime accepted. The options are asserted on
/// what the accept path hands the sink, never on a socket the test built for
/// itself.
async fn accepted_connection() -> (TcpStream, TcpStream) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = TcpStream::connect(listener.local_addr().unwrap())
        .await
        .unwrap();
    let (server, _) = listener.accept().await.unwrap();
    (server, client)
}

#[cfg(any(target_os = "android", target_os = "fuchsia", target_os = "linux"))]
fn quickack_is_on(stream: &TcpStream) -> bool {
    SockRef::from(stream).tcp_quickack().unwrap_or(false)
}

#[cfg(not(any(target_os = "android", target_os = "fuchsia", target_os = "linux")))]
fn quickack_is_on(_stream: &TcpStream) -> bool {
    false
}

/// asyncio disables Nagle on every TCP transport it creates
/// (`asyncio/selector_events.py`, `_set_nodelay` in the transport's
/// constructor), so every number this instrument ever produced was produced
/// with Nagle off on the sink's side. Tokio does not, and a sink that left it
/// on would hold small replies behind the same 40 ms timer quickack exists to
/// remove — at the low rungs, in the same invisible way.
#[tokio::test]
async fn an_accepted_connection_has_nagle_switched_off() {
    let (server, _client) = accepted_connection().await;

    accepted(&server);

    assert!(server.nodelay().unwrap());
}

/// The Python sink looked its socket up with an `isinstance(socket.socket)`
/// check, which silently returned `None` for the `TransportSocket` asyncio
/// actually hands back: quickack went unset, and a c=16 ScyllaDB point fell
/// back to 964 docs/s with every other test still green. Only a real accepted
/// connection catches that, so this asks the kernel what the socket carries.
#[cfg(any(target_os = "android", target_os = "fuchsia", target_os = "linux"))]
#[tokio::test]
async fn an_accepted_connection_is_asked_to_acknowledge_at_once() {
    let (server, _client) = accepted_connection().await;

    accepted(&server);

    assert!(quickack_is_on(&server));
}

/// `TCP_QUICKACK` is not sticky — the kernel clears it as the connection's ACK
/// policy evolves — which is why it is set after every read rather than once
/// at accept. A call that only worked on a socket nothing had cleared would
/// put the 40 ms delayed ACK back into the low rungs partway through a run.
#[cfg(any(target_os = "android", target_os = "fuchsia", target_os = "linux"))]
#[tokio::test]
async fn acknowledging_now_re_arms_a_connection_that_has_been_cleared() {
    let (server, _client) = accepted_connection().await;
    SockRef::from(&server).set_tcp_quickack(false).unwrap();

    acknowledge_now(&server);

    assert!(quickack_is_on(&server));
}

/// The note is what the run header tells a reader about whether its
/// low-concurrency points can carry a delayed-ACK artifact. A note that
/// disagreed with the socket would be worse than no note at all: it would
/// clear exactly the numbers that need the warning.
#[tokio::test]
async fn the_quickack_note_says_what_the_socket_actually_does() {
    let (server, _client) = accepted_connection().await;

    accepted(&server);

    assert_eq!(quickack_is_on(&server), quickack_note() == QUICKACK);
}
