import csv

import pytest

from ftsbench import probe_growth

from .test_probe_windows import (BASE_EPOCH, SCYLLA, VECTOR_STORE,  # noqa: F401
                                 level_lines, sample, stderr_log, write_probe)

SAMPLE_HEADER = "level,concurrency,t_s,docs_submitted,submit_docs_per_s," \
                "docs_indexed,index_docs_per_s,index_status," \
                "docs_accepted,accepted_docs_per_s"


def series_file(tmp_path, sweep="r2-low", rep=1, concurrency=32, rows=(),
                batch=None):
    directory = tmp_path / "samples" / f"{sweep}-rep{rep}"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"c{concurrency}" if batch is None else f"c{concurrency}-b{batch}"
    path = directory / f"{stem}-1.csv"
    body = "\n".join(",".join(str(cell) for cell in row) for row in rows)
    path.write_text(f"# corpus=/tmp/corpus.jsonl\n{SAMPLE_HEADER}\n{body}\n",
                    encoding="utf-8")
    return path


def growth_row(t_s, docs_indexed, rate=0.0, submitted=None):
    return (1, 32, t_s, submitted if submitted is not None else docs_indexed,
            0.0, docs_indexed, rate, "SERVING", "", "")


def run(tmp_path, probe, log, argv_extra=()):
    out = tmp_path / "growth.csv"
    code = probe_growth.main([
        "--arm", "r2", "--probe", str(probe),
        "--stderr", str(log), "--samples-root", str(tmp_path / "samples"),
        "--out", str(out), *argv_extra])
    return code, out


def read(path):
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def one_level(tmp_path, ticks, rows, reset_s=10, build_s=20):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=reset_s,
                                           build_s=build_s))
    probe = write_probe(tmp_path, [sample(elapsed) for elapsed in ticks])
    series_file(tmp_path, rows=rows)
    return probe, log


def test_joins_each_probe_tick_to_the_index_size_it_was_measured_at(tmp_path):
    probe, log = one_level(tmp_path, [10, 11, 12],
                           [growth_row(0.0, 0), growth_row(1.0, 5000),
                            growth_row(2.0, 11000)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert [row["docs_indexed"] for row in read(out)] == ["0", "5000", "11000"]


def test_derives_the_tick_rate_over_the_gap_actually_joined(tmp_path):
    probe, log = one_level(tmp_path, [10, 11, 12],
                           [growth_row(0.0, 0), growth_row(1.0, 5000),
                            growth_row(2.0, 11000)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert [row["tick_docs_per_s"] for row in read(out)] \
        == ["", "5000.0", "6000.0"]


def test_carries_the_harness_own_rate_column_unchanged(tmp_path):
    probe, log = one_level(tmp_path, [10, 11],
                           [growth_row(0.0, 0, rate=0.0),
                            growth_row(1.0, 5000, rate=42.5)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert [row["index_docs_per_s"] for row in read(out)] == ["0.0", "42.5"]


def test_drops_a_tick_whose_nearest_reading_is_past_max_skew(tmp_path):
    probe, log = one_level(tmp_path, [10, 11, 12],
                           [growth_row(0.0, 0), growth_row(1.0, 5000)])

    code, out = run(tmp_path, probe, log, ["--max-skew", "0.5"])

    assert code == 0
    assert len(read(out)) == 2


def test_keeps_a_skewed_tick_when_max_skew_allows_it(tmp_path):
    probe, log = one_level(tmp_path, [10, 11, 12],
                           [growth_row(0.0, 0), growth_row(1.0, 5000)])

    code, out = run(tmp_path, probe, log, ["--max-skew", "2.0"])

    assert code == 0
    assert [row["skew_s"] for row in read(out)] == ["0.0", "0.0", "-1.0"]


def test_a_tick_before_the_build_started_is_outside_the_window(tmp_path):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20))
    probe = write_probe(tmp_path, [sample(elapsed) for elapsed in [2, 10, 11]])
    series_file(tmp_path, rows=[growth_row(0.0, 0), growth_row(1.0, 5000)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert len(read(out)) == 2


def test_each_container_gets_its_own_tick_rate_series(tmp_path):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20))
    probe = write_probe(tmp_path, [
        sample(10, container=VECTOR_STORE), sample(10, container=SCYLLA),
        sample(11, container=VECTOR_STORE), sample(11, container=SCYLLA)])
    series_file(tmp_path, rows=[growth_row(0.0, 0), growth_row(1.0, 5000)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    rates = {(row["container"], row["t_s"]): row["tick_docs_per_s"]
             for row in read(out)}
    assert rates[(VECTOR_STORE, "0.0")] == ""
    assert rates[(SCYLLA, "0.0")] == ""
    assert rates[(VECTOR_STORE, "1.0")] == "5000.0"
    assert rates[(SCYLLA, "1.0")] == "5000.0"


def test_a_blank_index_cell_leaves_the_tick_rate_blank(tmp_path):
    probe, log = one_level(tmp_path, [10, 11, 12], [
        growth_row(0.0, 0),
        (1, 32, 1.0, 5000, 0.0, "", "", "SERVING", "", ""),
        growth_row(2.0, 11000)])

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert [row["tick_docs_per_s"] for row in read(out)] == ["", "", ""]


def test_finds_the_series_of_a_batched_engine_by_concurrency_alone(tmp_path):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20))
    probe = write_probe(tmp_path, [sample(10)])
    series_file(tmp_path, rows=[growth_row(0.0, 4096)], batch=1024)

    code, out = run(tmp_path, probe, log)

    assert code == 0
    assert read(out)[0]["docs_indexed"] == "4096"


def test_two_series_for_one_rung_is_an_error_rather_than_a_guess(tmp_path):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20))
    probe = write_probe(tmp_path, [sample(10)])
    series_file(tmp_path, rows=[growth_row(0.0, 0)])
    series_file(tmp_path, rows=[growth_row(0.0, 0)], batch=1024)

    with pytest.raises(ValueError, match="exactly one series"):
        run(tmp_path, probe, log)


def test_a_missing_series_names_the_path_it_looked_for(tmp_path):
    log = stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20))
    probe = write_probe(tmp_path, [sample(10)])
    (tmp_path / "samples").mkdir()

    with pytest.raises(ValueError, match="r2-low-rep1"):
        run(tmp_path, probe, log)


def test_reports_failure_when_nothing_joined(tmp_path, capsys):
    probe, log = one_level(tmp_path, [10], [growth_row(500.0, 0)])

    code, _ = run(tmp_path, probe, log)

    assert code == 1
    assert "no rows joined" in capsys.readouterr().err


def test_every_rep_lands_in_one_table_tagged_by_rep(tmp_path):
    for rep in (1, 2):
        stderr_log(tmp_path, level_lines(0, 32, reset_s=10, build_s=20),
                   name=f"r2-low-rep{rep}.stderr.tsv")
        series_file(tmp_path, rep=rep, rows=[growth_row(0.0, rep * 100)])
    probe = write_probe(tmp_path, [sample(10)])

    out = tmp_path / "growth.csv"
    code = probe_growth.main([
        "--arm", "r2", "--probe", str(probe),
        "--stderr", str(tmp_path / "r2-low-rep*.stderr.tsv"),
        "--samples-root", str(tmp_path / "samples"), "--out", str(out)])

    assert code == 0
    assert {(row["rep"], row["docs_indexed"]) for row in read(out)} \
        == {("1", "100"), ("2", "200")}
