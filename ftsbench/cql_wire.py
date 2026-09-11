"""Just enough CQL native protocol v4 to answer the real driver.

`ftsbench.null_sink` needs a CQL endpoint that accepts INSERTs and discards
them, because the client's own ceiling cannot be measured against a real engine:
at ~11.7k docs/s the engine saturates first, so the number that comes back is
the engine's rather than the client's (`BUILD-RATE-MATRIX-PLAN.md`, Phase 0).

**v4 and not v5.** `Cluster()` starts at v5, and v5 switches to CRC-checked
segment framing the moment it sees READY — several hundred lines of framing that
nothing in the measurement depends on. So this refuses anything but v4 with the
exact `unsupported protocol version` message the driver's own downgrade path
looks for (`cassandra/connection.py:1435`), and the driver negotiates down by
itself. Uncompressed for the same reason: SUPPORTED advertises no COMPRESSION,
so the driver finds no overlap and sends plain frames.

Encoders and one frame splitter, nothing else. What each frame *means* is
`ftsbench.null_sink_cql`'s business; this module only knows how to spell it.
"""
from __future__ import annotations

import ipaddress
import struct
import uuid
from dataclasses import dataclass

HEADER = struct.Struct(">BBhBI")
REQUEST_VERSION = 4
RESPONSE_VERSION = 0x80 | REQUEST_VERSION

OPCODE_ERROR = 0x00
OPCODE_STARTUP = 0x01
OPCODE_READY = 0x02
OPCODE_OPTIONS = 0x05
OPCODE_SUPPORTED = 0x06
OPCODE_QUERY = 0x07
OPCODE_RESULT = 0x08
OPCODE_PREPARE = 0x09
OPCODE_EXECUTE = 0x0A
OPCODE_REGISTER = 0x0B
OPCODE_BATCH = 0x0D

RESULT_VOID = 0x0001
RESULT_ROWS = 0x0002
RESULT_SET_KEYSPACE = 0x0003
RESULT_PREPARED = 0x0004
RESULT_SCHEMA_CHANGE = 0x0005

ERROR_PROTOCOL = 0x000A
ERROR_UNPREPARED = 0x2500

SCHEMA_TARGET_KEYSPACE = "KEYSPACE"
SCHEMA_TARGET_TABLE = "TABLE"
SCHEMA_CREATED = "CREATED"
SCHEMA_UPDATED = "UPDATED"
SCHEMA_DROPPED = "DROPPED"

# The driver keys its version downgrade off this substring, not off the error
# code, so the wording is load-bearing rather than cosmetic.
UNSUPPORTED_VERSION_MESSAGE = (
    "Invalid or unsupported protocol version ({version}); "
    "the lowest supported version is 3 and the greatest is 4")

_FLAG_GLOBAL_TABLES_SPEC = 0x0001

TYPE_VARCHAR = struct.pack(">H", 0x000D)
TYPE_BIGINT = struct.pack(">H", 0x0002)
TYPE_UUID = struct.pack(">H", 0x000C)
TYPE_INET = struct.pack(">H", 0x0010)
TYPE_SET_VARCHAR = struct.pack(">H", 0x0022) + TYPE_VARCHAR
TYPE_INT = struct.pack(">H", 0x0009)
TYPE_BOOLEAN = struct.pack(">H", 0x0004)
TYPE_LIST_VARCHAR = struct.pack(">H", 0x0020) + TYPE_VARCHAR
TYPE_MAP_VARCHAR_VARCHAR = struct.pack(">H", 0x0021) + TYPE_VARCHAR + TYPE_VARCHAR


@dataclass(frozen=True)
class Frame:
    version: int
    flags: int
    stream: int
    opcode: int
    body: bytes


@dataclass(frozen=True)
class Column:
    """One column of a result, name beside its already-encoded type option."""

    name: str
    type_option: bytes


def take_frame(buffer: bytes, offset: int) -> tuple[Frame | None, int]:
    """The frame starting at `offset`, and the offset after it.

    Returns `(None, offset)` when the buffer does not yet hold a whole frame,
    so a caller can keep the partial bytes and read more. The caller advances
    once per read rather than trimming per frame, which is why the cursor is
    returned instead of the buffer being consumed here.
    """
    if len(buffer) - offset < HEADER.size:
        return None, offset
    version, flags, stream, opcode, length = HEADER.unpack_from(buffer, offset)
    end = offset + HEADER.size + length
    if len(buffer) < end:
        return None, offset
    return Frame(version, flags, stream, opcode,
                 bytes(buffer[offset + HEADER.size:end])), end


def frame(opcode: int, stream: int, body: bytes) -> bytes:
    return HEADER.pack(RESPONSE_VERSION, 0, stream, opcode, len(body)) + body


def short_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def long_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">i", len(encoded)) + encoded


def short_bytes(value: bytes) -> bytes:
    return struct.pack(">H", len(value)) + value


def cell(value: bytes | None) -> bytes:
    """One result cell. `None` is the protocol's null (-1 length), which is not
    the same as a zero-length value and must not be spelled as one."""
    if value is None:
        return struct.pack(">i", -1)
    return struct.pack(">i", len(value)) + value


