//! One write per read, never one per request, is the rule the instrument rests
//! on: answering pipelined messages one at a time puts a `recvfrom`, a `sendto`
//! and a `setsockopt` between the client and its own ceiling, per document.
//!
//! A duplex pipe cannot say how many writes its bytes arrived in, so the shape
//! is proved twice: over a pipe, that a batch comes back as one payload in
//! request order, and against a double, that the loop wrote and acknowledged
//! once per read however many messages that read carried.
use std::collections::VecDeque;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use tokio::io::{DuplexStream, ReadBuf};

use super::*;

const PIPE_BYTES: usize = 1 << 20;

#[derive(Clone, Debug, Default)]
struct Tally {
    reads_served: usize,
    reads_acknowledged: usize,
    answer_calls: usize,
    writes: Vec<Vec<u8>>,
}

type Shared = Arc<Mutex<Tally>>;

fn tally() -> Shared {
    Shared::default()
}

fn snapshot(tally: &Shared) -> Tally {
    tally.lock().unwrap().clone()
}

fn message(n: usize) -> Vec<u8> {
    format!("m{n}\n").into_bytes()
}

fn answer_to(message: &[u8]) -> Vec<u8> {
    [b"ok:".as_slice(), message].concat()
}

fn answer(n: usize) -> Vec<u8> {
    answer_to(&message(n))
}

fn messages(count: usize) -> Vec<u8> {
    (0..count).flat_map(message).collect()
}

fn answers(count: usize) -> Vec<u8> {
    (0..count).flat_map(answer).collect()
}

/// A connection whose reads are a script and whose writes are counted.
struct ScriptedWire {
    chunks: VecDeque<Vec<u8>>,
    tally: Shared,
}

impl ScriptedWire {
    fn new(chunks: &[&[u8]], tally: &Shared) -> Self {
        Self {
            chunks: chunks.iter().map(|chunk| chunk.to_vec()).collect(),
            tally: Arc::clone(tally),
        }
    }
}

impl AsyncRead for ScriptedWire {
    fn poll_read(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        if let Some(chunk) = this.chunks.pop_front() {
            buf.put_slice(&chunk);
            this.tally.lock().unwrap().reads_served += 1;
        }
        Poll::Ready(Ok(()))
    }
}

