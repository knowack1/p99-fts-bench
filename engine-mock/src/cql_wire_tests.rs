use super::*;

fn read_i32(bytes: &[u8], offset: usize) -> (i32, usize) {
    let end = offset + 4;
    let word: [u8; 4] = bytes[offset..end].try_into().unwrap();
    (i32::from_be_bytes(word), end)
}

fn read_u16(bytes: &[u8], offset: usize) -> (u16, usize) {
    let end = offset + 2;
    let half: [u8; 2] = bytes[offset..end].try_into().unwrap();
    (u16::from_be_bytes(half), end)
}

fn read_short_bytes(bytes: &[u8], offset: usize) -> (Vec<u8>, usize) {
    let (length, start) = read_u16(bytes, offset);
    let end = start + length as usize;
    (bytes[start..end].to_vec(), end)
}

fn read_short_string(bytes: &[u8], offset: usize) -> (String, usize) {
    let (value, end) = read_short_bytes(bytes, offset);
    (String::from_utf8(value).unwrap(), end)
}

fn read_string_list(bytes: &[u8], offset: usize) -> (Vec<String>, usize) {
    let (count, mut at) = read_u16(bytes, offset);
    let mut values = Vec::new();
    for _ in 0..count {
        let (value, next) = read_short_string(bytes, at);
        values.push(value);
        at = next;
    }
    (values, at)
}

fn read_string_multimap(bytes: &[u8]) -> Vec<(String, Vec<String>)> {
    let (count, mut at) = read_u16(bytes, 0);
    let mut entries = Vec::new();
    for _ in 0..count {
        let (key, next) = read_short_string(bytes, at);
        let (values, next) = read_string_list(bytes, next);
        entries.push((key, values));
        at = next;
    }
    entries
}

fn read_cell(bytes: &[u8], offset: usize) -> (Option<Vec<u8>>, usize) {
    let (length, start) = read_i32(bytes, offset);
    if length < 0 {
        return (None, start);
    }
    let end = start + length as usize;
    (Some(bytes[start..end].to_vec()), end)
}

fn column_type_bytes(bytes: &[u8], offset: usize) -> usize {
    match read_u16(bytes, offset).0 {
        TYPE_SET | TYPE_LIST => 4,
        TYPE_MAP => 6,
        _ => 2,
    }
}

fn read_column(bytes: &[u8], offset: usize) -> ((String, Vec<u8>), usize) {
    let (name, start) = read_short_string(bytes, offset);
    let end = start + column_type_bytes(bytes, start);
    ((name, bytes[start..end].to_vec()), end)
}

fn read_columns(bytes: &[u8], offset: usize, count: i32) -> (Vec<(String, Vec<u8>)>, usize) {
    let mut at = offset;
    let mut columns = Vec::new();
    for _ in 0..count {
        let (column, next) = read_column(bytes, at);
        columns.push(column);
        at = next;
    }
    (columns, at)
}

fn read_row(bytes: &[u8], offset: usize, columns: i32) -> (Row, usize) {
    let mut at = offset;
    let mut row = Row::new();
    for _ in 0..columns {
        let (value, next) = read_cell(bytes, at);
        row.push(value);
        at = next;
    }
    (row, at)
}

fn read_rows(bytes: &[u8], offset: usize, rows: i32, columns: i32) -> (Vec<Row>, usize) {
    let mut at = offset;
    let mut taken = Vec::new();
    for _ in 0..rows {
        let (row, next) = read_row(bytes, at, columns);
        taken.push(row);
        at = next;
    }
    (taken, at)
}

fn simple_type(id: u16) -> Vec<u8> {
    id.to_be_bytes().to_vec()
}

struct Header {
    version: u8,
    flags: u8,
    stream: i16,
    opcode: u8,
    length: usize,
}

fn read_header(bytes: &[u8]) -> Header {
    Header {
        version: bytes[0],
        flags: bytes[1],
        stream: read_u16(bytes, 2).0 as i16,
        opcode: bytes[4],
        length: read_i32(bytes, 5).0 as usize,
    }
}

