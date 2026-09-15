use std::thread;

use super::*;
use crate::counters::AcceptedWork;

const LANES: usize = 4;
const LANE: usize = 0;
const THREADS: usize = 5;

fn an_index(serving_delay: Duration, refresh: Refresh) -> (ModelledIndex, Clock) {
    let clock = Clock::manual();
    let index = ModelledIndex::new(LANES, serving_delay, refresh, clock.clone());
    (index, clock)
}

fn a_created_index(serving_delay: Duration, refresh: Refresh) -> (ModelledIndex, Clock) {
    let (index, clock) = an_index(serving_delay, refresh);
    index.create();
    (index, clock)
}

fn a_serving_index() -> ModelledIndex {
    a_created_index(Duration::ZERO, Refresh::immediately()).0
}

fn serving_at(count: u64) -> Option<IndexStatus> {
    Some(IndexStatus {
        count,
        status: SERVING,
    })
}

fn accept(work: &AcceptedWork, index: &ModelledIndex, lane: usize, docs: u64) {
    work.add(lane, 1, docs);
    index.add(lane, docs);
}

fn every_thread_accepts(work: &AcceptedWork, index: &ModelledIndex, each: u64) {
    thread::scope(|scope| {
        for lane in 0..THREADS {
            scope.spawn(move || {
                for _ in 0..each {
                    accept(work, index, lane, 1);
                }
            });
        }
    });
}

/// The campaign applies its schema and index DDL before anything writes, and a
/// `--no-reset` ladder issues none at all. A mock that started with no index
/// would answer 404 to a run that was right to expect one.
#[test]
fn created_hands_a_loader_an_index_that_already_exists() {
    let index = ModelledIndex::created(
        LANES,
        Duration::ZERO,
        Refresh::immediately(),
        Clock::manual(),
    );

    assert!(index.present());
    assert_eq!(index.status(), serving_at(0));
}

/// The gate `scyllarate` blocks on is only proven to work against a mock that
/// can fail it once.
#[test]
fn a_serving_delay_holds_the_index_at_building() {
    let (index, clock) = a_created_index(Duration::from_millis(500), Refresh::immediately());

    clock.advance(Duration::from_millis(100));
    let early = index.status().expect("a created index has a status");
    clock.advance(Duration::from_millis(800));
    let late = index.status().expect("a created index has a status");

    assert_eq!(early.status, BUILDING);
    assert!(!early.is_serving());
    assert_eq!(late.status, SERVING);
}

/// The harness creates the index before it loads, so an add against an absent
/// index means the loader and the mock disagree about the lifecycle — recorded
/// rather than silently absorbed, for the same reason unexpected routes are.
#[test]
fn documents_arriving_before_the_index_exists_are_recorded() {
    let (index, _clock) = an_index(Duration::ZERO, Refresh::immediately());

    index.add(LANE, 4);

    assert_eq!(index.adds_while_absent(), 4);
    assert_eq!(index.count(), 0);
}

/// A count answered for an index nobody created lets a misdirected run pass its
/// own gate, so a drop has to read as absence rather than as an empty index —
/// which is the very state the post-reset gate is waiting for.
#[test]
fn a_dropped_index_is_absent_and_a_recreated_one_is_present_and_empty() {
    let index = a_serving_index();
    index.add(LANE, 5);

    index.drop_index();
    let dropped = index.status();
    let gone = index.present();
    index.create();

    assert_eq!(dropped, None);
    assert!(!gone);
    assert!(index.present());
    assert_eq!(index.status(), serving_at(0));
}

/// What `scyllarate` does before every concurrency level. A count that survived
/// the reset would let level 2 inherit level 1's documents and report a
/// complete, plausible, wrong build rate.
#[test]
fn the_reset_cycle_leaves_a_serving_index_at_zero() {
    let index = a_serving_index();
    index.add(LANE, 270_269);

    index.drop_index();
    index.create();

    assert_eq!(index.status(), serving_at(0));
}

