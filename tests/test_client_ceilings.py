"""The two constants must come out of the files, or not come out at all.

Every test here is about a way the derivation could produce a plausible number
from evidence that does not support it — which is the failure Phase 0 exists to
prevent, one layer up from the gate that consumes its output.
"""
import json

from ftsbench import client_ceilings, runmeta


def write_jsonl(path, header, records) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        runmeta.write_record(stream, {"record": "header", **header})
        for record in records:
            runmeta.write_record(stream, record)


def latency_log(directory, engine, batch, concurrency, workers, rep, shard,
                ops, wall_s, n_docs=None, failures=0) -> None:
    """`ops` operations spread evenly across `wall_s`, all but `failures` ok."""
    per_op = wall_s / ops
    records = [
        {"record": "latency_op", "i": index,
         "t_start_s": index * per_op, "t_end_s": (index + 1) * per_op,
         "n_docs": n_docs if n_docs is not None else batch,
         "op": "insert", "ok": index >= failures, "error": None}
        for index in range(ops)
    ]
    write_jsonl(directory / f"lat-{engine}-b{batch}-c{concurrency}"
                            f"-w{workers}-r{rep}-s{shard}.jsonl",
                {"producer": "test"}, records)


def probe_series(directory, engine, batch, concurrency, workers, rep,
                 process_cores, box_cores, cores_available=8,
                 ticks=6, busiest=None) -> None:
    records = []
    for tick in range(ticks):
        for pid in range(workers):
            records.append({
                "record": "generator_sample", "i": tick, "pid": 1000 + pid,
                "running": True, "cpu_cores_used": process_cores,
                "busiest_thread_cores": (busiest if busiest is not None
                                         else process_cores),
                "threads": 3, "rss_bytes": 1,
            })
        records.append({"record": "generator_box_sample", "i": tick,
                        "cpu_cores_used": box_cores,
                        "cores_available": cores_available,
                        "loaders_running": workers})
    write_jsonl(directory / f"gen-{engine}-b{batch}-c{concurrency}"
                            f"-w{workers}-r{rep}.jsonl",
                {"producer": "generator_probe"}, records)


def test_a_rate_divides_only_the_documents_that_landed(tmp_path):
    latency_log(tmp_path, "opensearch", 100, 16, 1, 1, 0, ops=10, wall_s=2.0,
                failures=2)
    point = client_ceilings.collect(tmp_path)[0]
    assert point.ok_ops == 8
    assert point.ok_docs == 800
    assert point.docs_per_s == 400.0
    assert point.errors == 2


def test_a_points_wall_is_the_slowest_of_its_workers(tmp_path):
    latency_log(tmp_path, "scylladb", 1, 64, 2, 101, 0, ops=100, wall_s=1.0)
    latency_log(tmp_path, "scylladb", 1, 64, 2, 101, 1, ops=100, wall_s=4.0)
    point = client_ceilings.collect(tmp_path)[0]
    assert point.shards == 2
    assert point.wall_s == 4.0
    assert point.ok_docs == 200


def test_the_ceiling_is_the_best_rung_of_the_median_repetition(tmp_path):
    for rep, wall in ((1, 1.0), (2, 1.0), (3, 10.0)):
        latency_log(tmp_path, "opensearch", 16, 16, 1, rep, 0, ops=100,
                    wall_s=wall)
    latency_log(tmp_path, "opensearch", 16, 64, 1, 1, 0, ops=100, wall_s=2.0)
    points = client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "opensearch")
    # c=16 medians to wall 1.0 -> 100 ops/s; c=64 gives 50. The slow third
    # repetition must not raise the ceiling, and must not lower it either.
    assert client_ceilings.ops_ceilings(points) == {"16": 100.0}
    assert client_ceilings.ceiling_rungs(points) == {"16": 16}


def test_an_unmeasured_batch_level_gets_no_number(tmp_path):
    latency_log(tmp_path, "opensearch", 16, 16, 1, 1, 0, ops=10, wall_s=1.0)
    points = client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "opensearch")
    ceilings = client_ceilings.ops_ceilings(points)
    assert set(ceilings) == {"16"}
    assert "512" not in ceilings


def test_the_ladder_is_the_cell_that_varies_only_the_worker_count(tmp_path):
    for batch in (16, 512):
        latency_log(tmp_path, "opensearch", batch, 64, 1, 1, 0, ops=100,
                    wall_s=1.0)
    for workers in (2, 4):
        for shard in range(workers):
            latency_log(tmp_path, "opensearch", 512, 64, workers, 101, shard,
                        ops=100, wall_s=1.0)
    ladder = client_ceilings.worker_ladder(
        client_ceilings.collect(tmp_path), "opensearch")
    assert {point.key.batch for point in ladder} == {512}
    assert [point.key.workers for point in ladder] == [1, 2, 4]


