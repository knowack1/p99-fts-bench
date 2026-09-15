//! The HTTP wire must answer every pipelined request in one write.
//!
//! The Python sink this replaces handled one request per loop iteration: two
//! awaits to read it, a write and a drain to answer it. Against `osrate
//! --batch-size 1` that is a syscall per document, and it capped that sink at
//! ~45,000 docs/s while the CQL half -- which drains every whole frame per read
//! and answers them in one write -- reached ~131,000 on the same box and the
//! same corpus.
//!
//! These are the pieces that would let that regress silently. A partial request
//! consumed as if it were whole, a pipelined batch answered out of order, or a
//! head allowed past `MAX_HEADER_BYTES` would each produce a mock that looks
//! correct and measures the wrong thing.
use super::*;
use crate::conn::Answers;

fn framed(head: &str, body: &[u8]) -> Vec<u8> {
    let mut request = format!("{head}\r\n\r\n").into_bytes();
    request.extend_from_slice(body);
    request
}

fn request_bytes(path: &str, body: &[u8]) -> Vec<u8> {
    framed(
        &format!(
            "POST {path} HTTP/1.1\r\nHost: sink\r\nContent-Length: {}",
            body.len()
        ),
        body,
    )
}

fn head_end_of(request: &[u8]) -> usize {
    request
        .windows(HEADER_TERMINATOR.len())
        .position(|window| window == HEADER_TERMINATOR)
        .unwrap()
}

fn a_request(path: &'static str, body: &'static [u8]) -> Request<'static> {
    Request {
        method: "POST",
        path: Cow::Borrowed(path),
        body,
    }
}

fn a_connection() -> Connection {
    Connection {
        lane: 0,
        local: "127.0.0.1:9200".parse().unwrap(),
    }
}

fn response_to(status: u16, body: &[u8]) -> String {
    String::from_utf8(http_response(status, body)).unwrap()
}

/// Answers each request with its own path, so ordering is checkable.
struct EchoRoutes;

impl Routes for EchoRoutes {
    type Session = Vec<String>;

    fn session(&self, _connection: &Connection) -> Self::Session {
        Vec::new()
    }

    fn respond(&self, request: &Request<'_>, session: &mut Self::Session) -> Answer {
        session.push(request.path.to_string());
        (200, Body::Owned(request.path.as_bytes().to_vec()))
    }
}

/// Answers with how many requests this connection has asked for, so a session
/// rebuilt per read shows up in the reply.
struct CountingRoutes;

impl Routes for CountingRoutes {
    type Session = usize;

    fn session(&self, _connection: &Connection) -> Self::Session {
        0
    }

    fn respond(&self, _request: &Request<'_>, session: &mut Self::Session) -> Answer {
        *session += 1;
        (200, Body::Owned(session.to_string().into_bytes()))
    }
}

struct Answered {
    seen: Vec<String>,
    payload: String,
    consumed: usize,
}

fn answered(buffer: &[u8]) -> Answered {
    let mut seen = Vec::new();
    let mut out = Vec::new();
    let consumed = answers_for(buffer, &EchoRoutes, &mut seen, &mut out).unwrap();
    Answered {
        seen,
        payload: String::from_utf8(out).unwrap(),
        consumed,
    }
}

#[test]
fn a_partial_head_is_kept_for_the_next_read() {
    let whole = request_bytes("/a", b"xy");
    let torn = &whole[..head_end_of(&whole) + 2];
    assert_eq!(take_request(torn, 0), Ok(None));
}

#[test]
fn a_whole_head_whose_body_has_not_arrived_is_kept_for_the_next_read() {
    let whole = request_bytes("/a", b"xyz");
    assert_eq!(take_request(&whole[..whole.len() - 1], 0), Ok(None));
}

#[test]
fn a_whole_request_is_returned_with_the_offset_after_it() {
    let whole = request_bytes("/a", b"xyz");

    let (request, cursor) = take_request(&whole, 0).unwrap().unwrap();

    assert_eq!(request, a_request("/a", b"xyz"));
    assert_eq!(cursor, whole.len());
}

/// The cursor is the only thing carrying a reader from one pipelined request to
/// the next: an offset that is short by the body re-reads that body as a head,
/// and one that is long drops a request the client is still waiting on.
#[test]
fn the_second_request_is_read_from_the_cursor_the_first_returned() {
    let buffer = [request_bytes("/a", b"x"), request_bytes("/b", b"yy")].concat();

    let (_, after_first) = take_request(&buffer, 0).unwrap().unwrap();
    let (request, cursor) = take_request(&buffer, after_first).unwrap().unwrap();

    assert_eq!(request, a_request("/b", b"yy"));
    assert_eq!(cursor, buffer.len());
}

