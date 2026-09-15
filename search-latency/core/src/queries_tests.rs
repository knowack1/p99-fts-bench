use super::*;

const SET: &str = r#"{
  "corpus": "data/corpus.jsonl",
  "classes": {
    "rare_term": ["kraken", "zeppelin"],
    "phrase": ["\"united states\""],
    "empty_class": []
  }
}"#;

fn parsed() -> QuerySet {
    QuerySet::parse(SET, "test").unwrap()
}

#[test]
fn a_query_set_carries_the_corpus_its_terms_came_from() {
    assert_eq!(parsed().corpus(), "data/corpus.jsonl");
}

#[test]
fn classes_are_listed_in_name_order() {
    assert_eq!(parsed().names(), ["empty_class", "phrase", "rare_term"]);
}

#[test]
fn selecting_nothing_would_take_every_class_including_an_empty_one() {
    let refused = parsed().select(&[]).unwrap_err().to_string();

    assert!(refused.contains("empty_class"), "{refused}");
}

#[test]
fn a_named_selection_keeps_the_order_it_was_asked_in() {
    let selected = parsed()
        .select(&["phrase".to_string(), "rare_term".to_string()])
        .unwrap();

    let names: Vec<&str> = selected.iter().map(QueryClass::name).collect();
    assert_eq!(names, ["phrase", "rare_term"]);
}

/// A typo must not cost a whole matrix to discover, and the message has to name
/// what the set does have.
#[test]
fn an_unknown_class_name_is_refused_and_lists_the_real_ones() {
    let refused = parsed()
        .select(&["rare_trem".to_string()])
        .unwrap_err()
        .to_string();

    assert!(refused.contains("rare_trem"), "{refused}");
    assert!(refused.contains("phrase"), "{refused}");
}

#[test]
fn an_empty_class_cannot_be_measured() {
    let refused = parsed()
        .select(&["empty_class".to_string()])
        .unwrap_err()
        .to_string();

    assert!(refused.contains("empty"), "{refused}");
}

#[test]
fn a_file_with_no_classes_is_not_a_query_set() {
    assert!(QuerySet::parse(r#"{"classes": {}}"#, "test").is_err());
}

#[test]
fn something_that_is_not_a_query_set_says_what_one_looks_like() {
    let refused = QuerySet::parse(r#"{"queries": ["kraken"]}"#, "set.json")
        .unwrap_err()
        .to_string();

    assert!(refused.contains("set.json"), "{refused}");
    assert!(refused.contains("classes"), "{refused}");
}

/// The selected classes, not the whole set: what an interface prepares has to
/// be exactly what the matrix will ask it.
#[test]
fn only_the_selected_classes_reach_an_interface_that_prepares_one_statement_each() {
    let set = parsed();
    let selected = set.select(&["rare_term".to_string()]).unwrap();

    let mut prepared = queries_of(&selected);
    prepared.sort_unstable();

    assert_eq!(prepared, ["kraken", "zeppelin"]);
}

#[test]
fn every_selected_class_contributes_its_queries_in_order() {
    let set = parsed();
    let selected = set
        .select(&["phrase".to_string(), "rare_term".to_string()])
        .unwrap();

    assert_eq!(
        queries_of(&selected),
        ["\"united states\"", "kraken", "zeppelin"]
    );
}

#[test]
fn a_rotation_hands_out_the_queries_in_order_and_wraps() {
    let class = parsed()
        .select(&["rare_term".to_string()])
        .unwrap()
        .remove(0);
    let rotation = class.rotation();

    let handed: Vec<&str> = (0..5).map(|_| rotation.next()).collect();

    assert_eq!(
        handed,
        ["kraken", "zeppelin", "kraken", "zeppelin", "kraken"]
    );
}

/// A cell always asks the same sequence however long the cell before it ran,
/// which is what makes two repetitions of a cell repetitions.
#[test]
fn each_cell_gets_a_rotation_that_starts_at_the_first_query() {
    let class = parsed()
        .select(&["rare_term".to_string()])
        .unwrap()
        .remove(0);
    let first = class.rotation();
    first.next();

    assert_eq!(class.rotation().next(), "kraken");
}

#[test]
fn a_class_reports_how_many_distinct_queries_a_cell_will_cycle_through() {
    let class = parsed()
        .select(&["rare_term".to_string()])
        .unwrap()
        .remove(0);

    assert_eq!(class.distinct(), 2);
    assert_eq!(class.rotation().len(), 2);
}

/// The class name is a CSV column in every row it produces, and this harness
/// writes that CSV without quoting.
#[test]
fn a_class_name_that_would_break_the_csv_is_refused_when_the_set_is_read() {
    let refused = QuerySet::parse(r#"{"classes": {"rare,term": ["kraken"]}}"#, "set.json")
        .unwrap_err()
        .to_string();

    assert!(refused.contains("separator"), "{refused}");
    assert!(refused.contains("rare,term"), "{refused}");
}
