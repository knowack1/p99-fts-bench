use super::*;

fn a_reading(docs: u64, ready: bool) -> IndexReading {
    IndexReading {
        docs,
        accepted: None,
        status: if ready { "SERVING" } else { "BUILDING" }.to_string(),
        ready,
    }
}

#[test]
fn an_index_that_is_present_but_not_answering_is_not_ready() {
    let building = IndexState::Present(a_reading(0, false));
    assert!(building.reading().is_some());
    assert!(building.ready().is_none());
}

/// The gate that waits for a dropped index accepts a present-but-building one,
/// because that is the state the vector-store reports between the drop landing
/// and the new index answering. Folding `ready` into a variant would have
/// silently narrowed that gate to "absent".
#[test]
fn readiness_is_a_property_of_a_reading_not_a_state_of_its_own() {
    let building = IndexState::Present(a_reading(7, false));
    assert!(!building.is_absent());
    assert!(building.ready().is_none());
    assert_eq!(building.docs(), 7);
}

#[test]
fn an_absent_index_holds_nothing_and_says_so() {
    assert_eq!(IndexState::Absent.docs(), 0);
    assert_eq!(IndexState::Absent.status_word(), ABSENT);
    assert!(IndexState::Absent.is_absent());
}

/// "The index is not there" and "the engine is not answering" are the two
/// states a reset gate must never confuse.
#[test]
fn an_unreadable_poll_is_not_an_absent_index() {
    let unreadable = IndexState::Unreadable("connection refused".to_string());
    assert!(!unreadable.is_absent());
    assert!(unreadable.reading().is_none());
    assert!(unreadable.describe().contains("connection refused"));
}

#[test]
fn an_engine_that_counts_accepted_documents_reports_both() {
    let reading = IndexReading {
        accepted: Some(900),
        ..a_reading(300, true)
    };
    let state = IndexState::Present(reading);
    assert_eq!((state.docs(), state.accepted()), (300, Some(900)));
}

/// The vector-store has no second counter, and a `Some(0)` there would read as
/// an engine that had accepted nothing rather than one that does not say.
#[test]
fn an_engine_with_one_counter_reports_no_accepted_count_at_all() {
    assert_eq!(IndexState::Present(a_reading(300, true)).accepted(), None);
    assert_eq!(IndexState::Absent.accepted(), None);
}

#[test]
fn a_description_names_the_status_and_the_count() {
    let serving = IndexState::Present(a_reading(270_269, true));
    assert_eq!(serving.describe(), "SERVING at 270269 docs");
}
