import argparse

import pytest

from ftsbench import resource_probe

MEMORY_STAT = """anon 2974883840
file 545132544
kernel 174755840
kernel_stack 3588096
pagetables 68964352
shmem 0
file_mapped 195829760
inactive_anon 2900000000
active_file 300000000
"""

CPU_STAT = """usage_usec 250358013
user_usec 129958621
system_usec 120399391
nr_throttled 0
"""

IO_STAT = """252:0 rbytes=0 wbytes=1388544 rios=0 wios=339 dbytes=0 dios=0
251:0 rbytes=37519360 wbytes=163860480 rios=9160 wios=40005 dbytes=0 dios=0
"""


def write_cgroup(tmp_path, memory_stat=MEMORY_STAT, cpu_stat=CPU_STAT,
                 io_stat=IO_STAT, memory_max="4294967296"):
    (tmp_path / "memory.stat").write_text(memory_stat, encoding="utf-8")
    (tmp_path / "cpu.stat").write_text(cpu_stat, encoding="utf-8")
    (tmp_path / "io.stat").write_text(io_stat, encoding="utf-8")
    (tmp_path / "memory.max").write_text(memory_max, encoding="utf-8")
    return tmp_path


def test_rss_is_the_anon_figure_not_a_total_that_includes_cache(tmp_path):
    """The whole point of reading memory.stat rather than memory.current: on the
    fixture, cache is 545 MB, so a total would overstate RSS by that much and
    flatter whichever engine touched less disk."""
    counters = resource_probe.read_cgroup_counters(write_cgroup(tmp_path))
    assert counters.rss_bytes == 2974883840
    assert counters.cache_bytes == 545132544
    assert counters.rss_bytes != counters.rss_bytes + counters.cache_bytes


def test_rss_is_not_read_from_memory_current_even_when_present(tmp_path):
    cgroup = write_cgroup(tmp_path)
    (cgroup / "memory.current").write_text("3696680960", encoding="utf-8")
    assert resource_probe.read_cgroup_counters(cgroup).rss_bytes == 2974883840


def test_source_records_which_path_produced_the_number(tmp_path):
    counters = resource_probe.read_cgroup_counters(write_cgroup(tmp_path))
    assert counters.source == "cgroup-anon"


def test_cpu_seconds_total_comes_from_usage_usec(tmp_path):
    counters = resource_probe.read_cgroup_counters(write_cgroup(tmp_path))
    assert counters.cpu_seconds_total == pytest.approx(250.358013)


def test_memory_limit_is_none_when_the_cgroup_is_unlimited(tmp_path):
    cgroup = write_cgroup(tmp_path, memory_max="max")
    assert resource_probe.memory_limit_bytes(cgroup) is None


def test_disk_counters_sum_across_devices(tmp_path):
    counters = resource_probe.read_cgroup_counters(write_cgroup(tmp_path))
    assert counters.disk_read_bytes == 37519360
    assert counters.disk_write_bytes == 1388544 + 163860480


def test_missing_cgroup_files_report_absent_not_zero(tmp_path):
    counters = resource_probe.read_cgroup_counters(tmp_path)
    assert counters.running is False
    assert counters.rss_bytes is None
    assert counters.cpu_seconds_total is None


def test_first_sample_cpu_rate_is_null_not_zero():
    """0.0 on the first tick would draw a container that was saturated at
    start-up as idle, and there is no earlier counter to difference against."""
    tracker = resource_probe.CpuRateTracker()
    assert tracker.rate("fts-bench-scylla", cpu_seconds=100.0, t_elapsed_s=0.0) is None


def test_second_sample_cpu_rate_is_the_derived_core_count():
    tracker = resource_probe.CpuRateTracker()
    tracker.rate("os", cpu_seconds=100.0, t_elapsed_s=0.0)
    assert tracker.rate("os", cpu_seconds=103.0, t_elapsed_s=1.0) == pytest.approx(3.0)


def test_cpu_rate_is_per_container_not_shared():
    tracker = resource_probe.CpuRateTracker()
    tracker.rate("scylla", cpu_seconds=100.0, t_elapsed_s=0.0)
    assert tracker.rate("vector-store", cpu_seconds=5.0, t_elapsed_s=0.0) is None


def test_cpu_rate_is_null_again_after_an_unreadable_tick():
    tracker = resource_probe.CpuRateTracker()
    tracker.rate("os", cpu_seconds=100.0, t_elapsed_s=0.0)
    assert tracker.rate("os", cpu_seconds=None, t_elapsed_s=1.0) is None


def test_in_ram_index_size_is_none_never_zero():
    """A 0 here would read as "ScyllaDB's index is free". Its cost is the
    vector-store's rss_bytes instead."""
    probes = resource_probe.EngineProbes(os_sampler=None, vs_sampler=None)
    for role in ("scylladb", "vector-store"):
        assert probes.index_size_for(role) is None