impl AsyncWrite for ScriptedWire {
    fn poll_write(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        self.tally.lock().unwrap().writes.push(buf.to_vec());
        Poll::Ready(Ok(buf.len()))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

impl Wire for ScriptedWire {
    fn after_read(&self) {
        self.tally.lock().unwrap().reads_acknowledged += 1;
    }
}

/// A protocol just big enough to be pipelined: one message per newline,
/// answered with `ok:` and the message back, so ordering is checkable.
struct LineAnswers(Shared);

impl Answers for LineAnswers {
    fn answer_all(&mut self, buffer: &[u8], out: &mut Vec<u8>) -> Result<usize, String> {
        self.0.lock().unwrap().answer_calls += 1;
        let mut cursor = 0;
        for message in buffer.split_inclusive(|byte| *byte == b'\n') {
            if !message.ends_with(b"\n") {
                break;
            }
            out.extend_from_slice(&answer_to(message));
            cursor += message.len();
        }
        Ok(cursor)
    }
}

struct RefusingAnswers(Shared);

impl Answers for RefusingAnswers {
    fn answer_all(&mut self, _buffer: &[u8], _out: &mut Vec<u8>) -> Result<usize, String> {
        self.0.lock().unwrap().answer_calls += 1;
        Err("nothing here frames".to_string())
    }
}

async fn converse_over(chunks: &[&[u8]]) -> Tally {
    let tally = tally();
    converse(
        ScriptedWire::new(chunks, &tally),
        LineAnswers(Arc::clone(&tally)),
        Duration::ZERO,
    )
    .await;
    snapshot(&tally)
}

async fn read_once(client: &mut DuplexStream, room: usize) -> Vec<u8> {
    let mut buffer = vec![0_u8; room + 1];
    let read = client.read(&mut buffer).await.unwrap();
    buffer.truncate(read);
    buffer
}

/// A client pairing replies to requests by position mis-pairs them if the order
/// slips, and neither side errors; a batch answered write-by-write instead
/// measures the mock's syscalls rather than the client's ceiling.
#[tokio::test]
async fn a_pipelined_batch_of_64_comes_back_as_one_payload_in_request_order() {
    let (mut client, sink) = tokio::io::duplex(PIPE_BYTES);
    tokio::spawn(converse(sink, LineAnswers(tally()), Duration::ZERO));

    client.write_all(&messages(64)).await.unwrap();
    let payload = read_once(&mut client, answers(64).len()).await;

    assert_eq!(payload, answers(64));
}

/// A read carries whatever the network handed over, not whole messages, and a
/// loop that answered the torn half would frame every later message wrong.
#[tokio::test]
async fn a_message_split_across_two_reads_is_answered_once_it_completes() {
    let whole = message(7);
    let (head, tail) = whole.split_at(2);

    let tally = converse_over(&[head, tail]).await;

    assert_eq!(tally.writes, vec![answer(7)]);
    assert_eq!(tally.answer_calls, 2);
}

#[tokio::test]
async fn a_read_that_completes_no_message_writes_nothing() {
    let whole = message(7);

    let tally = converse_over(&[&whole[..2]]).await;

    assert!(tally.writes.is_empty());
    assert_eq!(tally.answer_calls, 1);
}

/// Keep-alive is the measurement: a loop that hung up after answering would
/// have the loader reconnect per request and report a ceiling that is mostly
/// TCP handshakes. This returns at all only because the closed peer ends it.
#[tokio::test]
async fn the_connection_stays_open_across_reads_and_ends_when_the_peer_closes() {
    let tally = converse_over(&[&message(1), &message(2)]).await;

    assert_eq!(tally.writes, vec![answer(1), answer(2)]);
    assert_eq!(tally.reads_served, 2);
}

/// The one `setsockopt` this loop makes belongs to the read, not to the
/// messages in it: acknowledging per message is the per-document syscall that
/// took the Python sink from ~61,000 docs/s down to ~45,000.
#[tokio::test]
async fn a_read_is_acknowledged_and_answered_once_however_many_messages_it_carried() {
    let tally = converse_over(&[&messages(3)]).await;

    assert_eq!(tally.reads_acknowledged, 1);
    assert_eq!(tally.answer_calls, 1);
    assert_eq!(tally.writes, vec![answers(3)]);
}

/// A buffer the protocol cannot frame will not frame better with more bytes
/// appended to it; reading on would answer the rest of the connection from a
/// stream already out of step, which is worse than dropping it.
#[tokio::test]
async fn an_answering_half_that_refuses_the_buffer_ends_the_connection() {
    let shared = tally();
    let wire = ScriptedWire::new(&[&message(1), &message(2)], &shared);

    converse(wire, RefusingAnswers(Arc::clone(&shared)), Duration::ZERO).await;

    let tally = snapshot(&shared);
    assert_eq!(tally.reads_served, 1);
    assert!(tally.writes.is_empty());
}

/// The `Wire` impl that matters is the one for a real socket, and it is the one
/// the doubles above cannot reach: `after_read` there only counts. Emptying
/// `impl Wire for TcpStream` — the line that re-arms `TCP_QUICKACK` after every
/// read — left the whole suite green, and the cost of that is a 40 ms kernel
/// timer reported as the client's ceiling at the low rungs.
///
/// Read back at once, not after an exchange: the flag is deliberately not
/// sticky, and the kernel clears it again as the connection's ACK policy
/// evolves. That the loop calls `after_read` once per read is the assertion
/// above this one; that a socket's `after_read` re-arms the flag is this one,
/// and a mutation to either half now fails a test.
#[tokio::test]
async fn a_real_socket_re_arms_quickack_through_the_trait_the_loop_calls() {
    if !crate::tcp::HAS_QUICKACK {
        return;
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let connecting = tokio::spawn(async move { tokio::net::TcpStream::connect(address).await });
    let (served, _) = listener.accept().await.unwrap();
    let _client = connecting.await.unwrap().unwrap();
    let handle = socket2::SockRef::from(&served);
    handle.set_tcp_quickack(false).expect("quickack off");
    assert!(!handle.tcp_quickack().expect("the quickack flag"));

    Wire::after_read(&served);

    assert!(
        socket2::SockRef::from(&served)
            .tcp_quickack()
            .expect("the quickack flag"),
        "a socket's own after_read did not re-arm it"
    );
}
