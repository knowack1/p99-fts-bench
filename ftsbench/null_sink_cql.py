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

**One document per EXECUTE of a mutation.** The loaders issue one prepared
INSERT per row and `--batch-size` is a client-side loop window, so their frame
count *is* the document count. A BATCH frame is one exception, and its statement
count is read from the frame because `--unlogged-batch-rows` puts many rows in
one operation.

The other exception is what made this sink answer wrongly for a while: the
driver re-reads `system_schema` after a schema change, and it does so with
PREPARE + EXECUTE like anything else. Answering every EXECUTE with a Void result
told the driver its metadata page was not rows, and the DDL a reset issues
failed on the following schema agreement. So what was prepared is remembered per
statement id, and an EXECUTE is answered as the statement it belongs to —
counted only when it is a mutation.
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
from .sink_index import ModelledIndex

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
# A Rows result declares its column types even when it carries no rows, and the
# driver type-checks the declaration against what it means to deserialize. A
# uniformly-varchar answer therefore fails the schema refresh that follows the
# DDL a reset issues — on the column, not on the absent row. Only the columns
# the driver reads as something other than text need naming here.
SYSTEM_COLUMN_TYPES = {
    "initial_tablets": cql_wire.TYPE_INT,
    "position": cql_wire.TYPE_INT,
    "clustering_order": cql_wire.TYPE_VARCHAR,
    "durable_writes": cql_wire.TYPE_BOOLEAN,
    "replication": cql_wire.TYPE_MAP_VARCHAR_VARCHAR,
    "flags": cql_wire.TYPE_SET_VARCHAR,
    "argument_types": cql_wire.TYPE_LIST_VARCHAR,
    "field_names": cql_wire.TYPE_LIST_VARCHAR,
    "field_types": cql_wire.TYPE_LIST_VARCHAR,
    "options": cql_wire.TYPE_MAP_VARCHAR_VARCHAR,
}
INSERT_COLUMNS_RE = re.compile(r"insert\s+into\s+\S+\s*\(([^)]*)\)", re.I)
PREDICATE_COLUMN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\?")
SELECT_LIST_RE = re.compile(r"select\s+(.*?)\s+from\s+", re.I | re.S)
USE_KEYSPACE_RE = re.compile(r"use\s+\"?([A-Za-z0-9_]+)\"?", re.I)
# `scyllarate` empties the keyspace before every concurrency level, so DDL is no
# longer traffic the sink can answer with an empty Rows result: the index the
# vector-store half reports is created and dropped by these statements.
DDL_RE = re.compile(
    r"\s*(?P<verb>create|drop)\s+(?P<object>custom\s+index|index|keyspace|table)"
    r"\s+(?:if\s+not\s+exists\s+|if\s+exists\s+)?(?P<name>[A-Za-z0-9_.\"]+)",
    re.I)
INDEX_ON_RE = re.compile(r"\bon\s+([A-Za-z0-9_.\"]+)\s*\(", re.I)
KEYSPACE_OBJECT = "keyspace"
TABLE_OBJECT = "table"
INDEX_OBJECT = "index"
DROP_VERB = "drop"
MUTATION_VERBS = ("insert", "update", "delete")


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
        [Column(name, system_column_type(name)) for name in names], [])


def system_column_type(name: str) -> bytes:
    return SYSTEM_COLUMN_TYPES.get(name, cql_wire.TYPE_VARCHAR)


@dataclass(frozen=True)
class SchemaStatement:
    """One DDL statement, reduced to what the sink has to do about it."""

    verb: str
    object: str
    keyspace: str
    name: str

    @property
    def drops(self) -> bool:
        return self.verb == DROP_VERB


def parse_ddl(query: str) -> SchemaStatement | None:
    match = DDL_RE.match(query.strip())
    if match is None:
        return None
    obj = normalized_object(match.group("object"))
    verb = match.group("verb").lower()
    if obj == INDEX_OBJECT:
        return index_statement(query, verb)
    keyspace, name = split_qualified(match.group("name"))
    if obj == KEYSPACE_OBJECT:
        return SchemaStatement(verb, obj, name, "")
    return SchemaStatement(verb, obj, keyspace, name)


def normalized_object(raw: str) -> str:
    return INDEX_OBJECT if "index" in raw.lower() else raw.lower()


def index_statement(query: str, verb: str) -> SchemaStatement:
    """An index change is announced against the table it lives on.

    `CREATE CUSTOM INDEX ... ON ks.table(col)` names that table; `DROP INDEX`
    does not, so a drop is announced against the keyspace instead of inventing
    a table the driver would then fail to find.
    """
    on = INDEX_ON_RE.search(query)
    if on is None:
        return SchemaStatement(verb, KEYSPACE_OBJECT, DEFAULT_KEYSPACE, "")
    keyspace, table = split_qualified(on.group(1))
    return SchemaStatement(verb, INDEX_OBJECT, keyspace, table)


