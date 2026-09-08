"""The generator probe: what the load generator itself cost.

Nothing watched the harness box before this, so every "the client was the
bottleneck" conclusion in the repo was read off the shape of a throughput curve.
These tests pin the cases where the probe would report a plausible WRONG number
rather than fail: a fabricated zero where a null is owed, a comm with spaces
shifting every field of /proc/<tid>/stat, and a core budget taken from the wrong
process.
"""
import json

import pytest

from ftsbench import generator_probe


def make_proc(root, pid, cmdline, threads, statm_pages=1000,
              cpus_allowed="0-7", schedstat=True):
    """A fixture /proc tree. `threads` is a list of (utime, stime, run_ns,
    wait_ns) in clock ticks / nanoseconds."""
    proc = root / str(pid)
    (proc / "task").mkdir(parents=True)
    (proc / "cmdline").write_text(cmdline.replace(" ", "\0"))
    (proc / "statm").write_text(f"2000 {statm_pages} 100 1 0 200 0")
    (proc / "status").write_text(
        f"Name:\tpython3\nThreads:\t{len(threads)}\n"
        f"Cpus_allowed_list:\t{cpus_allowed}\n")
    for offset, (utime, stime, run_ns, wait_ns) in enumerate(threads):
        task = proc / "task" / str(pid + offset)
        task.mkdir()
        # Field 2 is the comm, deliberately containing a space and a paren.
        (task / "stat").write_text(
            f"{pid + offset} (py thr (x)) R 1 1 1 0 -1 0 0 0 0 0 "
            f"{utime} {stime} 0 0 20 0 {len(threads)} 0 0")
        if schedstat:
            (task / "schedstat").write_text(f"{run_ns} {wait_ns} 7\n")
    return proc


