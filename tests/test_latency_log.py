import argparse
import json
import threading
import time
import uuid
from pathlib import Path
from concurrent import futures

import pytest

from . import write_path

from ftsbench import (latency_log, load_driver, load_retry, opensearch_load,
                      runmeta, scylla_load)

ORIGIN = 1000.0


def make_log(stream=None, origin_s: float = ORIGIN) -> latency_log.LatencyLog:
    return latency_log.LatencyLog(stream, origin_s)


def timing(i: int, intended: float, start: float, end: float) -> latency_log.OpTiming:
    return latency_log.OpTiming(i=i, t_intended_s=ORIGIN + intended,
                                t_start_s=ORIGIN + start, t_end_s=ORIGIN + end)


def write_corpus(path, doc_count: int) -> str:
    """Mirrors the corpus contract: `uuid` is a real uuid5 of the page id, which
    scylla_load parses into the partition key."""
    with open(path, "w", encoding="utf-8") as handle:
        for doc_id in range(doc_count):
            doc_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(doc_id))
            handle.write(json.dumps({"id": doc_id, "uuid": str(doc_uuid),
                                     "title": f"t{doc_id}", "text": f"body {doc_id}"}) + "\n")
    return str(path)


def loader_args(corpus: str, batch_size: int) -> argparse.Namespace:
    return argparse.Namespace(corpus=corpus, max_docs=0, batch_size=batch_size,
                              index="wiki-articles", target_rate=0.0,
                              concurrency=1, latency_log=None, label="",
                              cache_state="unspecified",
                              no_refresh_during_load=False)


def action_ids(payload: bytes) -> list[str]:
    lines = payload.decode("utf-8").strip().split("\n")
    return [json.loads(line)["index"]["_id"] for line in lines[::2]]


def dispatch_with_fake_transport(monkeypatch, corpus: str, batch_size: int,
                                 concurrency: int) -> list[bytes]:
    sent: list[bytes] = []
    lock = threading.Lock()

    def fake_send(session, url, payload):
        with lock:
            sent.append(payload)

    monkeypatch.setattr(opensearch_load, "send_bulk", fake_send)
    monkeypatch.setattr(opensearch_load, "thread_session", lambda: None)
    monkeypatch.setattr(opensearch_load.samplers, "OpenSearchSampler",
                        lambda *a, **k: argparse.Namespace(version=lambda: "test"))
    args = loader_args(corpus, batch_size)
    args.concurrency = concurrency
    load_driver.run(args, opensearch_load.build_loader(args, "http://localhost:9200"))
    return sent


def test_latency_is_measured_from_intended_start_not_actual_start():
    """The whole point of the record: a dispatch that was 50 ms late must report
    60 ms, not the 10 ms the engine spent on the wire."""
    log = make_log()
    log.record(timing(0, intended=1.0, start=1.05, end=1.06), op="bulk", n_docs=500)
    summary = log.summary()
    assert summary["p50_ms"] == pytest.approx(60.0)
    assert summary["ops"] == 1
    assert summary["errors"] == 0


def test_record_fields_are_relative_to_the_run_origin(tmp_path):
    """SCHEMAS.md wants seconds from run start, not raw perf_counter stamps: a
    plot that assumed otherwise would place every point at machine uptime."""
    path = tmp_path / "relative.jsonl"
    origin_s = 987654.321
    with latency_log.open_log(str(path), runmeta.header("t", "opensearch"), origin_s) as log:
        log.record(latency_log.OpTiming(0, origin_s + 12.34, origin_s + 12.3421,
                                        origin_s + 12.3498), op="bulk", n_docs=500)
    _, records = runmeta.read_jsonl(path)
    assert records[0]["t_intended_s"] == pytest.approx(12.34)
    assert records[0]["t_end_s"] == pytest.approx(12.3498)


