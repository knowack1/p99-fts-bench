import csv
import json

import pytest

from ftsbench import probe_windows, verify_cpu_usage

STARTED_AT = "2026-09-15T10:00:00+00:00"
BASE_EPOCH = 1789466400.0

SCYLLA = "fts-bench-scylla"
VECTOR_STORE = "fts-bench-vector-store"


def sample(elapsed, container=VECTOR_STORE, role="vector-store",
           cpu=1.5, rss=1024, shmem=0, cache=0, limit=4096,
           source="cgroup-anon", index_docs=None):
    return {
        "record": "resource_sample", "i": int(elapsed),
        "t_elapsed_s": float(elapsed), "container": container, "role": role,
        "running": True, "source": source, "rss_bytes": rss,
        "cache_bytes": cache, "shmem_bytes": shmem, "mem_limit_bytes": limit,
        "cpu_seconds_total": elapsed, "cpu_cores_used": cpu,
        "disk_read_bytes": 0, "disk_write_bytes": 0, "index_size_bytes": None,
        "index_docs": index_docs,
    }


def write_probe(tmp_path, samples, name="r2.jsonl", started_at=STARTED_AT):
    path = tmp_path / name
    header = {"record": "header", "producer": "resource_probe",
              "started_at": started_at}
    with open(path, "w", encoding="utf-8") as out:
        for record in [header, *samples]:
            out.write(json.dumps(record) + "\n")
    return path


def stderr_log(tmp_path, lines, name="r2-low-rep1.stderr.tsv"):
    path = tmp_path / name
    path.write_text("".join(f"{at}\t{text}\n" for at, text in lines),
                    encoding="utf-8")
    return path


def level_lines(offset, concurrency, reset_s=10, build_s=20, position="1/2"):
    start = BASE_EPOCH + offset
    return [
        (start, f"[{position}] concurrency={concurrency}"),
        (start + 1, f"  resetting wiki"),
        (start + reset_s, "  index is SERVING at 0 documents"),
        (start + reset_s + build_s,
         "  -> 600000 docs in 20.00s = 30000.0 docs/s, p99 3 ms, 0 errors"),
    ]


def default_run(tmp_path, samples, lines, memory_read="anon", arm="r2"):
    probe = write_probe(tmp_path, samples)
    log = stderr_log(tmp_path, lines)
    table = tmp_path / "resource-by-rung.csv"
    code = probe_windows.main([
        "--arm", arm, "--probe", str(probe), "--stderr", str(log),
        "--out-dir", str(tmp_path / "probe"), "--table", str(table),
        "--memory-read", memory_read,
    ])
    with open(table, encoding="utf-8") as handle:
        return code, list(csv.DictReader(handle))


def test_window_starts_at_serving_not_at_the_level_announcement(tmp_path):
    """run_sweep announces a level before it opens the inserter, so the
    announce-to-result span carries the keyspace reset. A reset is idle time on
    the indexing container and would drag the median CPU of every rung down."""
    samples = [sample(elapsed, cpu=0.1) for elapsed in range(0, 10)]
    samples += [sample(elapsed, cpu=3.9) for elapsed in range(10, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32))

    assert code == 0
    assert len(rows) == 1
    assert rows[0]["samples"] == "21"
    assert rows[0]["cpu_cores_median"] == "3.9"
    assert rows[0]["reset_s"] == "10.0"
    assert rows[0]["build_s"] == "20.0"


def test_a_level_with_no_serving_line_falls_back_to_the_level_start(tmp_path):
    """--no-reset emits no SERVING line at all, and there the level start is
    the build start rather than a missing window."""
    lines = [(BASE_EPOCH, "[1/1] concurrency=8"),
             (BASE_EPOCH + 5, "  -> 100 docs in 5.00s = 20.0 docs/s, p99 1 ms, 0 errors")]
    samples = [sample(elapsed) for elapsed in range(0, 6)]
    code, rows = default_run(tmp_path, samples, lines)

    assert code == 0
    assert rows[0]["reset_s"] == "0.0"
    assert rows[0]["samples"] == "6"


def test_the_batch_announcement_is_not_mistaken_for_a_result_line(tmp_path):
    """At batch > 1 the level announcement reads '(65536 docs in flight)'. Only
    the result line starts with '->', which is why sweep.rs keeps them apart."""
    lines = [
        (BASE_EPOCH, "[1/1] concurrency=32 batch=512 (16384 docs in flight)"),
        (BASE_EPOCH + 2, "  index is answering at 0 documents"),
        (BASE_EPOCH + 12, "  -> 600000 docs in 10.00s = 60000.0 docs/s, p99 4 ms, 0 errors"),
    ]
    samples = [sample(elapsed) for elapsed in range(0, 13)]
    code, rows = default_run(tmp_path, samples, lines)

    assert code == 0
    assert len(rows) == 1
    assert rows[0]["concurrency"] == "32"
    assert rows[0]["build_s"] == "10.0"


