use std::time::Duration;

use super::*;
use crate::index::{Clock, IndexStatus, Refresh, SERVING};

const LANES: usize = 1;
const PEER_ADDRESS: &str = "127.0.0.1";
const AN_INSERT: &str =
    "INSERT INTO wiki.articles (article_id, page_id, title, body) VALUES (?, ?, ?, ?)";
const A_SELECT: &str = "SELECT * FROM system_schema.scylla_keyspaces";
const AN_INSERT_MD5: &str = "fa2801af972f4739a07777780b7483a0";
const AN_UNKNOWN_ID: StatementId = [0x01; 16];
const AN_UNANSWERED_OPCODE: u8 = 0x0F;

const LIST_TYPE: u16 = 0x0020;
const MAP_TYPE: u16 = 0x0021;
const SET_TYPE: u16 = 0x0022;

/// The four statements a reset sends, in the order the harness sends them.
const RESET_CQL: [&str; 4] = [
    "DROP KEYSPACE IF EXISTS wiki",
    "CREATE KEYSPACE wiki WITH replication = \
     {'class': 'NetworkTopologyStrategy', 'replication_factor': 1}",
    "CREATE TABLE wiki.articles (article_id uuid PRIMARY KEY, page_id bigint, \
     title text, body text)",
    "CREATE CUSTOM INDEX articles_body_fts ON wiki.articles(body) \
     USING 'fulltext_index'",
];

/// A response frame read back: opcode, stream, body.
type Answer = (u8, i16, Vec<u8>);

/// One connection to ask things of, over its own counter and its own created
/// index — created because that is the state a load runs against: the campaign
/// builds the index before it writes a document.
struct Sink {
    handler: Handler,
    node: Arc<Node>,
    work: Arc<AcceptedWork>,
    index: Arc<ModelledIndex>,
}

fn a_sink() -> Sink {
    a_sink_reached_at(PEER_ADDRESS)
}

fn a_sink_reached_at(address: &str) -> Sink {
    let work = Arc::new(AcceptedWork::new(LANES));
    let index = Arc::new(ModelledIndex::created(
        LANES,
        Duration::ZERO,
        Refresh::immediately(),
        Clock::monotonic(),
    ));
    let node = Arc::new(Node::new(
        NodeIdentity::default(),
        Arc::clone(&work),
        Arc::clone(&index),
    ));
    Sink {
        handler: Handler::new(Arc::clone(&node), address.to_string(), 0),
        node,
        work,
        index,
    }
}

/// A second connection to the same node, which is what a driver's second
/// session connection is.
fn another_connection(sink: &Sink) -> Sink {
    Sink {
        handler: Handler::new(Arc::clone(&sink.node), PEER_ADDRESS.to_string(), 1),
        node: Arc::clone(&sink.node),
        work: Arc::clone(&sink.work),
        index: Arc::clone(&sink.index),
    }
}

impl Sink {
    fn ask(&mut self, frame: &Frame<'_>) -> Answer {
        let mut out = Vec::new();
        self.handler.answer(frame, &mut out);
        parsed(&out)
    }

    fn query(&mut self, query: &str) -> Answer {
        self.ask(&request(wire::OPCODE_QUERY, 1, &long_string(query)))
    }

    fn prepare(&mut self, query: &str) -> Answer {
        self.ask(&request(wire::OPCODE_PREPARE, 1, &long_string(query)))
    }

    fn execute(&mut self, id: &StatementId, stream: i16) -> Answer {
        self.ask(&request(wire::OPCODE_EXECUTE, stream, &short_bytes(id)))
    }

    fn ops(&self) -> u64 {
        self.work.snapshot().ops
    }

    fn docs(&self) -> u64 {
        self.work.snapshot().docs
    }
}

fn request(opcode: u8, stream: i16, body: &[u8]) -> Frame<'_> {
    at_version(wire::REQUEST_VERSION, opcode, stream, body)
}

fn at_version(version: u8, opcode: u8, stream: i16, body: &[u8]) -> Frame<'_> {
    Frame {
        version,
        flags: 0,
        stream,
        opcode,
        body,
    }
}

/// A REQUEST on the wire. `wire::frame` stamps the response version, so using
/// it here would exercise the version refusal instead of the frame walk.
fn wire_request(opcode: u8, stream: i16, body: &[u8]) -> Vec<u8> {
    let mut out = vec![wire::REQUEST_VERSION, 0];
    out.extend_from_slice(&stream.to_be_bytes());
    out.push(opcode);
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    out.extend_from_slice(body);
    out
}

