//! The header the `--stats-out` document starts with: what ran, on what, from
//! which commit.
//!
//! One shape, shared with every other artifact this bench produces
//! (`SCHEMAS.md`), because the results tree that builds the per-chart write-ups
//! reads them all the same way. Two keys inside it are load-bearing rather than
//! decorative: a reconciliation gate reads `docs_accepted` against what the
//! harness CSVs claim to have submitted, and `unexpected_requests` is how a
//! setup call that stopped arriving is caught.
//!
//! The env block is what makes a laptop measurement auditable rather than
//! merely small: a run taken with 9 GB of swap in use, an unpinned CPU
//! affinity, or a load average of 4 from someone else's containers is still
//! usable, but only if the artifact says so.
use std::fs;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

pub const SCHEMA_VERSION: u64 = 1;
pub const PRODUCER: &str = "engine_mock";
pub const UNKNOWN: &str = "unknown";
pub const RUSTC_VERSION: &str = env!("ENGINE_MOCK_RUSTC_VERSION");
pub const CRATE_VERSION: &str = env!("CARGO_PKG_VERSION");
/// Where the git questions are asked from. The source tree at build time, so a
/// mock launched from a copied binary in some other directory still reports the
/// commit it was built from — or `unknown`, if that tree is not there either.
const SOURCE_DIR: &str = env!("CARGO_MANIFEST_DIR");
/// What the Python producers give a `git` call, for the same reason.
const GIT_PATIENCE: Duration = Duration::from_secs(5);
const GIT_POLL: Duration = Duration::from_millis(10);

/// What was true when the run began, captured then rather than read when the
/// artifact is written.
///
/// The header is built on the way out, and three of its fields describe a
/// moment rather than a run: when it started, and what the box was doing at the
/// time. Read at teardown they describe the end of the run instead — an error
/// exactly one run long, in the field `plot_growth` aligns several artifacts
/// on. The Python sink had the same defect; `SCHEMAS.md` has always said these
/// are run-start readings.
#[derive(Debug, Clone)]
pub struct RunStart {
    at: String,
    env: Value,
}

impl RunStart {
    pub fn now() -> Self {
        Self {
            at: started_at(),
            env: env_facts(),
        }
    }
}

impl Default for RunStart {
    fn default() -> Self {
        Self::now()
    }
}

pub fn header(engine: &str, label: &str, started: &RunStart, extra: Vec<(&str, Value)>) -> Value {
    let mut record = Map::new();
    record.insert("record".to_string(), json!("header"));
    record.insert("schema_version".to_string(), json!(SCHEMA_VERSION));
    record.insert("producer".to_string(), json!(PRODUCER));
    record.insert("engine".to_string(), json!(engine));
    record.insert(
        "engine_version".to_string(),
        json!("n/a — accept and discard, nothing is stored"),
    );
    record.insert("label".to_string(), json!(label));
    record.insert("cache_state".to_string(), json!("n/a"));
    record.insert("corpus".to_string(), json!(""));
    record.insert("max_docs".to_string(), json!(0));
    record.insert("started_at".to_string(), json!(started.at));
    record.insert("git_commit".to_string(), json!(git_commit()));
    record.insert("host".to_string(), host_facts());
    record.insert("env".to_string(), started.env.clone());
    for (key, value) in extra {
        record.insert(key.to_string(), value);
    }
    Value::Object(record)
}

/// UTC, to the second, in the shape `datetime.isoformat()` writes. Formatted by
/// hand rather than with a date crate: one timestamp per run does not justify a
/// dependency that can move underneath a recorded artifact.
fn started_at() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (year, month, day) = civil_from_days((now / 86_400) as i64);
    let seconds = now % 86_400;
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
        seconds / 3600,
        (seconds % 3600) / 60,
        seconds % 60
    )
}

/// Howard Hinnant's `civil_from_days`, which is the shortest correct way to get
/// a calendar date out of a Unix day number without a calendar library.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = (day_of_year - (153 * shifted_month + 2) / 5 + 1) as u32;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    } as u32;
    (year + i64::from(month <= 2), month, day)
}

/// The commit, suffixed `-dirty` when it is not what actually ran.
///
/// A bare hash on an artifact produced from uncommitted code names a commit
/// that does not contain the code that made it, which is worse than recording
/// nothing: it invites someone to check out that hash and conclude the numbers
/// are reproducible.
pub fn git_commit() -> String {
    let Some(commit) = git(&["rev-parse", "--short", "HEAD"]) else {
        return UNKNOWN.to_string();
    };
    let commit = commit.trim();
    if commit.is_empty() {
        return UNKNOWN.to_string();
    }
    if working_tree_is_dirty() {
        return format!("{commit}-dirty");
    }
    commit.to_string()
}

/// Dirtiness is scoped to the bench tree: an edit to the talk document cannot
/// change what the mock measured, and flagging every run dirty for it would
/// train the reader to ignore the flag.
fn working_tree_is_dirty() -> bool {
    let bench = Path::new(SOURCE_DIR).parent().map(Path::to_path_buf);
    let scope = bench.map_or_else(|| ".".to_string(), |path| path.display().to_string());
    git(&[
        "status",
        "--porcelain",
        "--untracked-files=no",
        "--",
        &scope,
    ])
    .is_some_and(|status| !status.trim().is_empty())
}