fn header_claiming(length: u32) -> Vec<u8> {
    let mut head = vec![REQUEST_VERSION, 0, 0, 1, OPCODE_EXECUTE];
    head.extend_from_slice(&length.to_be_bytes());
    head
}

fn cell(value: Option<&[u8]>) -> Vec<u8> {
    let mut out = Vec::new();
    write_cell(&mut out, value);
    out
}

fn schema_change_fields(body: &[u8]) -> Vec<String> {
    let (kind, mut at) = read_i32(body, 0);
    assert_eq!(kind, RESULT_SCHEMA_CHANGE);
    let mut fields = Vec::new();
    while at < body.len() {
        let (value, next) = read_short_string(body, at);
        fields.push(value);
        at = next;
    }
    fields
}

struct RowsResult {
    kind: i32,
    flags: i32,
    column_count: i32,
    keyspace: String,
    table: String,
    columns: Vec<(String, Vec<u8>)>,
    row_count: i32,
    rows: Vec<Row>,
    end: usize,
}

fn read_rows_result(body: &[u8]) -> RowsResult {
    let (kind, at) = read_i32(body, 0);
    let (flags, at) = read_i32(body, at);
    let (column_count, at) = read_i32(body, at);
    let (keyspace, at) = read_short_string(body, at);
    let (table, at) = read_short_string(body, at);
    let (columns, at) = read_columns(body, at, column_count);
    let (row_count, at) = read_i32(body, at);
    let (rows, end) = read_rows(body, at, row_count, column_count);
    RowsResult {
        kind,
        flags,
        column_count,
        keyspace,
        table,
        columns,
        row_count,
        rows,
        end,
    }
}

struct PreparedResult {
    kind: i32,
    id: Vec<u8>,
    flags: i32,
    bind_count: i32,
    partition_keys: i32,
    keyspace: String,
    table: String,
    bind_columns: Vec<(String, Vec<u8>)>,
    result_metadata: (i32, i32),
    end: usize,
}

fn read_prepared_result(body: &[u8]) -> PreparedResult {
    let (kind, at) = read_i32(body, 0);
    let (id, at) = read_short_bytes(body, at);
    let (flags, at) = read_i32(body, at);
    let (bind_count, at) = read_i32(body, at);
    let (partition_keys, at) = read_i32(body, at);
    let (keyspace, at) = read_short_string(body, at);
    let (table, at) = read_short_string(body, at);
    let (bind_columns, at) = read_columns(body, at, bind_count);
    let (metadata_flags, at) = read_i32(body, at);
    let (metadata_columns, end) = read_i32(body, at);
    PreparedResult {
        kind,
        id,
        flags,
        bind_count,
        partition_keys,
        keyspace,
        table,
        bind_columns,
        result_metadata: (metadata_flags, metadata_columns),
        end,
    }
}

/// The driver reads exactly the many body bytes the header declares and then
/// expects the next header: a length that disagrees with the body behind it
/// desynchronises the whole connection rather than spoiling one answer.
#[test]
fn a_response_frame_is_v4_unflagged_and_declares_the_body_it_carries() {
    let body = b"a body";

    let bytes = frame(OPCODE_RESULT, 7, body);
    let head = read_header(&bytes);

    assert_eq!(head.version, 0x84);
    assert_eq!(head.flags, 0);
    assert_eq!(head.stream, 7);
    assert_eq!(head.opcode, OPCODE_RESULT);
    assert_eq!(head.length, body.len());
    assert_eq!(&bytes[HEADER_BYTES..], body);
}

/// The stream id is a signed short, and it is how the driver matches an answer
/// to the request that asked for it. Written back through an unsigned path it
/// answers a stream nobody opened, and the one that did wait times out.
#[test]
fn a_negative_stream_id_survives_the_header_as_the_same_negative_number() {
    let bytes = frame(OPCODE_RESULT, -12345, b"");

    let (taken, cursor) = take_frame(&bytes, 0).unwrap().unwrap();

    assert_eq!(&bytes[2..4], (-12345i16).to_be_bytes().as_slice());
    assert_eq!(read_header(&bytes).stream, -12345);
    assert_eq!(taken.stream, -12345);
    assert_eq!(cursor, bytes.len());
}