def read_short_bytes(body: bytes, offset: int = 0) -> tuple[bytes, int]:
    length, = struct.unpack_from(">H", body, offset)
    start = offset + 2
    return body[start:start + length], start + length


def read_long_string(body: bytes, offset: int = 0) -> tuple[str, int]:
    (length,) = struct.unpack_from(">i", body, offset)
    start = offset + 4
    return body[start:start + length].decode("utf-8", "replace"), start + length


def batch_statement_count(body: bytes) -> int:
    """Statements in a BATCH frame: one type byte, then the count.

    Read rather than assumed because `--unlogged-batch-rows` makes one operation
    carry many rows, and a sink that counted the frame as one document would
    report a rate the loader never offered.
    """
    if len(body) < 3:
        return 0
    return struct.unpack_from(">H", body, 1)[0]


def string_multimap(entries: dict[str, list[str]]) -> bytes:
    parts = [struct.pack(">H", len(entries))]
    for key, values in entries.items():
        parts.append(short_string(key))
        parts.append(struct.pack(">H", len(values)))
        parts.extend(short_string(value) for value in values)
    return b"".join(parts)


def error_body(code: int, message: str) -> bytes:
    return struct.pack(">i", code) + short_string(message)


def unprepared_body(query_id: bytes) -> bytes:
    """Ask the driver to prepare again, carrying the id it asked about.

    The id is part of the error body, not decoration: the driver keys its
    re-prepare on it.
    """
    return (struct.pack(">i", ERROR_UNPREPARED)
            + short_string("unknown prepared statement")
            + short_bytes(query_id))


def unsupported_version_body(version: int) -> bytes:
    return error_body(ERROR_PROTOCOL,
                      UNSUPPORTED_VERSION_MESSAGE.format(version=version))


def void_result() -> bytes:
    return struct.pack(">i", RESULT_VOID)


def set_keyspace_result(keyspace: str) -> bytes:
    return struct.pack(">i", RESULT_SET_KEYSPACE) + short_string(keyspace)


def schema_change_result(change: str, target: str, keyspace: str,
                         name: str = "") -> bytes:
    """What DDL answers with: CREATED/UPDATED/DROPPED against a target.

    A KEYSPACE target carries only the keyspace; every other target carries a
    name after it. Getting that wrong does not fail loudly — the driver reads
    the next field as a string and blocks waiting for a schema agreement that
    describes an object nobody named.

    Index DDL reports `UPDATED TABLE`, not a target of its own: v4 has no
    INDEX target, and Cassandra and ScyllaDB both announce a created index as a
    change to the table it lives on.
    """
    parts = [struct.pack(">i", RESULT_SCHEMA_CHANGE), short_string(change),
             short_string(target), short_string(keyspace)]
    if target != SCHEMA_TARGET_KEYSPACE:
        parts.append(short_string(name))
    return b"".join(parts)


def _metadata(keyspace: str, table: str, columns: list[Column]) -> bytes:
    parts = [struct.pack(">ii", _FLAG_GLOBAL_TABLES_SPEC, len(columns)),
             short_string(keyspace), short_string(table)]
    for column in columns:
        parts.append(short_string(column.name))
        parts.append(column.type_option)
    return b"".join(parts)


def rows_result(keyspace: str, table: str, columns: list[Column],
                rows: list[list[bytes | None]]) -> bytes:
    parts = [struct.pack(">i", RESULT_ROWS),
             _metadata(keyspace, table, columns),
             struct.pack(">i", len(rows))]
    for row in rows:
        parts.extend(cell(value) for value in row)
    return b"".join(parts)


def prepared_result(query_id: bytes, keyspace: str, table: str,
                    bind_columns: list[Column]) -> bytes:
    """A PREPARED result with no result metadata — an INSERT returns no rows.

    v4 puts the partition-key indexes in the bind metadata; an empty list says
    the sink is not claiming to know which markers are the key, which costs the
    driver only its token-aware routing hint.
    """
    return b"".join([
        struct.pack(">i", RESULT_PREPARED),
        short_bytes(query_id),
        struct.pack(">iii", _FLAG_GLOBAL_TABLES_SPEC, len(bind_columns), 0),
        short_string(keyspace),
        short_string(table),
        *(short_string(column.name) + column.type_option
          for column in bind_columns),
        struct.pack(">ii", 0, 0),
    ])


def varchar_value(value: str) -> bytes:
    return value.encode("utf-8")


def uuid_value(value: uuid.UUID) -> bytes:
    return value.bytes


def inet_value(address: str) -> bytes:
    """4 or 16 bytes, as the protocol's `inet` wants them.

    Both families, because the address comes from the accepted socket rather
    than from configuration: a sink bound on an IPv6 private address would
    otherwise drop every connection while looking like a client ceiling.
    """
    return ipaddress.ip_address(address).packed


def varchar_set_value(values: list[str]) -> bytes:
    return struct.pack(">i", len(values)) + b"".join(
        long_string(value) for value in values)
