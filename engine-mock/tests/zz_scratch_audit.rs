use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use engine_mock::counters::AcceptedWork;
use engine_mock::http_wire::{answers_for, Routes};
use engine_mock::index::{Clock, ModelledIndex, Refresh};
use engine_mock::opensearch::Table;
use engine_mock::server::Connection;

fn table() -> (Table, Arc<ModelledIndex>) {
    let work = Arc::new(AcceptedWork::new(4));
    let index = Arc::new(ModelledIndex::created(
        4,
        Duration::ZERO,
        Refresh::immediately(),
        Clock::monotonic(),
    ));
    (Table::new(work, Arc::clone(&index)), index)
}

/// Hypothesis 1: a PUT whose path's first character is multi-byte panics in
/// Table::lifecycle at `&path[1..]`.
#[test]
fn put_with_a_multibyte_leading_path_char() {
    let (routes, _index) = table();
    let connection = Connection {
        lane: 0,
        local: "127.0.0.1:9200".parse().unwrap(),
    };
    let mut session = routes.session(&connection);
    let mut out = Vec::new();
    // "\xc3\xa9/x" -> "é/x": exactly one '/', not "/", so names_an_index() is true.
    let request = b"PUT \xc3\xa9/x HTTP/1.1\r\nHost: x\r\n\r\n";
    let consumed = answers_for(request, &routes, &mut session, &mut out);
    println!(
        "consumed = {consumed:?}, out = {:?}",
        String::from_utf8_lossy(&out)
    );
}

/// Hypothesis 2: an add that has already passed the present() check can land
/// after a reset captured its base offset, so the freshly created index reports
/// documents from the previous generation.
#[test]
fn reset_races_with_in_flight_adds() {
    let mut leaked_rounds = 0;
    for _round in 0..400 {
        let index = Arc::new(ModelledIndex::created(
            64,
            Duration::ZERO,
            Refresh::immediately(),
            Clock::monotonic(),
        ));
        let go = Arc::new(AtomicBool::new(false));
        let issued = Arc::new(AtomicU64::new(0));
        let adders: Vec<_> = (0..4_usize)
            .map(|lane| {
                let index = Arc::clone(&index);
                let go = Arc::clone(&go);
                let issued = Arc::clone(&issued);
                std::thread::spawn(move || {
                    while !go.load(Ordering::Acquire) {
                        std::hint::spin_loop();
                    }
                    for _ in 0..20_000 {
                        index.add(lane, 1);
                        issued.fetch_add(1, Ordering::Relaxed);
                    }
                })
            })
            .collect();
        go.store(true, Ordering::Release);
        std::thread::sleep(Duration::from_micros(200));
        // The reset a level boundary issues, concurrent with the tail of a level.
        index.drop_index();
        index.create();
        let right_after_create = index.count();
        for adder in adders {
            adder.join().unwrap();
        }
        if right_after_create > 0 {
            leaked_rounds += 1;
        }
        let _ = issued;
    }
    println!("rounds where count() was non-zero immediately after create(): {leaked_rounds}/400");
}
