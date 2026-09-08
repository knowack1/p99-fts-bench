"""Churn must offer work the same way on both engines.

The defect these tests exist to prevent shipped a published claim. `churn_load`
held its own dispatch per engine: OpenSearch pipelined through a
`ThreadPoolExecutor` with a hardcoded `IN_FLIGHT = 4` and ignored
`--concurrency`, while ScyllaDB honoured it. So the 8,000 ops/s row of the S28
grid compared OpenSearch offered at a depth of 800 items against ScyllaDB
offered at a depth of 16, and reported whichever client constant fell below
8,000 as an engine that could not sustain the rate.

The in-flight test below fails against that implementation with `peak == 4` at
`concurrency=16`.
"""
import argparse
import dataclasses
import json
import re
import threading
from pathlib import Path

import pytest

from ftsbench import churn_load, churn_stream, load_driver, runmeta

BENCH_DIR = Path(__file__).resolve().parent.parent
GATE_SCRIPT = BENCH_DIR / "tools" / "churn_bench.sh"
RELEASE_TIMEOUT_S = 5.0

DOCUMENTS = [{"title": f"t{i}", "body": f"body {i}"} for i in range(8)]


class CountingSends:
    """Counts sends in flight, releasing them only once `expect` are
    outstanding together.

    Deadlock-or-pass on purpose. A client that can never get `expect`
    operations in flight — because it holds its own in-flight constant instead
    of using `--concurrency` — fails on the timeout naming the peak it reached,
    where a `<=` assertion would have quietly passed a too-shallow client.
    """

    def __init__(self, expect: int, fail_every: int = 0) -> None:
        self._expect = expect
        self._fail_every = fail_every
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._inflight = 0
        self._calls = 0
        self.peak = 0
        self.payloads: list = []

    def send(self, payload, tally) -> None:
        with self._lock:
            self._calls += 1
            call = self._calls
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
            self.payloads.append(payload)
            if self._inflight >= self._expect:
                self._gate.set()
        self._gate.wait(timeout=RELEASE_TIMEOUT_S)
        with self._lock:
            self._inflight -= 1
        if self._fail_every and call % self._fail_every == 0:
            raise RuntimeError(f"injected failure on call {call}")


def churn_args(concurrency: int, batch_size: int, **overrides):
    defaults = dict(
        corpus="unused", batch_size=batch_size, concurrency=concurrency,
        target_rate=0.0, latency_log=None, label="", cache_state="warm",
        max_docs=0, rate=1000.0, duration=1.0, ring=4,
        sample_docs=len(DOCUMENTS), output="unused",
        url="http://localhost:9200", index="wiki-articles",
        hosts="127.0.0.1", port=9042, keyspace="wiki",
        target_flag="--opensearch-disk-store-refresh3", config=None,
        engine="opensearch",
    )
    return argparse.Namespace(**{**defaults, **overrides})


def fixed_source(stream: churn_stream.ChurnStream, count: int):
    def source(args, origin_s):
        for _ in range(count):
            yield stream.next_batch()

    return source


def drive(args, sends: CountingSends, batches: int):
    stream = churn_stream.ChurnStream(DOCUMENTS, args.ring, args.batch_size)
    with churn_load.opensearch_loader(args) as real:
        loader = dataclasses.replace(real, send=sends.send)
        log, tally, wall_s = load_driver.run_timed(
            args, loader, fixed_source(stream, batches))
    return churn_load.churn_summary(args, log, tally, stream, wall_s)


@pytest.mark.parametrize("concurrency", [1, 4, 16])
def test_concurrency_is_operations_in_flight(concurrency):
    """One test over the shared driver rather than one per engine: the point is
    that the quantity is the SAME on both sides, and two per-engine assertions
    can drift apart while both keep passing."""
    args = churn_args(concurrency, batch_size=4)
    sends = CountingSends(expect=concurrency)
    drive(args, sends, batches=4 * concurrency)
    assert sends.peak == concurrency


def test_the_achieved_rate_counts_only_what_landed():
    """`ops_sent` must come from completions, never submissions. The old
    implementation incremented a counter after `apply()` returned, which on the
    pipelined OpenSearch client was before the bulk had been sent."""
    args = churn_args(4, batch_size=5)
    sends = CountingSends(expect=4, fail_every=2)
    summary = drive(args, sends, batches=8)
    assert summary["errors"] == 4
    assert summary["failed_items"] == 4 * 5
    assert summary["ops_sent"] == 4 * 5
    assert summary["requests"] == 8