def test_the_index_build_summary_line_does_not_close_a_window(tmp_path):
    """announce_build writes '-> index N docs = …', which has no 'docs in'."""
    lines = level_lines(0, 16)
    lines.insert(3, (BASE_EPOCH + 25,
                     "  -> index 600000 docs = 100.0 docs/s, 0 behind at submit end, settled in 1.0s"))
    samples = [sample(elapsed) for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, lines)

    assert code == 0
    assert len(rows) == 1


def test_every_rung_of_the_ladder_gets_its_own_window(tmp_path):
    lines = level_lines(0, 4) + level_lines(100, 8) + level_lines(200, 16)
    samples = [sample(elapsed) for elapsed in range(0, 231)]
    code, rows = default_run(tmp_path, samples, lines)

    assert code == 0
    assert [row["concurrency"] for row in rows] == ["4", "8", "16"]


def test_slice_filenames_are_what_verify_cpu_usage_parses(tmp_path):
    """The only existing CPU gate globs cpu-*.jsonl and reads config,
    concurrency and rep out of the name; a slice it cannot parse is skipped in
    silence."""
    samples = [sample(elapsed) for elapsed in range(0, 31)]
    default_run(tmp_path, samples, level_lines(0, 32))

    written = sorted(path.name for path in (tmp_path / "probe").iterdir())
    assert written == ["cpu-r2-low-c32-1.jsonl"]
    match = verify_cpu_usage.PROBE_RE.search(written[0])
    assert match is not None
    assert match.group("config") == "r2-low"
    assert match.group("concurrency") == "32"
    assert match.group("rep") == "1"


def test_the_slice_carries_the_header_so_it_reads_as_a_probe_series(tmp_path):
    samples = [sample(elapsed) for elapsed in range(0, 31)]
    default_run(tmp_path, samples, level_lines(0, 32))

    lines = (tmp_path / "probe" / "cpu-r2-low-c32-1.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["record"] == "header"
    assert {record["record"] for record in records[1:]} == {"resource_sample"}


def test_samples_outside_the_window_are_not_in_the_slice(tmp_path):
    samples = [sample(elapsed) for elapsed in range(0, 121)]
    default_run(tmp_path, samples, level_lines(0, 32))

    lines = (tmp_path / "probe" / "cpu-r2-low-c32-1.jsonl").read_text().splitlines()
    assert len(lines) == 22


def test_two_containers_get_one_row_each(tmp_path):
    """The ScyllaDB side is two processes and the campaign reports them split
    and summed, never merged."""
    samples = []
    for elapsed in range(0, 31):
        samples.append(sample(elapsed, container=SCYLLA, role="scylladb", cpu=3.5))
        samples.append(sample(elapsed, cpu=1.9))
    code, rows = default_run(tmp_path, samples, level_lines(0, 32))

    assert code == 0
    assert [row["container"] for row in rows] == [SCYLLA, VECTOR_STORE]
    assert [row["cpu_cores_peak"] for row in rows] == ["3.5", "1.9"]


def test_a_thin_window_is_marked_and_kept(tmp_path):
    """Mirrors the campaign's existing thin-series gate: named, never dropped."""
    lines = [(BASE_EPOCH, "[1/1] concurrency=4"),
             (BASE_EPOCH + 1, "  index is SERVING at 0 documents"),
             (BASE_EPOCH + 3, "  -> 100 docs in 2.00s = 50.0 docs/s, p99 1 ms, 0 errors")]
    samples = [sample(elapsed) for elapsed in range(0, 4)]
    code, rows = default_run(tmp_path, samples, lines)

    assert code == 0
    assert rows[0]["note"] == "thin"
    assert rows[0]["samples"] == "3"


def test_a_window_no_sample_covers_is_named_rather_than_missing(tmp_path):
    samples = [sample(elapsed) for elapsed in range(0, 5)]
    code, rows = default_run(tmp_path, samples, level_lines(500, 64))

    assert code == 0
    assert len(rows) == 1
    assert rows[0]["note"] == "empty"
    assert rows[0]["samples"] == "0"


def test_a_docker_stats_fallback_refuses_the_arm(tmp_path):
    """docker stats MEM USAGE is not anon-only, so it destroys both the
    file-backed and the tmpfs index readings."""
    samples = [sample(elapsed, source="docker-stats-memusage")
               for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32))

    assert code == 1
    assert rows[0]["source"] == "docker-stats-memusage"


