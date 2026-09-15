use std::path::PathBuf;

use super::*;

const SHARED_KEYS: [&str; 13] = [
    "record",
    "schema_version",
    "producer",
    "engine",
    "engine_version",
    "label",
    "cache_state",
    "corpus",
    "max_docs",
    "started_at",
    "git_commit",
    "host",
    "env",
];
const HOST_KEYS: [&str; 7] = [
    "hostname",
    "machine",
    "platform",
    "rustc_version",
    "binary_version",
    "cpu_count",
    "total_ram_bytes",
];
const ENV_KEYS: [&str; 3] = ["swap_used_bytes", "load_avg_1m", "cpu_affinity"];

fn a_header() -> Value {
    header_with(&RunStart::now())
}

fn header_with(started: &RunStart) -> Value {
    header(
        "scylladb",
        "a label",
        started,
        vec![("mode", json!("cql")), ("tokio_workers", json!(8))],
    )
}

fn keys(of: &Value) -> Vec<String> {
    of.as_object().unwrap().keys().cloned().collect()
}

fn named(keys: &[&str]) -> Vec<String> {
    keys.iter().map(|key| key.to_string()).collect()
}

fn fields(of: &str, separator: char) -> Vec<u32> {
    of.split(separator)
        .map(|field| {
            field
                .parse()
                .unwrap_or_else(|_| panic!("not a number in {of}"))
        })
        .collect()
}

/// The results tree that builds the per-chart write-ups reads every producer's
/// artifact the same way, so a header that drops one of the shared keys — or
/// spells this run's own settings ahead of them — reads as a different schema
/// than the one `SCHEMAS.md` promises.
#[test]
fn a_header_carries_the_shared_keys_first_and_the_runs_own_settings_after() {
    let mut expected = named(&SHARED_KEYS);
    expected.extend(named(&["mode", "tokio_workers"]));

    assert_eq!(keys(&a_header()), expected);
}

/// A chart footer names the tuning that produced it out of these fields. An
/// engine or a label that did not reach the artifact is a run that cannot be
/// told apart from the one beside it.
#[test]
fn a_header_records_the_engine_the_label_and_the_settings_it_was_given() {
    let header = a_header();

    assert_eq!(header["engine"], json!("scylladb"));
    assert_eq!(header["label"], json!("a label"));
    assert_eq!(header["mode"], json!("cql"));
    assert_eq!(header["tokio_workers"], json!(8));
    assert_eq!(header["record"], json!("header"));
    assert_eq!(header["schema_version"], json!(SCHEMA_VERSION));
    assert_eq!(header["producer"], json!(PRODUCER));
}

/// The host block is how a number is attributed to the machine and the
/// toolchain that produced it. Two runs of the same mock on two hosts are
/// otherwise indistinguishable in the results tree.
#[test]
fn the_host_block_names_the_machine_and_the_toolchain_the_binary_was_built_with() {
    let header = a_header();
    let host = &header["host"];

    assert_eq!(keys(host), named(&HOST_KEYS));
    assert_eq!(host["binary_version"], json!(CRATE_VERSION));
    assert_eq!(host["rustc_version"], json!(RUSTC_VERSION));
    assert!(host["cpu_count"].as_u64().unwrap() >= 1);
    assert!(!host["platform"].as_str().unwrap().is_empty());
    assert!(!host["hostname"].as_str().unwrap().is_empty());
}

/// A run taken with 9 GB of swap in use, an unpinned CPU affinity, or a load
/// average of 4 from someone else's containers is still usable — but only if
/// the artifact says so, which is what makes a laptop measurement auditable
/// rather than merely small.
#[test]
fn the_env_block_names_what_else_the_machine_was_doing() {
    let header = a_header();
    let env = &header["env"];

    assert_eq!(keys(env), named(&ENV_KEYS));
    assert!(env["swap_used_bytes"].is_i64());
    assert!(env["load_avg_1m"].is_f64());
    assert!(env["cpu_affinity"].is_array());
}

/// Every other producer writes `datetime.now(timezone.utc).isoformat()`, and
/// the merge steps that line runs up against each other parse the string. A
/// local-time or fractional-second stamp reads as a run at another hour.
#[test]
fn a_run_is_stamped_in_utc_to_the_second_in_the_shape_every_producer_writes() {
    let stamp = started_at();

    let (date, rest) = stamp.split_once('T').unwrap();
    let time = rest
        .strip_suffix("+00:00")
        .unwrap_or_else(|| panic!("not UTC to the second: {stamp}"));
    let (date, time) = (fields(date, '-'), fields(time, ':'));

    assert!((2025..=2100).contains(&date[0]), "{stamp}");
    assert!((1..=12).contains(&date[1]), "{stamp}");
    assert!((1..=31).contains(&date[2]), "{stamp}");
    assert!(time[0] < 24 && time[1] < 60 && time[2] < 60, "{stamp}");
}