/// A head with no terminator is a request that will never complete, and one
/// connection sending it would otherwise grow the mock's buffer without bound.
#[test]
fn a_head_that_never_terminates_is_refused_rather_than_buffered() {
    let flood = [
        b"GET /a HTTP/1.1\r\nX: ".as_slice(),
        &vec![b'z'; MAX_HEADER_BYTES],
    ]
    .concat();

    assert!(take_request(&flood, 0).is_err());
}

/// Header names are case-insensitive by the grammar, and a client that spells
/// this one lowercase would have its body read as the head of the next request:
/// every later request on that connection is then framed wrong.
#[test]
fn a_content_length_is_found_whatever_case_the_client_spells_it() {
    for spelling in ["content-length", "Content-Length", "CONTENT-LENGTH"] {
        let whole = framed(&format!("POST /a HTTP/1.1\r\n{spelling}: 3"), b"xyz");

        let (request, cursor) = take_request(&whole, 0).unwrap().unwrap();

        assert_eq!(request.body, b"xyz".as_slice(), "{spelling}");
        assert_eq!(cursor, whole.len(), "{spelling}");
    }
}

#[test]
fn a_request_with_no_content_length_has_an_empty_body() {
    let whole = framed("GET /_cluster/health HTTP/1.1\r\nHost: sink", b"");

    let (request, cursor) = take_request(&whole, 0).unwrap().unwrap();

    assert!(request.body.is_empty());
    assert_eq!(cursor, whole.len());
}

/// Both routing tables match on the route, so a loader that appends
/// `?refresh=wait_for` or `?pretty` would fall through to 404 and be counted as
/// an error rather than as the document it was.
#[test]
fn a_route_is_the_path_without_its_query_string() {
    assert_eq!(
        a_request("/wiki-articles/_search?pretty=true", b"").route(),
        "/wiki-articles/_search"
    );
}

#[test]
fn a_path_with_no_query_string_is_its_own_route() {
    assert_eq!(
        a_request("/_cluster/health", b"").route(),
        "/_cluster/health"
    );
}

/// A client pairing replies to requests by position mis-pairs them if the order
/// slips, and neither side errors: the run completes and every latency in it
/// belongs to some other request.
#[test]
fn every_whole_request_is_answered_in_request_order_and_the_torn_tail_is_left() {
    let whole = [request_bytes("/a", b"x"), request_bytes("/b", b"y")].concat();
    let third = request_bytes("/c", b"z");
    let torn = &third[..10];
    let buffer = [whole.as_slice(), torn].concat();

    let reply = answered(&buffer);

    assert_eq!(reply.seen, ["/a", "/b"]);
    assert_eq!(reply.payload.matches("HTTP/1.1 200 OK").count(), 2);
    assert!(reply.payload.find("/a") < reply.payload.find("/b"));
    assert_eq!(buffer.len() - reply.consumed, torn.len());
}

/// Consuming a partial request answers a request the client never finished
/// sending, and leaves the rest of its bytes to be framed as the next one.
#[test]
fn a_buffer_with_nothing_whole_in_it_is_neither_answered_nor_consumed() {
    let whole = request_bytes("/a", b"xyz");

    let reply = answered(&whole[..whole.len() - 1]);

    assert!(reply.seen.is_empty());
    assert!(reply.payload.is_empty());
    assert_eq!(reply.consumed, 0);
}

/// A Content-Length that does not match the body, or a missing keep-alive, ends
/// the connection reuse the whole measurement assumes: the loader reconnects
/// per request and reports a ceiling that is mostly TCP handshakes.
#[test]
fn a_response_head_states_the_status_the_type_the_length_and_keep_alive() {
    let response = response_to(200, b"{\"ok\":true}");

    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"), "{response}");
    assert!(
        response.contains("Content-Type: application/json; charset=UTF-8\r\n"),
        "{response}"
    );
    assert!(response.contains("Content-Length: 11\r\n"), "{response}");
    assert!(
        response.contains("Connection: keep-alive\r\n"),
        "{response}"
    );
    assert!(response.ends_with("\r\n\r\n{\"ok\":true}"), "{response}");
}

