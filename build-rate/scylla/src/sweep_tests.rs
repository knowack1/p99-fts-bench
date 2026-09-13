//! The sweep itself is `build_rate_core`'s and tested there. What belongs here
//! is the one property that is this half's alone: a request carries exactly one
//! document, and carries it without a container.
use super::*;
use crate::corpus::InsertParams;
use crate::insert::CqlInserter;

/// The harness exists to have a higher ceiling than the engine it measures, so
/// the shape of the thing that moves per document is load-bearing. Wrapping it
/// — a one-element `Vec` to share a batch type with the other half — would put
/// a `malloc`/`free` pair on the hottest path, and it would show up only
/// against the null sink, which is the one measurement the campaign depends on.
#[test]
fn a_request_on_this_half_is_one_document_and_carries_no_container() {
    assert_eq!(
        std::mem::size_of::<<CqlInserter as Inserter>::Item>(),
        std::mem::size_of::<InsertParams>()
    );
}

#[test]
fn one_document_per_request_is_what_this_half_reports() {
    let params = InsertParams {
        article_id: uuid::Uuid::nil(),
        page_id: 1,
        title: "t".to_string(),
        body: "b".to_string(),
    };
    assert_eq!(params.docs(), 1);
    assert_eq!(loader().shape.batch_size, 1);
    assert_eq!(loader().engine, crate::report::ENGINE);
}
