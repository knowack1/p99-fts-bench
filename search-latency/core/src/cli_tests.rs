use super::*;

#[test]
fn a_class_list_keeps_the_order_it_was_written_in() {
    let classes: Classes = "phrase, rare_term ,bool_and".parse().unwrap();

    assert_eq!(classes.0, ["phrase", "rare_term", "bool_and"]);
    assert_eq!(classes.to_string(), "phrase,rare_term,bool_and");
}

#[test]
fn a_list_of_separators_is_not_a_class_list() {
    assert!(",, ,".parse::<Classes>().is_err());
}

/// The header has to be able to say that the run did not narrow the set, rather
/// than leaving a blank a reader has to interpret.
#[test]
fn a_flag_that_was_not_given_reads_as_every_class() {
    assert_eq!(classes_setting(None), ALL_CLASSES);
    assert!(chosen_classes(None).is_empty());
}

/// `QuerySet::select` takes an empty list to mean every class, so the absence
/// of the flag has to arrive there as an empty list and not as the word.
#[test]
fn a_flag_that_was_given_reaches_the_selection_and_the_header_alike() {
    let chosen: Classes = "phrase,rare_term".parse().unwrap();

    assert_eq!(chosen_classes(Some(&chosen)), ["phrase", "rare_term"]);
    assert_eq!(classes_setting(Some(&chosen)), "phrase,rare_term");
}

#[test]
fn a_measured_window_of_zero_seconds_is_not_a_cell() {
    assert!(positive_seconds("0", "--duration").is_err());
    assert!(positive_seconds("-3", "--duration").is_err());
    assert!(positive_seconds("nope", "--duration").is_err());
    assert_eq!(positive_seconds("20", "--duration"), Ok(20.0));
}

#[test]
fn declining_to_warm_up_is_allowed_and_mistyping_a_warm_up_is_not() {
    assert_eq!(non_negative_seconds("0", "--warmup"), Ok(0.0));
    assert!(non_negative_seconds("-1", "--warmup").is_err());
}

#[test]
fn a_ladder_and_a_worker_count_keep_the_floors_the_sibling_set() {
    assert!("0".parse::<Levels>().is_err());
    assert!(at_least_one("0", "--tokio-workers").is_err());
    assert_eq!("8,8,16".parse::<Levels>().unwrap().0, [8, 8, 16]);
}
