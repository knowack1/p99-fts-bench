//! HTTP/1.1 framing for the mock endpoints, with no opinion about what they
//! answer.
//!
//! Two endpoints speak HTTP and they are shaped by two different engines: the
//! OpenSearch-shaped one in `crate::opensearch` and the vector-store-shaped one
//! in `crate::vstore`. The wire between them is the same — answer every whole
//! request the last read completed, write the replies once, keep the connection
//! alive — so it lives here rather than once per endpoint, and a routing table
//! is whatever type implements `Routes`.
//!
//! Keep-alive and **one write per read** are not incidental. The loaders hold
//! many requests outstanding on few connections, so a read can carry several
//! whole requests, and answering them one at a time puts a `recvfrom`, a
//! `sendto` and a `setsockopt` between the client and its own ceiling — per
//! document, once `osrate --batch-size 1` makes a request a document.
//!
//! That cost was measured on the Python sink this replaces. Reading one request
//! per iteration cost 6.04 syscalls per document against 0.36 for the CQL half,
//! which has always drained its whole buffer per read; draining per read
//! removed the per-request buffer churn outright — 603,782 syscalls per 100,000
//! documents became 306,759, and the sink went from ~45,000 to ~61,000 docs/s
//! on one thread. `answers_for` therefore mirrors `crate::cql::answers_for`
//! deliberately, down to the name.
//!
//! What remains is HTTP/1.1 itself rather than this loop. Keep-alive is serial
//! reuse: each of `--concurrency` sockets carries one outstanding request at a
//! time, so a read cannot hold more than one and there is nothing left to
//! batch. CQL multiplexes many statements over one connection by stream id,
//! which is why its reads take ~14 frames at once. Closing that gap is what the
//! tokio worker pool is for: the Python sink had one thread to answer every
//! connection, and this one answers each connection on whichever worker is
//! free.
use std::borrow::Cow;
use std::sync::Arc;

use crate::conn;
use crate::server::Connection;

pub const HEADER_TERMINATOR: &[u8] = b"\r\n\r\n";
pub const MAX_HEADER_BYTES: usize = 1 << 16;
/// Far above any `_bulk` a loader sends — the largest measured is a few
/// megabytes at 1024 documents a request — and far below a number that can
/// exhaust the box.
pub const MAX_BODY_BYTES: usize = 64 << 20;

/// The Python sink this replaces knew two reason phrases and spelled every
/// other status `OK`, including the 503 its own unallocated-primary answer
/// uses. No client reads the phrase, so that never broke anything; it is
/// spelled correctly here because there is no reason not to.
fn reason(status: u16) -> &'static str {
    match status {
        404 => "Not Found",
        503 => "Service Unavailable",
        _ => "OK",
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request<'a> {
    pub method: &'a str,
    pub path: Cow<'a, str>,
    pub body: &'a [u8],
}

/// The request line, borrowed from the read buffer.
///
/// Borrowed rather than copied because at a batch size of 1 a request is a
/// document: a `String` per path is an allocation per document, on every worker
/// at once. A path that is not UTF-8 cannot be borrowed as `&str` and is copied
/// lossily instead — no client sends one, and the copy only pays on the way to
/// a 404.
fn request_parts(head: &[u8]) -> Result<(&str, Cow<'_, str>), String> {
    let line = head.split(|byte| *byte == b'\r').next().unwrap_or(head);
    let mut fields = line.split(|byte| *byte == b' ');
    let (Some(method), Some(path)) = (fields.next(), fields.next()) else {
        return Err(format!(
            "malformed request line: {:?}",
            String::from_utf8_lossy(line)
        ));
    };
    let method = std::str::from_utf8(method).map_err(|_| {
        format!(
            "malformed request line: {:?}",
            String::from_utf8_lossy(line)
        )
    })?;
    Ok((method, String::from_utf8_lossy(path)))
}

impl Request<'_> {
    /// The path with any query string removed, which is what every route in
    /// both tables matches on.
    pub fn route(&self) -> &str {
        let path = self.path.as_ref();
        match path.find('?') {
            Some(cut) => &path[..cut],
            None => path,
        }
    }
}

/// A reply body. Shared rather than owned wherever one is answered more than
/// once: the bulk reply for a given item count is built on its first use and
/// then only copied into the write buffer.
#[derive(Debug, Clone)]
pub enum Body {
    Empty,
    Owned(Vec<u8>),
    Shared(Arc<[u8]>),
}

impl Body {
    pub fn as_bytes(&self) -> &[u8] {
        match self {
            Self::Empty => &[],
            Self::Owned(bytes) => bytes,
            Self::Shared(bytes) => bytes,
        }
    }
}

impl From<Vec<u8>> for Body {
    fn from(bytes: Vec<u8>) -> Self {
        Self::Owned(bytes)
    }
}

pub type Answer = (u16, Body);

/// A routing table, plus whatever it wants to keep per connection.
///
/// `Session` is why this is a trait with an associated type rather than a
/// closure: the OpenSearch table caches one bulk reply per item count and holds
/// the counter lane it adds to, and a cache shared between connections would
/// need a lock on the path that answers every document. Per connection it needs
/// none.
pub trait Routes: Send + Sync + 'static {
    type Session: Send;

    fn session(&self, connection: &Connection) -> Self::Session;

    fn respond(&self, request: &Request<'_>, session: &mut Self::Session) -> Answer;

    /// A request the framing could not read ends the connection. Recording it
    /// is what keeps that from being invisible: the loader would report a
    /// dropped connection, and without this the mock's own artifact would not
    /// mention the request that dropped it.
    fn note_malformed(&self) {}
}

