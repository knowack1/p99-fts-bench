//! Just enough CQL native protocol v4 to answer a real driver.
//!
//! The mock needs a CQL endpoint that accepts INSERTs and discards them,
//! because the client's own ceiling cannot be measured against a real engine:
//! at ~11.7k docs/s the engine saturates first, so the number that comes back
//! is the engine's rather than the client's.
//!
//! **v4 and not v5.** The Python driver's `Cluster()` starts at v5, and v5
//! switches to CRC-checked segment framing the moment it sees READY — several
//! hundred lines of framing that nothing in the measurement depends on. So this
//! refuses anything but v4 with the exact `unsupported protocol version`
//! message the driver's own downgrade path looks for
//! (`cassandra/connection.py:1435`), and the driver negotiates down by itself.
//! The Rust driver `scyllarate` links speaks v4 outright. Uncompressed for the
//! same reason: SUPPORTED advertises no COMPRESSION, so a driver finds no
//! overlap and sends plain frames.
//!
//! Encoders, one frame splitter, and nothing else. What each frame *means* is
//! `crate::cql`'s business; this module only knows how to spell it.
use std::net::IpAddr;

use uuid::Uuid;

pub const HEADER_BYTES: usize = 9;
pub const REQUEST_VERSION: u8 = 4;
pub const RESPONSE_VERSION: u8 = 0x80 | REQUEST_VERSION;

/// The protocol's own ceiling is 256 MiB, and nothing this mock answers comes
/// near it. It is enforced rather than trusted because the alternative is
/// buffering whatever a confused client claims to be sending until the box runs
/// out of memory — a failure that would read as the loader stalling.
pub const MAX_FRAME_BYTES: usize = 256 << 20;

pub const OPCODE_ERROR: u8 = 0x00;
pub const OPCODE_STARTUP: u8 = 0x01;
pub const OPCODE_READY: u8 = 0x02;
pub const OPCODE_OPTIONS: u8 = 0x05;
pub const OPCODE_SUPPORTED: u8 = 0x06;
pub const OPCODE_QUERY: u8 = 0x07;
pub const OPCODE_RESULT: u8 = 0x08;
pub const OPCODE_PREPARE: u8 = 0x09;
pub const OPCODE_EXECUTE: u8 = 0x0A;
pub const OPCODE_REGISTER: u8 = 0x0B;
pub const OPCODE_BATCH: u8 = 0x0D;

pub const RESULT_VOID: i32 = 0x0001;
pub const RESULT_ROWS: i32 = 0x0002;
pub const RESULT_SET_KEYSPACE: i32 = 0x0003;
pub const RESULT_PREPARED: i32 = 0x0004;
pub const RESULT_SCHEMA_CHANGE: i32 = 0x0005;

pub const ERROR_PROTOCOL: i32 = 0x000A;
pub const ERROR_UNPREPARED: i32 = 0x2500;

pub const SCHEMA_TARGET_KEYSPACE: &str = "KEYSPACE";
pub const SCHEMA_TARGET_TABLE: &str = "TABLE";
pub const SCHEMA_CREATED: &str = "CREATED";
pub const SCHEMA_UPDATED: &str = "UPDATED";
pub const SCHEMA_DROPPED: &str = "DROPPED";

const FLAG_GLOBAL_TABLES_SPEC: i32 = 0x0001;

/// The driver keys its version downgrade off this substring, not off the error
/// code, so the wording is load-bearing rather than cosmetic.
pub fn unsupported_version_message(version: u8) -> String {
    format!(
        "Invalid or unsupported protocol version ({version}); \
         the lowest supported version is 3 and the greatest is 4"
    )
}

pub const TYPE_VARCHAR: u16 = 0x000D;
pub const TYPE_BIGINT: u16 = 0x0002;
pub const TYPE_UUID: u16 = 0x000C;
pub const TYPE_INET: u16 = 0x0010;
pub const TYPE_INT: u16 = 0x0009;
pub const TYPE_BOOLEAN: u16 = 0x0004;
const TYPE_LIST: u16 = 0x0020;
const TYPE_MAP: u16 = 0x0021;
const TYPE_SET: u16 = 0x0022;