def test_written_record_matches_the_schema_fields(tmp_path):
    path = tmp_path / "c3-opensearch-1.jsonl"
    with latency_log.open_log(str(path), runmeta.header("t", "opensearch"), ORIGIN) as log:
        log.record(timing(3, intended=1.0, start=1.002, end=1.010),
                   op="bulk", n_docs=500)
    _, records = runmeta.read_jsonl(path)
    assert records[0] == {
        "record": "latency_op", "i": 3,
        "t_intended_s": 1.0, "t_start_s": 1.002, "t_end_s": 1.01,
        "latency_ms": 10.0, "service_ms": 8.0, "queue_ms": 2.0,
        "op": "bulk", "n_docs": 500, "class": None, "query_i": None,
        "hits": None, "ok": True, "error": None,
    }


def test_records_round_trip_through_read_jsonl(tmp_path):
    path = tmp_path / "c3-scylla-1.jsonl"
    header = runmeta.header("scylla_load", "scylladb", label="round trip")
    with latency_log.open_log(str(path), header, ORIGIN) as log:
        for i in range(5):
            log.record(timing(i, intended=float(i), start=float(i), end=i + 0.01),
                       op="insert", n_docs=1000)
    read_header, records = runmeta.read_jsonl(path)
    assert read_header["producer"] == "scylla_load"
    assert read_header["schema_version"] == runmeta.SCHEMA_VERSION
    assert [record["i"] for record in records] == [0, 1, 2, 3, 4]
    assert all(record["record"] == "latency_op" for record in records)


def test_every_record_is_flushed_so_a_killed_run_is_still_readable(tmp_path):
    path = tmp_path / "killed.jsonl"
    with latency_log.open_log(str(path), runmeta.header("t", "opensearch"), ORIGIN) as log:
        log.record(timing(0, 0.0, 0.0, 0.01), op="bulk", n_docs=1)
        _, records = runmeta.read_jsonl(path)
        assert len(records) == 1


def test_failed_op_is_recorded_with_ok_false_and_its_message(tmp_path, capsys):
    path = tmp_path / "failed.jsonl"
    with latency_log.open_log(str(path), runmeta.header("t", "opensearch"), ORIGIN) as log:
        log.record(timing(0, 0.0, 0.0, 2.0), op="bulk", n_docs=500,
                   ok=False, error="RuntimeError: bulk rejected")
    _, records = runmeta.read_jsonl(path)
    assert records[0]["ok"] is False
    assert records[0]["error"] == "RuntimeError: bulk rejected"
    assert "bulk rejected" in capsys.readouterr().err


def test_failed_op_is_excluded_from_percentiles_but_counted_as_an_error():
    """An engine that rejects writes under merge pressure must not be rewarded
    with a shorter tail than one that answers them slowly."""
    log = make_log()
    log.record(timing(0, 0.0, 0.0, 0.010), op="bulk", n_docs=500)
    log.record(timing(1, 1.0, 1.0, 31.0), op="bulk", n_docs=500,
               ok=False, error="ReadTimeout")
    summary = log.summary()
    assert summary["ops"] == 2
    assert summary["errors"] == 1
    assert summary["count"] == 1
    assert summary["max_ms"] == pytest.approx(10.0)


def test_summary_reports_no_percentile_when_every_op_failed():
    log = make_log()
    log.record(timing(0, 0.0, 0.0, 1.0), op="bulk", n_docs=1, ok=False, error="boom")
    assert "p50_ms" not in log.summary()
    assert "no successful operation" in log.summary_line()


def test_queue_time_is_reported_so_a_generator_bound_run_is_visible():
    log = make_log()
    log.record(timing(0, 0.0, 0.9, 1.0), op="bulk", n_docs=500)
    assert log.summary()["queue_p99_ms"] == pytest.approx(900.0)