/// Written straight into the connection's output buffer, never through a
/// `String` first: at a batch size of 1 a reply is a document, and an
/// allocation per document is one the mock would be spending on the axis being
/// measured.
pub fn write_response(out: &mut Vec<u8>, status: u16, body: &[u8]) {
    use std::io::Write;
    let _ = write!(
        out,
        "HTTP/1.1 {status} {}\r\n\
         Content-Type: application/json; charset=UTF-8\r\n\
         Content-Length: {}\r\n\
         Connection: keep-alive\r\n\r\n",
        reason(status),
        body.len()
    );
    out.extend_from_slice(body);
}

pub fn http_response(status: u16, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 128);
    write_response(&mut out, status, body);
    out
}

/// Only `Content-Length`. A `Transfer-Encoding: chunked` body is therefore read
/// as no body at all and the chunk-size line is then read as the next request
/// line, which ends the connection — the same thing the Python sink did with
/// it, and honest: no client here sends one, and silently accepting chunked
/// would make a client change that started streaming bodies invisible.
fn content_length(head: &[u8]) -> Option<usize> {
    head.split(|byte| *byte == b'\n')
        .skip(1)
        .filter_map(|line| {
            let cut = line.iter().position(|byte| *byte == b':')?;
            let (name, value) = line.split_at(cut);
            std::str::from_utf8(name)
                .ok()?
                .eq_ignore_ascii_case("content-length")
                .then(|| std::str::from_utf8(&value[1..]).ok()?.trim().parse().ok())?
        })
        .next()
}

fn refuse_an_unbounded_head(size: usize) -> Result<(), String> {
    if size > MAX_HEADER_BYTES {
        return Err(format!(
            "request head over {MAX_HEADER_BYTES} bytes ({size} buffered)"
        ));
    }
    Ok(())
}

fn find(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    haystack
        .get(from..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|at| at + from)
}

/// The request starting at `offset`, and the offset after it.
///
/// `Ok(None)` when the buffer does not yet hold a whole request, so a caller
/// can keep the partial bytes and read more. The caller advances once per read
/// rather than trimming per request, which is why the cursor is returned
/// instead of the buffer being consumed here.
pub fn take_request(buffer: &[u8], offset: usize) -> Result<Option<(Request<'_>, usize)>, String> {
    let Some(head_end) = find(buffer, HEADER_TERMINATOR, offset) else {
        refuse_an_unbounded_head(buffer.len().saturating_sub(offset))?;
        return Ok(None);
    };
    refuse_an_unbounded_head(head_end - offset)?;
    let head = &buffer[offset..head_end];
    let (method, path) = request_parts(head)?;
    let body_start = head_end + HEADER_TERMINATOR.len();
    let length = content_length(head).unwrap_or(0);
    refuse_an_unbounded_body(length)?;
    let end = body_start + length;
    if buffer.len() < end {
        return Ok(None);
    }
    Ok(Some((
        Request {
            method,
            path,
            body: &buffer[body_start..end],
        },
        end,
    )))
}

/// The head is bounded, the body was not. In Python an absurd `Content-Length`
/// grew one bytearray; here it is a reservation the allocator may not survive,
/// and an out-of-memory abort takes every connection with it rather than the
/// one that asked for it.
fn refuse_an_unbounded_body(length: usize) -> Result<(), String> {
    if length > MAX_BODY_BYTES {
        return Err(format!(
            "request body over {MAX_BODY_BYTES} bytes ({length} claimed)"
        ));
    }
    Ok(())
}

/// Every whole request in the buffer, answered into `out`, with the rest left
/// behind. Returns how many bytes of the buffer were consumed.
///
/// Replies are concatenated and written once per read rather than per request,
/// for the reason in the module docstring. They are appended in request order,
/// which HTTP pipelining requires: a client pairing replies to requests by
/// position would otherwise mis-pair them without either side erroring.
pub fn answers_for<R: Routes>(
    buffer: &[u8],
    routes: &R,
    session: &mut R::Session,
    out: &mut Vec<u8>,
) -> Result<usize, String> {
    let mut cursor = 0;
    while let Some((request, next)) = take_request(buffer, cursor)? {
        let (status, body) = routes.respond(&request, session);
        write_response(out, status, body.as_bytes());
        cursor = next;
    }
    Ok(cursor)
}

/// The HTTP half of `conn::Answers`: a routing table plus the session that
/// table keeps for this one connection.
pub struct Answering<R: Routes> {
    routes: Arc<R>,
    session: R::Session,
}

impl<R: Routes> Answering<R> {
    pub fn new(routes: Arc<R>, connection: &Connection) -> Self {
        let session = routes.session(connection);
        Self { routes, session }
    }
}

impl<R: Routes> conn::Answers for Answering<R> {
    fn answer_all(&mut self, buffer: &[u8], out: &mut Vec<u8>) -> Result<usize, String> {
        answers_for(buffer, self.routes.as_ref(), &mut self.session, out).inspect_err(|_| {
            self.routes.note_malformed();
        })
    }
}

#[cfg(test)]
#[path = "http_wire_tests.rs"]
mod tests;
