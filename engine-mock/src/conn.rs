//! One connection's loop, shared by both wire protocols: read, answer
//! everything that read completed, write once.
//!
//! The loop is the same for HTTP and for CQL because the rule it enforces is
//! the same, and it is the rule the whole instrument rests on: **one write per
//! read, never one per request**. The loaders hold many requests outstanding on
//! few connections, so a read commonly carries several whole messages, and
//! answering them one at a time puts a `recvfrom`, a `sendto` and a
//! `setsockopt` between the client and its own ceiling — per document, once a
//! batch size of 1 makes a request a document. Measured on the Python sink this
//! replaces, draining per read took 603,782 syscalls per 100,000 documents down
//! to 306,759 and the HTTP half from ~45,000 to ~61,000 docs/s.
//!
//! `Wire` exists so the loop can be driven by an in-memory pipe in a test and
//! by a socket in a run. A socket acknowledges after every read and an
//! in-memory pipe has nothing to acknowledge, which is the only difference
//! between them.
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::tcp;

pub const READ_CHUNK_BYTES: usize = 1 << 16;

/// Something a connection can be held on, and what to do to it after a read.
pub trait Wire: AsyncRead + AsyncWrite + Unpin + Send {
    fn after_read(&self) {}
}

impl Wire for TcpStream {
    fn after_read(&self) {
        tcp::acknowledge_now(self);
    }
}

impl Wire for tokio::io::DuplexStream {}

/// A protocol's answering half: given the bytes read so far, append the answer
/// to every whole message in them and say how many bytes were consumed.
///
/// Returning the cursor rather than consuming the buffer is what lets the
/// caller trim once per read instead of once per message.
pub trait Answers: Send {
    fn answer_all(&mut self, buffer: &[u8], out: &mut Vec<u8>) -> Result<usize, String>;
}

pub async fn converse<S: Wire, A: Answers>(mut stream: S, mut answers: A, delay: Duration) {
    let mut buffer = Vec::with_capacity(READ_CHUNK_BYTES);
    let mut out = Vec::with_capacity(READ_CHUNK_BYTES);
    let mut chunk = vec![0_u8; READ_CHUNK_BYTES];
    loop {
        let read = match stream.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(read) => read,
        };
        stream.after_read();
        buffer.extend_from_slice(&chunk[..read]);
        out.clear();
        let consumed = match answers.answer_all(&buffer, &mut out) {
            Ok(consumed) => consumed,
            Err(_) => return,
        };
        consume(&mut buffer, consumed);
        if out.is_empty() {
            continue;
        }
        if !delay.is_zero() {
            tokio::time::sleep(delay).await;
        }
        if stream.write_all(&out).await.is_err() {
            return;
        }
    }
}

/// The common case is a buffer with nothing left in it, and clearing it moves
/// no bytes; only a message torn across two reads pays for the shift.
fn consume(buffer: &mut Vec<u8>, consumed: usize) {
    if consumed == buffer.len() {
        buffer.clear();
        return;
    }
    buffer.drain(..consumed);
}

#[cfg(test)]
#[path = "conn_tests.rs"]
mod tests;