fn long_string(value: &str) -> Vec<u8> {
    let mut out = Vec::new();
    wire::write_long_string(&mut out, value);
    out
}

fn short_bytes(value: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    wire::write_short_bytes(&mut out, value);
    out
}

fn parsed(response: &[u8]) -> Answer {
    let mut answers = parsed_all(response);
    assert_eq!(answers.len(), 1);
    answers.remove(0)
}

fn parsed_all(responses: &[u8]) -> Vec<Answer> {
    let mut answers = Vec::new();
    let mut cursor = 0;
    while let Some((frame, next)) = wire::take_frame(responses, cursor).expect("whole frames") {
        assert_eq!(frame.version, wire::RESPONSE_VERSION);
        answers.push((frame.opcode, frame.stream, frame.body.to_vec()));
        cursor = next;
    }
    answers
}

fn text(body: &[u8]) -> String {
    String::from_utf8_lossy(body).into_owned()
}

/// A result body walked the way a driver walks one.
struct Reader<'a> {
    body: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn new(body: &'a [u8]) -> Self {
        Self { body, at: 0 }
    }

    fn done(&self) -> bool {
        self.at >= self.body.len()
    }

    fn take(&mut self, count: usize) -> &'a [u8] {
        let from = self.at;
        self.at += count;
        &self.body[from..self.at]
    }

    fn int(&mut self) -> i32 {
        i32::from_be_bytes(self.take(4).try_into().expect("four bytes"))
    }

    fn short(&mut self) -> u16 {
        u16::from_be_bytes(self.take(2).try_into().expect("two bytes"))
    }

    fn string(&mut self) -> String {
        let length = self.short() as usize;
        String::from_utf8(self.take(length).to_vec()).expect("utf-8")
    }

    fn bytes(&mut self) -> Vec<u8> {
        let length = self.short() as usize;
        self.take(length).to_vec()
    }

    fn cell(&mut self) -> Option<Vec<u8>> {
        let length = self.int();
        if length < 0 {
            return None;
        }
        Some(self.take(length as usize).to_vec())
    }

    fn skip_column_type(&mut self) {
        match self.short() {
            LIST_TYPE | SET_TYPE => {
                self.short();
            }
            MAP_TYPE => {
                self.short();
                self.short();
            }
            _ => {}
        }
    }
}

fn kind_of(body: &[u8]) -> i32 {
    Reader::new(body).int()
}

/// A Rows result read back as a driver reads it: the columns it declares, in
/// order, and the cells of each row.
fn rows_of(body: &[u8]) -> (Vec<String>, Vec<Row>) {
    let mut reader = Reader::new(body);
    assert_eq!(reader.int(), wire::RESULT_ROWS);
    let _flags = reader.int();
    let column_count = reader.int() as usize;
    let _keyspace = reader.string();
    let _table = reader.string();
    let mut columns = Vec::new();
    for _ in 0..column_count {
        columns.push(reader.string());
        reader.skip_column_type();
    }
    let mut rows = Vec::new();
    for _ in 0..reader.int() {
        rows.push((0..column_count).map(|_| reader.cell()).collect());
    }
    (columns, rows)
}

fn schema_change_fields(body: &[u8]) -> Vec<String> {
    let mut reader = Reader::new(body);
    assert_eq!(reader.int(), wire::RESULT_SCHEMA_CHANGE);
    let mut fields = Vec::new();
    while !reader.done() {
        fields.push(reader.string());
    }
    fields
}

fn prepared_statement_id(body: &[u8]) -> StatementId {
    let mut reader = Reader::new(body);
    assert_eq!(reader.int(), wire::RESULT_PREPARED);
    reader.bytes().try_into().expect("a 16-byte statement id")
}

fn names_of(columns: &[Column]) -> Vec<&str> {
    columns.iter().map(|column| column.name.as_str()).collect()
}

fn types_of(columns: &[Column]) -> Vec<ColumnType> {
    columns
        .iter()
        .map(|column| column.column_type.clone())
        .collect()
}

/// The columns the query named, or every column of the row for `SELECT *`.
fn wanted_local_columns(query: &str) -> Vec<String> {
    let cells = local_row(&NodeIdentity::default(), PEER_ADDRESS);
    let available: Vec<&str> = cells.iter().map(|cell| cell.name).collect();
    selected_names(query, &available)
}

