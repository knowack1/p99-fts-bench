//! A CQL endpoint that connects a real driver and discards every write.
//!
//! Answers exactly the traffic the loaders produce: the handshake, `USE`, the
//! `SELECT release_version FROM system.local` a version banner reads, the
//! prepare of the INSERT, and then one execute per row. The control
//! connection's own topology and schema queries are answered too, because a
//! session does not finish connecting until they have been.
//!
//! **It answers, it does not emulate.** Nothing is stored, no consistency is
//! enforced, `system_schema` comes back empty, and there is no token map — an
//! EXECUTE is a Void result and 13 bytes on the wire. That is the point: the
//! mock must not be the bottleneck, or the ceiling that comes back is the
//! mock's.
//!
//! **One document per EXECUTE of a mutation.** The loaders issue one prepared
//! INSERT per row and a batch size is a client-side loop window, so their frame
//! count *is* the document count. A BATCH frame is one exception, and its
//! statement count is read from the frame because an unlogged batch puts many
//! rows in one operation.
//!
//! The other exception is what made the Python sink answer wrongly for a while:
//! a driver re-reads `system_schema` after a schema change, and it does so with
//! PREPARE + EXECUTE like anything else. Answering every EXECUTE with a Void
//! result told the driver its metadata page was not rows, and the DDL a reset
//! issues failed on the following schema agreement. So what was prepared is
//! remembered per statement id, and an EXECUTE is answered as the statement it
//! belongs to — counted only when it is a mutation.
use std::collections::HashMap;
use std::sync::{Arc, LazyLock, RwLock};

use md5::{Digest, Md5};
use regex::Regex;
use uuid::Uuid;

use crate::conn;
use crate::counters::AcceptedWork;
use crate::cql_wire::{self as wire, Column, ColumnType, Frame, Row};
use crate::index::ModelledIndex;

pub const SYSTEM_KEYSPACE: &str = "system";
pub const PEERS_TABLE: &str = "peers";
pub const LOCAL_TABLE: &str = "local";
pub const DEFAULT_KEYSPACE: &str = "wiki";
pub const CQL_VERSION: &str = "3.3.1";
const MUTATION_VERBS: [&str; 3] = ["insert", "update", "delete"];

pub type StatementId = [u8; 16];

fn supported_options() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        ("CQL_VERSION", &[CQL_VERSION] as &[&str]),
        ("COMPRESSION", &[]),
        ("PROTOCOL_VERSIONS", &["3/v3", "4/v4"]),
    ]
}

/// The campaign's table, so a prepared INSERT binds the types the loader
/// actually sends (`scylladb/schema.cql`). An unlisted column binds as varchar,
/// which is what a text-only stub can honestly claim to know.
fn table_column_type(name: &str) -> ColumnType {
    match name {
        "article_id" => ColumnType::UUID,
        "page_id" => ColumnType::BIGINT,
        _ => ColumnType::VARCHAR,
    }
}

/// A Rows result declares its column types even when it carries no rows, and
/// the driver type-checks the declaration against what it means to
/// deserialize. A uniformly-varchar answer therefore fails the schema refresh
/// that follows the DDL a reset issues — on the column, not on the absent row.
/// Only the columns a driver reads as something other than text need naming.
fn system_column_type(name: &str) -> ColumnType {
    match name {
        "initial_tablets" | "position" => ColumnType::INT,
        "durable_writes" => ColumnType::BOOLEAN,
        "replication" | "options" => ColumnType::MAP_VARCHAR_VARCHAR,
        "flags" => ColumnType::SET_VARCHAR,
        "argument_types" | "field_names" | "field_types" => ColumnType::LIST_VARCHAR,
        _ => ColumnType::VARCHAR,
    }
}

/// `system.peers` is matched by substring, so `system.peers_v2` is answered
/// from this list too — which is why the two columns only `peers_v2` has are in
/// it. They cost a zero-row answer two more declarations; leaving them out
/// would have the first driver version that prefers `peers_v2` fail its
/// typed read of a column the mock declared as text, in the middle of a run.
fn peers_column_types() -> Vec<(&'static str, ColumnType)> {
    vec![
        ("peer", ColumnType::INET),
        ("data_center", ColumnType::VARCHAR),
        ("host_id", ColumnType::UUID),
        ("rack", ColumnType::VARCHAR),
        ("release_version", ColumnType::VARCHAR),
        ("rpc_address", ColumnType::INET),
        ("schema_version", ColumnType::UUID),
        ("tokens", ColumnType::SET_VARCHAR),
        ("preferred_ip", ColumnType::INET),
        ("native_address", ColumnType::INET),
        ("native_port", ColumnType::INT),
    ]
}