/// A column's type as the protocol spells it: an id, and for a collection the
/// id of what it holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ColumnType {
    Simple(u16),
    SetOf(u16),
    ListOf(u16),
    MapOf(u16, u16),
}

impl ColumnType {
    pub const VARCHAR: Self = Self::Simple(TYPE_VARCHAR);
    pub const BIGINT: Self = Self::Simple(TYPE_BIGINT);
    pub const UUID: Self = Self::Simple(TYPE_UUID);
    pub const INET: Self = Self::Simple(TYPE_INET);
    pub const INT: Self = Self::Simple(TYPE_INT);
    pub const BOOLEAN: Self = Self::Simple(TYPE_BOOLEAN);
    pub const SET_VARCHAR: Self = Self::SetOf(TYPE_VARCHAR);
    pub const LIST_VARCHAR: Self = Self::ListOf(TYPE_VARCHAR);
    pub const MAP_VARCHAR_VARCHAR: Self = Self::MapOf(TYPE_VARCHAR, TYPE_VARCHAR);

    fn write(&self, out: &mut Vec<u8>) {
        match *self {
            Self::Simple(id) => put_u16(out, id),
            Self::SetOf(inner) => {
                put_u16(out, TYPE_SET);
                put_u16(out, inner);
            }
            Self::ListOf(inner) => {
                put_u16(out, TYPE_LIST);
                put_u16(out, inner);
            }
            Self::MapOf(key, value) => {
                put_u16(out, TYPE_MAP);
                put_u16(out, key);
                put_u16(out, value);
            }
        }
    }
}

/// One column of a result: a name beside its type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Column {
    pub name: String,
    pub column_type: ColumnType,
}

impl Column {
    pub fn new(name: impl Into<String>, column_type: ColumnType) -> Self {
        Self {
            name: name.into(),
            column_type,
        }
    }
}

/// A request frame, borrowing its body from the connection's read buffer: the
/// body of an EXECUTE is the whole document, and copying it to answer `VOID`
/// would put an allocation and a memcpy on the hottest path in the run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame<'a> {
    pub version: u8,
    pub flags: u8,
    pub stream: i16,
    pub opcode: u8,
    pub body: &'a [u8],
}

/// The frame starting at `offset`, and the offset after it.
///
/// `Ok(None)` when the buffer does not yet hold a whole frame, so a caller can
/// keep the partial bytes and read more. The caller advances once per read
/// rather than trimming per frame, which is why the cursor is returned instead
/// of the buffer being consumed here.
pub fn take_frame(buffer: &[u8], offset: usize) -> Result<Option<(Frame<'_>, usize)>, String> {
    if buffer.len().saturating_sub(offset) < HEADER_BYTES {
        return Ok(None);
    }
    let head = &buffer[offset..offset + HEADER_BYTES];
    let length = u32::from_be_bytes([head[5], head[6], head[7], head[8]]) as usize;
    if length > MAX_FRAME_BYTES {
        return Err(format!(
            "frame claims {length} bytes, over the {MAX_FRAME_BYTES}-byte ceiling"
        ));
    }
    let end = offset + HEADER_BYTES + length;
    if buffer.len() < end {
        return Ok(None);
    }
    let frame = Frame {
        version: head[0],
        flags: head[1],
        stream: i16::from_be_bytes([head[2], head[3]]),
        opcode: head[4],
        body: &buffer[offset + HEADER_BYTES..end],
    };
    Ok(Some((frame, end)))
}

pub fn frame(opcode: u8, stream: i16, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_BYTES + body.len());
    write_frame(&mut out, opcode, stream, body);
    out
}

pub fn write_frame(out: &mut Vec<u8>, opcode: u8, stream: i16, body: &[u8]) {
    write_frame_header(out, opcode, stream, body.len());
    out.extend_from_slice(body);
}

pub fn write_frame_header(out: &mut Vec<u8>, opcode: u8, stream: i16, body_len: usize) {
    out.push(RESPONSE_VERSION);
    out.push(0);
    out.extend_from_slice(&stream.to_be_bytes());
    out.push(opcode);
    out.extend_from_slice(&(body_len as u32).to_be_bytes());
}