def test_timed_op_records_a_failing_action_instead_of_raising():
    log = make_log(origin_s=time.perf_counter())

    def boom() -> None:
        raise RuntimeError("bulk request had item failures")

    latency_log.timed_op(log, 4, time.perf_counter(), "bulk", 500, boom)
    summary = log.summary()
    assert summary["ops"] == 1
    assert summary["errors"] == 1


def test_timed_op_records_a_successful_action():
    log = make_log(origin_s=time.perf_counter())
    latency_log.timed_op(log, 0, time.perf_counter(), "insert", 1000, lambda: None)
    assert log.summary()["errors"] == 0
    assert log.summary()["docs"] == 1000


def test_unpaced_schedule_intends_now_so_latency_equals_service():
    schedule = latency_log.op_schedule(0.0, batch_size=500,
                                       origin_s=time.perf_counter())
    ops = [next(schedule) for _ in range(3)]
    assert [op.i for op in ops] == [0, 1, 2]
    assert ops[0].t_intended_s <= ops[1].t_intended_s <= ops[2].t_intended_s


def test_paced_schedule_spaces_ops_by_batch_size_over_target_rate():
    origin_s = time.perf_counter() - 100.0
    schedule = latency_log.op_schedule(1000.0, batch_size=500, origin_s=origin_s)
    ops = [next(schedule) for _ in range(4)]
    assert [op.t_intended_s - origin_s for op in ops] == pytest.approx(
        [0.0, 0.5, 1.0, 1.5])


def test_paced_schedule_stays_contiguous_across_a_chunk_boundary():
    """The chunking is an implementation detail of pacer.schedule materialising
    its offsets; it must not renumber operations or shift the schedule."""
    origin_s = time.perf_counter() - 100.0
    schedule = latency_log.op_schedule(1000.0, batch_size=1, origin_s=origin_s)
    count = latency_log.SCHEDULE_CHUNK_OPS + 4
    ops = [next(schedule) for _ in range(count)]
    assert [op.i for op in ops] == list(range(count))
    assert [op.t_intended_s - origin_s for op in ops[-3:]] == pytest.approx(
        [(count - 3) / 1000.0, (count - 2) / 1000.0, (count - 1) / 1000.0])


def test_batch_assignment_is_identical_at_concurrency_1_and_8(monkeypatch, tmp_path):
    """Concurrency must change only *when* a batch is sent, never *what* is in
    it — otherwise two runs are loading two different corpora."""
    corpus = write_corpus(tmp_path / "corpus.jsonl", 37)
    serial = dispatch_with_fake_transport(monkeypatch, corpus, 10, concurrency=1)
    concurrent = dispatch_with_fake_transport(monkeypatch, corpus, 10, concurrency=8)
    serial_batches = sorted(action_ids(payload) for payload in serial)
    concurrent_batches = sorted(action_ids(payload) for payload in concurrent)
    assert serial_batches == concurrent_batches
    assert [len(batch) for batch in sorted(serial_batches, key=len)] == [7, 10, 10, 10]


def test_every_document_is_sent_exactly_once_under_concurrency(monkeypatch, tmp_path):
    corpus = write_corpus(tmp_path / "corpus.jsonl", 250)
    payloads = dispatch_with_fake_transport(monkeypatch, corpus, 16, concurrency=8)
    ids = [doc_id for payload in payloads for doc_id in action_ids(payload)]
    assert sorted(int(doc_id) for doc_id in ids) == list(range(250))


def scylla_dispatch_with_fake_driver(monkeypatch, corpus: str, batch_size: int,
                                     concurrency: int) -> list[list[tuple]]:
    sent: list[list[tuple]] = []
    lock = threading.Lock()

    def fake_results(session, statement, parameters, rows_in_flight):
        with lock:
            sent.append(list(parameters))
        return [(True, None) for _ in parameters]

    monkeypatch.setattr(scylla_load, "concurrent_results", fake_results)
    args = loader_args(corpus, batch_size)
    args.concurrency = concurrency
    args.rows_in_flight = 0
    args.unlogged_batch_rows = 0
    args.keyspace, args.table = "wiki", "articles"
    load_driver.run(args, scylla_load.build_loader(args, object(), None))
    return sent