/// A Rows result must declare at least one column even when it carries no
/// rows: the driver falls back to the statement's cached metadata when the
/// result's column list is empty, and for a one-off SELECT there is none, so a
/// zero-column answer raises inside its row parser rather than reading as "no
/// rows". Everything the mock does not model — all of `system_schema` — comes
/// back as this one column and no rows.
const PLACEHOLDER_COLUMN: &str = "key";

static INSERT_COLUMNS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)insert\s+into\s+\S+\s*\(([^)]*)\)").unwrap());
static PREDICATE_COLUMN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\?").unwrap());
static SELECT_LIST: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)select\s+(.*?)\s+from\s+").unwrap());
static USE_KEYSPACE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)^use\s+"?([A-Za-z0-9_]+)"?"#).unwrap());
static STATEMENT_TARGET: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)(?:insert\s+into|update|from)\s+([A-Za-z0-9_."]+)"#).unwrap()
});
/// A reset empties the keyspace before every concurrency level, so DDL is not
/// traffic the mock can answer with an empty Rows result: the index the
/// vector-store half reports is created and dropped by these statements.
static DDL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?i)^\s*(?P<verb>create|drop)\s+(?P<object>custom\s+index|index|keyspace|table)\s+(?:if\s+not\s+exists\s+|if\s+exists\s+)?(?P<name>[A-Za-z0-9_."]+)"#,
    )
    .unwrap()
});
static INDEX_ON: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)\bon\s+([A-Za-z0-9_."]+)\s*\("#).unwrap());

/// What the mock claims to be, stable for the life of the process.
///
/// Stable because it must be: a `host_id` that changed between connections
/// would make a driver believe it had found two nodes on one endpoint, log a
/// duplicate-host warning and drop one of them.
#[derive(Debug, Clone)]
pub struct NodeIdentity {
    pub host_id: Uuid,
    pub schema_version: Uuid,
    pub cluster_name: String,
    pub release_version: String,
    pub data_center: String,
    pub rack: String,
    pub partitioner: String,
}

impl Default for NodeIdentity {
    fn default() -> Self {
        Self {
            host_id: Uuid::new_v4(),
            schema_version: Uuid::new_v4(),
            cluster_name: "null-sink".to_string(),
            release_version: "6.2.0-null-sink".to_string(),
            data_center: "datacenter1".to_string(),
            rack: "rack1".to_string(),
            partitioner: "org.apache.cassandra.dht.Murmur3Partitioner".to_string(),
        }
    }
}

/// One column of `system.local`: its name, its type, and its value on this
/// connection.
struct Cell {
    name: &'static str,
    column_type: ColumnType,
    value: Vec<u8>,
}

/// The `system.local` row, column by column, with its wire type.
///
/// `rpc_address` is the address the client reached this mock on, taken from the
/// accepted socket rather than configured: a driver adds a host for whatever
/// this row advertises, so a fixed 127.0.0.1 here would send a loader on
/// another box off to connect to itself.
fn local_row(identity: &NodeIdentity, address: &str) -> Vec<Cell> {
    let text = |name: &'static str, value: &str| Cell {
        name,
        column_type: ColumnType::VARCHAR,
        value: wire::varchar_value(value),
    };
    let inet = |name: &'static str| Cell {
        name,
        column_type: ColumnType::INET,
        value: wire::inet_value(address),
    };
    let id = |name: &'static str, value: Uuid| Cell {
        name,
        column_type: ColumnType::UUID,
        value: wire::uuid_value(value),
    };
    vec![
        text("key", "local"),
        text("bootstrapped", "COMPLETED"),
        text("cluster_name", &identity.cluster_name),
        text("cql_version", CQL_VERSION),
        text("data_center", &identity.data_center),
        text("rack", &identity.rack),
        text("native_protocol_version", "4"),
        text("partitioner", &identity.partitioner),
        text("release_version", &identity.release_version),
        id("host_id", identity.host_id),
        id("schema_version", identity.schema_version),
        inet("broadcast_address"),
        inet("listen_address"),
        inet("rpc_address"),
        Cell {
            name: "tokens",
            column_type: ColumnType::SET_VARCHAR,
            value: wire::varchar_set_value(&["0"]),
        },
    ]
}