def test_a_failed_operation_is_reported_not_swallowed():
    args = churn_args(2, batch_size=2)
    sends = CountingSends(expect=2, fail_every=1)
    summary = drive(args, sends, batches=4)
    assert summary["ops_sent"] == 0
    assert "injected failure" in summary["first_error"]


def test_both_engines_are_offered_the_same_items_in_the_same_order():
    """One stream, shared. Two per-engine streams could differ in their
    add/delete mix or in which ids they recycled, and that difference would
    land on the chart as an engine property."""
    stream = churn_stream.ChurnStream(DOCUMENTS, ring_size=3, batch_size=8)
    batch = stream.next_batch()
    for _ in range(3):
        batch = stream.next_batch()

    bulk_ids = ids_from_bulk(churn_load.bulk_payload(batch.items, "idx"))
    statement_ids = [str(parameters[0])
                     for _, parameters in churn_load.statement_parameters(
                         batch.items, insert="INSERT", delete="DELETE")]
    assert bulk_ids == statement_ids


def ids_from_bulk(payload: bytes) -> list[str]:
    actions = [json.loads(line) for line in payload.decode().splitlines()]
    return [next(iter(action.values()))["_id"] for action in actions
            if set(action) & {"index", "delete"}]


def test_the_two_encoders_agree_on_which_items_are_deletes():
    stream = churn_stream.ChurnStream(DOCUMENTS, ring_size=2, batch_size=6)
    for _ in range(3):
        batch = stream.next_batch()

    bulk = [json.loads(line)
            for line in churn_load.bulk_payload(batch.items, "idx").decode().splitlines()]
    bulk_deletes = sum(1 for action in bulk if "delete" in action)
    statement_deletes = sum(1 for statement, _ in churn_load.statement_parameters(
        batch.items, insert="INSERT", delete="DELETE") if statement == "DELETE")
    assert bulk_deletes == statement_deletes


def test_the_header_states_the_in_flight_bound():
    """The churn artifacts on disk record no `concurrency` at all, so the
    hardcoded four was not even auditable from the files it produced."""
    args = churn_args(16, batch_size=200)
    with churn_load.opensearch_loader(args) as loader:
        header = load_driver._header(args, loader)
    assert header["concurrency"] == 16
    assert header["concurrency_unit"] == "operations in flight"
    assert header["n_docs_unit"] == "churn items (adds + deletes)"


def test_the_header_names_the_arm_that_was_measured():
    args = churn_args(4, batch_size=4)
    with churn_load.opensearch_loader(args) as loader:
        header = load_driver._header(args, loader)
    assert header["config"] == "opensearch-refresh3"
    assert header["churn_ops_per_s"] == args.rate


def gate_keys() -> set[str]:
    source = GATE_SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r'summary\.get\("([a-z_]+)"', source)) | set(
        re.findall(r'summary\["([a-z_]+)"\]', source))


def test_the_gate_reads_only_keys_the_summary_still_has():
    """Read out of the script rather than restated here: a summary that renamed
    a key the gate reads would fail every healthy row, and the gate is what
    decides whether a churn row is admissible at all."""
    args = churn_args(2, batch_size=2)
    sends = CountingSends(expect=2)
    summary = drive(args, sends, batches=4)
    missing = gate_keys() - set(summary)
    assert not missing, f"{GATE_SCRIPT.name} reads absent keys: {sorted(missing)}"


def test_the_summary_survives_a_stop_signal(tmp_path):
    """tools/churn_bench.sh TERMs the stream once its query cells finish. The
    summary must still be written, or the row gate reads an empty artifact and
    fails a healthy row."""
    output = tmp_path / "churn.jsonl"
    args = churn_args(2, batch_size=2, latency_log=str(output),
                      output=str(output))
    stopper = _StopAfter(2)
    stream = churn_stream.ChurnStream(DOCUMENTS, args.ring, args.batch_size)
    source = churn_stream.churn_source(stream, duration_s=60.0,
                                       should_stop=stopper)
    sends = CountingSends(expect=1)
    with churn_load.opensearch_loader(args) as real:
        loader = dataclasses.replace(real, send=sends.send)
        log, tally, wall_s = load_driver.run_timed(args, loader, source)
    load_driver.append_record(str(output),
                              churn_load.churn_summary(args, log, tally,
                                                       stream, wall_s))
    header, records = runmeta.read_jsonl(output)
    summaries = [item for item in records if item["record"] == "churn_summary"]
    assert header["producer"] == "churn_load"
    assert len(summaries) == 1


class _StopAfter:
    def __init__(self, batches: int) -> None:
        self._remaining = batches

    def __call__(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0