fn accept_documents(sink: &mut Sink, count: usize) {
    sink.prepare(AN_INSERT);
    let id = query_id(AN_INSERT);
    for stream in 0..count {
        sink.execute(&id, stream as i16);
    }
}

fn apply_reset(sink: &mut Sink) {
    for statement in RESET_CQL {
        sink.query(statement);
    }
}

/// A driver keys its downgrade to v4 off the wording of the refusal rather than
/// off the error code, so a reworded refusal leaves a `Cluster()` that starts at
/// v5 unable to connect at all.
#[test]
fn a_v5_request_is_refused_with_the_words_the_driver_downgrades_on() {
    let mut sink = a_sink();

    let (opcode, _, body) = sink.ask(&at_version(5, wire::OPCODE_OPTIONS, 1, b""));

    assert_eq!(opcode, wire::OPCODE_ERROR);
    assert!(
        text(&body).contains("unsupported protocol version"),
        "{}",
        text(&body)
    );
}

/// A driver sends plain frames only when it finds no overlap, so COMPRESSION is
/// advertised with an empty value list rather than left out.
#[test]
fn options_advertises_no_compression() {
    let mut sink = a_sink();

    let (opcode, _, body) = sink.ask(&request(wire::OPCODE_OPTIONS, 1, b""));

    assert_eq!(opcode, wire::OPCODE_SUPPORTED);
    assert!(text(&body).contains("COMPRESSION"));
    assert!(!text(&body).contains("snappy") && !text(&body).contains("lz4"));
}

#[test]
fn startup_and_register_are_ready_on_the_stream_that_asked() {
    let mut sink = a_sink();

    for opcode in [wire::OPCODE_STARTUP, wire::OPCODE_REGISTER] {
        let answered = sink.ask(&request(opcode, 3, b""));
        assert_eq!(answered, (wire::OPCODE_READY, 3, Vec::new()));
    }
}

/// The loaders issue one prepared INSERT per row and a batch size is a
/// client-side loop window, so their frame count *is* the document count: a
/// miscount here is a complete, plausible, wrong client ceiling.
#[test]
fn each_execute_of_a_prepared_mutation_is_exactly_one_document() {
    let mut sink = a_sink();
    sink.prepare(AN_INSERT);

    for stream in 0..5 {
        let (opcode, _, body) = sink.execute(&query_id(AN_INSERT), stream);
        assert_eq!(opcode, wire::OPCODE_RESULT);
        assert_eq!(body, wire::void_result());
    }

    assert_eq!((sink.ops(), sink.docs()), (5, 5));
}

/// An unlogged batch puts many rows in one operation, so a mock that counted the
/// frame as one document would report a rate the loader never offered.
#[test]
fn a_batch_frame_is_counted_by_the_statement_count_in_the_frame() {
    let mut sink = a_sink();
    let mut body = vec![1_u8];
    body.extend_from_slice(&40_u16.to_be_bytes());
    body.extend_from_slice(b"rest of the batch");

    sink.ask(&request(wire::OPCODE_BATCH, 1, &body));

    assert_eq!((sink.ops(), sink.docs()), (1, 40));
}

/// A driver re-reads `system_schema` with PREPARE + EXECUTE after a schema
/// change. Answering those with a Void result — and counting them as documents —
/// failed the schema agreement that follows a reset's DDL, and inflated the
/// document count while doing it.
#[test]
fn an_execute_of_a_prepared_select_is_rows_and_no_document() {
    let mut sink = a_sink();
    sink.prepare(A_SELECT);

    let (opcode, _, body) = sink.execute(&query_id(A_SELECT), 1);

    assert_eq!(opcode, wire::OPCODE_RESULT);
    assert_eq!(kind_of(&body), wire::RESULT_ROWS);
    assert_eq!(sink.docs(), 0);
}

/// UNPREPARED rather than a guess: a mock that assumed every unknown id was an
/// INSERT is how a metadata read became a counted document. The driver keys its
/// re-prepare on the id, so the id has to come back with the refusal.
#[test]
fn an_execute_of_an_unknown_id_asks_for_a_re_prepare() {
    let mut sink = a_sink();

    let (opcode, _, body) = sink.execute(&AN_UNKNOWN_ID, 1);

    let mut reader = Reader::new(&body);
    assert_eq!(opcode, wire::OPCODE_ERROR);
    assert_eq!(reader.int(), wire::ERROR_UNPREPARED);
    let _message = reader.string();
    assert_eq!(reader.bytes(), AN_UNKNOWN_ID);
    assert_eq!(sink.docs(), 0);
    assert_eq!(
        sink.work.unexpected()[&format!("execute of unprepared {}", "01".repeat(16))],
        1
    );
}

