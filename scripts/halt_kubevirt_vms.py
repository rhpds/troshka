"""Request VM/controller stop or bare Pod deletion; default server dry-run.

Never proves physical shutdown, fences writers, or releases quota.
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "backend"))
sys.path.insert(0, str(REPO / "src" / "operator"))

from app.schemas.native_vm_halt import HaltManifest
from app.services.native_vm_halt import HaltJournal, halt_vms
from app.services.native_vm_halt_transport import HaltClient


def _manifest(args):
    destination = Path(args.output).resolve()
    if destination in {Path(args.manifest).resolve(), Path(args.kubeconfig).resolve()}:
        raise ValueError("output aliases input")
    with open(args.manifest, "rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("manifest limit")
    return HaltManifest.model_validate(json.loads(data))


def _run(args, manifest, journal):
    client = None
    try:
        client = HaltClient(
            args.kubeconfig,
            None,
            collector_id=manifest.collector_id,
            cache_roots=[],
            max_objects=args.max_objects,
            scan_deadline=time.monotonic() + args.timeout_seconds,
        )
        report = halt_vms(client, manifest, journal, apply=args.apply)
        return 0 if report["status"] == "complete" else 2
    finally:
        if client is not None:
            client.api.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument(
        "--output", required=True, help="new private JSONL journal; never reused"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="send writes; requires independently verified writer maintenance",
    )
    parser.add_argument("--max-objects", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not 1 <= args.max_objects <= 10000 or not 1 <= args.timeout_seconds <= 300:
        parser.error("bounds: max-objects 1..10000, timeout-seconds 1..300")
    try:
        manifest = _manifest(args)
        with HaltJournal(args.output) as journal:
            return _run(args, manifest, journal)
    except Exception:
        print(
            "Halt command incomplete. Inspect the journal; do not redispatch an unresolved attempt.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
