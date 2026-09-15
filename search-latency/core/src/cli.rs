//! Flags every search-latency binary parses the same way.
use std::fmt;
use std::str::FromStr;

pub use build_rate_core::cli::{
    at_least_one, available_cores, path_setting, split_fields, Levels, OFF,
};

/// Which query classes the matrix's other dimension is made of, in the order
/// the run named them.
///
/// Never empty, and never defaulted: "every class in the set" is the *absence*
/// of this flag rather than a value of it. A default rendered through `Display`
/// and parsed back — which is what clap's `default_value_t` does — would arrive
/// as a request for one class whose name happened to be the word standing in
/// for all of them.
#[derive(Debug, Clone, PartialEq)]
pub struct Classes(pub Vec<String>);

impl FromStr for Classes {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let names: Vec<String> = split_fields(raw).map(str::to_string).collect();
        if names.is_empty() {
            return Err("--query-classes needs at least one class name".to_string());
        }
        Ok(Self(names))
    }
}

impl fmt::Display for Classes {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0.join(","))
    }
}

/// What a header says when the run did not narrow the query set, and what
/// `QuerySet::select` is handed to mean it.
pub const ALL_CLASSES: &str = "all";

/// The names to select, and the word the header records, from a flag that may
/// not have been given.
pub fn chosen_classes(classes: Option<&Classes>) -> Vec<String> {
    classes.map(|chosen| chosen.0.clone()).unwrap_or_default()
}

pub fn classes_setting(classes: Option<&Classes>) -> String {
    classes.map_or_else(|| ALL_CLASSES.to_string(), Classes::to_string)
}

/// A measured window of zero seconds is not a cell, and a negative one is a
/// typo; both would otherwise reach the CSV as a row with no queries in it.
pub fn positive_seconds(raw: &str, what: &str) -> Result<f64, String> {
    let value: f64 = raw.parse().map_err(|_| format!("not a number: {raw:?}"))?;
    if !(value.is_finite() && value > 0.0) {
        return Err(format!("{what} must be greater than 0, got {value}"));
    }
    Ok(value)
}

/// Zero is allowed here and nowhere else: a run may legitimately decline to
/// warm up, and saying so is different from mistyping a duration.
pub fn non_negative_seconds(raw: &str, what: &str) -> Result<f64, String> {
    let value: f64 = raw.parse().map_err(|_| format!("not a number: {raw:?}"))?;
    if !(value.is_finite() && value >= 0.0) {
        return Err(format!("{what} must be 0 or more, got {value}"));
    }
    Ok(value)
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