def test_in_ram_index_size_stays_none_even_with_an_opensearch_sampler_present():
    probes = resource_probe.EngineProbes(os_sampler=FakeOpenSearchSampler(),
                                         vs_sampler=None)
    assert probes.index_size_for("vector-store") is None
    assert probes.index_size_for("opensearch") == 12345


class FakeOpenSearchSampler:
    def sample(self) -> dict:
        return {"store_size_bytes": 12345}

    def version(self) -> str:
        return "3.8.0"


class FailingSampler:
    def sample(self) -> dict:
        raise RuntimeError("connection refused")

    def version(self) -> str:
        return "unknown"


class FakeVectorStoreSampler:
    def sample(self) -> dict:
        return {"docs_indexed": 150000, "docs_searchable": 150000,
                "index_status": "SERVING"}

    def version(self) -> str:
        return "1.10.0"


def test_a_failed_index_size_probe_yields_none_not_zero():
    probes = resource_probe.EngineProbes(os_sampler=FailingSampler(),
                                         vs_sampler=None)
    assert probes.index_size_for("opensearch") is None


def test_vector_store_records_its_doc_count_so_a_stalled_index_is_visible():
    """SIZING.md: vector-store stops adding documents at its memory limit, logs
    an error and keeps serving queries. A count that stops advancing next to an
    rss_bytes pinned at mem_limit_bytes is that failure."""
    probes = resource_probe.EngineProbes(os_sampler=None,
                                         vs_sampler=FakeVectorStoreSampler())
    assert probes.extras_for("vector-store") == {"index_docs": 150000,
                                                 "index_status": "SERVING"}


def test_only_the_vector_store_role_carries_index_docs():
    probes = resource_probe.EngineProbes(os_sampler=None,
                                         vs_sampler=FakeVectorStoreSampler())
    assert probes.extras_for("scylladb") == {}
    assert probes.extras_for("opensearch") == {}


def test_scylladb_runs_must_include_the_vector_store():
    """ScyllaDB FTS runs a ScyllaDB cluster plus a vector-store cluster holding
    the in-RAM index; a C4 bar without it understates the ScyllaDB side."""
    specs = [resource_probe.ContainerSpec("fts-bench-scylla", "scylladb")]
    with pytest.raises(SystemExit):
        resource_probe.require_vector_store("scylladb", specs)


def test_scylladb_runs_pass_when_the_vector_store_is_sampled():
    specs = [resource_probe.ContainerSpec("fts-bench-scylla", "scylladb"),
             resource_probe.ContainerSpec("fts-bench-vector-store", "vector-store")]
    resource_probe.require_vector_store("scylladb", specs)


def test_opensearch_runs_need_no_vector_store():
    specs = [resource_probe.ContainerSpec("fts-bench-opensearch", "opensearch")]
    resource_probe.require_vector_store("opensearch", specs)


def index_size_args(engine: str, os_url: str) -> argparse.Namespace:
    return argparse.Namespace(engine=engine, os_url=os_url)


def test_a_scylladb_run_records_that_there_is_no_on_disk_index_to_size():
    """null must not be readable as "the index is free": the header says the
    Tantivy index is in RAM and its cost is the vector-store's rss_bytes."""
    source = resource_probe.index_size_source(index_size_args("scylladb", ""))
    assert "in RAM" in source
    assert "rss_bytes" in source


def test_an_opensearch_run_without_an_index_endpoint_says_unmeasured():
    """The other reason index_size_bytes can be null. Conflating the two would
    let a forgotten flag look like an engine that needs no index storage."""
    source = resource_probe.index_size_source(index_size_args("opensearch", ""))
    assert "unmeasured" in source


def test_an_opensearch_run_with_an_index_endpoint_names_the_field():
    source = resource_probe.index_size_source(
        index_size_args("opensearch", "http://localhost:9200"))
    assert "store.size_in_bytes" in source


def test_a_missing_index_endpoint_is_warned_about(capsys):
    resource_probe.warn_on_unmeasurable_index_size(index_size_args("opensearch", ""))
    assert "WARNING" in capsys.readouterr().err


def test_a_scylladb_run_is_not_warned_about_a_missing_index_endpoint(capsys):
    resource_probe.warn_on_unmeasurable_index_size(index_size_args("scylladb", ""))
    assert capsys.readouterr().err == ""


def test_container_spec_rejects_an_unknown_role():
    with pytest.raises(Exception):
        resource_probe.parse_container_spec("fts-bench-scylla:database")


