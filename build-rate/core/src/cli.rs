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
    let level: usize = field
        .parse()
        .map_err(|_| format!("not an integer: {field:?}"))?;
    if level < 1 {
        return Err(format!("concurrency must be >= 1, got {level}"));
    }
    Ok(level)
}

pub fn split_fields(raw: &str) -> impl Iterator<Item = &str> {
    raw.split(',')
        .map(str::trim)
        .filter(|field| !field.is_empty())
}
