"""A CQL endpoint that connects the real driver and discards every write.

Answers exactly the traffic `ftsbench.scylla_load` produces: the handshake,
`USE`, the `SELECT release_version FROM system.local` in `engine_version`, the
`session.prepare` of the INSERT, and then `execute_async` per row. The control
connection's own topology and schema queries are answered too, because
`Cluster.connect` does not return until they have been.

**It answers, it does not emulate.** Nothing is stored, no consistency is
enforced, `system_schema` comes back empty, and there is no token map — an
EXECUTE is a Void result and 13 bytes on the wire. That is the point: the sink
must not be the bottleneck, or the ceiling that comes back is the sink's.

**One document per EXECUTE.** `scylla_load` issues one prepared statement per
row and `--batch-size` is a client-side loop window, so the frame count *is* the
document count. A BATCH frame is the exception and its statement count is read
from the frame rather than assumed, because `--unlogged-batch-rows` puts many
rows in one operation.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass

from . import cql_wire, sink_tcp
from .cql_wire import Column
from .sink_counters import AcceptedWork

READ_CHUNK_BYTES = 1 << 16
SUPPORTED_OPTIONS = {
    "CQL_VERSION": ["3.3.1"],
    "COMPRESSION": [],
    "PROTOCOL_VERSIONS": ["3/v3", "4/v4"],
}
SYSTEM_KEYSPACE = "system"
PEERS_TABLE = "peers"
LOCAL_TABLE = "local"
DEFAULT_KEYSPACE = "wiki"

# The campaign's table, so a prepared INSERT binds the types the loader actually
# sends (scylladb/schema.cql). An unlisted column binds as varchar, which is
# what a text-only stub can honestly claim to know.
TABLE_COLUMN_TYPES = {
    "article_id": cql_wire.TYPE_UUID,
    "page_id": cql_wire.TYPE_BIGINT,
    "title": cql_wire.TYPE_VARCHAR,
    "body": cql_wire.TYPE_VARCHAR,
}
PEERS_COLUMN_TYPES = {
    "peer": cql_wire.TYPE_INET,
    "data_center": cql_wire.TYPE_VARCHAR,
    "host_id": cql_wire.TYPE_UUID,
    "rack": cql_wire.TYPE_VARCHAR,
    "release_version": cql_wire.TYPE_VARCHAR,
    "rpc_address": cql_wire.TYPE_INET,
    "schema_version": cql_wire.TYPE_UUID,
    "tokens": cql_wire.TYPE_SET_VARCHAR,
    "preferred_ip": cql_wire.TYPE_INET,
}
# A Rows result must declare at least one column even when it carries no rows:
# the driver falls back to the statement's cached metadata when the result's
# column list is empty, and for a one-off SELECT there is none, so a
# zero-column answer raises inside its row parser rather than reading as "no
# rows". Everything the sink does not model — all of `system_schema` — comes
# back as this one column and no rows.
PLACEHOLDER_COLUMN = "key"
INSERT_COLUMNS_RE = re.compile(r"insert\s+into\s+\S+\s*\(([^)]*)\)", re.I)
PREDICATE_COLUMN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\?")
SELECT_LIST_RE = re.compile(r"select\s+(.*?)\s+from\s+", re.I | re.S)
USE_KEYSPACE_RE = re.compile(r"use\s+\"?([A-Za-z0-9_]+)\"?", re.I)


@dataclass(frozen=True)
class NodeIdentity:
    """What the sink claims to be, stable for the life of the process.

    Stable because it must be: a `host_id` that changed between connections
    would make the driver believe it had found two nodes on one endpoint, log a
    duplicate-host warning and drop one of them.
    """

    host_id: uuid.UUID
    schema_version: uuid.UUID
    cluster_name: str = "null-sink"
    release_version: str = "6.2.0-null-sink"
    data_center: str = "datacenter1"
    rack: str = "rack1"
    partitioner: str = "org.apache.cassandra.dht.Murmur3Partitioner"


def new_identity() -> NodeIdentity:
    return NodeIdentity(host_id=uuid.uuid4(), schema_version=uuid.uuid4())


def local_row_values(identity: NodeIdentity,
                     address: str) -> dict[str, tuple[bytes, bytes]]:
    """The `system.local` row, column by column, with its wire type.

    `rpc_address` is the address the client reached this sink on, taken from the
    accepted socket rather than configured: the driver adds a host for whatever
    this row advertises, so a fixed 127.0.0.1 here would send a loader on the
    harness box off to connect to itself.
    """
    return {
        "key": (cql_wire.TYPE_VARCHAR, cql_wire.varchar_value("local")),
        "bootstrapped": (cql_wire.TYPE_VARCHAR,
                         cql_wire.varchar_value("COMPLETED")),
        "cluster_name": (cql_wire.TYPE_VARCHAR,
                         cql_wire.varchar_value(identity.cluster_name)),
        "cql_version": (cql_wire.TYPE_VARCHAR,
                        cql_wire.varchar_value(SUPPORTED_OPTIONS["CQL_VERSION"][0])),
        "data_center": (cql_wire.TYPE_VARCHAR,
                        cql_wire.varchar_value(identity.data_center)),
        "rack": (cql_wire.TYPE_VARCHAR, cql_wire.varchar_value(identity.rack)),
        "native_protocol_version": (cql_wire.TYPE_VARCHAR,
                                    cql_wire.varchar_value("4")),
        "partitioner": (cql_wire.TYPE_VARCHAR,
                        cql_wire.varchar_value(identity.partitioner)),
        "release_version": (cql_wire.TYPE_VARCHAR,
                            cql_wire.varchar_value(identity.release_version)),
        "host_id": (cql_wire.TYPE_UUID, cql_wire.uuid_value(identity.host_id)),
        "schema_version": (cql_wire.TYPE_UUID,
                           cql_wire.uuid_value(identity.schema_version)),
        "broadcast_address": (cql_wire.TYPE_INET, cql_wire.inet_value(address)),
        "listen_address": (cql_wire.TYPE_INET, cql_wire.inet_value(address)),
        "rpc_address": (cql_wire.TYPE_INET, cql_wire.inet_value(address)),
        "tokens": (cql_wire.TYPE_SET_VARCHAR, cql_wire.varchar_set_value(["0"])),
    }


def selected_names(query: str, available: list[str]) -> list[str]:
    """The SELECT list, or every available column for `SELECT *`.

    Parsed rather than pattern-matched per query because the driver asks for
    five different column subsets of `system.local` depending on what it is
    refreshing, and a row whose columns do not match the request is read by
    position — which silently hands `cluster_name` the value of `tokens`.
    """
    match = SELECT_LIST_RE.search(query)
    if match is None or match.group(1).strip() == "*":
        return available
    return [name.strip() for name in match.group(1).split(",")]


def table_rows(query: str, values: dict[str, tuple[bytes, bytes]],
               present: bool) -> tuple[list[Column], list[list[bytes | None]]]:
    names = selected_names(query, list(values))
    columns = [Column(name, values.get(name, (cql_wire.TYPE_VARCHAR, b""))[0])
               for name in names]
    if not present:
        return columns, []
    return columns, [[values.get(name, (None, None))[1] for name in names]]


def insert_bind_names(query: str) -> list[str]:
    match = INSERT_COLUMNS_RE.search(query)
    if match is None:
        return PREDICATE_COLUMN_RE.findall(query)
    return [name.strip() for name in match.group(1).split(",")]


def bind_columns(query: str) -> list[Column]:
    """One bind column per `?`, typed from the campaign's table where possible.

    The type decides how the driver serialises the parameter, so getting
    `page_id` wrong would not merely mislabel the column — it would change the
    bytes the client spends CPU producing, which is the quantity being measured.
    """
    names = insert_bind_names(query)
    markers = query.count("?")
    padded = names[:markers] + [f"bind{i}" for i in range(len(names), markers)]
    return [Column(name, TABLE_COLUMN_TYPES.get(name, cql_wire.TYPE_VARCHAR))
            for name in padded]


def query_id(query: str) -> bytes:
    return hashlib.md5(query.encode("utf-8")).digest()


def statement_target(query: str) -> tuple[str, str]:
    match = re.search(r"(?:insert\s+into|update|from)\s+([A-Za-z0-9_.\"]+)",
                      query, re.I)
    target = (match.group(1) if match else DEFAULT_KEYSPACE).replace('"', "")
    if "." in target:
        keyspace, _, table = target.partition(".")
        return keyspace, table
    return DEFAULT_KEYSPACE, target


def empty_rows_result(query: str) -> bytes:
    """No rows, with the requested columns declared so the answer is readable."""
    names = selected_names(query, [PLACEHOLDER_COLUMN])
    return cql_wire.rows_result(
        SYSTEM_KEYSPACE, "unmodelled",
        [Column(name, cql_wire.TYPE_VARCHAR) for name in names], [])


def answer_query(query: str, identity: NodeIdentity, address: str) -> bytes:
    lowered = query.strip().lower()
    keyspace = USE_KEYSPACE_RE.match(query.strip())
    if lowered.startswith("use ") and keyspace is not None:
        return cql_wire.set_keyspace_result(keyspace.group(1))
    if "system.local" in lowered:
        columns, rows = table_rows(query, local_row_values(identity, address),
                                   present=True)
        return cql_wire.rows_result(SYSTEM_KEYSPACE, LOCAL_TABLE, columns, rows)
    if "system.peers" in lowered:
        columns, rows = table_rows(
            query, {name: (option, b"")
                    for name, option in PEERS_COLUMN_TYPES.items()},
            present=False)
        return cql_wire.rows_result(SYSTEM_KEYSPACE, PEERS_TABLE, columns, rows)
    return empty_rows_result(query)


def answer_prepare(query: str) -> bytes:
    keyspace, table = statement_target(query)
    return cql_wire.prepared_result(query_id(query), keyspace, table,
                                    bind_columns(query))


@dataclass(frozen=True)
class Handler:
    """One connection's answer function, bound to its peer's own address."""

    identity: NodeIdentity
    address: str
    work: AcceptedWork

    def answer(self, frame: cql_wire.Frame) -> bytes:
        if frame.version != cql_wire.REQUEST_VERSION:
            return cql_wire.frame(
                cql_wire.OPCODE_ERROR, frame.stream,
                cql_wire.unsupported_version_body(frame.version))
        opcode, body = self._response(frame)
        return cql_wire.frame(opcode, frame.stream, body)

    def _response(self, frame: cql_wire.Frame) -> tuple[int, bytes]:
        """Ordered by frequency rather than by opcode: EXECUTE is every
        document of the run, and everything else happens once per connection."""
        if frame.opcode == cql_wire.OPCODE_EXECUTE:
            return self._accept(1)
        if frame.opcode == cql_wire.OPCODE_BATCH:
            return self._accept(cql_wire.batch_statement_count(frame.body))
        return self._handshake_or_metadata(frame)

    def _accept(self, docs: int) -> tuple[int, bytes]:
        self.work.add(ops=1, docs=docs)
        return cql_wire.OPCODE_RESULT, cql_wire.void_result()

    def _handshake_or_metadata(self,
                               frame: cql_wire.Frame) -> tuple[int, bytes]:
        opcode = frame.opcode
        if opcode == cql_wire.OPCODE_OPTIONS:
            return (cql_wire.OPCODE_SUPPORTED,
                    cql_wire.string_multimap(SUPPORTED_OPTIONS))
        if opcode in (cql_wire.OPCODE_STARTUP, cql_wire.OPCODE_REGISTER):
            return cql_wire.OPCODE_READY, b""
        if opcode == cql_wire.OPCODE_QUERY:
            query, _ = cql_wire.read_long_string(frame.body)
            return (cql_wire.OPCODE_RESULT,
                    answer_query(query, self.identity, self.address))
        if opcode == cql_wire.OPCODE_PREPARE:
            query, _ = cql_wire.read_long_string(frame.body)
            return cql_wire.OPCODE_RESULT, answer_prepare(query)
        return self._unanswered(opcode)

    def _unanswered(self, opcode: int) -> tuple[int, bytes]:
        self.work.note_unexpected(f"cql opcode 0x{opcode:02x}")
        return (cql_wire.OPCODE_ERROR,
                cql_wire.error_body(
                    cql_wire.ERROR_PROTOCOL,
                    f"null sink does not answer opcode 0x{opcode:02x}"))


def answers_for(buffer: bytearray, handler: Handler) -> bytes:
    """Every whole frame in the buffer, answered, with the rest left behind.

    Answers are concatenated and written once per read rather than per frame:
    the loader holds `--concurrency` statements outstanding on one connection,
    so a read commonly carries many frames and a write per frame would put a
    syscall per document between the client and its own ceiling.
    """
    cursor, out = 0, []
    while True:
        frame, cursor = cql_wire.take_frame(buffer, cursor)
        if frame is None:
            break
        out.append(handler.answer(frame))
    del buffer[:cursor]
    return b"".join(out)


def local_address(writer: asyncio.StreamWriter) -> str:
    socket_name = writer.get_extra_info("sockname")
    if isinstance(socket_name, tuple) and socket_name:
        return str(socket_name[0])
    return "127.0.0.1"


async def serve_connection(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter,
                           identity: NodeIdentity, work: AcceptedWork,
                           delay_s: float) -> None:
    handler = Handler(identity, local_address(writer), work)
    handle = sink_tcp.accepted_socket(writer)
    buffer = bytearray()
    try:
        while True:
            chunk = await reader.read(READ_CHUNK_BYTES)
            if not chunk:
                return
            sink_tcp.acknowledge_now(handle)
            buffer += chunk
            payload = answers_for(buffer, handler)
            if not payload:
                continue
            if delay_s:
                await asyncio.sleep(delay_s)
            writer.write(payload)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        return
    finally:
        writer.close()


async def serve(host: str, port: int, work: AcceptedWork,
                delay_s: float = 0.0) -> asyncio.Server:
    identity = new_identity()

    async def client_connected(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, identity, work, delay_s)

    return await asyncio.start_server(client_connected, host, port)
