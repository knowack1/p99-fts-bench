use std::time::{Duration, Instant};

use super::*;

const RATE: u64 = 100;

fn schedule_at(rate: u64) -> Schedule {
    Schedule::at_rate(rate)
}

const NEVER: &dyn Fn() -> bool = &|| false;

fn due_at(schedule: Schedule, docs_offered: u64) -> Instant {
    schedule
        .due(docs_offered)
        .expect("a paced schedule has a due time")
        .instant()
}

#[test]
fn the_schedule_is_absolute_so_offsets_never_drift() {
    let schedule = schedule_at(RATE);
    let first = due_at(schedule, 0);
    let later = due_at(schedule, 200);
    assert_eq!(later - first, Duration::from_secs(2));
}

#[test]
fn a_documents_due_time_is_measured_from_the_levels_origin() {
    let schedule = schedule_at(RATE);
    assert_eq!(due_at(schedule, 0), schedule.origin());
    assert_eq!(
        due_at(schedule, 50),
        schedule.origin() + Duration::from_millis(500)
    );
}

#[test]
fn asking_twice_for_the_same_offset_gives_the_same_instant() {
    let schedule = schedule_at(RATE);
    assert_eq!(schedule.due(37), schedule.due(37));
}

/// Not "now": an item the producer released may wait in the bounded channel,
/// and calling that wait lateness would charge the harness's own read-ahead to
/// the engine. Closed loop has no schedule, so nothing is ever late.
#[test]
fn an_unpaced_schedule_gives_a_document_no_due_time_at_all() {
    assert_eq!(Schedule::unpaced().due(1_000_000), None);
    assert_eq!(Schedule::unpaced().wait_for(1_000_000, NEVER), None);
}

#[test]
fn with_no_due_time_the_requests_own_start_stands_in() {
    let started = Instant::now();
    let timing = Timing::measure(None, started, started + Duration::from_millis(9));

    assert!((timing.latency_ms - 9.0).abs() < 1e-6);
    assert!((timing.service_ms - 9.0).abs() < 1e-6);
    assert_eq!(timing.queue_ms, 0.0);
}

#[test]
fn an_unpaced_schedule_reports_no_rate_and_predicts_no_wall() {
    let schedule = Schedule::unpaced();
    assert!(!schedule.is_paced());
    assert_eq!(schedule.rate(), None);
    assert_eq!(schedule.expected_wall(1_000), None);
}

#[test]
fn a_paced_schedule_reports_the_rate_it_was_built_with() {
    let schedule = schedule_at(RATE);
    assert!(schedule.is_paced());
    assert_eq!(schedule.rate(), Some(RATE));
}

#[test]
fn from_rate_picks_the_mode_without_a_second_code_path() {
    assert!(!Schedule::from_rate(None).is_paced());
    assert_eq!(Schedule::from_rate(Some(RATE)).rate(), Some(RATE));
}

#[test]
#[should_panic(expected = "positive rate")]
fn a_zero_rate_is_refused_rather_than_silently_becoming_closed_loop() {
    Schedule::at_rate(0);
}

#[test]
fn expected_wall_is_documents_over_rate() {
    let schedule = schedule_at(RATE);
    assert_eq!(
        schedule.expected_wall(250),
        Some(Duration::from_millis(2500))
    );
}

#[test]
fn waiting_for_a_document_already_overdue_returns_at_once_and_does_not_skip_it() {
    let schedule = schedule_at(RATE);
    let overdue_by_now = 1;
    std::thread::sleep(Duration::from_millis(60));

    let began = Instant::now();
    let due = schedule.wait_for(overdue_by_now, NEVER);

    assert!(began.elapsed() < Duration::from_millis(20));
    assert_eq!(due, schedule.due(overdue_by_now));
    assert!(due.is_some());
}

#[test]
fn waiting_for_a_future_document_actually_waits() {
    let schedule = schedule_at(RATE);
    let began = Instant::now();
    schedule.wait_for(5, NEVER);
    assert!(began.elapsed() >= Duration::from_millis(40));
}

#[test]
fn sleeping_until_a_past_instant_returns_immediately() {
    let began = Instant::now();
    sleep_until(began - Duration::from_secs(1));
    assert!(began.elapsed() < Duration::from_millis(50));
}

#[test]
fn a_gap_under_the_floor_is_not_slept_through() {
    let began = Instant::now();
    sleep_until(began + MIN_SLEEP / 2);
    assert!(began.elapsed() < MIN_SLEEP);
}

#[test]
fn queue_time_is_the_gap_between_intended_and_actual_start() {
    let origin = Instant::now();
    let due = Some(Due::at(origin));
    let started = origin + Duration::from_millis(30);
    let ended = started + Duration::from_millis(70);

    let timing = Timing::measure(due, started, ended);

    assert!((timing.latency_ms - 100.0).abs() < 1e-6);
    assert!((timing.service_ms - 70.0).abs() < 1e-6);
    assert!((timing.queue_ms - 30.0).abs() < 1e-6);
}

#[test]
fn a_request_sent_when_it_was_due_records_no_queueing() {
    let started = Instant::now();
    let timing = Timing::measure(
        Some(Due::at(started)),
        started,
        started + Duration::from_millis(12),
    );

    assert!((timing.latency_ms - timing.service_ms).abs() < 1e-6);
    assert_eq!(timing.queue_ms, 0.0);
}

#[test]
fn a_request_dispatched_early_by_clock_granularity_never_queues_negative() {
    let started = Instant::now();
    let due = Some(Due::at(started + Duration::from_millis(5)));
    let timing = Timing::measure(due, started, started + Duration::from_millis(10));

    assert_eq!(timing.queue_ms, 0.0);
    assert!(timing.latency_ms < timing.service_ms);
}

#[test]
fn a_backwards_clock_records_zero_rather_than_killing_the_run() {
    let ended = Instant::now();
    let started = ended + Duration::from_millis(50);
    let timing = Timing::measure(Some(Due::at(started)), started, ended);

    assert_eq!(timing.latency_ms, 0.0);
    assert_eq!(timing.service_ms, 0.0);
    assert_eq!(timing.queue_ms, 0.0);
}

#[test]
fn a_closed_loop_level_can_never_overrun_a_schedule_it_does_not_have() {
    assert!(!Schedule::unpaced().overrun(1));
}

#[test]
fn a_level_inside_its_grace_period_is_never_called_hopeless() {
    let schedule = schedule_at(1);
    assert!(!schedule.overrun(0));
}

#[test]
fn a_level_keeping_up_with_its_schedule_does_not_overrun() {
    let schedule = schedule_at(1_000_000);
    assert!(!schedule.overrun(1_000_000_000));
}

/// A Ctrl-C during a long inter-arrival gap must not have to outlast it:
/// `Runtime::drop` waits for the blocking producer, so the wait is how long the
/// process stays up.
#[test]
fn a_wait_stops_early_when_asked_to() {
    let schedule = schedule_at(1);
    let began = Instant::now();

    schedule.wait_for(5, &|| true);

    assert!(
        began.elapsed() < Duration::from_millis(100),
        "{:?}",
        began.elapsed()
    );
}

#[test]
fn a_wait_nobody_interrupts_still_runs_its_course() {
    let began = Instant::now();
    sleep_until_unless(began + Duration::from_millis(120), &|| false);
    assert!(began.elapsed() >= Duration::from_millis(100));
}
