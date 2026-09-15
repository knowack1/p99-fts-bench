//! Command line: which engine to impersonate, on which port, and how much of a
//! delay to put in front of the answer.
//!
//! The flag surface is the Python sink's, name for name and default for
//! default, because scripts in two runbooks launch it by those names and a
//! rename would land as a run that never started. `--tokio-workers` is the one
//! addition, and it is the reason this rewrite exists.
use std::path::PathBuf;
use std::time::Duration;

use clap::{Parser, ValueEnum};

pub const DEFAULT_HTTP_PORT: u16 = 9200;
pub const DEFAULT_CQL_PORT: u16 = 9042;
/// Served by default with the ScyllaDB-shaped endpoint, because `scyllarate`
/// gates every level on it. NOT with the OpenSearch-shaped one: there is no
/// index to report there, and a fixed default would make the second of N http
/// mocks fail to bind. An explicit `--vs-port` is obeyed in either mode.
pub const DEFAULT_VS_PORT: u16 = 6080;
pub const DEFAULT_VS_KEYSPACE: &str = "wiki";
pub const DEFAULT_VS_INDEX: &str = "articles_body_fts";
pub const DEFAULT_REPORT_INTERVAL_S: &str = "5.0";
pub const DEFAULT_HOST: &str = "0.0.0.0";

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Mode {
    Http,
    Cql,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Http => "http",
            Self::Cql => "cql",
        }
    }

    fn default_port(self) -> u16 {
        match self {
            Self::Http => DEFAULT_HTTP_PORT,
            Self::Cql => DEFAULT_CQL_PORT,
        }
    }
}

#[derive(Debug, Parser)]
#[command(
    name = "engine-mock",
    about = "Accept-and-discard OpenSearch and ScyllaDB endpoints: the instrument a client ceiling is measured against",
    long_about = "Answers correctly, stores nothing, and cannot be the constraint.\n\n\
A client's own ceiling cannot be measured against a real engine: at the rate where the engine \
saturates, the engine is what the number describes. So this impersonates one — the wire \
protocol, the routes, the index lifecycle a loader gates on — and discards every document.\n\n\
NO STORAGE, NO CONSISTENCY, NO SCHEMA, NO RELEVANCE. A run against this measures the loader \
and nothing else, and no number taken from one belongs beside an engine result."
)]
pub struct Args {
    #[arg(long, value_enum)]
    pub mode: Mode,

    /// Bind address. The fleet runs the mock on its own host, so a localhost
    /// bind would hide the network RTT the in-flight count exists to cover.
    #[arg(long, default_value = DEFAULT_HOST)]
    pub host: String,

    /// Default 9200 for http, 9042 for cql
    #[arg(long)]
    pub port: Option<u16>,

    /// Vector-store index-status endpoint; 0 disables. Defaults to 6080 with
    /// --mode cql, where scyllarate gates every level on it, and to off with
    /// --mode http.
    #[arg(long)]
    pub vs_port: Option<u16>,

    #[arg(long, default_value = DEFAULT_VS_KEYSPACE)]
    pub vs_keyspace: String,

    /// The one index this mock answers a count for; any other is 404 and
    /// recorded, so a harness pointed at the wrong index cannot pass its own
    /// gate
    #[arg(long, default_value = DEFAULT_VS_INDEX)]
    pub vs_index: String,

    /// Hold a freshly created index unready for this long, to exercise a
    /// loader's readiness gate. BUILDING on the vector-store endpoint, 503 from
    /// _count and _stats on the OpenSearch one — the two engines' name for the
    /// same state.
    #[arg(
        long = "vs-serving-delay-ms",
        alias = "index-ready-delay-ms",
        allow_hyphen_values = true,
        value_parser = not_negative,
        default_value = "0.0"
    )]
    pub vs_serving_delay_ms: f64,

    /// Publish accepted documents to _count and _stats only this often, the way
    /// an OpenSearch refresh_interval does; negative never publishes except on
    /// an explicit _refresh. 0 (the default) publishes immediately, which is
    /// what every run recorded before this flag existed measured.
    ///
    /// Every numeric flag here takes `allow_hyphen_values`, so that a negative
    /// reaches the value parser and is answered with what to do about it rather
    /// than with clap's "unexpected argument". On this one a negative is not a
    /// mistake at all — it is the documented way to ask for "never" — so
    /// without the attribute `--os-refresh-interval-ms -1` would die at launch,
    /// inside a runbook that has already backgrounded the mock.
    #[arg(
        long,
        allow_hyphen_values = true,
        value_parser = a_duration,
        default_value_t = 0.0
    )]
    pub os_refresh_interval_ms: f64,

    /// Delay every response by this much, to construct a case where the client
    /// is NOT the constraint
    #[arg(long, allow_hyphen_values = true, value_parser = not_negative, default_value = "0.0")]
    pub delay_ms: f64,

    /// 0 = until terminated
    #[arg(long, allow_hyphen_values = true, value_parser = not_negative, default_value = "0.0")]
    pub duration: f64,

    #[arg(long, allow_hyphen_values = true, value_parser = above_zero, default_value = DEFAULT_REPORT_INTERVAL_S)]
    pub report_interval: f64,

    #[arg(long, default_value = "")]
    pub label: String,

    /// Write what the mock accepted as JSON on exit
    #[arg(long)]
    pub stats_out: Option<PathBuf>,

    /// Tokio worker threads; defaults to every core the machine reports. This
    /// is what the rewrite added: the Python sink answered every connection on
    /// one thread, and its own CPU became a gate of every run against it.
    #[arg(long, value_parser = at_least_one)]
    pub tokio_workers: Option<usize>,
}

