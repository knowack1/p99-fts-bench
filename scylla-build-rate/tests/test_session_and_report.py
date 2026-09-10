import argparse

import pytest

from scyllarate import cli, report, session
from scyllarate.__main__ import (_collector, _describe, _echo_summary, _exit_code,
                                 _settings, _source_factory)

from .fakes import FakeCluster, FakeSession, FakeTablets, a_point, a_topology


def an_args(**overrides) -> argparse.Namespace:
    defaults = {"corpus": "data/corpus.jsonl", "max_docs": 0,
                "consistency": session.consistency_from_name("LOCAL_ONE"),
                "request_timeout": 10.0, "executor_threads": 2, "out": "-"}
    return argparse.Namespace(**{**defaults, **overrides})


def test_the_prepared_insert_names_the_articles_columns():
    fake = FakeSession()
    assert session.prepare_insert(fake, "articles") == (
        "INSERT INTO articles (article_id, page_id, title, body) VALUES (?, ?, ?, ?)")


def test_the_table_name_reaches_the_statement():
    assert "INTO other_table" in session.prepare_insert(FakeSession(), "other_table")


def test_shard_stats_report_shards_and_connections_per_endpoint():
    cluster = FakeCluster(stats={"127.0.0.1:9042": {"shards_count": 3, "connected": 3}})
    assert session._format_shard_stats(cluster) == "127.0.0.1:9042=shards:3,connected:3"


def test_shard_stats_say_none_when_the_server_is_not_sharded():
    assert session._format_shard_stats(FakeCluster(stats=None)) == "none"


def test_a_partly_connected_pool_is_visible_in_the_stats():
    cluster = FakeCluster(stats={"10.0.0.1:9042": {"shards_count": 8, "connected": 5}})
    assert "shards:8,connected:5" in session._format_shard_stats(cluster)


def test_tablet_state_is_read_from_cluster_metadata():
    cluster = FakeCluster(tablets=FakeTablets(answer=True))
    assert session._table_tablet_state(cluster, "wiki", "articles") == "True"


def test_tablet_state_is_unknown_when_metadata_has_none():
    assert session._table_tablet_state(FakeCluster(), "wiki", "articles") == "unknown"


def test_consistency_names_round_trip():
    assert session.consistency_from_name("local_quorum") == session.consistency_from_name(
        "LOCAL_QUORUM")


def test_an_unknown_consistency_name_is_rejected():
    with pytest.raises(ValueError, match="unknown consistency level"):
        session.consistency_from_name("NEARLY")


def test_the_summary_table_has_a_header_and_one_row_per_point():
    lines = report.summary_table([a_point(8), a_point(16)]).splitlines()
    assert lines[0].split() == ["conc", "docs", "err", "wall_s", "docs/s", "p50_ms", "p99_ms"]
    assert len(lines) == 3


def test_the_summary_row_leads_with_the_concurrency_level():
    assert report.summary_table([a_point(64)]).splitlines()[1].split()[0] == "64"


def test_the_csv_can_be_written_to_a_file(tmp_path):
    destination = tmp_path / "sweep.csv"
    with report.open_csv(str(destination)) as handle:
        report.append_row(handle, a_point(8))
    assert destination.read_text().startswith("8,100,0,")


def test_the_csv_goes_to_stdout_when_asked(capsys):
    with report.open_csv(report.STDOUT) as handle:
        report.append_row(handle, a_point(8))
    assert capsys.readouterr().out.startswith("8,100,0,")


def test_notes_go_to_stderr_not_stdout(capsys):
    report.note("halfway")
    captured = capsys.readouterr()
    assert captured.err.strip() == "halfway" and captured.out == ""


def test_settings_record_the_consistency_by_name():
    assert _settings(an_args())["consistency"] == "LOCAL_ONE"


def test_settings_record_the_corpus_and_the_document_limit():
    recorded = _settings(an_args(corpus="/tmp/small.jsonl", max_docs=5000))
    assert (recorded["corpus"], recorded["max_docs"]) == ("/tmp/small.jsonl", "5000")


