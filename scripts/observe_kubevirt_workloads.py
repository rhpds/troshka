"""Read-only namespace workload evidence, never settlement or quota release."""

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from app.schemas.kubevirt_storage import Evidence, ProjectRoot, Text
from app.schemas.native_workload_observations import WorkloadObservation
from app.services.native_workload_observations import WorkloadReader, observe_workloads


class Manifest(Evidence):
    collector_id: Text
    root: ProjectRoot


def _write(path, report):
    destination = Path(path).absolute()
    fd, temporary = tempfile.mkstemp(prefix=".native-workloads-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(report.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _collect(args, manifest, started):
    reader = None
    try:
        reader = WorkloadReader(
            args.kubeconfig,
            None,
            collector_id=manifest.collector_id,
            cache_roots=[],
            max_objects=args.max_objects,
            scan_deadline=time.monotonic() + args.timeout_seconds,
        )
        return observe_workloads(
            reader, manifest.root, collector_id=manifest.collector_id, max_objects=args.max_objects
        )
    except Exception:
        return WorkloadObservation(
            collector_id=manifest.collector_id,
            root=manifest.root,
            started_at=started,
            finished_at=datetime.now(UTC),
            inventory_complete=False,
            blockers=["reader_unavailable"],
        )
    finally:
        if reader is not None:
            reader.api.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="JSON collector_id and pinned ProjectRoot")
    parser.add_argument("--kubeconfig", required=True, help="explicit static operator-owned kubeconfig")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-objects", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not 1 <= args.max_objects <= 10000 or not 1 <= args.timeout_seconds <= 300:
        parser.error("bounds: max-objects 1..10000, timeout-seconds 1..300")
    started = datetime.now(UTC)
    try:
        destination = Path(args.output).resolve()
        if destination in {Path(args.root).resolve(), Path(args.kubeconfig).resolve()}:
            raise ValueError("output must not replace input")
        with open(args.root, "rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("manifest bound")
        manifest = Manifest.model_validate(json.loads(data))
        report = _collect(args, manifest, started)
        _write(args.output, report)
        return 0 if report.inventory_complete else 2
    except (ValueError, OSError):
        print("Observation failed: invalid input or output unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