def test_container_spec_rejects_a_missing_role():
    with pytest.raises(Exception):
        resource_probe.parse_container_spec("fts-bench-scylla")


def test_container_spec_parses_name_and_role():
    spec = resource_probe.parse_container_spec("fts-bench-vector-store:vector-store")
    assert spec == resource_probe.ContainerSpec("fts-bench-vector-store",
                                                "vector-store")


def test_record_shape_matches_the_schema_contract():
    tick = resource_probe.Tick(i=42, t_elapsed_s=42.0004)
    spec = resource_probe.ContainerSpec("fts-bench-vector-store", "vector-store")
    reading = resource_probe.Reading(
        counters=resource_probe.Counters(running=True, source="cgroup-anon",
                                         rss_bytes=1024, cache_bytes=2048,
                                         cpu_seconds_total=1.5,
                                         disk_read_bytes=1, disk_write_bytes=2),
        cpu_cores_used=None, index_size_bytes=None,
        extras={"index_docs": 10, "index_status": "SERVING"})
    record = resource_probe.build_record(tick, spec, reading)
    assert record["record"] == "resource_sample"
    assert (record["i"], record["container"], record["role"]) == (
        42, "fts-bench-vector-store", "vector-store")
    assert record["t_elapsed_s"] == 42.0
    assert record["index_size_bytes"] is None
    assert record["cpu_cores_used"] is None
    assert record["index_docs"] == 10


def test_docker_stats_fallback_parses_iec_memory_and_si_block_io(monkeypatch):
    """docker mixes unit systems: MEM USAGE is IEC, BLOCK I/O is SI. Reading one
    off the other's table is a silent 7% error."""
    monkeypatch.setattr(resource_probe, "run_docker",
                        lambda argv: "2.971GiB / 30.79GiB|2.45GB / 2.35GB")
    counters = resource_probe.read_docker_stats_counters("sharp_perlman")
    assert counters.rss_bytes == int(2.971 * 1024 ** 3)
    assert counters.disk_read_bytes == 2_450_000_000
    assert counters.disk_write_bytes == 2_350_000_000


def test_docker_stats_fallback_is_labelled_as_not_anon_only(monkeypatch):
    monkeypatch.setattr(resource_probe, "run_docker",
                        lambda argv: "2.971GiB / 30.79GiB|2.45GB / 2.35GB")
    counters = resource_probe.read_docker_stats_counters("sharp_perlman")
    assert counters.source == "docker-stats-memusage"
    assert counters.cpu_seconds_total is None


def test_a_container_that_is_not_running_is_absent_not_zero(monkeypatch):
    monkeypatch.setattr(resource_probe, "run_docker", lambda argv: None)
    counters = resource_probe.read_counters("fts-bench-opensearch",
                                            resource_probe.CgroupLocator())
    assert counters == resource_probe.ABSENT
    assert counters.rss_bytes is None


def test_human_bytes_rejects_an_unknown_unit():
    assert resource_probe.parse_human_bytes("12 parsecs") is None


def test_human_bytes_rejects_a_docker_dash_placeholder():
    assert resource_probe.parse_human_bytes("--") is None


def test_keyed_values_ignores_non_numeric_lines():
    parsed = resource_probe.keyed_values("anon 10\nthrottled x\nfile 20\n")
    assert parsed == {"anon": 10, "file": 20}


def test_cgroup_dir_is_taken_from_the_containers_own_proc_entry(tmp_path,
                                                               monkeypatch):
    proc = tmp_path / "proc" / "999"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text(
        "0::/system.slice/docker-abc123.scope\n", encoding="utf-8")
    monkeypatch.setattr(resource_probe, "CGROUP_ROOT", tmp_path / "cgroup")
    monkeypatch.setattr(resource_probe, "read_cgroup_file",
                        lambda directory, name: (proc / name).read_text())
    resolved = resource_probe.cgroup_dir_for_pid(999)
    assert resolved == tmp_path / "cgroup" / "system.slice" / "docker-abc123.scope"


def test_cgroup_dir_for_a_stopped_container_is_none():
    assert resource_probe.cgroup_dir_for_pid(None) is None


def test_locator_caches_the_resolved_directory(tmp_path, monkeypatch):
    cgroup = write_cgroup(tmp_path)
    calls = []

    def fake_resolve(name: str):
        calls.append(name)
        return cgroup

    monkeypatch.setattr(resource_probe, "resolve_cgroup_dir", fake_resolve)
    locator = resource_probe.CgroupLocator()
    assert locator.locate("fts-bench-scylla") == cgroup
    assert locator.locate("fts-bench-scylla") == cgroup
    assert calls == ["fts-bench-scylla"]


def test_expired_is_false_when_no_duration_was_requested():
    assert resource_probe.expired(0.0, started_s=0.0) is False