/// Every accepted document in a run is answered by this hand-written path, and
/// it exists only to skip the body the general one would allocate and copy. A
/// drift between the two is a wrong answer on every document, not on one.
#[test]
fn the_void_fast_path_writes_the_same_thirteen_bytes_as_the_general_one() {
    for stream in [0i16, 1, -1, i16::MAX] {
        let mut fast = Vec::new();
        write_void_frame(&mut fast, stream);

        assert_eq!(
            fast,
            frame(OPCODE_RESULT, stream, &void_result()),
            "stream {stream}"
        );
        assert_eq!(fast.len(), HEADER_BYTES + 4);
    }
    assert_eq!(void_result(), vec![0x00, 0x00, 0x00, 0x01]);
}

/// A read hands back the bytes that had arrived, not whole frames. The tail of
/// a split frame has to be kept for the next read rather than parsed as far as
/// it goes.
#[test]
fn take_frame_keeps_a_partial_frame_for_the_next_read() {
    let whole = frame(OPCODE_EXECUTE, 1, b"abc");

    assert!(take_frame(&whole[..whole.len() - 1], 0).unwrap().is_none());

    let (taken, cursor) = take_frame(&whole, 0).unwrap().unwrap();

    assert_eq!(taken.body, b"abc");
    assert_eq!(cursor, whole.len());
}

/// Nine bytes is the smallest whole frame there is: an OPTIONS carries no body,
/// so a header short by one must wait while a complete one must be answered.
#[test]
fn a_header_that_has_not_all_arrived_is_not_read_as_one() {
    let bodiless = frame(OPCODE_OPTIONS, 1, b"");

    assert!(take_frame(&[], 0).unwrap().is_none());
    assert!(take_frame(&bodiless[..HEADER_BYTES - 1], 0)
        .unwrap()
        .is_none());

    let (taken, cursor) = take_frame(&bodiless, 0).unwrap().unwrap();

    assert_eq!(taken.opcode, OPCODE_OPTIONS);
    assert!(taken.body.is_empty());
    assert_eq!(cursor, HEADER_BYTES);
}

/// The caller advances a cursor once per read instead of trimming the buffer
/// per frame, so a read carrying many pipelined statements is answered from one
/// buffer: a second frame that could not be found there is a lost answer.
#[test]
fn take_frame_reads_a_second_frame_from_the_cursor_it_returned() {
    let mut buffer = frame(OPCODE_EXECUTE, 1, b"one");
    buffer.extend_from_slice(&frame(OPCODE_EXECUTE, 2, b"two"));
    buffer.extend_from_slice(&frame(OPCODE_EXECUTE, 3, b"three")[..4]);

    let (first, cursor) = take_frame(&buffer, 0).unwrap().unwrap();
    let (second, cursor) = take_frame(&buffer, cursor).unwrap().unwrap();

    assert_eq!((first.stream, first.body), (1, b"one".as_slice()));
    assert_eq!((second.stream, second.body), (2, b"two".as_slice()));
    assert!(take_frame(&buffer, cursor).unwrap().is_none());
    assert_eq!(buffer.len() - cursor, 4);
}

/// A declared length is a claim, not a fact. Waiting on an oversized one
/// buffers whatever a confused client says it is sending until the box runs out
/// of memory — a failure that reads as the loader stalling.
#[test]
fn a_frame_claiming_more_than_the_ceiling_is_refused_not_awaited() {
    let at_the_ceiling = header_claiming(MAX_FRAME_BYTES as u32);
    let over_it = header_claiming(MAX_FRAME_BYTES as u32 + 1);

    assert!(take_frame(&at_the_ceiling, 0).unwrap().is_none());

    let refused = take_frame(&over_it, 0).unwrap_err();

    assert!(
        refused.contains(&(MAX_FRAME_BYTES + 1).to_string()),
        "{refused}"
    );
}

