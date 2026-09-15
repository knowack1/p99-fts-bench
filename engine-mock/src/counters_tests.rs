use std::thread;

use super::*;

const LANES: usize = 4;
const THREADS: usize = 8;
const DOCS_PER_THREAD: u64 = 5_000;

fn docs_added_by_every_thread_at_once(lanes: usize, lane_for: fn(usize) -> usize) -> u64 {
    let work = AcceptedWork::new(lanes);
    thread::scope(|scope| {
        for worker in 0..THREADS {
            let work = &work;
            scope.spawn(move || {
                for _ in 0..DOCS_PER_THREAD {
                    work.add(lane_for(worker), 1, 1);
                }
            });
        }
    });
    work.snapshot().docs
}

fn summary_keys(work: &AcceptedWork) -> Vec<String> {
    work.summary()
        .as_object()
        .expect("a summary is a JSON object")
        .keys()
        .cloned()
        .collect()
}

/// Lanes are handed out at accept and the shard count is sized from the worker
/// count, so the two are unrelated and a lane past the end is ordinary. One
/// that panicked would take down the connection that drew it; one that was
/// dropped would lose its documents without saying so.
#[test]
fn a_striped_total_is_every_lane_summed_and_lanes_past_the_end_wrap() {
    let counter = Striped::new(LANES);

    for lane in 0..LANES * 3 {
        counter.add(lane, 2);
    }

    assert_eq!(counter.total(), 2 * (LANES * 3) as u64);
}

/// The lane is chosen by a remainder, so a counter asked for no shards at all
/// would divide by zero on the run's first document.
#[test]
fn a_counter_asked_for_no_lanes_still_counts() {
    let counter = Striped::new(0);

    counter.add(7, 3);

    assert_eq!(counter.total(), 3);
}

/// One `_bulk` is one operation carrying many documents; one CQL `EXECUTE` is
/// one of each. A mock that could not tell them apart would report a
/// large-batch arm's operation rate as its document rate.
#[test]
fn operations_and_documents_are_counted_separately() {
    let work = AcceptedWork::new(LANES);

    work.add(0, 1, 500);
    work.add(1, 1, 500);

    let snapshot = work.snapshot();
    assert_eq!((snapshot.ops, snapshot.docs), (2, 1_000));
}

/// The runbook's Phase 5 gate reads `docs_accepted` to reconcile against what
/// the harness CSVs claim to have submitted, and `unexpected_requests` to catch
/// a dropped setup call. A renamed key does not fail that gate — it makes the
/// gate read nothing and report OK.
#[test]
fn a_summary_carries_exactly_the_keys_a_runbook_gate_reads_by_name() {
    let work = AcceptedWork::new(LANES);
    work.add(0, 3, 90);
    work.note_unexpected("GET /wiki-articles/_mapping");

    let summary = work.summary();

    assert_eq!(
        summary_keys(&work),
        [
            "ops_accepted",
            "docs_accepted",
            "sink_wall_s",
            "sink_ops_per_s",
            "sink_docs_per_s",
            "unexpected_requests",
        ]
    );
    assert_eq!(summary["ops_accepted"], json!(3));
    assert_eq!(summary["docs_accepted"], json!(90));
    assert_eq!(
        summary["unexpected_requests"],
        json!({"GET /wiki-articles/_mapping": 1})
    );
}

/// A read-back the mock does not answer arrives once per batch arm. A route
/// recorded as merely seen would hide three dropped calls behind one, and the
/// gate that counts them is how a loader change is stopped from landing as a
/// throughput difference.
#[test]
fn an_unexpected_route_is_counted_every_time_it_is_asked() {
    let work = AcceptedWork::new(LANES);

    for _ in 0..3 {
        work.note_unexpected("GET /wiki-articles/_settings");
    }
    work.note_unexpected("GET /_cat/indices");

    let seen = work.unexpected();
    assert_eq!(seen["GET /wiki-articles/_settings"], 3);
    assert_eq!(seen["GET /_cat/indices"], 1);
}