/// `AcceptedWork` spans the process and feeds the summary line and
/// `--stats-out`; only the index resets with the keyspace. A drop that zeroed
/// both would make a six-level ladder report the documents of its last level as
/// the documents of the run.
#[test]
fn a_drop_zeroes_the_index_but_not_the_runs_own_total() {
    let work = AcceptedWork::new(LANES);
    let index = a_serving_index();

    accept(&work, &index, LANE, 100);
    index.drop_index();
    index.create();
    accept(&work, &index, LANE, 30);

    assert_eq!(work.snapshot().docs, 130);
    assert_eq!(index.count(), 30);
}

/// The shape a build-rate watch has to be able to measure: what a search would
/// find steps at the refresh interval while what the mock accepted climbs
/// continuously.
#[test]
fn documents_are_searchable_only_after_a_refresh() {
    let (index, clock) = a_created_index(Duration::ZERO, Refresh::Every(Duration::from_secs(3)));
    index.add(LANE, 3);

    clock.advance(Duration::from_secs(1));
    let before = index.searchable();
    clock.advance(Duration::from_secs(3));
    let after = index.searchable();

    assert_eq!((before, index.count()), (0, 3));
    assert_eq!(after, 3);
}

/// `Refresh::Never` is `refresh_interval: -1`: nothing becomes visible on a
/// timer, so a watch that gave up waiting asks for a refresh outright. A mock
/// that acknowledged that without publishing would make the last resort look
/// like a stalled index.
#[test]
fn a_refresh_request_publishes_what_the_mock_accepted() {
    let (index, clock) = a_created_index(Duration::ZERO, Refresh::Never);
    index.add(LANE, 3);

    clock.advance(Duration::from_secs(3600));
    let never = index.searchable();
    index.refresh();
    let asked = index.searchable();

    assert_eq!(never, 0);
    assert_eq!(asked, 3);
}

/// Every run recorded before the refresh model existed measured a mock where
/// accepted and searchable were the same number, and the default has to keep
/// meaning that.
#[test]
fn by_default_nothing_waits_for_a_refresh() {
    let index = a_serving_index();

    index.add(LANE, 3);
    let first = index.searchable();
    index.add(LANE, 4);
    let second = index.searchable();

    assert_eq!((first, second), (3, 7));
}

/// Scheduled refreshes are modelled lazily — they happen when someone looks —
/// so counting every one would report how often the harness polled rather than
/// how often the index turned over, and that number would move with
/// `--index-interval` while nothing about the build had changed.
#[test]
fn a_poll_that_publishes_nothing_does_not_count_as_a_refresh() {
    let (index, clock) = a_created_index(Duration::ZERO, Refresh::Every(Duration::from_secs(3)));
    index.add(LANE, 3);

    clock.advance(Duration::from_secs(3));
    let published = index.searchable();
    clock.advance(Duration::from_secs(3));
    index.searchable();
    clock.advance(Duration::from_secs(3));
    index.searchable();

    assert_eq!(published, 3);
    assert_eq!(index.refresh_total(), 1);
}

/// Every tokio worker adds to this index at once while the harness polls it. A
/// reset zeroing a counter those workers are still adding to would lose
/// documents at the level boundary, so the count is measured from the last
/// lifecycle change instead — and the run's own total is untouched by either.
#[test]
fn concurrent_adds_all_land_and_the_count_is_measured_from_the_last_reset() {
    let work = AcceptedWork::new(LANES);
    let index = a_serving_index();

    every_thread_accepts(&work, &index, 20);
    index.drop_index();
    index.create();
    every_thread_accepts(&work, &index, 6);

    assert_eq!(index.count(), 30);
    assert_eq!(work.snapshot().docs, 130);
}

/// Reading the searchable count is what performs a scheduled refresh, so a
/// reply that read the refresh counter before it would report the refresh one
/// poll behind the documents it just published — the exact signature a
/// build-rate watch reads as "the index published without a refresh".
#[test]
fn one_stats_reading_reports_the_refresh_it_just_caused() {
    let clock = Clock::manual();
    let index = ModelledIndex::created(
        LANES,
        Duration::ZERO,
        Refresh::Every(Duration::from_secs(3)),
        clock.clone(),
    );
    index.add(0, 7);

    let before = index.stats();
    clock.advance(Duration::from_secs(4));
    let after = index.stats();

    assert_eq!(
        (before.searchable, before.accepted, before.refreshes),
        (0, 7, 0)
    );
    assert_eq!(
        (after.searchable, after.accepted, after.refreshes),
        (7, 7, 1)
    );
}

