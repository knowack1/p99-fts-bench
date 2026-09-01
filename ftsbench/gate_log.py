"""Record a gate outcome into a run manifest.

`ftsbench.results_tree` builds each chart's "Gates that passed" section from a
`gates` object on the sidecar or a manifest. Nothing wrote one, so every chart
README said the gates were not machine-recorded — which is exactly the claim a
reviewer cannot check. This is the producer.

Two properties it exists to guarantee:

- **A failed gate is recorded, not just shouted.** The run aborts either way, but
  the aborted repetition leaves a manifest saying which gate failed and what was
  observed. A gate that only prints to a terminal is gone as soon as the terminal
  is, and the surviving artifact then looks like a run that was never checked.
- **Recording is additive and idempotent per name.** Gates fire at different
  points in a repetition, each in its own process, so the manifest is read and
  rewritten per gate and a re-run of the same gate replaces its own entry rather
  than appending a second opinion.

    python3 -m ftsbench.gate_log --manifest data/manifest-opensearch-1.json \\
        --name opensearch_doc_count --status pass \\
        --observed "270269 docs, expected 270269"

Exits 1 when the recorded status is not `pass`, so a caller can record and abort
in one step and cannot record a failure while continuing.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PASS = "pass"
STATUSES = (PASS, "fail", "skipped")
GATES_KEY = "gates"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True,
                        help="run manifest JSON to record the outcome in")
    parser.add_argument("--name", required=True, help="gate name")
    parser.add_argument("--status", required=True, choices=STATUSES)
    parser.add_argument("--observed", default="",
                        help="what the gate actually saw — the part a reviewer "
                             "needs in order to disagree with it")
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    """A missing or unparseable manifest is not a reason to lose the gate.

    Gates run after the manifest is written, but a run that failed early may not
    have one; recording into a fresh document keeps the outcome rather than
    trading it for a traceback.
    """
    if not path.exists():
        return {"record": "run_manifest", "note": "created by gate_log — the "
                "manifest did not exist when this gate ran"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        return {"record": "run_manifest",
                "note": f"unparseable manifest replaced by gate_log: {err}"}


def gate_entry(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "status": args.status,
        "observed": args.observed,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def with_gate(manifest: dict[str, Any], name: str,
              entry: dict[str, Any]) -> dict[str, Any]:
    gates = dict(manifest.get(GATES_KEY) or {})
    gates[name] = entry
    return {**manifest, GATES_KEY: gates}


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    path = Path(args.manifest)
    write_manifest(path, with_gate(load_manifest(path), args.name,
                                   gate_entry(args)))
    print(f"gate {args.name}={args.status} recorded in {path}", file=sys.stderr)
    return 0 if args.status == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