/// The date is arithmetic here rather than a crate, so nothing else in a run
/// would notice a leap-year slip: it would misdate a whole campaign's
/// artifacts and leave them looking perfectly well-formed.
#[test]
fn a_unix_day_number_becomes_the_calendar_date_a_reader_expects() {
    let epochs = [
        (0_i64, (1970_i64, 1_u32, 1_u32)),
        (11_016, (2000, 2, 29)),
        (20_088, (2024, 12, 31)),
        (20_711, (2026, 9, 15)),
    ];

    for (days, date) in epochs {
        assert_eq!(civil_from_days(days), date, "day {days}");
    }
}

/// Every artifact of the first laptop campaign carries `39eae6e`, a commit
/// made before the harness that produced them existed, because the repair was
/// uncommitted at run time. A bare hash is worse there than no hash: it
/// invites a reader to check that revision out and conclude the numbers are
/// reproducible.
#[test]
fn the_commit_stamp_is_a_hash_a_reader_can_check_out_or_an_honest_unknown() {
    let commit = git_commit();

    let hash = commit.strip_suffix("-dirty").unwrap_or(&commit);

    assert!(
        commit == UNKNOWN
            || (hash.len() >= 4 && hash.chars().all(|digit| digit.is_ascii_hexdigit())),
        "{commit}"
    );
}

/// `available_parallelism` honours the affinity mask, so a mock launched under
/// the runbooks' `taskset -c 0-1` would report a two-core host — and since
/// `cpu_affinity` comes from the same mask, "the full core list means the run
/// was NOT pinned" could never be true again. The two fields are only a finding
/// while they can disagree.
#[test]
fn the_host_core_count_is_the_machines_not_this_process_s_share_of_it() {
    let host = a_header()["host"].clone();
    let counted = std::fs::read_to_string("/proc/cpuinfo")
        .map(|text| {
            text.lines()
                .filter(|line| line.starts_with("processor"))
                .count()
        })
        .unwrap_or(0);

    if counted > 0 {
        assert_eq!(host["cpu_count"], json!(counted));
    }
    assert!(host["cpu_count"].as_u64().unwrap() >= crate::cli::available_cores() as u64);
}

/// The header is built on the way out, so a start time read there is the stop
/// time — an error exactly one run long, in the field that aligns this artifact
/// against the loader's own run windows.
#[test]
fn the_start_time_is_when_the_run_began_not_when_the_artifact_was_written() {
    let started = RunStart::now();

    std::thread::sleep(std::time::Duration::from_millis(1100));
    let written_later = header_with(&started);

    assert_eq!(written_later["started_at"], json!(started_at_of(&started)));
    assert_ne!(written_later["started_at"], json!(super::started_at()));
}

fn started_at_of(started: &RunStart) -> String {
    header_with(started)["started_at"]
        .as_str()
        .expect("a timestamp")
        .to_string()
}

/// The commit is worth five seconds and not one more: this runs on the way out,
/// after the signal handlers have stopped listening, so a `git` that blocks is
/// a mock that ignores the SIGTERM its stop script just sent and leaves the
/// reconciliation gate with no witness to read.
#[test]
fn a_subprocess_that_never_answers_does_not_hold_the_artifact_up() {
    let patience = std::time::Duration::from_millis(200);

    let at = std::time::Instant::now();
    let answer = answered_within(std::process::Command::new("sleep").arg("60"), patience);

    assert_eq!(answer, None);
    assert!(at.elapsed() < patience * 10, "waited {:?}", at.elapsed());
}

/// The bound is the Python's, which passes `timeout=5` to every `git` it runs.
#[test]
fn the_bound_on_a_git_call_is_the_one_the_python_producers_use() {
    assert_eq!(GIT_PATIENCE, std::time::Duration::from_secs(5));
}

/// `SCHEMAS.md` is the contract, so the field list is read out of it rather
/// than restated here. A test that hardcodes the keys only proves it agrees
/// with itself, and the results tree that builds the per-chart write-ups reads
/// every producer's artifact the same way: a header that quietly stopped
/// matching the document would be found by a reader, not by a test.
///
/// `batch_size` is the one documented key this producer does not write.
/// `SCHEMAS.md` calls it optional and **absent rather than zero** where the
/// caller did not name it, and the mock has no batch to report — it counts what
/// arrived, in whatever shape the loader chose to send it.
#[test]
fn the_header_carries_exactly_the_shared_keys_schemas_md_documents() {
    let documented: Vec<String> = documented_header_keys()
        .into_iter()
        .filter(|key| key != "batch_size")
        .collect();

    assert_eq!(named(&SHARED_KEYS), documented);
}

/// The example object under `## Header` in `SCHEMAS.md`, in the order it is
/// written there: the header's keys are an order as well as a set.
fn documented_header_keys() -> Vec<String> {
    let text = fs::read_to_string(schemas_md()).expect("SCHEMAS.md is beside the bench tree");
    let (_, after_heading) = text
        .split_once("\n## Header")
        .expect("SCHEMAS.md has no `## Header` section");
    let (_, fenced) = after_heading
        .split_once("```json\n")
        .expect("the header section has no JSON example");
    let (example, _) = fenced
        .split_once("\n```")
        .expect("the header section's JSON example is unterminated");
    let parsed: Value = serde_json::from_str(example).expect("the example parses as JSON");
    keys(&parsed)
}

fn schemas_md() -> PathBuf {
    Path::new(SOURCE_DIR).join("../SCHEMAS.md")
}
