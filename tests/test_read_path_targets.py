"""Every read-path producer must offer every arm the machinery supports.

`engines.build_engine` has always been able to dispatch three clients, but
`load_gen`, `sweep` and `query_bench` each wrote their own `--engine`
`choices=` list with only two of them, so the index-only vector-store arm could
never appear in an open-loop chart. Nothing failed and nothing warned — an arm
a producer refuses to accept is indistinguishable from an arm nobody ran.

These tests are the mechanical version of that promise: the flags come from the
registry, so an arm added there is immediately required of every producer.
"""
import importlib

import pytest

from ftsbench import target

READ_PATH_PRODUCERS = ("load_gen", "sweep", "query_bench", "cell_bench")

MINIMAL_ARGV = {
    "load_gen": ("--queries", "q.json", "--rate", "1"),
    "sweep": ("--queries", "q.json", "--output", "o.json"),
    "query_bench": ("--queries", "q.json", "--output", "o.json"),
    "cell_bench": ("--queries", "q.json", "--query-class", "rare_term",
                   "--limit", "10", "--concurrency", "1",
                   "--output", "o.jsonl"),
}


def parse(module_name: str, extra: tuple[str, ...]):
    module = importlib.import_module(f"ftsbench.{module_name}")
    argv = [*MINIMAL_ARGV[module_name], *extra]
    return module, module.parse_args_from(argv) if hasattr(
        module, "parse_args_from") else parse_via_sys_argv(module, argv)


def parse_via_sys_argv(module, argv: list[str]):
    import sys
    saved = sys.argv
    sys.argv = ["prog", *argv]
    try:
        return module.parse_args()
    finally:
        sys.argv = saved


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.flag)
def test_every_producer_accepts_every_arm(producer, arm):
    _, args = parse(producer, (arm.flag,))
    assert target.resolve(args) is arm


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
@pytest.mark.parametrize("arm", target.TARGETS, ids=lambda arm: arm.flag)
def test_every_producer_back_fills_the_engine_for_build_engine(producer, arm):
    """`engines.build_engine` dispatches on `args.engine`; if a knob did not
    back-fill it, selecting an arm would build whichever client the namespace
    happened to carry."""
    _, args = parse(producer, (arm.flag,))
    assert target.resolve_into(args).engine == args.engine


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
def test_no_producer_declares_its_own_engine_choices(producer):
    """The four `choices=` lists were the defect. They are gone, and the only
    `--engine` in the tree is the registry's own legacy alias."""
    module = importlib.import_module(f"ftsbench.{producer}")
    source = open(module.__file__, encoding="utf-8").read()
    assert '"--engine"' not in source


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
@pytest.mark.parametrize("engine,expected", [
    ("opensearch", "opensearch"),
    ("scylladb", "scylla-cdc"),
])
def test_every_producer_still_accepts_the_legacy_engine_flag(producer, engine,
                                                             expected):
    """The Makefile and a dozen shell drivers pass `--engine`; a campaign that
    breaks on the flag it has always used is not an improvement."""
    _, args = parse(producer, ("--engine", engine))
    assert target.resolve(args).config == expected


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
def test_a_producer_refuses_two_arms_at_once(producer):
    with pytest.raises(SystemExit):
        parse(producer, ("--scylladb-cdc", "--opensearch-ram-nostore-refresh3"))


@pytest.mark.parametrize("producer", READ_PATH_PRODUCERS)
def test_a_producer_given_no_arm_refuses_rather_than_defaulting(producer):
    _, args = parse(producer, ())
    with pytest.raises(SystemExit):
        target.resolve(args)