def test_memory_read_anon_shmem_counts_the_tmpfs_index(tmp_path):
    """On the ramindex arms the index is tmpfs, which is shmem and not anon."""
    samples = [sample(elapsed, rss=1000, shmem=12000) for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32),
                             memory_read="anon+shmem")

    assert code == 0
    assert rows[0]["rss_peak_bytes"] == "1000"
    assert rows[0]["mem_peak_bytes"] == "13000"
    assert rows[0]["mem_headroom_bytes"] == "-8904"


def test_memory_read_anon_counts_only_anon(tmp_path):
    samples = [sample(elapsed, rss=1000, shmem=12000, cache=500)
               for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32))

    assert rows[0]["mem_peak_bytes"] == "1000"


def test_memory_read_anon_cache_counts_the_file_backed_index(tmp_path):
    samples = [sample(elapsed, rss=1000, cache=9000) for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32),
                             memory_read="anon+cache")

    assert rows[0]["mem_peak_bytes"] == "10000"


def test_memory_read_has_no_default(tmp_path):
    """A default would silently report a 12 GiB tmpfs index as free on four
    of the campaign's nine arms."""
    with pytest.raises(SystemExit):
        probe_windows.parse_args(["--arm", "r2", "--probe", "p", "--stderr", "s",
                                  "--out-dir", "o", "--table", "t"])


def test_the_peak_memory_is_the_peak_of_the_per_tick_sum(tmp_path):
    """Not the sum of the per-field peaks, which adds two maxima that may never
    have coexisted."""
    samples = [sample(0, rss=1000, shmem=10), sample(1, rss=10, shmem=1000),
               sample(2, rss=500, shmem=500), sample(3, rss=1, shmem=1),
               sample(4, rss=2, shmem=2), sample(5, rss=3, shmem=3)]
    lines = [(BASE_EPOCH, "[1/1] concurrency=4"),
             (BASE_EPOCH, "  index is SERVING at 0 documents"),
             (BASE_EPOCH + 5, "  -> 100 docs in 5.00s = 20.0 docs/s, p99 1 ms, 0 errors")]
    code, rows = default_run(tmp_path, samples, lines, memory_read="anon+shmem")

    assert rows[0]["mem_peak_bytes"] == "1010"


def test_the_last_index_doc_count_is_carried(tmp_path):
    """A doc count short of the cap beside an RSS at the budget is the
    vector-store's silent document skipping."""
    samples = [sample(elapsed, index_docs=elapsed * 100)
               for elapsed in range(0, 31)]
    code, rows = default_run(tmp_path, samples, level_lines(0, 32))

    assert rows[0]["index_docs_last"] == "3000"


def test_the_two_sub_sweeps_overlapping_at_32_do_not_collide(tmp_path):
    """Both sweeps number their reps from 1 and both measure c=32, so a slice
    named for the campaign arm alone would be written twice."""
    probe = write_probe(tmp_path, [sample(elapsed) for elapsed in range(0, 331)])
    low = stderr_log(tmp_path, level_lines(0, 32), "r2-low-rep1.stderr.tsv")
    high = stderr_log(tmp_path, level_lines(200, 32), "r2-high-rep1.stderr.tsv")
    table = tmp_path / "t.csv"
    code = probe_windows.main([
        "--arm", "r2", "--probe", str(probe), "--stderr", str(low),
        "--stderr", str(high), "--out-dir", str(tmp_path / "probe"),
        "--table", str(table), "--memory-read", "anon"])

    assert code == 0
    assert sorted(path.name for path in (tmp_path / "probe").iterdir()) == [
        "cpu-r2-high-c32-1.jsonl", "cpu-r2-low-c32-1.jsonl"]


def test_a_repeated_sweep_rung_and_rep_is_refused_rather_than_overwritten(tmp_path):
    probe = write_probe(tmp_path, [sample(elapsed) for elapsed in range(0, 31)])
    log = stderr_log(tmp_path, level_lines(0, 32) + level_lines(100, 32))
    code = probe_windows.main([
        "--arm", "r2", "--probe", str(probe), "--stderr", str(log),
        "--out-dir", str(tmp_path / "probe"), "--table", str(tmp_path / "t.csv"),
        "--memory-read", "anon"])

    assert code == 1


def test_a_stderr_log_that_is_not_run_arm_shaped_is_refused(tmp_path):
    with pytest.raises(ValueError):
        probe_windows.sweep_and_rep("/tmp/whatever.log")


def test_the_rep_comes_from_the_filename(tmp_path):
    assert probe_windows.sweep_and_rep("r4-low-b512-rep3.stderr.tsv") == (
        "r4-low-b512", 3)


def test_a_probe_header_without_started_at_cannot_be_placed_on_a_clock(tmp_path):
    with pytest.raises(ValueError):
        probe_windows.started_epoch({"record": "header"})


def test_no_matching_stderr_log_is_an_error_not_an_empty_table(tmp_path):
    with pytest.raises(ValueError):
        probe_windows.collect_windows([str(tmp_path / "nothing-*.tsv")])