/// Bounded, the way `runmeta.git_output` bounds it with `timeout=5`.
///
/// This runs on the way out, after the signal handlers have stopped being
/// listened to, so a `git` that blocks — a network-mounted worktree, a fork
/// under memory pressure — is a mock that ignores the SIGTERM its stop script
/// just sent and leaves no artifact behind. A commit is worth five seconds and
/// not one more; past that the header says `unknown`, which is the honest
/// answer.
fn git(arguments: &[&str]) -> Option<String> {
    answered_within(
        Command::new("git")
            .arg("-C")
            .arg(SOURCE_DIR)
            .args(arguments),
        GIT_PATIENCE,
    )
}

/// Run a command and take its output, or kill it and take nothing.
///
/// Separate from `git` so that the bound can be tested with a command that is
/// certain to outlast it, rather than by putting a slow `git` on the PATH — an
/// environment variable is process-wide, and the test suite runs in threads.
fn answered_within(command: &mut Command, patience: Duration) -> Option<String> {
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + patience;
    while Instant::now() < deadline {
        match child.try_wait() {
            Err(_) => return None,
            Ok(Some(status)) => return finished(child, status),
            Ok(None) => std::thread::sleep(GIT_POLL),
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    None
}

fn finished(child: Child, status: ExitStatus) -> Option<String> {
    if !status.success() {
        return None;
    }
    let mut stdout = child.stdout?;
    let mut text = String::new();
    std::io::Read::read_to_string(&mut stdout, &mut text).ok()?;
    Some(text)
}

fn host_facts() -> Value {
    json!({
        "hostname": hostname(),
        "machine": std::env::consts::ARCH,
        "platform": platform(),
        "rustc_version": RUSTC_VERSION,
        "binary_version": CRATE_VERSION,
        "cpu_count": configured_cores(),
        "total_ram_bytes": total_ram_bytes(),
    })
}

fn env_facts() -> Value {
    json!({
        "swap_used_bytes": swap_used_bytes(),
        "load_avg_1m": load_avg_1m(),
        "cpu_affinity": cpu_affinity(),
    })
}

/// The cores the machine has, NOT the cores this process may use.
///
/// `available_parallelism` honours `sched_getaffinity`, so a mock launched
/// under the runbooks' `taskset -c 0-1` would record a two-core host — and
/// `env.cpu_affinity` is built from the same mask, so `cpu_count ==
/// len(cpu_affinity)` would become an identity and the comment below it ("the
/// full core list here means the run was NOT pinned") could never be true
/// again. Python's `os.cpu_count()` does not honour the mask, which is what
/// makes the pair of fields a finding rather than a tautology.
///
/// `--tokio-workers` still defaults to the affinity-aware count: that one is
/// about what this process may use, which is the opposite question.
fn configured_cores() -> usize {
    let counted = fs::read_to_string("/proc/cpuinfo")
        .map(|text| {
            text.lines()
                .filter(|line| line.starts_with("processor"))
                .count()
        })
        .unwrap_or(0);
    if counted > 0 {
        return counted;
    }
    crate::cli::available_cores()
}

fn hostname() -> String {
    read_trimmed("/proc/sys/kernel/hostname").unwrap_or_else(|| UNKNOWN.to_string())
}

fn platform() -> String {
    let release = read_trimmed("/proc/sys/kernel/osrelease").unwrap_or_else(|| UNKNOWN.to_string());
    format!(
        "{}-{release}-{}",
        std::env::consts::OS,
        std::env::consts::ARCH
    )
}

fn read_trimmed(path: &str) -> Option<String> {
    Some(fs::read_to_string(path).ok()?.trim().to_string())
}

/// 0 when the platform does not expose it — treat as unknown, not zero.
fn total_ram_bytes() -> u64 {
    meminfo_kb("MemTotal:").unwrap_or(0) * 1024
}

/// -1 when it could not be read, which is how the Python producers spell "not
/// answered" for a number whose zero is a real value.
fn swap_used_bytes() -> i64 {
    match (meminfo_kb("SwapTotal:"), meminfo_kb("SwapFree:")) {
        (Some(total), Some(free)) => (total.saturating_sub(free) * 1024) as i64,
        _ => -1,
    }
}

fn meminfo_kb(field: &str) -> Option<u64> {
    fs::read_to_string("/proc/meminfo")
        .ok()?
        .lines()
        .find(|line| line.starts_with(field))
        .and_then(|line| line.split_whitespace().nth(1)?.parse().ok())
}

fn load_avg_1m() -> f64 {
    read_trimmed("/proc/loadavg")
        .and_then(|line| line.split_whitespace().next()?.parse().ok())
        .unwrap_or(-1.0)
}

/// The full core list here means the run was NOT pinned — which is the finding,
/// on a host whose cores range from 2.5 to 4.8 GHz.
fn cpu_affinity() -> Vec<u32> {
    let Some(line) = read_trimmed("/proc/self/status").and_then(|status| {
        status
            .lines()
            .find(|line| line.starts_with("Cpus_allowed_list:"))
            .map(|line| {
                line.split(':')
                    .nth(1)
                    .unwrap_or_default()
                    .trim()
                    .to_string()
            })
    }) else {
        return Vec::new();
    };
    line.split(',').flat_map(expand_range).collect()
}

fn expand_range(field: &str) -> Vec<u32> {
    match field.trim().split_once('-') {
        None => field.trim().parse().into_iter().collect(),
        Some((first, last)) => match (first.parse::<u32>(), last.parse::<u32>()) {
            (Ok(first), Ok(last)) if first <= last => (first..=last).collect(),
            _ => Vec::new(),
        },
    }
}

#[cfg(test)]
#[path = "provenance_tests.rs"]
mod tests;
