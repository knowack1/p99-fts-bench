//! What survives a sweep that goes wrong: the points already measured, and the
//! difference between "no bulk came back clean" and "the latency was zero".
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;

use crate::corpus::DocumentBatch;
use crate::fakes::{
    a_cluster, a_ladder, a_shape, a_source, a_truncated_source, quiet_notes, FakeInserter,
};
use crate::report::{CsvSink, PointResult};
use crate::sweep::{run_sweep, Cancel};

const BATCH: usize = 5;

type BatchSource = Box<dyn Iterator<Item = Result<DocumentBatch>> + Send>;

/// Fails once `levels_before_failure` levels have been served, the way a
/// truncated JSONL line does part way down a ladder.
fn a_source_that_breaks_after(
    good_docs: usize,
    levels_before_failure: usize,
) -> impl Fn() -> Result<BatchSource> {
    let served = std::sync::Mutex::new(0usize);
    move || {
        let mut level = served.lock().unwrap();
        *level += 1;
        Ok(if *level > levels_before_failure {
            Box::new(a_truncated_source(good_docs, BATCH)) as BatchSource
        } else {
            Box::new(a_source(good_docs, BATCH)) as BatchSource
        })
    }
}

async fn sweep_into(
    sink: &mut CsvSink,
    open_source: impl Fn() -> Result<BatchSource>,
    levels: &[usize],
) -> Result<()> {
    let mut collect = |result: PointResult| -> Result<()> {
        sink.append_row(&result)?;
        Ok(())
    };
    run_sweep(
        a_ladder(Arc::new(FakeInserter::new()), levels, a_shape(BATCH)),
        open_source,
        &quiet_notes(),
        &Cancel::default(),
        &mut collect,
    )
    .await?;
    Ok(())
}

fn measured_levels(text: &str) -> Vec<String> {
    text.lines()
        .filter(|line| !line.starts_with('#'))
        .skip(1)
        .filter_map(|row| row.split(',').next().map(str::to_string))
        .collect()
}

#[tokio::test]
async fn points_measured_before_a_mid_sweep_failure_are_still_on_disk() {
    let tmp = tempfile::tempdir().unwrap();
    let destination = tmp.path().join("sweep.csv");

    let mut sink = CsvSink::open(destination.to_str().unwrap()).unwrap();
    sink.write_preamble(&a_cluster(), &[]).unwrap();
    let outcome = sweep_into(&mut sink, a_source_that_breaks_after(20, 2), &[2, 4, 8]).await;
    drop(sink);

    assert!(outcome.is_err());
    let text = std::fs::read_to_string(&destination).unwrap();
    assert_eq!(measured_levels(&text), ["2", "4"]);
}

#[tokio::test]
async fn a_clean_ladder_writes_every_level_it_was_asked_for() {
    let tmp = tempfile::tempdir().unwrap();
    let destination = tmp.path().join("sweep.csv");

    let mut sink = CsvSink::open(destination.to_str().unwrap()).unwrap();
    sink.write_preamble(&a_cluster(), &[]).unwrap();
    let outcome = sweep_into(&mut sink, a_source_that_breaks_after(20, 99), &[2, 4]).await;
    drop(sink);

    assert!(outcome.is_ok());
    let text = std::fs::read_to_string(&destination).unwrap();
    assert_eq!(measured_levels(&text), ["2", "4"]);
}

#[tokio::test]
async fn an_interrupted_sweep_keeps_the_levels_it_measured() {
    let tmp = tempfile::tempdir().unwrap();
    let destination = tmp.path().join("sweep.csv");
    let cancel = Cancel::default();

    let mut sink = CsvSink::open(destination.to_str().unwrap()).unwrap();
    sink.write_preamble(&a_cluster(), &[]).unwrap();
    let outcome = {
        let stop = cancel.clone();
        let mut collect = move |result: PointResult| -> Result<()> {
            sink.append_row(&result)?;
            stop.trigger();
            Ok(())
        };
        run_sweep(
            a_ladder(
                Arc::new(FakeInserter::with_latency(Duration::from_millis(1))),
                &[2, 4],
                a_shape(BATCH),
            ),
            || Ok(a_source(100, BATCH)),
            &quiet_notes(),
            &cancel,
            &mut collect,
        )
        .await
    };

    assert!(format!("{:#}", outcome.unwrap_err()).contains("interrupted"));
    let text = std::fs::read_to_string(&destination).unwrap();
    assert_eq!(measured_levels(&text), ["2"]);
}