/// The SELECT list, or every available column for `SELECT *`.
///
/// Parsed rather than pattern-matched per query because a driver asks for
/// several different column subsets of `system.local` depending on what it is
/// refreshing, and a row whose columns do not match the request is read by
/// position — which silently hands `cluster_name` the value of `tokens`.
pub fn selected_names(query: &str, available: &[&str]) -> Vec<String> {
    let Some(found) = SELECT_LIST.captures(query) else {
        return available.iter().map(|name| name.to_string()).collect();
    };
    let list = found[1].trim();
    if list == "*" {
        return available.iter().map(|name| name.to_string()).collect();
    }
    list.split(',')
        .map(|name| name.trim().to_string())
        .collect()
}

fn row_for(query: &str, cells: &[Cell], present: bool) -> (Vec<Column>, Vec<Row>) {
    let available: Vec<&str> = cells.iter().map(|cell| cell.name).collect();
    let names = selected_names(query, &available);
    let found = |name: &str| cells.iter().find(|cell| cell.name == name);
    let columns = names
        .iter()
        .map(|name| {
            Column::new(
                name.clone(),
                found(name).map_or(ColumnType::VARCHAR, |cell| cell.column_type.clone()),
            )
        })
        .collect();
    if !present {
        return (columns, Vec::new());
    }
    let row = names
        .iter()
        .map(|name| found(name).map(|cell| cell.value.clone()))
        .collect();
    (columns, vec![row])
}

fn insert_bind_names(query: &str) -> Vec<String> {
    match INSERT_COLUMNS.captures(query) {
        Some(found) => found[1]
            .split(',')
            .map(|name| name.trim().to_string())
            .collect(),
        None => PREDICATE_COLUMN
            .captures_iter(query)
            .map(|found| found[1].to_string())
            .collect(),
    }
}

/// One bind column per `?`, typed from the campaign's table where possible.
///
/// The type decides how a driver serialises the parameter, so getting `page_id`
/// wrong would not merely mislabel the column — it would change the bytes the
/// client spends CPU producing, which is the quantity being measured.
pub fn bind_columns(query: &str) -> Vec<Column> {
    let names = insert_bind_names(query);
    let markers = query.matches('?').count();
    (0..markers)
        .map(|position| match names.get(position) {
            Some(name) => Column::new(name.clone(), table_column_type(name)),
            None => Column::new(format!("bind{position}"), ColumnType::VARCHAR),
        })
        .collect()
}

pub fn query_id(query: &str) -> StatementId {
    let mut hasher = Md5::new();
    hasher.update(query.as_bytes());
    hasher.finalize().into()
}

fn statement_target(query: &str) -> (String, String) {
    let target = STATEMENT_TARGET
        .captures(query)
        .map_or_else(
            || DEFAULT_KEYSPACE.to_string(),
            |found| found[1].to_string(),
        )
        .replace('"', "");
    split_qualified(&target)
}

fn split_qualified(target: &str) -> (String, String) {
    let bare = target.replace('"', "");
    match bare.split_once('.') {
        Some((keyspace, name)) => (keyspace.to_string(), name.to_string()),
        None => (DEFAULT_KEYSPACE.to_string(), bare),
    }
}

/// No rows, with the requested columns declared so the answer is readable.
fn empty_rows_result(query: &str) -> Vec<u8> {
    let columns = selected_names(query, &[PLACEHOLDER_COLUMN])
        .into_iter()
        .map(|name| {
            let column_type = system_column_type(&name);
            Column::new(name, column_type)
        })
        .collect::<Vec<_>>();
    wire::rows_result(SYSTEM_KEYSPACE, "unmodelled", &columns, &[])
}