def test_scylla_also_sends_every_document_exactly_once_under_concurrency(
        monkeypatch, tmp_path):
    """The ScyllaDB side dispatched one batch at a time on the caller's thread
    until the loaders were unified, which capped it at one GIL core. It now
    goes through the same pool as OpenSearch, so it has to survive the same
    test — losing or duplicating a document under concurrency would shorten a
    corpus without failing a run."""
    corpus = write_corpus(tmp_path / "corpus.jsonl", 250)
    batches = scylla_dispatch_with_fake_driver(monkeypatch, corpus, 16,
                                               concurrency=8)
    page_ids = [row[1] for batch in batches for row in batch]
    assert sorted(page_ids) == list(range(250))


@pytest.mark.parametrize("producer", write_path.WRITE_PATH_PRODUCERS)
def test_no_write_path_producer_owns_its_own_dispatch(producer):
    """The property the unification exists to guarantee: no producer may own
    its own dispatch loop, because that is how the two --concurrency flags came
    to mean different quantities — twice. churn_load was outside this list
    while holding a ThreadPoolExecutor with a hardcoded four bulks in flight,
    which is what put two client constants on the S28 chart."""
    assert "ThreadPoolExecutor" not in write_path.referenced_names(producer)
    assert (write_path.calls_qualified(producer, "load_driver.run")
            or write_path.calls_qualified(producer, "load_driver.run_timed"))


def test_the_registry_covers_every_write_path_module():
    """A producer that sends write traffic and is absent from the registry is
    exempt from every comparability test here, which is exactly how churn_load
    kept its own executor."""
    missing = write_path.modules_on_disk() - set(write_path.WRITE_PATH_PRODUCERS)
    assert not missing, f"write-path modules not covered: {sorted(missing)}"


def test_bulk_pool_surfaces_a_worker_exception_rather_than_dropping_it():
    with futures.ThreadPoolExecutor(max_workers=2) as executor:
        pool = load_driver.InFlightPool(executor, 2)
        pool.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            pool.drain()


def test_bulk_pool_blocks_the_dispatcher_once_max_inflight_is_reached():
    """Unbounded submission would absorb a paced run's backlog into client
    memory and hide it from queue_ms."""
    release = threading.Event()
    third_submitted = threading.Event()

    def blocking_work() -> None:
        release.wait(timeout=5)

    with futures.ThreadPoolExecutor(max_workers=4) as executor:
        pool = load_driver.InFlightPool(executor, 2)
        pool.submit(blocking_work)
        pool.submit(blocking_work)
        dispatcher = threading.Thread(
            target=lambda: (pool.submit(blocking_work), third_submitted.set()))
        dispatcher.start()
        assert not third_submitted.wait(timeout=0.2)
        release.set()
        dispatcher.join(timeout=5)
        assert third_submitted.is_set()
        pool.drain()


def test_client_bound_warning_fires_only_at_concurrency_one(capsys):
    load_driver.warn_if_client_bound(1)
    assert "not quotable" in capsys.readouterr().err
    load_driver.warn_if_client_bound(16)
    assert capsys.readouterr().err == ""


def test_summary_line_says_when_there_are_too_few_ops_for_a_p99():
    """A 10-batch ingest run has no p99; printing one anyway is how a smoke test
    acquires a tail number."""
    log = make_log()
    for i in range(10):
        log.record(timing(i, float(i), float(i), i + 0.01), op="bulk", n_docs=500)
    assert "p99 undefined at 10 operations" in log.summary_line()


def test_summary_line_omits_the_note_once_the_p99_is_defined():
    log = make_log()
    for i in range(150):
        log.record(timing(i, float(i), float(i), i + 0.01), op="bulk", n_docs=500)
    assert "undefined" not in log.summary_line()
