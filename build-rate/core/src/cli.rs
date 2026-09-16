//! Flags both harnesses parse the same way.
use std::fmt;
use std::str::FromStr;

pub fn available_cores() -> usize {
    std::thread::available_parallelism().map_or(1, |cores| cores.get())
}

/// Repeats are kept: a throwaway leading level absorbs the cold page cache, and
/// dropping it silently would make the ladder disagree with what was asked for.
#[derive(Debug, Clone, PartialEq)]
pub struct Levels(pub Vec<usize>);

impl FromStr for Levels {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        parse_ladder(raw, "--concurrency", "concurrency").map(Self)
    }
}

impl fmt::Display for Levels {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", join_ladder(&self.0))
    }
}

/// The offered-rate ladder, in documents per second.
///
/// A separate type from [`Levels`] rather than a second use of it, because the
/// two are alternative axes and the harness refuses both at once: sharing one
/// type would make "which ladder is this" a question about a call site instead
/// of about a value. What they do share — the comma-separated form, the
/// repeats-are-kept rule, the `>= 1` floor — is `parse_ladder`, so a step means
/// the same thing on either axis.
#[derive(Debug, Clone, PartialEq)]
pub struct Rates(pub Vec<u64>);

impl FromStr for Rates {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let steps = parse_ladder(raw, "--target-rate", "target rate")?;
        Ok(Self(steps.into_iter().map(|rate| rate as u64).collect()))
    }
}

impl fmt::Display for Rates {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", join_ladder(&self.0))
    }
}

/// `flag` names the knob for the empty case and `what` names one step for a bad
/// one, so neither ladder inherits the other's word for its own error.
fn parse_ladder(raw: &str, flag: &str, what: &str) -> Result<Vec<usize>, String> {
    let steps = split_fields(raw)
        .map(|field| at_least_one(field, what))
        .collect::<Result<Vec<_>, _>>()?;
    if steps.is_empty() {
        return Err(format!("{flag} needs at least one level"));
    }
    Ok(steps)
}

fn join_ladder<T: fmt::Display>(steps: &[T]) -> String {
    steps
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join(",")
}

/// The count every knob in this harness shares a floor of. Zero is never the
/// degenerate-but-harmless case it looks like: zero concurrency offers nothing,
/// a zero batch carries nothing, a zero offered rate is closed loop wearing an
/// open loop's flag, and zero tokio workers reaches
/// `Builder::worker_threads(0)`, which panics instead of failing.
pub fn at_least_one(raw: &str, what: &str) -> Result<usize, String> {
    let value: usize = raw
        .parse()
        .map_err(|_| format!("not an integer: {raw:?}"))?;
    if value < 1 {
        return Err(format!("{what} must be >= 1, got {value}"));
    }
    Ok(value)
}

pub fn split_fields(raw: &str) -> impl Iterator<Item = &str> {
    raw.split(',')
        .map(str::trim)
        .filter(|field| !field.is_empty())
}

/// `off` rather than an empty cell when a path was not given, so a header can
/// say that an optional output was not written rather than leaving a reader to
/// guess. Both halves name their `--samples-dir` this way.
pub fn path_setting(path: Option<&std::path::PathBuf>) -> String {
    path.map_or_else(|| OFF.to_string(), |path| path.display().to_string())
}

/// What a header says about an output or a watch that was not switched on.
pub const OFF: &str = "off";

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