def scaling_ladder(tmp_path, rates: dict[int, float], box: dict[int, float],
                   batch: int = 1, thread_cores: float = 0.9) -> None:
    """One ladder point per worker count, hitting `rates` docs/s in aggregate."""
    for workers, rate in rates.items():
        for shard in range(workers):
            latency_log(tmp_path, "scylladb", batch, 64, workers, 101, shard,
                        ops=int(rate / workers), wall_s=1.0, n_docs=1)
        probe_series(tmp_path, "scylladb", batch, 64, workers, 101,
                     process_cores=thread_cores, box_cores=box[workers],
                     busiest=thread_cores)


def ceiling_point(tmp_path, engine="scylladb", batch=1, concurrency=64, rep=1,
                  ops=1000, wall_s=1.0, thread_cores=0.9, box_cores=1.0,
                  n_docs=1) -> None:
    latency_log(tmp_path, engine, batch, concurrency, 1, rep, 0, ops=ops,
                wall_s=wall_s, n_docs=n_docs)
    probe_series(tmp_path, engine, batch, concurrency, 1, rep,
                 process_cores=thread_cores, box_cores=box_cores,
                 busiest=thread_cores)


def test_the_core_bound_is_a_margin_below_a_saturated_thread(tmp_path):
    ceiling_point(tmp_path, thread_cores=0.9)
    outcome = client_ceilings.core_bound(client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert outcome.bound is not None
    assert outcome.bound.saturated_at == 0.9
    assert outcome.bound.fraction == client_ceilings.SATURATION_MARGIN * 0.9
    assert outcome.bound.points == 1


def test_a_thread_measured_above_one_core_cannot_raise_the_bound(tmp_path):
    """Two counters sampled a tick apart can report 1.03 cores for one thread.
    The threshold must not follow that above a whole core."""
    ceiling_point(tmp_path, thread_cores=1.03)
    outcome = client_ceilings.core_bound(client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert outcome.bound.saturated_at == client_ceilings.ONE_CORE
    assert outcome.bound.fraction == client_ceilings.SATURATION_MARGIN


def test_the_core_bound_takes_the_median_of_the_ceiling_points(tmp_path):
    for rep, cores in ((1, 0.4), (2, 0.8), (3, 0.9)):
        ceiling_point(tmp_path, concurrency=16 * rep, rep=rep,
                      thread_cores=cores)
    outcome = client_ceilings.core_bound(client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert outcome.bound.saturated_at == 0.8


def test_no_ceiling_point_with_cpu_data_yields_no_bound_and_says_why(tmp_path):
    latency_log(tmp_path, "scylladb", 1, 64, 1, 1, 0, ops=100, wall_s=1.0,
                n_docs=1)
    outcome = client_ceilings.core_bound(client_ceilings.single_process(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert outcome.bound is None
    assert "generator-probe ticks" in outcome.reason


def test_the_core_bound_does_not_come_from_the_worker_ladder(tmp_path):
    """The ladder's box utilisation is a plausible fraction and a wrong bound:
    an early version handed the gate 0.235, which as a per-thread threshold
    marks an idle loader pinned."""
    ceiling_point(tmp_path, thread_cores=0.95, rep=1, concurrency=16)
    scaling_ladder(tmp_path, {1: 1000, 2: 2000, 4: 2100},
                   {1: 1.0, 2: 2.0, 4: 5.6}, thread_cores=0.95)
    points = client_ceilings.collect(tmp_path)
    outcome = client_ceilings.core_bound(
        client_ceilings.single_process(points, "scylladb"))
    assert outcome.bound.fraction == client_ceilings.SATURATION_MARGIN * 0.95


def test_the_worker_ceiling_is_where_another_process_stopped_helping(tmp_path):
    scaling_ladder(tmp_path, {1: 1000, 2: 2000, 4: 2100},
                   {1: 1.0, 2: 2.0, 4: 5.6})
    ceiling = client_ceilings.worker_ceiling(client_ceilings.worker_ladder(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert ceiling is not None
    assert ceiling.workers == 4
    assert ceiling.is_lower_bound is False
    assert ceiling.box_fraction == 5.6 / 8


def test_a_ladder_that_never_stopped_scaling_gives_a_lower_bound(tmp_path):
    scaling_ladder(tmp_path, {1: 1000, 2: 2000, 4: 4000},
                   {1: 1.0, 2: 2.0, 4: 4.0})
    ceiling = client_ceilings.worker_ceiling(client_ceilings.worker_ladder(
        client_ceilings.collect(tmp_path), "scylladb"))
    assert ceiling.is_lower_bound is True
    assert ceiling.workers == 4


def test_one_worker_count_yields_no_worker_ceiling(tmp_path):
    latency_log(tmp_path, "scylladb", 1, 64, 1, 101, 0, ops=100, wall_s=1.0)
    probe_series(tmp_path, "scylladb", 1, 64, 1, 101, 0.9, 1.0)
    assert client_ceilings.worker_ceiling(client_ceilings.worker_ladder(
        client_ceilings.collect(tmp_path), "scylladb")) is None


def test_n_max_is_not_computed_without_a_stated_engine_ceiling(tmp_path):
    ceiling_point(tmp_path)
    derived = client_ceilings.derive(client_ceilings.collect(tmp_path),
                                     "scylladb", None,
                                     client_ceilings.DEFAULT_HEADROOM)
    assert derived.worker_count_note["n_max"] is None
    assert "ENGINE's ceiling" in derived.worker_count_note[
        "not_computed_because"]


def test_n_max_covers_the_stated_headroom_over_the_engine(tmp_path):
    ceiling_point(tmp_path, ops=1000, wall_s=1.0, n_docs=1)
    derived = client_ceilings.derive(client_ceilings.collect(tmp_path),
                                     "scylladb", 12000.0, 3.0)
    # 1,000 docs/s per process against 3 x 12,000 wanted.
    assert derived.worker_count_note["n_max"] == 36


def test_the_ceilings_file_omits_a_bound_it_could_not_measure(tmp_path):
    latency_log(tmp_path, "opensearch", 16, 16, 1, 1, 0, ops=10, wall_s=1.0)
    out = tmp_path / "ceilings.json"
    assert client_ceilings.main([
        "--data-dir", str(tmp_path), "--engine", "opensearch",
        "--measured-on", "a laptop", "--out", str(out), "--quiet"]) == 0
    document = json.loads(out.read_text())
    assert set(document) == {"engine", "measured_on", "ops_per_s"}
    assert document["measured_on"] == "a laptop"


def test_the_ceilings_file_is_exactly_the_four_gate_keys(tmp_path):
    ceiling_point(tmp_path, thread_cores=0.9)
    out = tmp_path / "ceilings.json"
    provenance = tmp_path / "provenance.json"
    assert client_ceilings.main([
        "--data-dir", str(tmp_path), "--engine", "scylladb",
        "--measured-on", "fts-harness i8g.2xlarge", "--out", str(out),
        "--provenance-out", str(provenance), "--quiet"]) == 0
    document = json.loads(out.read_text())
    assert set(document) == {"engine", "measured_on", "ops_per_s",
                             "loader_core_bound_at"}
    assert document["loader_core_bound_at"] == 0.765
    recorded = json.loads(provenance.read_text())["core_bound"]
    assert recorded["saturated_thread_cores"] == 0.9
    assert recorded["margin_below_saturation"] == 0.85


def test_the_bound_the_gate_reads_marks_a_pinned_thread_and_not_an_idle_one():
    """The one property the number has to have: a saturated thread is above it
    and a waiting thread is below it. A box-utilisation figure is not."""
    bound = client_ceilings.SATURATION_MARGIN * client_ceilings.ONE_CORE
    assert 0.39 < bound < 1.03


def test_provenance_records_the_points_that_had_no_generator_data(tmp_path):
    latency_log(tmp_path, "opensearch", 16, 16, 1, 1, 0, ops=10, wall_s=1.0)
    provenance = tmp_path / "provenance.json"
    client_ceilings.main([
        "--data-dir", str(tmp_path), "--engine", "opensearch",
        "--measured-on", "a laptop", "--out", str(tmp_path / "c.json"),
        "--provenance-out", str(provenance), "--quiet"])
    document = json.loads(provenance.read_text())
    assert document["points_without_generator_data"] == 1
    assert document["points"][0]["generator"] is None
    assert document["core_bound"]["loader_core_bound_at"] is None


def test_an_empty_directory_is_an_error_rather_than_an_empty_ceiling(tmp_path):
    assert client_ceilings.main([
        "--data-dir", str(tmp_path), "--engine", "opensearch",
        "--measured-on", "nowhere", "--out", str(tmp_path / "c.json")]) == 2
    assert not (tmp_path / "c.json").exists()


def test_a_file_that_is_not_a_point_is_ignored(tmp_path):
    latency_log(tmp_path, "opensearch", 16, 16, 1, 1, 0, ops=10, wall_s=1.0)
    (tmp_path / "lat-notapoint.jsonl").write_text("{}\n")
    assert len(client_ceilings.collect(tmp_path)) == 1
