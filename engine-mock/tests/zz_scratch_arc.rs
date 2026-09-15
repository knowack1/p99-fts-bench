use std::sync::Arc;
use std::time::Instant;

struct Prepared {
    mutation: bool,
    query: String,
}

#[test]
fn shared_arc_refcount_on_the_hot_path() {
    let threads = std::thread::available_parallelism().map_or(1, |c| c.get());
    let per_thread = 5_000_000_u64;

    // What StatementCache::get does today: clone the Arc every connection shares.
    let shared = Arc::new(Prepared {
        mutation: true,
        query: "insert".into(),
    });
    let start = Instant::now();
    std::thread::scope(|scope| {
        for _ in 0..threads {
            let shared = Arc::clone(&shared);
            scope.spawn(move || {
                let mut sink = 0_u64;
                for _ in 0..per_thread {
                    let held = Arc::clone(&shared);
                    if std::hint::black_box(held).mutation {
                        sink += 1;
                    }
                }
                std::hint::black_box(sink);
            });
        }
    });
    let contended = start.elapsed();

    // What a per-connection copy would do: no shared counter at all.
    let start = Instant::now();
    std::thread::scope(|scope| {
        for _ in 0..threads {
            let own = Arc::new(Prepared {
                mutation: true,
                query: "insert".into(),
            });
            scope.spawn(move || {
                let mut sink = 0_u64;
                for _ in 0..per_thread {
                    let held = Arc::clone(&own);
                    if std::hint::black_box(held).mutation {
                        sink += 1;
                    }
                }
                std::hint::black_box(sink);
            });
        }
    });
    let private = start.elapsed();

    let total = threads as u64 * per_thread;
    println!("threads={threads} ops={total}");
    println!(
        "  one shared Arc : {contended:?}  ({:.1} ns/op)",
        contended.as_nanos() as f64 / total as f64
    );
    println!(
        "  per-conn Arc   : {private:?}  ({:.1} ns/op)",
        private.as_nanos() as f64 / total as f64
    );
}
