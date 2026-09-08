"""The one registry of measurable arms: which client to build, and what the
artifact it produces is called.

Three lists used to answer "which configurations exist", and they disagreed:
`run_manifest.CONFIGS` had five names, `archive_artifacts.KNOWN_CONFIGS` six,
`render_results.EVERY_CONFIG` four, and `vector-store-direct` — invented in
`tools/read_sweep.sh` — was in none of them. Separately, four read-path
producers each wrote their own `--engine` `choices=` list and three of them
silently omitted the vector-store arm that `engines.build_engine` had always
been able to dispatch. Both are one defect: a configuration is a fact about the
benchmark, and it was being restated per module.

Two fields carry what a single string used to conflate:

- `engine` selects the CLIENT; `config` names the DEPLOYMENT the run was taken
  against and is the only one of the two that reaches a filename. Conflating
  them is why `opensearch-ramindex` existed as a label with no way to select it.
- `variant` says what differs about that deployment, named for *every* axis it
  flips. `opensearch-ramindex` was never a one-axis change — it moves the
  segment files onto tmpfs, disables `_source`, and sets refresh_interval to
  3s — and naming an arm for whichever axis a reader happens to care about is
  how three labels came to describe the same deployment.

`flag` and `config` differ on purpose where they have to. The knobs are spelled
the way an operator asks for them (`--scylladb-cdc`,
`--opensearch-disk-store-refresh3`); the labels stay as they are because they
are embedded in every artifact filename the AWS campaign has already written,
and renaming a label orphans that data and breaks every plot glob. This module
is the one place the two spellings are tied together.

This module imports nothing from `ftsbench` and nothing third-party, so
`render_results` and `archive_artifacts` can read the registry without dragging
`requests` and `cassandra` into the plotting path.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

OPENSEARCH = "opensearch"
SCYLLADB = "scylladb"
VECTOR_STORE = "vector-store"

ENDPOINT_OPENSEARCH = "opensearch"
ENDPOINT_CQL = "cql"
ENDPOINT_VECTOR_STORE = "vector-store"


@dataclass(frozen=True)
class Target:
    """One measurable arm of the benchmark."""

    flag: str
    config: str
    engine: str
    variant: str
    endpoint: str
    in_campaign: bool = True
    env: tuple[tuple[str, str], ...] = ()


# An empty value in `env` means "explicitly unset". The compose files spell
# every optional knob `VECTOR_STORE_X${VS_X:+=${VS_X}}`, so an empty string
# drops the variable rather than passing it empty, and the service falls back to
# its compiled-in default. That distinction has to be expressible: the whole
# point of the `buf15` arm is that the writer buffer is *absent*, and inheriting
# `.env.sut`'s 376 would silently measure the wrong thing under its label.
UNSET = ""


OS_DISK = (("OS_RAM_INDEX", UNSET), ("OS_INDEX_CONFIG", "index-config.json"))
OS_RAM_NOSTORE = (("OS_RAM_INDEX", "1"),
                  ("OS_INDEX_CONFIG", "index-config-ramindex.json"))
# Held equal across all three ScyllaDB knob arms. The threshold is disabled so
# commits are purely interval-driven; the metrics interval is on so `added/s` —
# documents entering the writer, ungated by commit — is recorded, which is the
# only throughput signal that survives raising the commit interval. It is a
# deviation, so it is constant across the arms rather than set where convenient:
# the 2026-09-07 inline A/B left it on for one arm and off for the other.
VS_COMMON = (("VS_FTS_COMMIT_THRESHOLD", "0"), ("VS_FTS_METRICS_INTERVAL", "1s"))

# `in_campaign` is not a capability gate. Every target here is fully runnable;
# the flag only says whether the default campaign set sweeps it. The
# load-then-index ScyllaDB path, the disk+stored OpenSearch arms and the
# knob-matrix arms below are kept selectable and out of the campaign, because a
# path the harness can no longer run is a path whose old artifacts nobody can
# ever reproduce.
TARGETS: tuple[Target, ...] = (
    Target(flag="--opensearch-disk-store-refresh3",
           config="opensearch-refresh3",
           engine=OPENSEARCH, variant="disk-store-refresh3",
           endpoint=ENDPOINT_OPENSEARCH,
           env=OS_DISK + (("OS_REFRESH", "3s"),)),
    Target(flag="--opensearch-ram-nostore-refresh3",
           config="opensearch-ramindex",
           engine=OPENSEARCH, variant="ram-nostore-refresh3",
           endpoint=ENDPOINT_OPENSEARCH,
           env=OS_RAM_NOSTORE + (("OS_REFRESH", "3s"),)),
    Target(flag="--opensearch-ram-nostore-refresh30",
           config="opensearch-ramindex-refresh30",
           engine=OPENSEARCH, variant="ram-nostore-refresh30",
           endpoint=ENDPOINT_OPENSEARCH, in_campaign=False,
           env=OS_RAM_NOSTORE + (("OS_REFRESH", "30s"),)),
    Target(flag="--opensearch-disk-store-refresh1",
           config="opensearch",
           engine=OPENSEARCH, variant="disk-store-refresh1",
           endpoint=ENDPOINT_OPENSEARCH, in_campaign=False,
           env=OS_DISK + (("OS_REFRESH", "1s"),)),
    Target(flag="--opensearch-disk-store-refresh30",
           config="opensearch-refresh30",
           engine=OPENSEARCH, variant="disk-store-refresh30",
           endpoint=ENDPOINT_OPENSEARCH, in_campaign=False,
           env=OS_DISK + (("OS_REFRESH", "30s"),)),
    # `scylla-cdc` carries no env of its own: it predates the registry holding
    # knobs, and its artifacts were taken with whatever `.env.sut` happened to
    # say at the time — which is why nothing in them records the writer buffer,
    # and why the three explicit arms below exist rather than this label being
    # redefined underneath its own data.
    Target(flag="--scylladb-cdc",
           config="scylla-cdc",
           engine=SCYLLADB, variant="cdc",
           endpoint=ENDPOINT_CQL),
    Target(flag="--scylladb-cdc-buf15",
           config="scylla-cdc-buf15",
           engine=SCYLLADB, variant="cdc-threshold0-buf15-commit3s",
           endpoint=ENDPOINT_CQL, in_campaign=False,
           env=VS_COMMON + (("VS_FTS_WRITER_MEMORY_MB", UNSET),
                            ("VS_FTS_COMMIT_INTERVAL", UNSET))),
    Target(flag="--scylladb-cdc-buf376",
           config="scylla-cdc-buf376",
           engine=SCYLLADB, variant="cdc-threshold0-buf376-commit3s",
           endpoint=ENDPOINT_CQL, in_campaign=False,
           env=VS_COMMON + (("VS_FTS_WRITER_MEMORY_MB", "376"),
                            ("VS_FTS_COMMIT_INTERVAL", UNSET))),
    Target(flag="--scylladb-cdc-buf376-commit30",
           config="scylla-cdc-buf376-commit30",
           engine=SCYLLADB, variant="cdc-threshold0-buf376-commit30s",
           endpoint=ENDPOINT_CQL, in_campaign=False,
           env=VS_COMMON + (("VS_FTS_WRITER_MEMORY_MB", "376"),
                            ("VS_FTS_COMMIT_INTERVAL", "30s"))),
    Target(flag="--scylladb-bootstrap",
           config="scylla-bootstrap",
           engine=SCYLLADB, variant="bootstrap",
           endpoint=ENDPOINT_CQL, in_campaign=False),
    Target(flag="--vector-store",
           config="vector-store-direct",
           engine=VECTOR_STORE, variant="direct",
           endpoint=ENDPOINT_VECTOR_STORE),
)

# What `--engine X` alone means, for the Makefile targets and shell drivers that
# predate the knobs. Ambiguous by nature — one engine has four deployments — so
# it resolves to the arm that engine's artifacts were labelled with before the
# registry existed, and `--config` overrides it.
LEGACY_ENGINE_DEFAULT = {
    OPENSEARCH: "opensearch",
    SCYLLADB: "scylla-cdc",
    VECTOR_STORE: "vector-store-direct",
}

CONFIGS: tuple[str, ...] = tuple(target.config for target in TARGETS)
CAMPAIGN_CONFIGS: tuple[str, ...] = tuple(target.config for target in TARGETS
                                          if target.in_campaign)
FLAGS: tuple[str, ...] = tuple(target.flag for target in TARGETS)
ENGINES: tuple[str, ...] = (OPENSEARCH, SCYLLADB, VECTOR_STORE)


def by_flag(flag: str) -> Target:
    for target in TARGETS:
        if target.flag == flag:
            return target
    raise KeyError(f"no target for flag {flag!r}; known: {', '.join(FLAGS)}")


def by_config(config: str) -> Target:
    for target in TARGETS:
        if target.config == config:
            return target
    raise KeyError(f"no target for config {config!r}; "
                   f"known: {', '.join(CONFIGS)}")


def configs_for_engine(engine: str) -> tuple[str, ...]:
    return tuple(target.config for target in TARGETS if target.engine == engine)


def targets_for_engines(engines: tuple[str, ...]) -> tuple[Target, ...]:
    if not engines:
        return TARGETS
    return tuple(target for target in TARGETS if target.engine in engines)


def add_target_args(parser: argparse.ArgumentParser,
                    engines: tuple[str, ...] = ()) -> None:
    """One knob per arm, mutually exclusive.

    Built by iterating `TARGETS` rather than typed out per producer: the four
    read-path tools each kept their own `choices=` list, and three of them
    omitted the vector-store arm their own machinery already supported. Nobody
    noticed because omitting an arm looks exactly like not having run it.

    `engines` narrows the group for a producer that genuinely cannot serve an
    arm — `freshness_probe` has no write path into the vector-store index, so
    offering it there would promise a measurement that cannot exist.
    """
    group = parser.add_mutually_exclusive_group()
    for target in targets_for_engines(engines):
        group.add_argument(target.flag, dest="target_flag",
                           action="store_const", const=target.flag,
                           help=f"{target.engine}, {target.variant} "
                                f"(artifacts: {target.config})")
    add_legacy_engine_args(parser, engines)


def add_legacy_engine_args(parser: argparse.ArgumentParser,
                           engines: tuple[str, ...] = ()) -> None:
    """`--engine` / `--config`, kept because the Makefile and a dozen shell
    drivers pass them. `--config` is what lets those callers keep naming the
    deployment (`OS_CONFIG`) while `--engine` alone cannot: one engine has four
    of them."""
    choices = engines or ENGINES
    parser.add_argument("--engine", choices=choices, default=None)
    parser.add_argument("--config", choices=CONFIGS, default=None,
                        help="deployment label; overrides --engine's default")


def resolve(args: argparse.Namespace) -> Target:
    """The chosen arm, from a knob or from the legacy pair.

    Refuses rather than guesses when nothing was given: a producer that
    defaulted to one engine would silently measure it under another engine's
    label, which is the failure the registry exists to make impossible.
    """
    flag = getattr(args, "target_flag", None)
    if flag is not None:
        return by_flag(flag)
    config = getattr(args, "config", None)
    if config is not None:
        return by_config(config)
    engine = getattr(args, "engine", None)
    if engine is not None:
        return by_config(LEGACY_ENGINE_DEFAULT[engine])
    raise SystemExit("no target selected; pass one of: " + ", ".join(FLAGS))


def resolve_into(args: argparse.Namespace) -> Target:
    """Resolve the arm and back-fill `args.engine`.

    Back-filling rather than teaching `engines.build_engine` about targets: it
    is the one place a client is chosen, and a second dispatch keyed on
    something else is how the read path would drift apart again.
    """
    arm = resolve(args)
    args.engine = arm.engine
    return arm


def header_fields(target: Target) -> dict[str, str]:
    """What the artifact must carry so the run is identifiable from the file.

    `config` lived only in the filename, so a renamed or copied artifact lost
    which deployment it came from irrecoverably — and an `opensearch-ramindex`
    file was indistinguishable from an `opensearch-refresh3` one by content
    alone. `target_flag` goes in too, so a run is re-invocable from its own
    artifact.
    """
    return {
        "config": target.config,
        "target_flag": target.flag,
        "variant": target.variant,
        "endpoint_kind": target.endpoint,
        "target_env": format_env(target),
    }


def format_env(target: Target) -> str:
    """The arm's knobs as one readable string, for an artifact header.

    This is the *intent*. What the engine actually did is read back off its own
    startup log by `run_manifest`, because an environment variable that the
    image ignores — as public vector-store 1.10.0 ignores every `VS_FTS_*` —
    looks identical here to one that took effect.
    """
    return " ".join(f"{name}={value or '<unset>'}" for name, value in target.env)


def shell_bindings(target: Target) -> str:
    """The arm, as something a shell driver can `eval`.

    The drivers used to answer "which deployment is this" with their own `case`
    block — that is how `vector-store-direct` came to exist in
    `tools/read_sweep.sh` and in no Python list at all. A driver that evals this
    cannot disagree with the registry, because it is not restating it.
    """
    lines = [
        f"TARGET_CONFIG={target.config}",
        f"TARGET_ENGINE={target.engine}",
        f"TARGET_VARIANT={target.variant}",
        f"TARGET_ENDPOINT={target.endpoint}",
        f"TARGET_FLAG={target.flag}",
    ]
    lines += [f"export {name}={value!r}".replace("'", '"')
              for name, value in target.env]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve one measurable arm.")
    add_target_args(parser)
    parser.add_argument("--shell", action="store_true",
                        help="print eval-able bindings for a shell driver")
    args = parser.parse_args()
    arm = resolve(args)
    print(shell_bindings(arm) if args.shell else arm.config)
    return 0


def env_exports(target: Target) -> dict[str, str]:
    """The knobs to put in the environment before the engine stack starts.

    Returned including the empty values: docker compose resolves interpolation
    from the shell environment before `--env-file`, so exporting a knob empty is
    what overrides a value `.env.sut` sets, and omitting it is what inherits it.
    """
    return dict(target.env)


if __name__ == "__main__":
    raise SystemExit(main())