/// One DDL statement, reduced to what the mock has to do about it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchemaStatement {
    pub verb: Verb,
    pub object: SchemaObject,
    pub keyspace: String,
    pub name: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verb {
    Create,
    Drop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchemaObject {
    Keyspace,
    Table,
    Index,
}

impl SchemaStatement {
    fn drops(&self) -> bool {
        self.verb == Verb::Drop
    }
}

pub fn parse_ddl(query: &str) -> Option<SchemaStatement> {
    let found = DDL.captures(query.trim())?;
    let verb = match found.name("verb")?.as_str().to_ascii_lowercase().as_str() {
        "drop" => Verb::Drop,
        _ => Verb::Create,
    };
    let raw_object = found.name("object")?.as_str().to_ascii_lowercase();
    if raw_object.contains("index") {
        return Some(index_statement(query, verb));
    }
    let (keyspace, name) = split_qualified(found.name("name")?.as_str());
    if raw_object == "keyspace" {
        return Some(SchemaStatement {
            verb,
            object: SchemaObject::Keyspace,
            keyspace: name,
            name: String::new(),
        });
    }
    Some(SchemaStatement {
        verb,
        object: SchemaObject::Table,
        keyspace,
        name,
    })
}

/// An index change is announced against the table it lives on.
///
/// `CREATE CUSTOM INDEX ... ON ks.table(col)` names that table; `DROP INDEX`
/// does not, so a drop is announced against the keyspace instead of inventing a
/// table the driver would then fail to find.
fn index_statement(query: &str, verb: Verb) -> SchemaStatement {
    let Some(found) = INDEX_ON.captures(query) else {
        return SchemaStatement {
            verb,
            object: SchemaObject::Keyspace,
            keyspace: DEFAULT_KEYSPACE.to_string(),
            name: String::new(),
        };
    };
    let (keyspace, table) = split_qualified(&found[1]);
    SchemaStatement {
        verb,
        object: SchemaObject::Index,
        keyspace,
        name: table,
    }
}

/// Dropping the keyspace or the table takes the index with it, which is why a
/// reset can be `DROP KEYSPACE` alone.
fn apply_to_index(statement: &SchemaStatement, index: &ModelledIndex) {
    if statement.drops() {
        index.drop_index();
        return;
    }
    if statement.object == SchemaObject::Index {
        index.create();
    }
}

fn schema_change_for(statement: &SchemaStatement) -> Vec<u8> {
    if statement.object == SchemaObject::Keyspace {
        return wire::schema_change_result(
            keyspace_change(statement),
            wire::SCHEMA_TARGET_KEYSPACE,
            &statement.keyspace,
            "",
        );
    }
    wire::schema_change_result(
        table_change(statement),
        wire::SCHEMA_TARGET_TABLE,
        &statement.keyspace,
        &statement.name,
    )
}

fn keyspace_change(statement: &SchemaStatement) -> &'static str {
    if statement.drops() {
        return wire::SCHEMA_DROPPED;
    }
    wire::SCHEMA_CREATED
}

/// An index lives on a table that outlives it, so its creation and removal are
/// both an update to that table rather than its birth or death.
fn table_change(statement: &SchemaStatement) -> &'static str {
    if statement.object == SchemaObject::Index {
        return wire::SCHEMA_UPDATED;
    }
    keyspace_change(statement)
}

pub fn is_mutation(query: &str) -> bool {
    let lowered = query.trim_start().to_ascii_lowercase();
    MUTATION_VERBS.iter().any(|verb| lowered.starts_with(verb))
}

/// What one statement id resolves to: whether executing it is a document, and
/// the statement text an execute of a non-mutation has to be answered as.
#[derive(Debug)]
pub struct Prepared {
    pub mutation: bool,
    pub query: String,
}

/// The process-wide state one CQL endpoint answers from.
///
/// The prepared map is shared across connections because a driver may prepare
/// on one and execute on another: the id is a hash of the statement, not of the
/// socket.
pub struct Node {
    identity: NodeIdentity,
    work: Arc<AcceptedWork>,
    index: Arc<ModelledIndex>,
    prepared: RwLock<HashMap<StatementId, Arc<Prepared>>>,
}

impl Node {
    pub fn new(identity: NodeIdentity, work: Arc<AcceptedWork>, index: Arc<ModelledIndex>) -> Self {
        Self {
            identity,
            work,
            index,
            prepared: RwLock::new(HashMap::new()),
        }
    }

    fn remember(&self, id: StatementId, query: &str) -> Arc<Prepared> {
        let statement = Arc::new(Prepared {
            mutation: is_mutation(query),
            query: query.to_string(),
        });
        self.prepared
            .write()
            .expect("prepared statements")
            .insert(id, Arc::clone(&statement));
        statement
    }

