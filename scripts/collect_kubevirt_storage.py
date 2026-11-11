"""Standalone read-only native storage evidence collector (no app/DB imports)."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

from app.schemas.kubevirt_storage import (
    CollectionLimits,
    Evidence,
    NativeStorageReport,
    ProjectRoot,
)
from app.services.kubevirt_storage_collector import collect_report
from app.services.kubevirt_storage_inventory import KubernetesReader, discover_inventory
from app.services.kubevirt_storage_transport import (
    CapabilityProfile,
    DirectTransport,
    PoolConfig,
    RbdReader,
    ToolboxConfig,
    ToolboxTransport,
    utcnow,
)
from pydantic import Field


class CacheRoot(Evidence):
    cluster_key: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=256)
    namespace_uid: str = Field(min_length=1, max_length=256)


class Manifest(Evidence):
    collector_id: str = Field(min_length=1, max_length=256)
    ceph_fsid: UUID
    project_roots: list[ProjectRoot] = Field(min_length=1, max_length=10000)
    cache_roots: list[CacheRoot] = Field(default_factory=list, max_length=100)
    pools: list[PoolConfig] = Field(min_length=1, max_length=100)
    capability: CapabilityProfile
    toolbox: ToolboxConfig | None = None
    csi_requests: dict[str, str] = Field(default_factory=dict, max_length=10000)


def atomic_report(path, report):
    """Create private output in the destination directory, then atomically replace."""
    path = Path(path).absolute()
    fd, temporary = tempfile.mkstemp(prefix=".native-storage-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(report.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load(path, limit):
    with open(path, "rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("input exceeds bound")
    return json.loads(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--transport", choices=("direct", "toolbox"), default="direct")
    parser.add_argument("--ceph-conf")
    parser.add_argument("--ceph-keyring")
    parser.add_argument("--output", required=True)
    parser.add_argument("--previous-report")
    for name, default in CollectionLimits().model_dump().items():
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=default)
    args = parser.parse_args(argv)
    scan_started_at = utcnow()
    try:
        manifest = Manifest.model_validate(_load(args.inventory, 4 * 1024 * 1024))
        limits = CollectionLimits(
            **{n: getattr(args, n) for n in CollectionLimits.model_fields}
        )
        retained, retained_cache = [], []
        if args.previous_report:
            previous = NativeStorageReport.model_validate(
                _load(args.previous_report, 64 * 1024 * 1024)
            )
            retained = list(
                {
                    b.model_dump_json(): b.model_copy(
                        update={"provisioned_bytes": None}
                    )
                    for b in previous.volume_bindings + previous.retained_bindings
                }.values()
            )
            retained_cache = previous.cache_bindings
        scan_deadline = time.monotonic() + limits.scan_timeout_seconds
        kube = KubernetesReader(
            args.kubeconfig,
            None,
            collector_id=manifest.collector_id,
            cache_roots=[r.model_dump() for r in manifest.cache_roots],
            scan_deadline=scan_deadline,
            csi_requests=manifest.csi_requests,
            max_objects=limits.max_objects,
        )
        kube.scan_started_at = scan_started_at
        kube.retained_cache_bindings = retained_cache
        if args.transport == "toolbox":
            if manifest.toolbox is None or manifest.toolbox.ceph_fsid != str(
                manifest.ceph_fsid
            ):
                raise ValueError("toolbox enrollment required")
            transport = ToolboxTransport(
                limits,
                manifest.toolbox,
                kube,
                args.kubeconfig,
                scan_deadline=scan_deadline,
            )
        else:
            transport = DirectTransport(
                limits, ceph_conf=args.ceph_conf, ceph_keyring=args.ceph_keyring
            )
            transport.process.scan_deadline = scan_deadline
        reader = RbdReader(
            transport, str(manifest.ceph_fsid), manifest.pools, manifest.capability
        )
        reader.scan_started_at = scan_started_at
        kube.rbd_reader = reader
        inventory = discover_inventory(kube, retained, manifest.project_roots)
        reader.recheck_inventory = lambda: discover_inventory(
            kube, retained, manifest.project_roots
        )
        report = collect_report(inventory, reader, limits=limits, clock=utcnow)
        atomic_report(args.output, report)
    except Exception:  # noqa: BLE001 -- sanitize credential-bearing client exceptions
        # Avoid Kubernetes/CLI exceptions embedding credentials or raw provider data.
        print(
            "Native storage collection failed: invalid configuration or unavailable initialization.",
            file=sys.stderr,
        )
        return 1
    return (
        0
        if not report.collection_errors
        and all(p.coverage == "complete" for p in report.projects)
        and all(p.allocated_bytes is not None for p in report.platform_pools)
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