/// An EXECUTE is counted as a document only when the statement it names was a
/// mutation, so what PREPARE recorded is what the run's document count means.
#[test]
fn preparing_a_statement_registers_whether_it_mutates() {
    let mut sink = a_sink();

    for (statement, mutation) in [(AN_INSERT, true), (A_SELECT, false)] {
        sink.prepare(statement);

        let registered = sink
            .node
            .recall(&query_id(statement))
            .expect("a registered statement");
        assert_eq!(registered.mutation, mutation, "{statement}");
        assert_eq!(registered.query, statement, "{statement}");
    }
}

/// The id is a hash of the statement rather than a counter, which is what makes
/// it the same id on every connection and across a re-prepare.
#[test]
fn a_prepared_id_is_the_md5_of_the_statement_text() {
    let mut sink = a_sink();

    let (opcode, _, body) = sink.prepare(AN_INSERT);

    assert_eq!(opcode, wire::OPCODE_RESULT);
    assert_eq!(hex(&prepared_statement_id(&body)), AN_INSERT_MD5);
}

/// A driver may prepare on one connection and execute on another, so a registry
/// held per connection would answer UNPREPARED to a statement the session had
/// already prepared.
#[test]
fn a_statement_prepared_on_one_connection_executes_on_another() {
    let mut sink = a_sink();
    sink.prepare(AN_INSERT);
    let mut second = another_connection(&sink);

    let answered = second.execute(&query_id(AN_INSERT), 1);

    assert_eq!(answered, (wire::OPCODE_RESULT, 1, wire::void_result()));
    assert_eq!(sink.docs(), 1);
}

/// The type decides how a driver serialises the parameter, so getting `page_id`
/// wrong would not merely mislabel the column — it would change the bytes the
/// client spends CPU producing, which is the quantity being measured.
#[test]
fn a_prepared_insert_binds_the_campaign_tables_types() {
    let columns =
        bind_columns("INSERT INTO articles (article_id, page_id, title, body) VALUES (?, ?, ?, ?)");

    assert_eq!(
        names_of(&columns),
        ["article_id", "page_id", "title", "body"]
    );
    assert_eq!(
        types_of(&columns),
        [
            ColumnType::UUID,
            ColumnType::BIGINT,
            ColumnType::VARCHAR,
            ColumnType::VARCHAR
        ]
    );
}

#[test]
fn a_prepared_delete_binds_the_column_its_predicate_names() {
    let columns = bind_columns("DELETE FROM articles WHERE article_id = ?");

    assert_eq!(names_of(&columns), ["article_id"]);
    assert_eq!(types_of(&columns), [ColumnType::UUID]);
}

#[test]
fn a_prepared_statement_never_binds_fewer_columns_than_it_has_markers() {
    let columns = bind_columns("SELECT * FROM t WHERE token(x) > ?");

    assert_eq!(columns.len(), 1);
}

#[test]
fn use_returns_a_set_keyspace_result_naming_the_keyspace() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query("USE \"wiki\"");

    let mut reader = Reader::new(&body);
    assert_eq!(reader.int(), wire::RESULT_SET_KEYSPACE);
    assert_eq!(reader.string(), "wiki");
}

/// A driver asks for several different column subsets of `system.local`
/// depending on what it is refreshing, and reads the row back by position: a row
/// whose columns are not the ones the request named silently hands
/// `cluster_name` the value of `tokens`.
#[test]
fn a_local_answer_declares_exactly_the_columns_asked_for() {
    let mut sink = a_sink();
    let shapes = [
        "SELECT release_version FROM system.local",
        "SELECT broadcast_address, cluster_name, data_center, host_id, \
         listen_address, partitioner, rack, release_version, rpc_address, \
         schema_version, tokens FROM system.local WHERE key='local'",
        "SELECT schema_version FROM system.local WHERE key='local'",
        "SELECT * FROM system.local WHERE key='local'",
    ];

    for shape in shapes {
        let (_, _, body) = sink.query(shape);

        let (columns, rows) = rows_of(&body);
        assert_eq!(columns, wanted_local_columns(shape), "{shape}");
        assert_eq!(rows.len(), 1, "{shape}");
        assert_eq!(rows[0].len(), columns.len(), "{shape}");
        assert!(rows[0].iter().all(Option::is_some), "{shape}");
    }
}

