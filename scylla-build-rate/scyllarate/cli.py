"""Command line: a corpus, a list of concurrency levels, and where the CSV goes."""
import argparse
import os

from .session import consistency_from_name

DEFAULT_HOSTS = "127.0.0.1"
DEFAULT_PORT = 9042
DEFAULT_KEYSPACE = "wiki"
DEFAULT_TABLE = "articles"
DEFAULT_CONSISTENCY = "LOCAL_ONE"
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_EXECUTOR_THREADS = 2


def _concurrency_list(raw: str) -> list[int]:
    levels = [_concurrency_level(part) for part in raw.split(",") if part.strip()]
    if not levels:
        raise argparse.ArgumentTypeError("--concurrency needs at least one level")
    return levels


def _concurrency_level(part: str) -> int:
    try:
        level = int(part)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {part.strip()!r}") from None
    if level < 1:
        raise argparse.ArgumentTypeError(f"concurrency must be >= 1, got {level}")
    return level


def _host_list(raw: str) -> list[str]:
    hosts = [host.strip() for host in raw.split(",") if host.strip()]
    if not hosts:
        raise argparse.ArgumentTypeError("--hosts needs at least one contact point")
    return hosts


def _consistency(raw: str) -> int:
    try:
        return consistency_from_name(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _env_hosts() -> str:
    return os.environ.get("SCYLLA_HOSTS", DEFAULT_HOSTS)


def _env_port() -> int:
    return int(os.environ.get("SCYLLA_PORT", DEFAULT_PORT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scyllarate", description=__doc__)
    _add_workload_args(parser)
    _add_connection_args(parser)
    _add_driver_args(parser)
    parser.add_argument("--out", default="-",
                        help="CSV destination; '-' writes to stdout")
    return parser


def _add_workload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", required=True,
                        help="corpus JSONL: one {id, uuid, title, text} per line")
    parser.add_argument("--concurrency", required=True, type=_concurrency_list,
                        help="comma-separated levels, e.g. 8,16,32,64,128")
    parser.add_argument("--max-docs", type=int, default=0,
                        help="documents per point; 0 loads the whole corpus")


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hosts", type=_host_list, default=_host_list(_env_hosts()),
                        help="comma-separated contact points")
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument("--keyspace", default=DEFAULT_KEYSPACE)
    parser.add_argument("--table", default=DEFAULT_TABLE)


def _add_driver_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--consistency", type=_consistency,
                        default=_consistency(DEFAULT_CONSISTENCY))
    parser.add_argument("--request-timeout", type=float,
                        default=DEFAULT_REQUEST_TIMEOUT_S)
    parser.add_argument("--executor-threads", type=int,
                        default=DEFAULT_EXECUTOR_THREADS,
                        help="driver callback thread pool; raise if the curve flattens")