/// The answer to a mutation, written whole: a 9-byte header and a 4-byte VOID.
///
/// Every accepted document in a run goes through here, which is the reason it
/// exists at all — the general path would allocate a body, and then copy it.
pub fn write_void_frame(out: &mut Vec<u8>, stream: i16) {
    write_frame_header(out, OPCODE_RESULT, stream, 4);
    out.extend_from_slice(&RESULT_VOID.to_be_bytes());
}

fn put_u16(out: &mut Vec<u8>, value: u16) {
    out.extend_from_slice(&value.to_be_bytes());
}

fn put_i32(out: &mut Vec<u8>, value: i32) {
    out.extend_from_slice(&value.to_be_bytes());
}

pub fn write_short_string(out: &mut Vec<u8>, value: &str) {
    put_u16(out, value.len() as u16);
    out.extend_from_slice(value.as_bytes());
}

pub fn short_string(value: &str) -> Vec<u8> {
    let mut out = Vec::new();
    write_short_string(&mut out, value);
    out
}

pub fn write_long_string(out: &mut Vec<u8>, value: &str) {
    put_i32(out, value.len() as i32);
    out.extend_from_slice(value.as_bytes());
}

pub fn write_short_bytes(out: &mut Vec<u8>, value: &[u8]) {
    put_u16(out, value.len() as u16);
    out.extend_from_slice(value);
}

/// One result cell. `None` is the protocol's null (-1 length), which is not the
/// same as a zero-length value and must not be spelled as one.
pub fn write_cell(out: &mut Vec<u8>, value: Option<&[u8]>) {
    match value {
        None => put_i32(out, -1),
        Some(bytes) => {
            put_i32(out, bytes.len() as i32);
            out.extend_from_slice(bytes);
        }
    }
}

pub fn read_short_bytes(body: &[u8], offset: usize) -> Option<(&[u8], usize)> {
    let (length, start) = read_u16(body, offset)?;
    let end = start + length as usize;
    if body.len() < end {
        return None;
    }
    Some((&body[start..end], end))
}

pub fn read_long_string(body: &[u8], offset: usize) -> Option<(String, usize)> {
    if body.len() < offset + 4 {
        return None;
    }
    let length = i32::from_be_bytes([
        body[offset],
        body[offset + 1],
        body[offset + 2],
        body[offset + 3],
    ]);
    let start = offset + 4;
    let end = start + length.max(0) as usize;
    if body.len() < end {
        return None;
    }
    Some((String::from_utf8_lossy(&body[start..end]).into_owned(), end))
}

fn read_u16(body: &[u8], offset: usize) -> Option<(u16, usize)> {
    if body.len() < offset + 2 {
        return None;
    }
    Some((
        u16::from_be_bytes([body[offset], body[offset + 1]]),
        offset + 2,
    ))
}

/// Statements in a BATCH frame: one type byte, then the count.
///
/// Read rather than assumed because an unlogged batch makes one operation carry
/// many rows, and a mock that counted the frame as one document would report a
/// rate the loader never offered.
pub fn batch_statement_count(body: &[u8]) -> u64 {
    match read_u16(body, 1) {
        Some((count, _)) => u64::from(count),
        None => 0,
    }
}

pub fn string_multimap(entries: &[(&str, &[&str])]) -> Vec<u8> {
    let mut out = Vec::new();
    put_u16(&mut out, entries.len() as u16);
    for (key, values) in entries {
        write_short_string(&mut out, key);
        put_u16(&mut out, values.len() as u16);
        for value in *values {
            write_short_string(&mut out, value);
        }
    }
    out
}

pub fn error_body(code: i32, message: &str) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, code);
    write_short_string(&mut out, message);
    out
}

/// Ask the driver to prepare again, carrying the id it asked about. The id is
/// part of the error body, not decoration: the driver keys its re-prepare on it.
pub fn unprepared_body(query_id: &[u8]) -> Vec<u8> {
    let mut out = error_body(ERROR_UNPREPARED, "unknown prepared statement");
    write_short_bytes(&mut out, query_id);
    out
}

pub fn unsupported_version_body(version: u8) -> Vec<u8> {
    error_body(ERROR_PROTOCOL, &unsupported_version_message(version))
}

pub fn void_result() -> Vec<u8> {
    RESULT_VOID.to_be_bytes().to_vec()
}