/// Three separate reads can interleave with another poller's refresh and report
/// an accepted count from before it beside a searchable count from after —
/// which is a negative lag, a number the watch has no reading for.
#[test]
fn a_stats_reading_never_reports_more_searchable_than_accepted() {
    let index = Arc::new(ModelledIndex::created(
        LANES,
        Duration::ZERO,
        Refresh::immediately(),
        Clock::monotonic(),
    ));
    let writing = Arc::clone(&index);
    let writer = std::thread::spawn(move || {
        for _ in 0..20_000 {
            writing.add(0, 1);
        }
    });

    for _ in 0..20_000 {
        let stats = index.stats();
        assert!(
            stats.searchable <= stats.accepted,
            "{} searchable of {} accepted",
            stats.searchable,
            stats.accepted
        );
    }

    writer.join().expect("the writing thread");
}

/// Every document is either in the new index or recorded as having arrived
/// while none existed. A `create` captures the base offset its count is
/// measured from, and an add that lands while it does so is counted into
/// `accepted` and into the offset at once — dropped, and dropped in the one way
/// this instrument cannot report, because the whole point of
/// `adds_while_absent` is that a loader/mock lifecycle disagreement is never
/// silently absorbed.
#[test]
fn a_create_racing_with_adds_absorbs_none_of_them() {
    const ROUNDS: usize = 200;
    const EACH: u64 = 500;
    for _ in 0..ROUNDS {
        let (index, _clock) = an_index(Duration::ZERO, Refresh::immediately());
        let start = AtomicBool::new(false);
        thread::scope(|scope| {
            for lane in 0..THREADS {
                let (index, start) = (&index, &start);
                scope.spawn(move || {
                    while !start.load(Ordering::Acquire) {
                        std::hint::spin_loop();
                    }
                    for _ in 0..EACH {
                        index.add(lane, 1);
                    }
                });
            }
            start.store(true, Ordering::Release);
            index.create();
        });
        assert_eq!(
            index.count() + index.adds_while_absent(),
            THREADS as u64 * EACH
        );
    }
}

/// The other half of the lifecycle pair, which cannot be fixed the way `create`
/// was and does not need to be.
///
/// `drop_index` clears the flag and then captures the offset, and reordering
/// that would not close the window: a thread can read `present` as true and be
/// descheduled arbitrarily long before it adds, so no snapshot the drop takes
/// bounds it. What the reorder buys on the `create` side is an accident of
/// direction — the flag is false until the store, so an offset taken first
/// bounds every reader that will ever see true.
///
/// So this pins what the window can and cannot do rather than pretending it is
/// shut. A document caught in it belongs to the generation being dropped: it is
/// left out of that index's count, which nothing reads once `status` answers
/// `None`, and absorbed by the next `create`. It never reaches the new index,
/// and it is never missing from `docs_accepted`, which is the key the run's
/// reconciliation gate reads and which `add`'s lifecycle check does not gate.
#[test]
fn a_reset_racing_with_adds_carries_nothing_into_the_new_index() {
    const ROUNDS: usize = 300;
    const EACH: u64 = 500;
    let issued = THREADS as u64 * EACH;
    for _ in 0..ROUNDS {
        let work = AcceptedWork::new(LANES);
        let (index, _clock) = a_created_index(Duration::ZERO, Refresh::immediately());
        let start = AtomicBool::new(false);
        thread::scope(|scope| {
            for lane in 0..THREADS {
                let (index, start, work) = (&index, &start, &work);
                scope.spawn(move || {
                    while !start.load(Ordering::Acquire) {
                        std::hint::spin_loop();
                    }
                    for _ in 0..EACH {
                        accept(work, index, lane, 1);
                    }
                });
            }
            start.store(true, Ordering::Release);
            index.drop_index();
            index.create();
        });

        assert_eq!(
            work.snapshot().docs,
            issued,
            "docs_accepted lost a document"
        );
        assert!(
            index.count() <= issued,
            "the new index counted a document twice"
        );
        assert!(
            index.count() + index.adds_while_absent() <= issued,
            "a document was attributed to the new index and to the absent count"
        );
    }
}