/// A driver adds a host for whatever this row advertises, so a fixed 127.0.0.1
/// here would send a loader on another box off to connect to itself.
#[test]
fn local_reports_the_address_the_connection_came_in_on() {
    let mut sink = a_sink_reached_at("10.0.0.1");

    let (_, _, body) = sink.query("SELECT rpc_address FROM system.local");

    let (columns, rows) = rows_of(&body);
    assert_eq!(columns, ["rpc_address"]);
    assert_eq!(rows[0], [Some(vec![10, 0, 0, 1])]);
}

#[test]
fn system_peers_answers_columns_but_no_rows() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query("SELECT * FROM system.peers_v2");

    let (columns, rows) = rows_of(&body);
    assert!(!columns.is_empty());
    assert!(rows.is_empty());
}

/// A zero-column Rows result makes a driver fall back to the statement's cached
/// metadata, which for a one-off SELECT is none — and its row parser raises
/// rather than reading as "no rows".
#[test]
fn an_unmodelled_select_still_declares_at_least_one_column() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query("SELECT * FROM system_schema.keyspaces");

    let (columns, rows) = rows_of(&body);
    assert!(!columns.is_empty());
    assert!(rows.is_empty());
}

/// A mock that refused in silence would let a loader change land as a throughput
/// difference: the run would still complete, the number would still look like a
/// client ceiling, and nothing in the artifacts would say what went unanswered.
#[test]
fn an_unanswered_opcode_is_an_error_and_is_recorded() {
    let mut sink = a_sink();

    let (opcode, _, _) = sink.ask(&request(AN_UNANSWERED_OPCODE, 1, b""));

    assert_eq!(opcode, wire::OPCODE_ERROR);
    assert_eq!(sink.work.unexpected()["cql opcode 0x0f"], 1);
}

/// What the harness does before every concurrency level, end to end through the
/// DDL path a driver actually sends: it will not start loading until the *new*
/// index reports count 0 and status SERVING.
#[test]
fn the_reset_cycle_leaves_a_serving_index_at_zero() {
    let mut sink = a_sink();
    accept_documents(&mut sink, 270);

    apply_reset(&mut sink);

    assert!(sink.index.present());
    assert_eq!(
        sink.index.status(),
        Some(IndexStatus {
            count: 0,
            status: SERVING
        })
    );
}

/// `AcceptedWork` spans the process and feeds the mock's summary line and
/// `--stats-out`; only the index resets with the keyspace, or a six-level ladder
/// would report the documents of its last level as the documents of the run.
#[test]
fn a_reset_zeroes_the_index_but_not_the_runs_own_total() {
    let mut sink = a_sink();
    accept_documents(&mut sink, 100);

    apply_reset(&mut sink);
    accept_documents(&mut sink, 30);

    assert_eq!(sink.docs(), 130);
    assert_eq!(sink.index.count(), 30);
}

/// A KEYSPACE target carries three fields, not four. A fourth would leave the
/// driver reading the next statement's bytes as a name, and blocking on a schema
/// agreement that describes an object nobody named.
#[test]
fn a_dropped_keyspace_is_announced_with_no_name_after_it() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query(RESET_CQL[0]);

    assert_eq!(schema_change_fields(&body), ["DROPPED", "KEYSPACE", "wiki"]);
}

/// v4 has no INDEX target: an index is announced as a change to the table it
/// lives on, which is also what the driver will go and re-read.
#[test]
fn a_created_custom_index_is_announced_as_an_updated_table() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query(RESET_CQL[3]);

    assert_eq!(
        schema_change_fields(&body),
        ["UPDATED", "TABLE", "wiki", "articles"]
    );
    assert_eq!(
        sink.index.status(),
        Some(IndexStatus {
            count: 0,
            status: SERVING
        })
    );
}