    fn recall(&self, id: &StatementId) -> Option<Arc<Prepared>> {
        self.prepared
            .read()
            .expect("prepared statements")
            .get(id)
            .cloned()
    }
}

/// What a connection remembers so the hot path touches no shared state.
///
/// One statement answers every document of a level, so the last one used is
/// checked first: a 16-byte compare, against a hash lookup and the shared map's
/// read lock behind it.
#[derive(Default)]
struct StatementCache {
    last: Option<(StatementId, Arc<Prepared>)>,
    known: HashMap<StatementId, Arc<Prepared>>,
}

impl StatementCache {
    fn get(&mut self, id: &StatementId) -> Option<Arc<Prepared>> {
        if let Some((last, statement)) = &self.last {
            if last == id {
                return Some(Arc::clone(statement));
            }
        }
        let statement = self.known.get(id).cloned()?;
        self.last = Some((*id, Arc::clone(&statement)));
        Some(statement)
    }

    fn insert(&mut self, id: StatementId, statement: Arc<Prepared>) {
        self.last = Some((id, Arc::clone(&statement)));
        self.known.insert(id, statement);
    }
}

/// One connection's answer function, bound to its own peer address and counter
/// lane.
pub struct Handler {
    node: Arc<Node>,
    address: String,
    lane: usize,
    statements: StatementCache,
}

impl Handler {
    pub fn new(node: Arc<Node>, address: String, lane: usize) -> Self {
        Self {
            node,
            address,
            lane,
            statements: StatementCache::default(),
        }
    }

    pub fn answer(&mut self, frame: &Frame<'_>, out: &mut Vec<u8>) {
        if frame.version != wire::REQUEST_VERSION {
            let body = wire::unsupported_version_body(frame.version);
            wire::write_frame(out, wire::OPCODE_ERROR, frame.stream, &body);
            return;
        }
        if frame.opcode == wire::OPCODE_EXECUTE {
            self.execute(frame, out);
            return;
        }
        if frame.opcode == wire::OPCODE_BATCH {
            self.accept(wire::batch_statement_count(frame.body), frame.stream, out);
            return;
        }
        let (opcode, body) = self.handshake_or_metadata(frame);
        wire::write_frame(out, opcode, frame.stream, &body);
    }

    /// Every document of the run comes through here, which is why it writes its
    /// answer straight into the connection's buffer rather than returning one.
    fn execute(&mut self, frame: &Frame<'_>, out: &mut Vec<u8>) {
        let Some(id) = statement_id(frame.body) else {
            let body = wire::error_body(wire::ERROR_PROTOCOL, "malformed EXECUTE");
            wire::write_frame(out, wire::OPCODE_ERROR, frame.stream, &body);
            return;
        };
        let Some(statement) = self.resolve(&id) else {
            self.unprepared(&id, frame.stream, out);
            return;
        };
        if statement.mutation {
            self.accept(1, frame.stream, out);
            return;
        }
        let body = self.answer_query(&statement.query);
        wire::write_frame(out, wire::OPCODE_RESULT, frame.stream, &body);
    }

    fn resolve(&mut self, id: &StatementId) -> Option<Arc<Prepared>> {
        if let Some(statement) = self.statements.get(id) {
            return Some(statement);
        }
        let statement = self.node.recall(id)?;
        self.statements.insert(*id, Arc::clone(&statement));
        Some(statement)
    }

    /// The driver re-prepares and retries, which is how a real node answers a
    /// statement it has never seen.
    fn unprepared(&self, id: &StatementId, stream: i16, out: &mut Vec<u8>) {
        self.node
            .work
            .note_unexpected(&format!("execute of unprepared {}", hex(id)));
        let body = wire::unprepared_body(id);
        wire::write_frame(out, wire::OPCODE_ERROR, stream, &body);
    }

    fn accept(&self, docs: u64, stream: i16, out: &mut Vec<u8>) {
        self.node.work.add(self.lane, 1, docs);
        self.node.index.add(self.lane, docs);
        wire::write_void_frame(out, stream);
    }