def split_qualified(target: str) -> tuple[str, str]:
    bare = target.replace('"', "")
    if "." in bare:
        keyspace, _, name = bare.partition(".")
        return keyspace, name
    return DEFAULT_KEYSPACE, bare


def apply_to_index(statement: SchemaStatement, index: ModelledIndex) -> None:
    """Dropping the keyspace or the table takes the index with it, which is why
    `scyllarate` resets with `DROP KEYSPACE` alone."""
    if statement.drops:
        index.drop()
        return
    if statement.object == INDEX_OBJECT:
        index.create()


def schema_change_for(statement: SchemaStatement) -> bytes:
    if statement.object == KEYSPACE_OBJECT:
        return cql_wire.schema_change_result(
            keyspace_change(statement), cql_wire.SCHEMA_TARGET_KEYSPACE,
            statement.keyspace)
    return cql_wire.schema_change_result(
        table_change(statement), cql_wire.SCHEMA_TARGET_TABLE,
        statement.keyspace, statement.name)


def keyspace_change(statement: SchemaStatement) -> str:
    if statement.verb == DROP_VERB:
        return cql_wire.SCHEMA_DROPPED
    return cql_wire.SCHEMA_CREATED


def table_change(statement: SchemaStatement) -> str:
    """An index lives on a table that outlives it, so its creation and removal
    are both an update to that table rather than its birth or death."""
    if statement.object == INDEX_OBJECT:
        return cql_wire.SCHEMA_UPDATED
    return keyspace_change(statement)


def answer_ddl(statement: SchemaStatement, index: ModelledIndex) -> bytes:
    apply_to_index(statement, index)
    return schema_change_for(statement)


def answer_query(query: str, identity: NodeIdentity, address: str,
                 index: ModelledIndex) -> bytes:
    lowered = query.strip().lower()
    keyspace = USE_KEYSPACE_RE.match(query.strip())
    if lowered.startswith("use ") and keyspace is not None:
        return cql_wire.set_keyspace_result(keyspace.group(1))
    statement = parse_ddl(query)
    if statement is not None:
        return answer_ddl(statement, index)
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


def is_mutation(query: str) -> bool:
    return query.strip().lower().startswith(MUTATION_VERBS)


@dataclass(frozen=True)
class Handler:
    """One connection's answer function, bound to its peer's own address."""

    identity: NodeIdentity
    address: str
    work: AcceptedWork
    index: ModelledIndex
    prepared: dict[bytes, tuple[bool, str]]

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
            return self._execute(frame.body)
        if frame.opcode == cql_wire.OPCODE_BATCH:
            return self._accept(cql_wire.batch_statement_count(frame.body))
        return self._handshake_or_metadata(frame)

    def _execute(self, body: bytes) -> tuple[int, bytes]:
        statement_id, _ = cql_wire.read_short_bytes(body)
        known = self.prepared.get(statement_id)
        if known is None:
            return self._unprepared(statement_id)
        mutation, query = known
        if mutation:
            return self._accept(1)
        return (cql_wire.OPCODE_RESULT,
                answer_query(query, self.identity, self.address, self.index))

    def _unprepared(self, statement_id: bytes) -> tuple[int, bytes]:
        """The driver re-prepares and retries, which is how a real node answers
        a statement it has never seen."""
        self.work.note_unexpected(f"execute of unprepared {statement_id.hex()}")
        return cql_wire.OPCODE_ERROR, cql_wire.unprepared_body(statement_id)

    def _accept(self, docs: int) -> tuple[int, bytes]:
        self.work.add(ops=1, docs=docs)
        self.index.add(docs)
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
                    answer_query(query, self.identity, self.address,
                                 self.index))
        if opcode == cql_wire.OPCODE_PREPARE:
            query, _ = cql_wire.read_long_string(frame.body)
            self.prepared[query_id(query)] = (is_mutation(query), query)
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
                           index: ModelledIndex,
                           prepared: dict[bytes, tuple[bool, str]],
                           delay_s: float) -> None:
    handler = Handler(identity, local_address(writer), work, index, prepared)
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
                index: ModelledIndex, delay_s: float = 0.0) -> asyncio.Server:
    identity = new_identity()
    # Shared across connections, because the driver may prepare on one and
    # execute on another: the id is a hash of the statement, not of the socket.
    prepared: dict[bytes, tuple[bool, str]] = {}

    async def client_connected(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        await serve_connection(reader, writer, identity, work, index, prepared,
                               delay_s)

    return await asyncio.start_server(client_connected, host, port)
