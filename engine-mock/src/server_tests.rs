use std::sync::Mutex;
use std::time::Instant;

use tokio::io::{AsyncReadExt, AsyncWriteExt};

use super::*;
use crate::http_wire::{Answer, Body, Request, Routes, HEADER_TERMINATOR};

const LOOPBACK: &str = "127.0.0.1";
const ANY_INTERFACE: &str = "0.0.0.0";
const ANOTHER_LOOPBACK: [u8; 4] = [127, 0, 0, 2];
const ANSWER_DELAY: Duration = Duration::from_millis(250);
const ANSWER_TIMEOUT: Duration = Duration::from_secs(5);
const PATIENCE: Duration = Duration::from_millis(10);
const TRIES: u32 = 200;

/// A routing table that keeps what the accept loop handed each connection.
#[derive(Debug, Default)]
struct Watched {
    connections: Mutex<Vec<Connection>>,
}

impl Watched {
    fn lanes(&self) -> Vec<usize> {
        self.connections
            .lock()
            .unwrap()
            .iter()
            .map(|connection| connection.lane)
            .collect()
    }

    fn locals(&self) -> Vec<SocketAddr> {
        self.connections
            .lock()
            .unwrap()
            .iter()
            .map(|connection| connection.local)
            .collect()
    }
}

impl Routes for Watched {
    type Session = ();

    fn session(&self, connection: &Connection) -> Self::Session {
        self.connections.lock().unwrap().push(*connection);
    }

    fn respond(&self, _request: &Request<'_>, _session: &mut Self::Session) -> Answer {
        (200, Body::Owned(b"{}".to_vec()))
    }
}

async fn served(host: &str, delay: Duration) -> (Endpoint, Arc<Watched>) {
    let routes = Arc::new(Watched::default());
    let endpoint = serve_http(host, 0, Arc::clone(&routes), delay)
        .await
        .unwrap();
    (endpoint, routes)
}

async fn asked(stream: &mut TcpStream, route: &str) {
    stream
        .write_all(format!("GET {route} HTTP/1.1\r\nHost: mock\r\n\r\n").as_bytes())
        .await
        .unwrap();
}

/// A byte at a time, so the head comes out of whatever the sink wrote however
/// the network segmented it, and nothing of the body is swallowed with it.
async fn head_of(stream: &mut TcpStream) -> String {
    let mut head = Vec::new();
    let mut byte = [0_u8; 1];
    while !head.ends_with(HEADER_TERMINATOR) && stream.read(&mut byte).await.unwrap_or(0) == 1 {
        head.extend_from_slice(&byte);
    }
    String::from_utf8_lossy(&head).to_string()
}

fn declared_length(head: &str) -> usize {
    head.split("\r\n")
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, value)| value.trim().parse().ok())
        .unwrap_or(0)
}

/// One whole answer, head and body, so the next one on a connection the sink
/// kept alive starts where this one ended.
async fn whole_answer(stream: &mut TcpStream) -> u16 {
    let head = head_of(stream).await;
    let mut body = vec![0_u8; declared_length(&head)];
    stream.read_exact(&mut body).await.unwrap();
    head.split(' ')
        .nth(1)
        .and_then(|code| code.parse().ok())
        .unwrap_or(0)
}

/// Bounded, so a sink that serialised its connections fails these tests
/// instead of hanging the suite behind one that is never answered.
async fn status(stream: &mut TcpStream) -> u16 {
    tokio::time::timeout(ANSWER_TIMEOUT, whole_answer(stream))
        .await
        .expect("no answer arrived")
}

async fn answered(address: SocketAddr, route: &str) -> u16 {
    let mut stream = TcpStream::connect(address).await.unwrap();
    asked(&mut stream, route).await;
    status(&mut stream).await
}

async fn eventually(mut true_yet: impl FnMut() -> bool) -> bool {
    for _ in 0..TRIES {
        if true_yet() {
            return true;
        }
        tokio::time::sleep(PATIENCE).await;
    }
    false
}

/// Port 0 is how a run takes a free port, and the launcher reads back the one
/// it got. An endpoint that reported the 0 it asked for would point every
/// loader in the fleet at nothing.
#[tokio::test]
async fn an_endpoint_asked_for_any_port_reports_the_one_it_got() {
    let (endpoint, _routes) = served(LOOPBACK, Duration::ZERO).await;

    let reached = answered(endpoint.address(), "/").await;

    assert_ne!(endpoint.port(), 0);
    assert_eq!(reached, 200);
}

/// Every counter the mock keeps is striped, and the stripe is handed out at
/// accept so the per-document counters are contended only when two connections
/// land on one lane. A lane handed out twice puts every connection back on one
/// cache line, inside the measurement whose whole claim is that the instrument
/// is not the constraint.
#[tokio::test]
async fn every_accepted_connection_gets_a_lane_of_its_own() {
    let (endpoint, routes) = served(LOOPBACK, Duration::ZERO).await;

    for _ in 0..3 {
        assert_eq!(answered(endpoint.address(), "/").await, 200);
    }

    assert_eq!(routes.lanes(), vec![0, 1, 2]);
}

