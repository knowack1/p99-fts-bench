use super::*;

fn a_shape() -> QueryShape {
    QueryShape {
        table: "articles".to_string(),
        column: "body".to_string(),
        limit: 10,
        fetch_documents: false,
    }
}

/// The M1 shape is not a template this harness may vary: the same term in the
/// `WHERE` and in the `ORDER BY`, a mandatory `LIMIT`, and no other
/// restriction.
#[test]
fn the_statement_is_the_one_shape_m1_supports() {
    let statement = a_shape().statement("kraken");

    assert_eq!(
        statement,
        "SELECT article_id FROM articles WHERE BM25(body, 'kraken') > 0 \
         ORDER BY BM25(body, 'kraken') LIMIT 10"
    );
}

#[test]
fn asking_for_documents_projects_the_columns_the_other_engine_projects() {
    let shape = QueryShape {
        fetch_documents: true,
        ..a_shape()
    };

    assert!(shape
        .statement("kraken")
        .starts_with("SELECT article_id, title, body FROM articles"));
}

#[test]
fn the_top_n_reaches_the_limit_clause() {
    let shape = QueryShape {
        limit: 1000,
        ..a_shape()
    };

    assert!(shape.statement("kraken").ends_with("LIMIT 1000"));
}

#[test]
fn the_indexed_column_is_the_one_bm25_is_called_on() {
    let shape = QueryShape {
        column: "summary".to_string(),
        ..a_shape()
    };

    assert!(shape
        .statement("kraken")
        .contains("BM25(summary, 'kraken')"));
}

/// A query set built from real article text contains apostrophes, and a single
/// quote that reached the coordinator unescaped would end the string literal
/// mid-query.
#[test]
fn an_apostrophe_is_doubled_on_both_sides_of_the_statement() {
    let statement = a_shape().statement("o'brien");

    assert_eq!(statement.matches("'o''brien'").count(), 2);
    assert_eq!(escape("it's o'brien"), "it''s o''brien");
}

#[test]
fn a_quoted_phrase_survives_into_the_statement_as_a_quoted_phrase() {
    let statement = a_shape().statement("\"united states\"");

    assert!(
        statement.contains("BM25(body, '\"united states\"')"),
        "{statement}"
    );
}

#[test]
fn a_statement_mode_names_itself_for_the_header() {
    assert_eq!(StatementMode::Literal.name(), LITERAL);
    assert_eq!(StatementMode::Prepared.name(), PREPARED);
}

/// A miss in `prepared` mode must not fall back to a coordinator parse: the
/// header would still say `statement=prepared` and nothing in the row could
/// tell that it had.
#[test]
fn a_prepared_run_asked_a_query_it_never_prepared_fails_loudly() {
    let nothing_prepared = HashMap::new();

    let refused = prepared_for(StatementMode::Prepared, &nothing_prepared, "kraken")
        .unwrap_err()
        .to_string();

    assert!(refused.contains("kraken"), "{refused}");
    assert!(refused.contains("no prepared statement"), "{refused}");
}

#[test]
fn a_literal_run_never_looks_for_a_prepared_statement() {
    let nothing_prepared = HashMap::new();

    assert!(
        prepared_for(StatementMode::Literal, &nothing_prepared, "kraken")
            .unwrap()
            .is_none()
    );
}