/// The mock's own rate is not the measurement, but a mock that printed nothing
/// would leave a stalled run indistinguishable from a slow one for the length
/// of the point — so the line is read by eye, and its shape is the interface.
#[test]
fn a_summary_line_reports_docs_then_ops_then_both_rates() {
    let snapshot = WorkSnapshot {
        ops: 20,
        docs: 1_000,
        elapsed_s: 4.0,
    };

    assert_eq!(
        snapshot.summary_line(),
        "sink: 1000 docs, 20 ops in 4.0s (250 docs/s, 5 ops/s)"
    );
}

/// A summary is written for runs that accepted nothing, and the reporter's
/// first line can be taken before any wall time has passed at all.
#[test]
fn no_elapsed_time_reports_zero_per_second_rather_than_dividing_by_it() {
    let snapshot = WorkSnapshot {
        ops: 7,
        docs: 9,
        elapsed_s: 0.0,
    };

    assert_eq!(snapshot.ops_per_s(), 0.0);
    assert_eq!(snapshot.docs_per_s(), 0.0);
    assert_eq!(
        snapshot.summary_line(),
        "sink: 9 docs, 7 ops in 0.0s (0 docs/s, 0 ops/s)"
    );
}

/// The Python sink added two integers on one asyncio thread, where losing a
/// count was not possible. Here every tokio worker adds at once, and a mock
/// that dropped documents under contention would under-report at exactly the
/// concurrency the run is trying to reach.
#[test]
fn every_thread_adding_at_once_on_its_own_lane_loses_nothing() {
    assert_eq!(
        docs_added_by_every_thread_at_once(THREADS, |worker| worker),
        THREADS as u64 * DOCS_PER_THREAD
    );
}

/// Striping is a contention choice, not a correctness one. If crowding the
/// same threads onto one lane changed the total, then how lanes are handed out
/// at accept would silently change the number a run reports.
#[test]
fn every_thread_adding_at_once_on_one_lane_loses_nothing_either() {
    assert_eq!(
        docs_added_by_every_thread_at_once(THREADS, |_| 0),
        THREADS as u64 * DOCS_PER_THREAD
    );
}

/// One of the keys is not a route: an EXECUTE of a statement id nobody prepared
/// is recorded under that id, and a client that has lost its prepared
/// statements sends a new one every request. Unbounded, that is the mock
/// spending memory on strings while a run is being measured against it.
#[test]
fn the_record_of_what_was_unexpected_is_bounded_but_still_counts_everything() {
    let work = AcceptedWork::new(LANES);

    for id in 0..MAX_UNEXPECTED_KEYS * 2 {
        work.note_unexpected(&format!("execute of unprepared {id:04x}"));
    }

    let recorded = work.unexpected();
    assert_eq!(recorded.len(), MAX_UNEXPECTED_KEYS + 1);
    assert_eq!(
        recorded[OVERFLOW_KEY],
        (MAX_UNEXPECTED_KEYS * 2 - MAX_UNEXPECTED_KEYS) as u64
    );
}

/// The summary line on stderr and the JSON on disk are read side by side, and
/// two readings taken moments apart disagree on the wall clock and on both
/// rates — which reads as a mock that lost documents between them.
#[test]
fn the_line_and_the_document_can_be_made_from_one_reading() {
    let work = AcceptedWork::new(LANES);
    work.add(0, 3, 300);
    let snapshot = work.snapshot();

    let document = work.summary_of(snapshot);

    assert_eq!(document["docs_accepted"], 300);
    assert_eq!(document["sink_wall_s"], json!(round(snapshot.elapsed_s, 3)));
    assert!(snapshot.summary_line().contains("300 docs, 3 ops"));
}

/// The rounding is what a reader compares between two runs, so it is pinned to
/// a literal rather than to the same `round` that produced it.
#[test]
fn the_rounded_fields_are_the_shapes_the_python_producers_wrote() {
    let work = AcceptedWork::new(LANES);
    work.add(0, 2, 9);

    let document = work.summary_of(WorkSnapshot {
        ops: 2,
        docs: 9,
        elapsed_s: 4.000_9,
    });

    assert_eq!(document["sink_docs_per_s"], json!(2.2));
    assert_eq!(document["sink_ops_per_s"], json!(0.5));
}