/// The Python sink knew two reason phrases and spelled every other status `OK`,
/// including the 503 its own unallocated-primary answer uses. No client reads
/// the phrase, so this pins a deliberate deviation rather than a requirement:
/// a capture taken during a run should not show a 503 calling itself OK.
#[test]
fn a_404_says_not_found_and_a_503_says_service_unavailable() {
    assert!(response_to(404, b"{}").starts_with("HTTP/1.1 404 Not Found\r\n"));
    assert!(response_to(503, b"{}").starts_with("HTTP/1.1 503 Service Unavailable\r\n"));
}

/// The OpenSearch table caches one bulk reply per item count in its session, so
/// the session has to outlive the read that built it. Rebuilt per read, the
/// cache never hits and every document pays again for a reply already in hand.
#[test]
fn an_answering_half_keeps_one_session_across_reads() {
    let mut answering = Answering::new(Arc::new(CountingRoutes), &a_connection());
    let mut out = Vec::new();

    answering
        .answer_all(&request_bytes("/a", b"x"), &mut out)
        .unwrap();
    answering
        .answer_all(&request_bytes("/b", b"y"), &mut out)
        .unwrap();

    assert!(String::from_utf8(out).unwrap().ends_with("\r\n\r\n2"));
}

/// The head has always been bounded; the body was not. In Python an absurd
/// `Content-Length` grew one bytearray, and here it is a reservation the
/// allocator may not survive — an out-of-memory abort takes every connection
/// with it rather than the one that asked for it.
#[test]
fn a_body_longer_than_the_mock_will_ever_be_sent_is_refused_rather_than_buffered() {
    let claimed = MAX_BODY_BYTES + 1;
    let head = format!("POST /_bulk HTTP/1.1\r\nContent-Length: {claimed}\r\n\r\n");

    assert!(take_request(head.as_bytes(), 0).is_err());
}

/// A bulk of a size a loader really sends is not refused by that bound.
#[test]
fn a_body_of_a_size_a_loader_sends_is_read_whole() {
    let payload = vec![b'x'; 1 << 20];
    let mut request = format!(
        "POST /_bulk HTTP/1.1\r\nContent-Length: {}\r\n\r\n",
        payload.len()
    )
    .into_bytes();
    request.extend_from_slice(&payload);

    let (taken, cursor) = take_request(&request, 0).unwrap().unwrap();

    assert_eq!(taken.body.len(), payload.len());
    assert_eq!(cursor, request.len());
}

/// A request the framing cannot read ends the connection, as it did in Python.
/// What is new is that it says so: a client that started corrupting requests
/// would otherwise show up only as dropped connections in the loader's own
/// error count, with nothing in the mock's artifact to match them against.
///
/// The bytes are the real case. `Transfer-Encoding: chunked` is not read — only
/// `Content-Length` is — so a chunked body is framed as no body and the
/// chunk-size line that follows arrives here as a request line with one field
/// in it.
#[tokio::test]
async fn a_request_the_framing_cannot_read_is_recorded_before_the_connection_goes() {
    let work = Arc::new(crate::counters::AcceptedWork::new(1));
    let table = crate::opensearch::Table::new(
        Arc::clone(&work),
        Arc::new(crate::index::ModelledIndex::created(
            1,
            std::time::Duration::ZERO,
            crate::index::Refresh::immediately(),
            crate::index::Clock::monotonic(),
        )),
    );
    let mut answering = Answering::new(
        Arc::new(table),
        &Connection {
            lane: 0,
            local: "127.0.0.1:9200".parse().unwrap(),
        },
    );

    let refused = conn::Answers::answer_all(&mut answering, b"14\r\n\r\n", &mut Vec::new());

    assert!(refused.is_err());
    assert_eq!(
        work.unexpected().into_keys().collect::<Vec<_>>(),
        vec!["malformed http request".to_string()]
    );
}

/// The head has two size guards: one for a head still waiting for its
/// terminator, and one for a head that arrived whole and is over the cap. Only
/// the first had a test, so the second could be deleted and the suite stayed
/// green.
#[test]
fn a_terminated_head_over_the_cap_is_refused_too() {
    let mut flood = b"GET /a HTTP/1.1\r\nX: ".to_vec();
    flood.extend(std::iter::repeat_n(b'z', MAX_HEADER_BYTES));
    flood.extend_from_slice(HEADER_TERMINATOR);

    assert!(take_request(&flood, 0).is_err());
}
