"""Per-run manifest: what a benchmark run actually did, recorded at run time.

Every C1 run writes one of these next to its time series so the analysis
document is written from records rather than from memory. It captures the
three things that are unrecoverable afterwards: the image tags the stack was
pinned to, the engine versions those images actually reported when running,
and the exact command sequence that produced the series.

Usage:

    python3 -m ftsbench.run_manifest \\
        --output data/manifest-scylla-cdc-1.json \\
        --config scylla-cdc --rep 1 --label "simplewiki 150k, laptop" \\
        --cache-state cold \\
        --commands "make scylla-index" --commands "make scylla-load"

Every live probe is best-effort: a manifest for a run that has already
finished (or crashed) is still worth having, so an unreachable engine is
recorded as "unknown" and never raises.
"""
import argparse
import json
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

PROBE_TIMEOUT_S = 5
# opensearch-refresh3 is the build-rate sweep's parity config: the vector-store
# commits on a 3s interval (vector-store 1.10.0, tantivy.rs COMMIT_INTERVAL), so
# the sweep runs OpenSearch at the same refresh_interval rather than at the 1s
# and 30s the C1-C8 campaign uses.
CONFIGS = ("opensearch", "opensearch-refresh3", "opensearch-refresh30",
           "scylla-bootstrap", "scylla-cdc")
OPENSEARCH_CONFIGS = ("opensearch", "opensearch-refresh3", "opensearch-refresh30")
UNKNOWN = "unknown"
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / "docker" / ".env"
PINNED_IMAGE_KEYS = ("SCYLLA_IMAGE", "VECTOR_STORE_IMAGE", "OPENSEARCH_IMAGE")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="manifest JSON path")
    parser.add_argument("--config", required=True, choices=CONFIGS,
                        help="which of the run configurations this is")
    parser.add_argument("--rep", type=int, default=1,
                        help="repetition number within this configuration")
    parser.add_argument("--label", default="", help="free-form run label")
    parser.add_argument("--cache-state", default="unspecified",
                        help="cold|warm|unspecified — must match the series header")
    parser.add_argument("--command", "--commands", dest="commands",
                        action="append", default=[], metavar="CMD",
                        help="a command this run performed, in order; repeatable")
    parser.add_argument("--corpus", default="", help="corpus path used")
    parser.add_argument("--max-docs", type=int, default=0, help="0 = uncapped")
    parser.add_argument("--series", default="", help="time series this run produced")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                        help="compose env file the image pins are read from")
    parser.add_argument("--os-url", default="http://localhost:9200")
    parser.add_argument("--vs-url", default=default_vs_url())
    parser.add_argument("--scylla-hosts", default="127.0.0.1",
                        help="comma-separated contact points")
    parser.add_argument("--scylla-port", type=int, default=default_scylla_port())
    return parser.parse_args()


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse the subset of env-file syntax docker compose interpolation uses."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def pinned_images(env_file: str | os.PathLike) -> dict[str, str]:
    values = read_env_file(env_file)
    return {key.lower(): values.get(key, UNKNOWN) for key in PINNED_IMAGE_KEYS}


def default_scylla_port() -> int:
    """The host-side port bindings live in docker/.env, and a devcontainer
    holding 9042/6080 is why they were moved off the defaults. Reading them from
    the same file compose reads keeps a bare invocation from probing a port
    nothing is listening on and recording reachable: false for a healthy run."""
    raw = read_env_file(DEFAULT_ENV_FILE).get("SCYLLA_HOST_PORT", "")
    try:
        return int(raw)
    except ValueError:
        return 9042


def default_vs_url() -> str:
    raw = read_env_file(DEFAULT_ENV_FILE).get("VS_HOST_PORT", "")
    try:
        port = int(raw)
    except ValueError:
        port = 6080
    return f"http://localhost:{port}"


def probe_opensearch(url: str) -> dict:
    """Root endpoint. Lucene version is recorded too: it, not the OpenSearch
    version, is what determines the merge behaviour chart C1 shows."""
    try:
        response = requests.get(url.rstrip("/"), timeout=PROBE_TIMEOUT_S)
        response.raise_for_status()
        version = response.json().get("version", {})
        return {
            "reachable": True,
            "version": version.get("number", UNKNOWN),
            "lucene_version": version.get("lucene_version", UNKNOWN),
            "distribution": version.get("distribution", UNKNOWN),
        }
    except Exception as err:
        return {"reachable": False, "version": UNKNOWN,
                "lucene_version": UNKNOWN, "distribution": UNKNOWN,
                "error": str(err)}


def probe_vector_store(url: str) -> dict:
    try:
        response = requests.get(f"{url.rstrip('/')}/api/v1/info",
                                timeout=PROBE_TIMEOUT_S)
        response.raise_for_status()
        body = response.json()
        return {
            "reachable": True,
            "version": str(body.get("version", UNKNOWN)),
            "service": str(body.get("service", UNKNOWN)),
            "engine": str(body.get("engine", UNKNOWN)),
        }
    except Exception as err:
        return {"reachable": False, "version": UNKNOWN, "service": UNKNOWN,
                "engine": UNKNOWN, "error": str(err)}


def probe_scylla(hosts: str, port: int) -> dict:
    """release_version over CQL. The driver import is deliberately inside the
    function: the OpenSearch-only path must work without scylla-driver."""
    try:
        from cassandra.cluster import Cluster
    except Exception as err:
        return {"reachable": False, "version": UNKNOWN,
                "error": f"scylla-driver unavailable: {err}"}

    cluster = None
    try:
        cluster = Cluster(hosts.split(","), port=port)
        session = cluster.connect()
        row = session.execute(
            "SELECT release_version FROM system.local"
        ).one()
        return {"reachable": True,
                "version": getattr(row, "release_version", UNKNOWN) or UNKNOWN}
    except Exception as err:
        return {"reachable": False, "version": UNKNOWN, "error": str(err)}
    finally:
        if cluster is not None:
            try:
                cluster.shutdown()
            except Exception:
                pass


def total_ram_bytes() -> int:
    """0 when the platform does not expose it — treat as unknown, not zero."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def host_facts() -> dict:
    return {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
        "total_ram_bytes": total_ram_bytes(),
    }


def engine_probes(config: str, args: argparse.Namespace) -> dict:
    """Probe only the engines a configuration actually uses, so an absent
    stack does not show up as a failed probe in an unrelated manifest."""
    if config in OPENSEARCH_CONFIGS:
        return {"opensearch": probe_opensearch(args.os_url)}
    return {
        "vector_store": probe_vector_store(args.vs_url),
        "scylladb": probe_scylla(args.scylla_hosts, args.scylla_port),
    }


def build_manifest(args: argparse.Namespace) -> dict:
    return {
        "record": "run_manifest",
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": args.config,
        "repetition": args.rep,
        "label": args.label,
        "cache_state": args.cache_state,
        "corpus": args.corpus,
        "max_docs": args.max_docs,
        "series": args.series,
        "images": pinned_images(args.env_file),
        "engines": engine_probes(args.config, args),
        "commands": list(args.commands),
        "host": host_facts(),
    }


def write_manifest(manifest: dict, output: str) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    manifest = build_manifest(args)
    write_manifest(manifest, args.output)
    versions = ", ".join(
        f"{name}={probe.get('version', UNKNOWN)}"
        for name, probe in manifest["engines"].items()
    )
    print(f"manifest {args.config} rep {args.rep} -> {args.output} ({versions})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
