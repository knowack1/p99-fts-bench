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
        let levels = split_fields(raw)
            .map(parse_level)
            .collect::<Result<Vec<_>, _>>()?;
        if levels.is_empty() {
            return Err("--concurrency needs at least one level".to_string());
        }
        Ok(Self(levels))
    }
}

impl fmt::Display for Levels {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let levels: Vec<String> = self.0.iter().map(ToString::to_string).collect();
        write!(f, "{}", levels.join(","))
    }
}

fn parse_level(field: &str) -> Result<usize, String> {
    at_least_one(field, "concurrency")
}

/// The count every knob in this harness shares a floor of. Zero is never the
/// degenerate-but-harmless case it looks like: zero concurrency offers nothing,
/// a zero batch carries nothing, and zero tokio workers reaches
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