pub fn set_keyspace_result(keyspace: &str) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, RESULT_SET_KEYSPACE);
    write_short_string(&mut out, keyspace);
    out
}

/// What DDL answers with: CREATED/UPDATED/DROPPED against a target.
///
/// A KEYSPACE target carries only the keyspace; every other target carries a
/// name after it. Getting that wrong does not fail loudly — the driver reads
/// the next field as a string and blocks waiting for a schema agreement that
/// describes an object nobody named.
///
/// Index DDL reports `UPDATED TABLE`, not a target of its own: v4 has no INDEX
/// target, and Cassandra and ScyllaDB both announce a created index as a change
/// to the table it lives on.
pub fn schema_change_result(change: &str, target: &str, keyspace: &str, name: &str) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, RESULT_SCHEMA_CHANGE);
    write_short_string(&mut out, change);
    write_short_string(&mut out, target);
    write_short_string(&mut out, keyspace);
    if target != SCHEMA_TARGET_KEYSPACE {
        write_short_string(&mut out, name);
    }
    out
}

fn write_metadata(out: &mut Vec<u8>, keyspace: &str, table: &str, columns: &[Column]) {
    put_i32(out, FLAG_GLOBAL_TABLES_SPEC);
    put_i32(out, columns.len() as i32);
    write_short_string(out, keyspace);
    write_short_string(out, table);
    for column in columns {
        write_short_string(out, &column.name);
        column.column_type.write(out);
    }
}

/// One row of a result: one cell per declared column, `None` for a null.
pub type Row = Vec<Option<Vec<u8>>>;

pub fn rows_result(keyspace: &str, table: &str, columns: &[Column], rows: &[Row]) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, RESULT_ROWS);
    write_metadata(&mut out, keyspace, table, columns);
    put_i32(&mut out, rows.len() as i32);
    for row in rows {
        for value in row {
            write_cell(&mut out, value.as_deref());
        }
    }
    out
}

/// A PREPARED result with no result metadata — an INSERT returns no rows.
///
/// v4 puts the partition-key indexes in the bind metadata; an empty list says
/// the mock is not claiming to know which markers are the key, which costs the
/// driver only its token-aware routing hint.
pub fn prepared_result(
    query_id: &[u8],
    keyspace: &str,
    table: &str,
    bind_columns: &[Column],
) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, RESULT_PREPARED);
    write_short_bytes(&mut out, query_id);
    put_i32(&mut out, FLAG_GLOBAL_TABLES_SPEC);
    put_i32(&mut out, bind_columns.len() as i32);
    put_i32(&mut out, 0);
    write_short_string(&mut out, keyspace);
    write_short_string(&mut out, table);
    for column in bind_columns {
        write_short_string(&mut out, &column.name);
        column.column_type.write(&mut out);
    }
    put_i32(&mut out, 0);
    put_i32(&mut out, 0);
    out
}

pub fn varchar_value(value: &str) -> Vec<u8> {
    value.as_bytes().to_vec()
}

pub fn uuid_value(value: Uuid) -> Vec<u8> {
    value.as_bytes().to_vec()
}

/// 4 or 16 bytes, as the protocol's `inet` wants them.
///
/// Both families, because the address comes from the accepted socket rather
/// than from configuration: a mock bound on an IPv6 private address would
/// otherwise drop every connection while looking like a client ceiling.
/// An address that will not parse answers as `0.0.0.0` rather than as a
/// zero-length cell: the driver reads this column as an `inet`, and a cell of
/// no bytes is a decode error that kills the control connection during the
/// handshake — which looks exactly like the mock refusing load.
pub fn inet_value(address: &str) -> Vec<u8> {
    match address.parse::<IpAddr>() {
        Ok(IpAddr::V4(v4)) => v4.octets().to_vec(),
        Ok(IpAddr::V6(v6)) => v6.octets().to_vec(),
        Err(_) => vec![0, 0, 0, 0],
    }
}

pub fn varchar_set_value(values: &[&str]) -> Vec<u8> {
    let mut out = Vec::new();
    put_i32(&mut out, values.len() as i32);
    for value in values {
        write_long_string(&mut out, value);
    }
    out
}

#[cfg(test)]
#[path = "cql_wire_tests.rs"]
mod tests;
