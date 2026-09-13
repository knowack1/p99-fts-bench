//! What a sweep reports, and the arithmetic behind it.
//!
//! Both harnesses write the same shape of CSV, so both compute their
//! percentiles the same way and render an absent latency the same way. A
//! latency that was never measured is blank or `-`, never `0`: a zero would
//! plot as the fastest point on the chart.
/// `None`, never 0.0, when nothing succeeded — no insert landed, or no
/// request came back clean. A point where everything failed would otherwise
/// plot as the best latency on the curve.
pub fn percentile(sorted_values: &[f64], fraction: f64) -> Option<f64> {
    if sorted_values.is_empty() {
        return None;
    }
    let rank = (fraction * sorted_values.len() as f64).ceil() as usize;
    Some(sorted_values[clamp(rank.saturating_sub(1), sorted_values.len())])
}

fn clamp(index: usize, length: usize) -> usize {
    index.min(length - 1)
}

/// A dash where nothing was measured — no insert succeeded, or no request
/// came back clean — so an unmeasured point cannot read as a fast one on
/// stderr any more than it can in the CSV.
pub fn latency_text(value: Option<f64>) -> String {
    value.map_or_else(|| "-".to_string(), |ms| format!("{ms:.2}"))
}