/// `None` is the protocol's null (-1 length), which is not the same as a
/// zero-length value and must not be spelled as one.
#[test]
fn a_null_cell_is_not_an_empty_one() {
    assert_eq!(cell(None), vec![0xFF, 0xFF, 0xFF, 0xFF]);
    assert_eq!(cell(Some(b"")), vec![0x00, 0x00, 0x00, 0x00]);
}

#[test]
fn a_uuid_cell_round_trips_through_the_wire_encoding() {
    let value = Uuid::new_v4();

    let encoded = cell(Some(&uuid_value(value)));

    assert_eq!(read_i32(&encoded, 0).0, 16);
    assert_eq!(&encoded[4..], value.as_bytes().as_slice());
}

/// The address comes from the accepted socket rather than from configuration:
/// a mock bound on an IPv6 address that answered a 4-byte `inet` would drop
/// every connection while looking like a client ceiling.
#[test]
fn an_inet_value_is_four_bytes_for_ipv4_and_sixteen_for_ipv6() {
    let mut loopback_v6 = vec![0u8; 16];
    loopback_v6[15] = 1;

    assert_eq!(inet_value("127.0.0.1"), vec![127, 0, 0, 1]);
    assert_eq!(inet_value("::1"), loopback_v6);
}

/// SUPPORTED advertises COMPRESSION with nothing in it, so a driver finds no
/// overlap and sends plain frames. Advertising lz4 here would oblige the mock
/// to decompress, and it cannot.
#[test]
fn supported_offers_a_cql_version_and_a_compression_list_with_nothing_in_it() {
    let body = string_multimap(&[("CQL_VERSION", &["3.3.1"] as &[&str]), ("COMPRESSION", &[])]);

    let entries = read_string_multimap(&body);

    assert_eq!(
        entries,
        vec![
            ("CQL_VERSION".to_string(), vec!["3.3.1".to_string()]),
            ("COMPRESSION".to_string(), Vec::new()),
        ]
    );
}

/// The driver keys its downgrade off this substring, not off the error code, so
/// the wording is what makes a v5 `Cluster()` come back as v4 instead of giving
/// up at STARTUP.
#[test]
fn the_version_error_carries_the_words_the_driver_downgrades_on() {
    let body = unsupported_version_body(5);

    let (code, at) = read_i32(&body, 0);
    let (message, end) = read_short_string(&body, at);

    assert_eq!(code, ERROR_PROTOCOL);
    assert!(
        message.contains("unsupported protocol version"),
        "{message}"
    );
    assert!(message.contains("(5)"), "{message}");
    assert_eq!(message, unsupported_version_message(5));
    assert_eq!(end, body.len());
}

/// The id is part of the error body, not decoration: the driver keys its
/// re-prepare on it.
#[test]
fn an_unprepared_error_carries_the_statement_id_after_the_message() {
    let id: Vec<u8> = (0u8..16).collect();

    let body = unprepared_body(&id);
    let (code, at) = read_i32(&body, 0);
    let (message, at) = read_short_string(&body, at);
    let (carried, end) = read_short_bytes(&body, at);

    assert_eq!(code, ERROR_UNPREPARED);
    assert_eq!(message, "unknown prepared statement");
    assert_eq!(carried, id);
    assert_eq!(end, body.len());
}

/// A KEYSPACE target carries three fields, not four. A fourth would leave the
/// driver reading the next statement's bytes as a name.
#[test]
fn a_dropped_keyspace_is_announced_with_no_name_after_it() {
    let body = schema_change_result(SCHEMA_DROPPED, SCHEMA_TARGET_KEYSPACE, "wiki", "articles");

    assert_eq!(schema_change_fields(&body), ["DROPPED", "KEYSPACE", "wiki"]);
}

/// Every other target does carry a name after the keyspace — it names the
/// object the driver goes and re-reads before it reports schema agreement.
#[test]
fn a_changed_table_is_announced_with_its_name_after_the_keyspace() {
    let body = schema_change_result(SCHEMA_UPDATED, SCHEMA_TARGET_TABLE, "wiki", "articles");

    assert_eq!(
        schema_change_fields(&body),
        ["UPDATED", "TABLE", "wiki", "articles"]
    );
}