@pytest.fixture
def proc_root(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(generator_probe, "PROC_ROOT", root)
    return root


TICKS = generator_probe.CLOCK_TICKS


def test_stat_is_parsed_after_the_last_close_paren(proc_root):
    """Field 2 is the executable name and can contain spaces and parentheses.
    Splitting on whitespace shifts every later field, so CPU would be read out
    of an unrelated column and look plausible."""
    make_proc(proc_root, 100, "python3 -m ftsbench.scylla_load",
              [(int(0.5 * TICKS), int(0.25 * TICKS), 0, 0)])
    counters = generator_probe.read_process(100)
    assert counters.cpu_seconds == pytest.approx(0.75)


def test_a_process_total_sums_its_threads_and_keeps_the_busiest(proc_root):
    """busiest close to the total means one thread is the cap — a single event
    loop; busiest well below means the GIL is round-robining and more PROCESSES
    are the fix. The gate reports both, so both must be right."""
    make_proc(proc_root, 200, "python3 -m ftsbench.opensearch_load",
              [(int(0.8 * TICKS), 0, 0, 0), (int(0.2 * TICKS), 0, 0, 0)])
    counters = generator_probe.read_process(200)
    assert counters.cpu_seconds == pytest.approx(1.0)
    assert counters.busiest_thread_seconds == pytest.approx(0.8)
    assert counters.threads == 2


def test_a_thread_that_vanishes_mid_read_does_not_abort_the_tick(proc_root):
    """An event loop adjusting its executor exits threads routinely; losing the
    whole sample for one of them would leave a hole in the series."""
    proc = make_proc(proc_root, 300, "python3 -m ftsbench.scylla_load",
                     [(TICKS, 0, 0, 0), (TICKS, 0, 0, 0)])
    ghost = proc / "task" / "999"
    ghost.mkdir()
    counters = generator_probe.read_process(300)
    assert counters.threads == 2


def test_a_missing_schedstat_reports_null_not_zero_wait(proc_root):
    """A zero wait reads as 'this loader was never starved', which is a claim.
    CONFIG_SCHEDSTATS can be off, and the gate must decline that clause."""
    make_proc(proc_root, 400, "python3 -m ftsbench.scylla_load",
              [(TICKS, 0, 0, 0)], schedstat=False)
    counters = generator_probe.read_process(400)
    assert counters.run_seconds is None
    assert counters.wait_seconds is None


def test_cores_available_comes_from_the_loaders_cpuset(proc_root):
    """A laptop run is taskset'd onto a subset of the box. Dividing by the whole
    machine would report a pinned loader as comfortably idle."""
    make_proc(proc_root, 500, "python3 -m ftsbench.scylla_load",
              [(TICKS, 0, 0, 0)], cpus_allowed="12-19")
    assert generator_probe.cpus_allowed(500) == tuple(range(12, 20))


@pytest.mark.parametrize("text,expected", [
    ("0-3", (0, 1, 2, 3)),
    ("0,2,4", (0, 2, 4)),
    ("0-1,6-7", (0, 1, 6, 7)),
    ("5", (5,)),
])
def test_the_cpu_list_grammar_covers_ranges_and_commas(text, expected):
    assert generator_probe.parse_cpu_list(text) == expected


def test_the_box_total_covers_only_the_cores_in_the_cpuset(proc_root):
    """Numerator and denominator must describe the same machine, or a run
    pinned to 8 of 22 cores looks 36% as busy as it is."""
    (proc_root / "stat").write_text(
        "cpu  100 0 100 100 0 0 0 0\n"
        "cpu0 100 0 100 0 0 0 0 0\n"
        "cpu1 100 0 100 0 0 0 0 0\n"
        "cpu2 999 0 999 0 0 0 0 0\n")
    busy_two, _ = generator_probe.read_box((0, 1))
    busy_all, _ = generator_probe.read_box((0, 1, 2))
    assert busy_two == pytest.approx(400 / TICKS)
    assert busy_all > busy_two


def test_missing_psi_reports_null_not_zero_pressure(proc_root):
    assert generator_probe.read_pressure() is None


def test_psi_is_read_when_present(proc_root):
    (proc_root / "pressure").mkdir()
    (proc_root / "pressure" / "cpu").write_text(
        "some avg10=1.0 avg60=1.0 avg300=1.0 total=2500000\n")
    assert generator_probe.read_pressure() == pytest.approx(2.5)


def test_the_first_tick_is_null_not_zero():
    """Reporting 0.0 would draw a loader that was pinned from the start as idle
    — the same rule resource_probe states for its own first tick."""
    rates = generator_probe.RateTracker()
    assert rates.rate("p", 10.0, 1.0) is None
    assert rates.rate("p", 11.0, 2.0) == pytest.approx(1.0)


def test_a_restarted_loader_yields_null_rather_than_a_fabricated_dip():
    """A new pid must not be differenced against a process that no longer
    exists, which would invent a negative or enormous rate."""
    rates = generator_probe.RateTracker()
    rates.rate("111", 50.0, 1.0)
    rates.rate("111", 51.0, 2.0)
    rates.forget_missing({"222"})
    assert rates.rate("111", 5.0, 3.0) is None


def test_a_null_counter_clears_the_predecessor_rather_than_carrying_it():
    """Otherwise a gap in an unreadable file would be differenced across, and
    the rate would blame one tick for several seconds of work."""
    rates = generator_probe.RateTracker()
    rates.rate("p", 10.0, 1.0)
    assert rates.rate("p", None, 2.0) is None
    assert rates.rate("p", 12.0, 3.0) is None


def test_the_finder_rescans_so_a_loader_starting_late_is_found(proc_root):
    """c1_run starts the monitor and sleeps before launching the loader, and a
    sharded run starts loaders a second apart. A set frozen at t=0 measures an
    empty box for the whole point."""
    finder = generator_probe.LoaderFinder(["ftsbench.scylla_load"])
    make_proc(proc_root, 600, "python3 -m ftsbench.build_monitor", [(1, 0, 0, 0)])
    assert finder.pids() == []
    make_proc(proc_root, 700, "python3 -m ftsbench.scylla_load", [(1, 0, 0, 0)])
    assert finder.pids() == [700]


def test_a_negatively_cached_pid_is_forgotten_when_it_disappears(proc_root):
    """A recycled pid must not inherit the previous process's verdict."""
    finder = generator_probe.LoaderFinder(["ftsbench.scylla_load"])
    make_proc(proc_root, 800, "python3 -m ftsbench.build_monitor", [(1, 0, 0, 0)])
    finder.pids()
    assert 800 in finder._not_loaders
    for path in sorted((proc_root / "800").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (proc_root / "800").rmdir()
    finder.pids()
    assert 800 not in finder._not_loaders


def test_the_record_is_not_a_resource_sample(proc_root):
    """plotlib.load_runs filters on the record name, which is the only free
    protection against the sites that sum RSS and CPU across a whole file."""
    make_proc(proc_root, 900, "python3 -m ftsbench.scylla_load", [(TICKS, 0, 0, 0)])
    tick = generator_probe.Tick(0, 0.0, 0.0)
    record = generator_probe.loader_record(
        tick, generator_probe.read_process(900), generator_probe.RateTracker())
    assert record["record"] == "generator_sample"
    assert "role" not in record


def test_no_record_carries_a_role_key(proc_root):
    """tools/inline_ab_report.probe_peaks buckets any line carrying `role`,
    whatever its record type."""
    tick = generator_probe.Tick(1, 1.0, 1.0)
    box = generator_probe.box_record(
        tick, generator_probe.BoxCounters(8, "loader-cpus-allowed", 1.0, 0.0,
                                          None, 1, 0),
        generator_probe.RateTracker(), loaders_running=1)
    assert box["record"] == "generator_box_sample"
    assert "role" not in box
    assert generator_probe.departed_record(tick, 5).get("role") is None


def test_a_loader_that_exits_mid_point_still_records_a_tick():
    """A generator that died must be visible, not look like a window in which
    nothing was offered."""
    record = generator_probe.departed_record(generator_probe.Tick(3, 3.0, 3.0), 42)
    assert record["running"] is False
    assert record["cpu_cores_used"] is None
    assert record["pid"] == 42


def test_a_shell_wrapper_that_merely_mentions_the_module_is_not_a_loader(proc_root):
    """The bug this replaced a substring matcher for. `make`'s c1_run is a
    multi-line recipe, so its /bin/sh carries the whole python command line in
    its own argv. A substring test counted that shell as a loader — measured as
    four "loaders" for one real one — which inflates loaders_running, widens the
    gate's window to the wrapper's lifetime, and admits a process with no CPU of
    its own into a verdict that is entirely per-process CPU.
    """
    finder = generator_probe.LoaderFinder(["ftsbench.scylla_load"])
    shell = proc_root / "1000"
    (shell / "task").mkdir(parents=True)
    (shell / "cmdline").write_text(
        "/bin/sh\0-c\0python3 -m ftsbench.scylla_load --corpus x\0")
    make_proc(proc_root, 1100, "python3 -m ftsbench.scylla_load", [(1, 0, 0, 0)])
    assert finder.pids() == [1100]


def test_a_module_named_only_as_an_argument_is_not_a_loader(proc_root):
    """`-m` adjacency, not mere presence: a monitor told which loader to watch
    would otherwise be counted as one."""
    finder = generator_probe.LoaderFinder(["ftsbench.scylla_load"])
    other = proc_root / "1200"
    (other / "task").mkdir(parents=True)
    (other / "cmdline").write_text(
        "python3\0-m\0ftsbench.build_monitor\0--about\0ftsbench.scylla_load\0")
    assert finder.pids() == []