/// The loaders hold their connections open for a whole level, so a sink that
/// served one to completion before it took the next would answer exactly the
/// same bytes and measure something else entirely: the second loader would
/// wait for its first answer until the first loader had finished the level.
#[tokio::test]
async fn a_second_connection_is_answered_while_the_first_is_still_open() {
    let (endpoint, routes) = served(LOOPBACK, Duration::ZERO).await;
    let mut first = TcpStream::connect(endpoint.address()).await.unwrap();
    asked(&mut first, "/").await;
    assert_eq!(status(&mut first).await, 200);

    let second = answered(endpoint.address(), "/").await;
    asked(&mut first, "/").await;

    assert_eq!(second, 200);
    assert_eq!(status(&mut first).await, 200);
    assert_eq!(routes.lanes(), vec![0, 1]);
}

/// The Python sink answered every connection from one asyncio loop on one
/// thread, which made its own CPU a documented gate of every run against it:
/// past ~61,000 documents per second it *was* the constraint, reported under
/// the loader's name. Each connection is a task here, so two answers in flight
/// cost one answer's wall clock, not two.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_connections_with_an_answer_in_flight_are_served_at_once() {
    let (endpoint, _routes) = served(LOOPBACK, ANSWER_DELAY).await;
    let mut first = TcpStream::connect(endpoint.address()).await.unwrap();
    let mut second = TcpStream::connect(endpoint.address()).await.unwrap();
    asked(&mut first, "/").await;
    asked(&mut second, "/").await;

    let asked_at = Instant::now();
    let answers = (status(&mut first).await, status(&mut second).await);
    let waited = asked_at.elapsed();

    assert_eq!(answers, (200, 200));
    assert!(
        waited < ANSWER_DELAY * 2,
        "two answers of {ANSWER_DELAY:?} took {waited:?}, as if they had queued"
    );
}

/// `stop` is what ends a run. An accept loop that kept running would keep the
/// port, and the next mock launched on it would either fail to bind or find
/// the previous run answering for it.
#[tokio::test]
async fn a_stopped_endpoint_stops_listening_on_its_port() {
    let (endpoint, _routes) = served(LOOPBACK, Duration::ZERO).await;
    assert_eq!(answered(endpoint.address(), "/").await, 200);

    endpoint.stop();

    assert!(eventually(|| std::net::TcpStream::connect(endpoint.address()).is_err()).await);
}

/// The CQL half advertises this address as `rpc_address`, so it has to be the
/// address the client actually reached rather than one from configuration: a
/// fixed 127.0.0.1 would send a loader on another box off to connect to
/// itself.
#[tokio::test]
async fn a_connection_carries_the_address_the_client_actually_reached() {
    let (endpoint, routes) = served(ANY_INTERFACE, Duration::ZERO).await;
    let reached = SocketAddr::from((ANOTHER_LOOPBACK, endpoint.port()));

    assert_eq!(answered(reached, "/").await, 200);

    assert_eq!(routes.locals(), vec![reached]);
}

/// The two socket options are the difference between measuring a loader and
/// measuring a kernel timer, and they are only worth anything if the accept
/// path actually applies them — `tcp_tests` proves the functions work on a
/// socket it accepted by hand, which is not the same claim.
#[tokio::test]
async fn the_accept_path_hands_back_a_socket_with_both_options_set() {
    let listener = tokio::net::TcpListener::bind((LOOPBACK, 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let lanes = Lanes::default();
    let connecting = tokio::spawn(async move { TcpStream::connect(address).await });

    let (served, connection) = accept(&listener, &lanes).await.expect("a connection");
    let _client = connecting.await.unwrap().unwrap();

    assert!(served.nodelay().expect("the nodelay flag"), "Nagle is on");
    assert_eq!(connection.lane, 0);
    if crate::tcp::HAS_QUICKACK {
        assert!(
            socket2::SockRef::from(&served)
                .tcp_quickack()
                .expect("the quickack flag"),
            "the accepted socket will delay its acknowledgements"
        );
    }
}

/// A delay is the instrument's other half: it is how a run constructs the case
/// where the client is NOT the constraint. Deleting the sleep, or making
/// `Args::delay()` return zero, left every test green — an upper bound on the
/// wait is satisfied trivially by no wait at all.
#[tokio::test]
async fn a_delay_is_actually_served_and_not_merely_configured() {
    let delay = Duration::from_millis(150);
    let (endpoint, _routes) = served(LOOPBACK, delay).await;

    let at = std::time::Instant::now();
    let status = answered(endpoint.address(), "/").await;
    let waited = at.elapsed();

    assert_eq!(status, 200);
    assert!(waited >= delay, "answered in {waited:?}, under the delay");
    assert!(waited < delay * 4, "answered in {waited:?}, far over it");
}
