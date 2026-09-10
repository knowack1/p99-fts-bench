"""The ScyllaDB half: a shard-aware session, a prepared INSERT, and the topology
facts every chart needs carried alongside it.

Shard awareness is left at the driver's defaults on purpose. `scylla-driver`
opens one connection per shard by itself and sizes the pool from
`SCYLLA_NR_SHARDS`; the Cassandra-era `core_connections_per_host` knobs do not
exist in the fork, so there is nothing here to tune and nothing to switch off.
"""
import cassandra
from cassandra import ConsistencyLevel
from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile, Session
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from dataclasses import dataclass

INSERT_TEMPLATE = "INSERT INTO {table} (article_id, page_id, title, body) VALUES (?, ?, ?, ?)"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Topology:
    scylla_version: str
    routing: str
    compression: str
    driver_version: str
    protocol_version: str
    reactor: str
    shard_aware: str
    shards: str
    tablets: str


def consistency_from_name(name: str) -> int:
    try:
        return ConsistencyLevel.name_to_value[name.upper()]
    except KeyError:
        raise ValueError(f"unknown consistency level: {name}") from None


def build_cluster(hosts: list[str], port: int, consistency: int,
                  request_timeout: float, executor_threads: int) -> Cluster:
    """Routing and compression are set explicitly, not left to the defaults.

    A prepared statement carries a routing key, so `TokenAwarePolicy` is what
    turns it into a shard-local write; the driver also warns that an implicit
    policy will become an error. Compression is off because whether `lz4`
    happens to be importable would otherwise silently change the measured rate.
    """
    profile = ExecutionProfile(
        load_balancing_policy=TokenAwarePolicy(DCAwareRoundRobinPolicy()),
        consistency_level=consistency,
        request_timeout=request_timeout)
    return Cluster(contact_points=hosts, port=port,
                   execution_profiles={EXEC_PROFILE_DEFAULT: profile},
                   executor_threads=executor_threads,
                   compression=False)


def connect(cluster: Cluster, keyspace: str) -> Session:
    try:
        return cluster.connect(keyspace)
    except cassandra.InvalidRequest as exc:
        raise SystemExit(
            f"cannot use keyspace {keyspace!r}: {exc}\n"
            "apply bench/scylladb/schema.cql first") from None


def prepare_insert(session: Session, table: str):
    return session.prepare(INSERT_TEMPLATE.format(table=table))


def read_topology(cluster: Cluster, session: Session,
                  keyspace: str, table: str) -> Topology:
    return Topology(
        scylla_version=_scylla_version(session),
        routing=_routing_policy_name(cluster),
        compression=str(cluster.compression),
        driver_version=cassandra.__version__,
        protocol_version=str(cluster.protocol_version),
        reactor=cluster.connection_class.__name__,
        shard_aware=str(cluster.is_shard_aware()),
        shards=_format_shard_stats(cluster),
        tablets=_table_tablet_state(cluster, keyspace, table),
    )


def _routing_policy_name(cluster: Cluster) -> str:
    policy = cluster.profile_manager.default.load_balancing_policy
    child = getattr(policy, "_child_policy", None)
    if child is None:
        return type(policy).__name__
    return f"{type(policy).__name__}({type(child).__name__})"


def _scylla_version(session: Session) -> str:
    row = session.execute("SELECT release_version FROM system.local").one()
    return str(row.release_version) if row else UNKNOWN


def _format_shard_stats(cluster: Cluster) -> str:
    stats = cluster.shard_aware_stats()
    if not stats:
        return "none"
    return ";".join(
        f"{endpoint}=shards:{info['shards_count']},connected:{info['connected']}"
        for endpoint, info in sorted(stats.items()))


def _table_tablet_state(cluster: Cluster, keyspace: str, table: str) -> str:
    tablets = getattr(cluster.metadata, "_tablets", None)
    if tablets is None:
        return UNKNOWN
    return str(tablets.table_has_tablets(keyspace, table))