def test_the_source_factory_rereads_the_corpus_each_call(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text('{"id": 1, "uuid": "00000000-0000-5000-8000-000000000000",'
                    ' "title": "t", "text": "x"}\n')
    factory = _source_factory(an_args(corpus=str(path)))
    assert len(list(factory())) == len(list(factory())) == 1


def test_describe_puts_the_topology_on_stderr(capsys):
    _describe(a_topology())
    assert "shard_aware=True" in capsys.readouterr().err


def test_a_collected_point_lands_in_the_csv(tmp_path):
    destination = tmp_path / "sweep.csv"
    results: list[report.PointResult] = []
    with report.open_csv(str(destination)) as handle:
        report.write_preamble(handle, a_topology(), {})
        _collector(results, handle)(a_point(8))
    assert "# shard_aware=True" in destination.read_text()
    assert results == [a_point(8)]


def test_the_summary_is_echoed_to_stderr(capsys):
    _echo_summary([a_point(8)])
    assert "p99_ms" in capsys.readouterr().err


def test_exit_code_is_zero_for_a_clean_sweep():
    assert _exit_code([a_point(8)], aborted=False) == 0


def test_the_parser_accepts_a_full_command_line():
    args = cli.build_parser().parse_args(
        ["--corpus", "c.jsonl", "--concurrency", "8,16", "--hosts", "10.0.0.1,10.0.0.2",
         "--port", "19042", "--consistency", "LOCAL_QUORUM", "--executor-threads", "8"])
    assert (args.concurrency, args.hosts, args.port, args.executor_threads) == (
        [8, 16], ["10.0.0.1", "10.0.0.2"], 19042, 8)


def test_connection_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SCYLLA_HOSTS", "192.168.1.5")
    monkeypatch.setenv("SCYLLA_PORT", "19042")
    assert (cli._env_hosts(), cli._env_port()) == ("192.168.1.5", 19042)


def test_connection_defaults_fall_back_to_localhost(monkeypatch):
    monkeypatch.delenv("SCYLLA_HOSTS", raising=False)
    monkeypatch.delenv("SCYLLA_PORT", raising=False)
    assert (cli._env_hosts(), cli._env_port()) == ("127.0.0.1", 9042)


def test_the_parser_defaults_to_the_wiki_articles_table():
    args = cli.build_parser().parse_args(["--corpus", "c.jsonl", "--concurrency", "8"])
    assert (args.keyspace, args.table, args.max_docs, args.out) == (
        "wiki", "articles", 0, "-")


def test_building_a_cluster_pins_the_requested_port_and_thread_pool():
    cluster = session.build_cluster(
        ["127.0.0.1"], 19042, session.consistency_from_name("LOCAL_ONE"), 10.0, 8)
    assert (cluster.port, cluster.executor._max_workers) == (19042, 8)


def test_a_built_cluster_keeps_the_drivers_shard_aware_defaults():
    cluster = session.build_cluster(
        ["127.0.0.1"], 9042, session.consistency_from_name("LOCAL_ONE"), 10.0, 2)
    assert cluster.shard_aware_options.disable is None


def test_routing_is_token_aware_so_a_prepared_write_lands_on_its_shard():
    cluster = session.build_cluster(
        ["127.0.0.1"], 9042, session.consistency_from_name("LOCAL_ONE"), 10.0, 2)
    assert session._routing_policy_name(cluster) == "TokenAwarePolicy(DCAwareRoundRobinPolicy)"


def test_compression_is_off_so_an_optional_library_cannot_shift_the_rate():
    cluster = session.build_cluster(
        ["127.0.0.1"], 9042, session.consistency_from_name("LOCAL_ONE"), 10.0, 2)
    assert cluster.compression is False


def test_a_bare_routing_policy_is_named_without_a_child():
    class Bare:
        pass

    class Manager:
        default = type("P", (), {"load_balancing_policy": Bare()})()

    assert session._routing_policy_name(
        type("C", (), {"profile_manager": Manager()})()) == "Bare"


def test_the_engine_version_comes_from_system_local():
    assert session._scylla_version(FakeSession()) == "2026.3.0-rc2"


def test_a_missing_version_row_reads_as_unknown():
    fake = FakeSession()
    fake.release_version = None
    assert session._scylla_version(fake) == session.UNKNOWN


def test_the_topology_gathers_what_the_csv_header_needs():
    cluster = FakeCluster(stats={"127.0.0.1:9042": {"shards_count": 3, "connected": 3}},
                          tablets=FakeTablets(answer=False))
    topology = session.read_topology(cluster, FakeSession(), "wiki", "articles")
    assert (topology.shard_aware, topology.tablets, topology.protocol_version,
            topology.reactor) == ("True", "False", "5", "LibevConnection")


def test_a_missing_keyspace_points_at_the_schema_file():
    import cassandra

    class RefusingCluster:
        def connect(self, keyspace):
            raise cassandra.InvalidRequest("Keyspace 'wiki' does not exist")

    with pytest.raises(SystemExit, match="schema.cql"):
        session.connect(RefusingCluster(), "wiki")