/// A Rows result the driver cannot parse is not a loud failure: it is a
/// connection that stops answering, and a complete, plausible, wrong number.
#[test]
fn a_rows_result_declares_its_columns_their_types_and_its_row_count() {
    let columns = [
        Column::new("keyspace_name", ColumnType::VARCHAR),
        Column::new("page_id", ColumnType::BIGINT),
    ];
    let rows = vec![
        vec![
            Some(varchar_value("wiki")),
            Some(7i64.to_be_bytes().to_vec()),
        ],
        vec![Some(varchar_value("other")), None],
    ];

    let body = rows_result("system_schema", "keyspaces", &columns, &rows);
    let result = read_rows_result(&body);

    assert_eq!(result.kind, RESULT_ROWS);
    assert_eq!(result.flags, 0x0001);
    assert_eq!(result.column_count, 2);
    assert_eq!(result.keyspace, "system_schema");
    assert_eq!(result.table, "keyspaces");
    assert_eq!(
        result.columns,
        vec![
            ("keyspace_name".to_string(), simple_type(TYPE_VARCHAR)),
            ("page_id".to_string(), simple_type(TYPE_BIGINT)),
        ]
    );
    assert_eq!(result.row_count, 2);
    assert_eq!(result.rows, rows);
    assert_eq!(result.end, body.len());
}

/// The driver parses the column metadata before it looks for rows, so an empty
/// answer that declared no columns is unreadable rather than empty.
#[test]
fn a_rows_result_with_no_rows_still_declares_its_columns() {
    let columns = [Column::new("peer", ColumnType::INET)];

    let body = rows_result("system", "peers", &columns, &[]);
    let result = read_rows_result(&body);

    assert_eq!(result.column_count, 1);
    assert_eq!(
        result.columns,
        vec![("peer".to_string(), simple_type(TYPE_INET))]
    );
    assert_eq!(result.row_count, 0);
    assert!(result.rows.is_empty());
    assert_eq!(result.end, body.len());
}

/// The driver executes against the id and binds against the types declared
/// here; the empty partition-key list costs it only its routing hint, and the
/// empty result-metadata block is how an INSERT says it returns no rows.
#[test]
fn a_prepared_result_carries_its_id_its_bindings_and_an_empty_result_block() {
    let id: Vec<u8> = (0u8..16).map(|byte| byte * 3).collect();
    let bind_columns = [
        Column::new("article_id", ColumnType::UUID),
        Column::new("body", ColumnType::VARCHAR),
    ];

    let body = prepared_result(&id, "wiki", "articles", &bind_columns);
    let prepared = read_prepared_result(&body);

    assert_eq!(prepared.kind, RESULT_PREPARED);
    assert_eq!(prepared.id, id);
    assert_eq!(prepared.flags, 0x0001);
    assert_eq!(prepared.bind_count, 2);
    assert_eq!(prepared.partition_keys, 0);
    assert_eq!(prepared.keyspace, "wiki");
    assert_eq!(prepared.table, "articles");
    assert_eq!(
        prepared.bind_columns,
        vec![
            ("article_id".to_string(), simple_type(TYPE_UUID)),
            ("body".to_string(), simple_type(TYPE_VARCHAR)),
        ]
    );
    assert_eq!(prepared.result_metadata, (0, 0));
    assert_eq!(prepared.end, body.len());
}

/// A collection type is two ids, not one. A reader handed a single id takes the
/// element id for the length of the next column's name and misparses the rest
/// of the metadata.
#[test]
fn a_set_column_is_the_set_id_followed_by_what_it_holds() {
    let columns = [Column::new("tags", ColumnType::SET_VARCHAR)];

    let body = rows_result("wiki", "articles", &columns, &[]);
    let result = read_rows_result(&body);

    assert_eq!(
        result.columns,
        vec![("tags".to_string(), vec![0x00, 0x22, 0x00, 0x0D])]
    );
    assert_eq!(result.end, body.len());
}