/// The loader holds `--concurrency` statements outstanding on one connection, so
/// a read commonly carries many frames and a write per frame would put a syscall
/// per document between the client and its own ceiling. A frame torn across two
/// reads has to stay in the buffer instead: answering it would answer a document
/// nobody finished sending.
#[test]
fn answers_for_drains_every_whole_frame_and_leaves_a_torn_one_behind() {
    let mut sink = a_sink();
    sink.prepare(AN_INSERT);
    let body = short_bytes(&query_id(AN_INSERT));
    let frames: Vec<u8> = (0..3)
        .flat_map(|stream| wire_request(wire::OPCODE_EXECUTE, stream, &body))
        .collect();
    let buffer = [frames.clone(), frames[..4].to_vec()].concat();

    let mut out = Vec::new();
    let cursor = answers_for(&buffer, &mut sink.handler, &mut out).expect("whole frames");

    let answers = parsed_all(&out);
    assert_eq!(cursor, frames.len());
    assert_eq!(
        answers
            .iter()
            .map(|(_, stream, _)| *stream)
            .collect::<Vec<_>>(),
        [0, 1, 2]
    );
    assert!(answers
        .iter()
        .all(|(opcode, _, body)| *opcode == wire::OPCODE_RESULT && *body == wire::void_result()));
    assert_eq!(sink.docs(), 3);
}

/// A frame the splitter cannot read ends the connection, as it did in Python.
/// Recording it is what keeps a client that started corrupting frames from
/// showing up only as dropped connections in the loader's own error count.
#[test]
fn a_frame_the_splitter_cannot_read_is_recorded_before_the_connection_goes() {
    let mut sink = a_sink();
    let mut absurd = vec![wire::REQUEST_VERSION, 0, 0, 1, wire::OPCODE_EXECUTE];
    absurd.extend_from_slice(&u32::MAX.to_be_bytes());

    let refused = answers_for(&absurd, &mut sink.handler, &mut Vec::new());

    assert!(refused.is_err());
    assert_eq!(
        sink.work.unexpected().into_keys().collect::<Vec<_>>(),
        vec!["malformed cql frame".to_string()]
    );
}

/// `system.peers` is matched by substring, so a `system.peers_v2` query is
/// answered from the same column list. The two columns only `peers_v2` has are
/// in it because the first driver version that prefers `peers_v2` would
/// otherwise fail its typed read of a column this mock had declared as text —
/// in the middle of a fleet run, looking like the mock refusing to connect.
#[test]
fn a_peers_v2_query_is_answered_with_the_columns_only_peers_v2_has() {
    let mut sink = a_sink();

    let (_, _, body) = sink.query("SELECT * FROM system.peers_v2");
    let (declared, rows) = rows_of(&body);

    assert!(declared.iter().any(|name| name == "native_address"));
    assert!(declared.iter().any(|name| name == "native_port"));
    assert!(rows.is_empty());
}

/// One connection checks the last statement it used before it looks anything
/// up, and a driver holding two prepared statements alternates between them.
/// A cache that answered from `last` without comparing the id would hand an
/// EXECUTE of the SELECT the INSERT's answer — counting a metadata read as a
/// document, which is the failure that produced wrong Phase 0 numbers before.
#[test]
fn two_statements_prepared_on_one_connection_stay_told_apart() {
    let mut sink = a_sink();
    let insert = prepared_statement_id(&sink.prepare(AN_INSERT).2);
    let select = prepared_statement_id(&sink.prepare(A_SELECT).2);

    let first = sink.execute(&insert, 1);
    let metadata = sink.execute(&select, 2);
    let second = sink.execute(&insert, 3);

    assert_eq!(kind_of(&first.2), wire::RESULT_VOID);
    assert_eq!(kind_of(&metadata.2), wire::RESULT_ROWS);
    assert_eq!(kind_of(&second.2), wire::RESULT_VOID);
    assert_eq!(sink.docs(), 2, "the metadata read is not a document");
}

/// The PREPARED result declares the keyspace and table the statement is
/// against, which is what a driver reads to route it. Nothing read them back
/// before, so the whole parse could be replaced by a constant and the suite
/// stayed green.
#[test]
fn a_prepared_result_names_the_keyspace_and_table_the_statement_is_against() {
    let mut sink = a_sink();

    let (_, _, body) = sink.prepare(AN_INSERT);
    let mut reader = Reader::new(&body);

    assert_eq!(reader.int(), wire::RESULT_PREPARED);
    let _id = reader.bytes();
    let _flags = reader.int();
    let _columns = reader.int();
    let _partition_key = reader.int();
    assert_eq!(reader.string(), "wiki");
    assert_eq!(reader.string(), "articles");
}