impl Args {
    pub fn port(&self) -> u16 {
        self.port.unwrap_or_else(|| self.mode.default_port())
    }

    /// An explicit `--vs-port 0` is off, and so is the default in http mode.
    pub fn vs_port(&self) -> Option<u16> {
        let port = match (self.vs_port, self.mode) {
            (Some(port), _) => port,
            (None, Mode::Cql) => DEFAULT_VS_PORT,
            (None, Mode::Http) => 0,
        };
        (port != 0).then_some(port)
    }

    pub fn tokio_workers(&self) -> usize {
        self.tokio_workers.unwrap_or_else(available_cores)
    }

    pub fn delay(&self) -> Duration {
        milliseconds(self.delay_ms)
    }

    pub fn serving_delay(&self) -> Duration {
        milliseconds(self.vs_serving_delay_ms)
    }

    pub fn duration(&self) -> Option<Duration> {
        (self.duration > 0.0).then(|| Duration::from_secs_f64(self.duration))
    }

    pub fn report_interval(&self) -> Duration {
        Duration::from_secs_f64(self.report_interval)
    }

    /// The one check a value parser cannot make, because it is about two flags
    /// at once. Both endpoints bind before anything is served, so the second
    /// would fail with an address-in-use the launcher reports as a mock that
    /// would not start — true, but not why.
    pub fn refuse_a_port_collision(&self) -> Result<(), String> {
        if self.vs_port() == Some(self.port()) {
            return Err(format!(
                "--port and --vs-port are both {}; the engine endpoint and the \
                 vector-store endpoint are two listeners and need two ports",
                self.port()
            ));
        }
        Ok(())
    }

    /// Negative stays never rather than becoming a small delay: that is
    /// OpenSearch's `refresh_interval: -1`, and a mock that turned it into a
    /// 3-millisecond refresh could not produce the state a build-rate watch has
    /// to survive.
    pub fn refresh(&self) -> crate::index::Refresh {
        if self.os_refresh_interval_ms < 0.0 {
            return crate::index::Refresh::Never;
        }
        crate::index::Refresh::Every(milliseconds(self.os_refresh_interval_ms))
    }
}

fn milliseconds(value: f64) -> Duration {
    Duration::from_secs_f64((value / 1000.0).max(0.0))
}

pub fn available_cores() -> usize {
    std::thread::available_parallelism().map_or(1, |cores| cores.get())
}

/// Zero is never the degenerate-but-harmless case it looks like: zero tokio
/// workers reaches `Builder::worker_threads(0)`, which panics instead of
/// failing.
/// A number of seconds or milliseconds that `Duration` can actually hold.
///
/// Every one of these flags ends in `Duration::from_secs_f64`, which panics on
/// a negative, on a NaN and on anything too large — and it would panic *after*
/// the readiness line has been printed and the ports bound, leaving a launcher
/// with a clean "ready" line for a process that is already dead and a recorded
/// pid that is stale. Refusing at the flag is the difference between a typo
/// that fails to start and a ladder that runs against nothing.
///
/// Every numeric flag takes `allow_hyphen_values` so that a negative reaches
/// this check rather than clap's argument scanner: "must be >= 0" says what to
/// do about it, where "unexpected argument '-5' found" does not.
fn not_negative(raw: &str) -> Result<f64, String> {
    let value = a_duration(raw)?;
    if value < 0.0 {
        return Err(format!("must be >= 0, got {value}"));
    }
    Ok(value)
}

/// The ceiling is `Duration`'s own, in the unit the flag is written in. Every
/// flag here is seconds or milliseconds, so no run can want a number near it.
const LONGEST: f64 = 1e15;

fn a_duration(raw: &str) -> Result<f64, String> {
    let value: f64 = raw.parse().map_err(|_| format!("not a number: {raw:?}"))?;
    if !value.is_finite() {
        return Err(format!("must be a finite number, got {raw}"));
    }
    if value.abs() > LONGEST {
        return Err(format!("must be smaller than {LONGEST:e}, got {raw}"));
    }
    Ok(value)
}

/// A zero report interval is a `sleep(0)` loop printing to stderr as fast as a
/// core allows, which competes for the machine with the thing being measured.
fn above_zero(raw: &str) -> Result<f64, String> {
    let value = not_negative(raw)?;
    if value == 0.0 {
        return Err("must be > 0".to_string());
    }
    Ok(value)
}

fn at_least_one(raw: &str) -> Result<usize, String> {
    let value: usize = raw
        .parse()
        .map_err(|_| format!("not an integer: {raw:?}"))?;
    if value < 1 {
        return Err(format!("must be >= 1, got {value}"));
    }
    Ok(value)
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