    fn handshake_or_metadata(&mut self, frame: &Frame<'_>) -> (u8, Vec<u8>) {
        match frame.opcode {
            wire::OPCODE_OPTIONS => (
                wire::OPCODE_SUPPORTED,
                wire::string_multimap(&supported_options()),
            ),
            wire::OPCODE_STARTUP | wire::OPCODE_REGISTER => (wire::OPCODE_READY, Vec::new()),
            wire::OPCODE_QUERY => match wire::read_long_string(frame.body, 0) {
                Some((query, _)) => (wire::OPCODE_RESULT, self.answer_query(&query)),
                None => self.unanswered(frame.opcode),
            },
            wire::OPCODE_PREPARE => match wire::read_long_string(frame.body, 0) {
                Some((query, _)) => (wire::OPCODE_RESULT, self.prepare(&query)),
                None => self.unanswered(frame.opcode),
            },
            _ => self.unanswered(frame.opcode),
        }
    }

    fn prepare(&mut self, query: &str) -> Vec<u8> {
        let id = query_id(query);
        let statement = self.node.remember(id, query);
        self.statements.insert(id, statement);
        let (keyspace, table) = statement_target(query);
        wire::prepared_result(&id, &keyspace, &table, &bind_columns(query))
    }

    /// A frame the splitter could not read ends the connection, as it did in
    /// Python. What is new is that it says so: a client that started corrupting
    /// frames would otherwise show up only as dropped connections in the
    /// loader's own error count, and nothing in the mock's artifact would
    /// mention it.
    fn note_malformed(&self, why: &str) -> String {
        self.node.work.note_unexpected("malformed cql frame");
        why.to_string()
    }

    fn unanswered(&self, opcode: u8) -> (u8, Vec<u8>) {
        self.node
            .work
            .note_unexpected(&format!("cql opcode 0x{opcode:02x}"));
        (
            wire::OPCODE_ERROR,
            wire::error_body(
                wire::ERROR_PROTOCOL,
                &format!("null sink does not answer opcode 0x{opcode:02x}"),
            ),
        )
    }

    fn answer_query(&self, query: &str) -> Vec<u8> {
        let trimmed = query.trim();
        let lowered = trimmed.to_ascii_lowercase();
        if lowered.starts_with("use ") {
            if let Some(found) = USE_KEYSPACE.captures(trimmed) {
                return wire::set_keyspace_result(&found[1]);
            }
        }
        if let Some(statement) = parse_ddl(trimmed) {
            apply_to_index(&statement, &self.node.index);
            return schema_change_for(&statement);
        }
        if lowered.contains("system.local") {
            let cells = local_row(&self.node.identity, &self.address);
            let (columns, rows) = row_for(query, &cells, true);
            return wire::rows_result(SYSTEM_KEYSPACE, LOCAL_TABLE, &columns, &rows);
        }
        if lowered.contains("system.peers") {
            let cells = peers_column_types()
                .into_iter()
                .map(|(name, column_type)| Cell {
                    name,
                    column_type,
                    value: Vec::new(),
                })
                .collect::<Vec<_>>();
            let (columns, rows) = row_for(query, &cells, false);
            return wire::rows_result(SYSTEM_KEYSPACE, PEERS_TABLE, &columns, &rows);
        }
        empty_rows_result(query)
    }
}

fn statement_id(body: &[u8]) -> Option<StatementId> {
    let (raw, _) = wire::read_short_bytes(body, 0)?;
    raw.try_into().ok()
}

fn hex(id: &StatementId) -> String {
    id.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Every whole frame in the buffer, answered into `out`, with the rest left
/// behind. Returns how many bytes of the buffer were consumed.
///
/// Answers are concatenated and written once per read rather than per frame:
/// the loader holds `--concurrency` statements outstanding on one connection,
/// so a read commonly carries many frames and a write per frame would put a
/// syscall per document between the client and its own ceiling.
pub fn answers_for(
    buffer: &[u8],
    handler: &mut Handler,
    out: &mut Vec<u8>,
) -> Result<usize, String> {
    let mut cursor = 0;
    loop {
        match wire::take_frame(buffer, cursor) {
            Err(why) => return Err(handler.note_malformed(&why)),
            Ok(None) => return Ok(cursor),
            Ok(Some((frame, next))) => {
                handler.answer(&frame, out);
                cursor = next;
            }
        }
    }
}

impl conn::Answers for Handler {
    fn answer_all(&mut self, buffer: &[u8], out: &mut Vec<u8>) -> Result<usize, String> {
        answers_for(buffer, self, out)
    }
}

#[cfg(test)]
#[path = "cql_tests.rs"]
mod tests;
