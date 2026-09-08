"""The registry is the only place a configuration is named.

Three modules used to keep their own list and they disagreed by two entries,
while `vector-store-direct` lived in a bash `case` block and in none of them.
The tests here hold the registry to being complete (nothing names a config it
does not know), unambiguous (no two arms share a spelling), and non-restrictive
(an arm out of the campaign is still runnable — the harness must never lose the
ability to reproduce an artifact it once produced).
"""
import argparse
import re
from pathlib import Path

import pytest

from ftsbench import target

BENCH_DIR = Path(__file__).resolve().parent.parent

# The forms that actually carry a deployment label, rather than every token that
# merely contains an engine's name: a container called `fts-bench-opensearch`
# and a target called `scylla-load` are not configurations.
CONFIG_BEARING_PATTERNS = (
    re.compile(r"^\s*OS_CONFIG\s*\??=\s*(\S+)", re.MULTILINE),
    re.compile(r"^\s*SCYLLA_CONFIG\s*\??=\s*(\S+)", re.MULTILINE),
    re.compile(r"--config\s+'?([A-Za-z][\w-]*)"),
    re.compile(r"^\s*CONFIG=\"?([A-Za-z][\w-]*)", re.MULTILINE),
)


def parser_with_targets(engines: tuple[str, ...] = ()) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    target.add_target_args(parser, engines)
    return parser


def test_every_flag_is_unique():
    assert len(set(target.FLAGS)) == len(target.FLAGS)


def test_every_config_is_unique():
    assert len(set(target.CONFIGS)) == len(target.CONFIGS)


def test_every_target_names_a_known_engine():
    assert {arm.engine for arm in target.TARGETS} <= set(target.ENGINES)


@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.flag)
def test_a_target_round_trips_through_both_lookups(arm):
    assert target.by_flag(arm.flag) is arm
    assert target.by_config(arm.config) is arm


def test_an_unknown_flag_names_the_ones_that_exist():
    """A typo'd knob must not read as "that arm was not measured"."""
    with pytest.raises(KeyError, match="scylladb-cdc"):
        target.by_flag("--scylladb-cdcc")


def test_an_unknown_config_is_refused_not_guessed():
    with pytest.raises(KeyError):
        target.by_config("opensearch-refresh4")


def test_the_knob_wins_over_the_legacy_pair():
    args = argparse.Namespace(target_flag="--vector-store",
                              config="opensearch", engine="opensearch")
    assert target.resolve(args).config == "vector-store-direct"


def test_an_explicit_config_wins_over_a_bare_engine():
    """`--engine opensearch` cannot say which of four deployments it is, so a
    caller that also passes the label must get the label."""
    args = argparse.Namespace(target_flag=None, config="opensearch-ramindex",
                              engine="opensearch")
    assert target.resolve(args).config == "opensearch-ramindex"


@pytest.mark.parametrize("engine,expected", [
    ("opensearch", "opensearch"),
    ("scylladb", "scylla-cdc"),
    ("vector-store", "vector-store-direct"),
])
def test_a_bare_engine_resolves_to_the_arm_its_artifacts_were_labelled(engine, expected):
    args = argparse.Namespace(target_flag=None, config=None, engine=engine)
    assert target.resolve(args).config == expected


def test_selecting_nothing_is_refused_rather_than_defaulted():
    args = argparse.Namespace(target_flag=None, config=None, engine=None)
    with pytest.raises(SystemExit):
        target.resolve(args)


@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.flag)
def test_every_arm_is_selectable_including_the_ones_out_of_the_campaign(arm):
    """`in_campaign` says what the default sweep covers, never what the harness
    can run. A path the harness cannot run is a path whose existing artifacts
    nobody can reproduce."""
    args = parser_with_targets().parse_args([arm.flag])
    assert target.resolve(args) is arm


def test_the_campaign_set_is_a_strict_subset():
    assert set(target.CAMPAIGN_CONFIGS) < set(target.CONFIGS)


@pytest.mark.parametrize("config", ["scylla-bootstrap", "opensearch",
                                    "opensearch-refresh30"])
def test_the_retired_arms_are_out_of_the_campaign_set(config):
    assert config not in target.CAMPAIGN_CONFIGS
    assert config in target.CONFIGS


def test_two_knobs_at_once_are_refused():
    with pytest.raises(SystemExit):
        parser_with_targets().parse_args(["--scylladb-cdc", "--vector-store"])


def test_narrowing_to_one_engine_hides_the_other_arms():
    """freshness_probe has no write path into the vector-store index; offering
    the knob there would promise a measurement that cannot exist."""
    parser = parser_with_targets(engines=("opensearch", "scylladb"))
    with pytest.raises(SystemExit):
        parser.parse_args(["--vector-store"])


def test_narrowing_still_offers_the_engines_it_kept():
    parser = parser_with_targets(engines=("opensearch", "scylladb"))
    assert target.resolve(parser.parse_args(["--scylladb-cdc"])).engine == "scylladb"


@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.flag)
def test_the_header_identifies_the_run_without_its_filename(arm):
    """`config` lived only in the filename, so a copied artifact lost which
    deployment produced it — and a ramindex file was indistinguishable from a
    refresh3 one by content alone."""
    fields = target.header_fields(arm)
    assert fields["config"] == arm.config
    assert fields["target_flag"] == arm.flag
    assert fields["variant"] == arm.variant


@pytest.mark.parametrize("engine", target.ENGINES)
def test_every_engine_has_at_least_one_arm(engine):
    assert target.configs_for_engine(engine)


def config_literals_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found: set[str] = set()
    for pattern in CONFIG_BEARING_PATTERNS:
        found |= {match.split(":")[0] for match in pattern.findall(text)}
    return {name for name in found if not name.startswith("$")}


def config_bearing_files() -> list[Path]:
    return [BENCH_DIR / "Makefile", *sorted((BENCH_DIR / "tools").glob("*.sh"))]


@pytest.mark.parametrize("path", config_bearing_files(), ids=lambda p: p.name)
def test_nothing_names_a_configuration_the_registry_does_not_know(path):
    """Catches the next `vector-store-direct`: a label invented in a shell
    `case` block, never added to any Python list, and therefore invisible to
    the archiver and to every plot glob."""
    unknown = config_literals_in(path) - set(target.CONFIGS)
    assert not unknown, f"{path.name} names unregistered configs: {sorted(unknown)}"


@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.config)
def test_a_configs_repetition_glob_never_matches_another_configs_artifact(arm):
    """`opensearch` is a prefix of `opensearch-refresh3`, so the labels alone
    are ambiguous; what disambiguates them is that a repetition is always a
    number. This is the collision that once folded the refresh=30s repetitions
    into plain OpenSearch and compared a mixture against itself.
    """
    for other in target.CONFIGS:
        if other == arm.config:
            continue
        for chart in ("c1", "c3", "c7"):
            artifact = f"{chart}-{other}-1.jsonl"
            assert not Path(artifact).match(f"{chart}-{arm.config}-[0-9]*.jsonl"), (
                f"{arm.config}'s glob matches {other}'s artifact {artifact}")
