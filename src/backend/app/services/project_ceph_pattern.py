"""Topology helpers for project Ceph pattern capture and restore."""

from __future__ import annotations

CEPH_SOURCE_MON = "ceph-mon"
CEPH_SOURCE_OSD = "ceph-osd"

_PROJECT_CEPH_CAPTURE_KEY = "projectCephCapture"
_RUNTIME_PROJECT_CEPH_KEY = "projectCeph"


def strip_runtime_project_ceph(topology: dict) -> None:
    """Remove runtime projectCeph stamp from topology (pattern save)."""
    topology.pop(_RUNTIME_PROJECT_CEPH_KEY, None)


def set_project_ceph_capture(
    topology: dict,
    *,
    mon_disk_id: str,
    osd_disk_ids: list[str],
    identity_objects: list[dict] | None = None,
) -> None:
    """Stamp pattern topology with captured mon/OSD PatternDisk ids plus the
    identity Secrets/ConfigMap Strategy A restore requires (fsid, mon/admin/
    csi cephx keys, mon-endpoints — see the identity-object capture matrix in
    ``docs/dev/project-ceph-pattern-restore.md``). Without these, restoring
    the mon/OSD PVCs alone still makes Rook mint a *new* fsid on CephCluster
    create, and any baked-in Ceph client keyring stops matching.
    """
    topology[_PROJECT_CEPH_CAPTURE_KEY] = {
        "monDiskId": mon_disk_id,
        "osdDiskIds": list(osd_disk_ids),
        "identityObjects": list(identity_objects or []),
    }


def get_project_ceph_capture(topology: dict) -> dict | None:
    """Return projectCephCapture block or None when absent."""
    capture = topology.get(_PROJECT_CEPH_CAPTURE_KEY)
    if not capture:
        return None
    return {
        "monDiskId": capture["monDiskId"],
        "osdDiskIds": list(capture["osdDiskIds"]),
        "identityObjects": list(capture.get("identityObjects") or []),
    }


def set_ceph_restore_devices(
    topology: dict, *, mon: dict | None, osds: list[dict]
) -> None:
    """Stamp resolved S3 restore devices onto ``projectCephCapture.restore``.

    Deploy-time-only mutation of the in-memory project topology sent to the
    operator (never persisted back to ``Pattern.topology`` — mirrors how
    ``resolvedS3Path`` is stamped onto storageNode data for VM disks). The
    operator has no DB access to turn ``monDiskId``/``osdDiskIds`` (PatternDisk
    row ids) into S3 paths itself, so the backend resolves them here before
    the ``TroshkaProject`` CR is created (see Task 9 brief /
    ``docs/dev/project-ceph-pattern-restore.md``).

    ``mon``/each item of ``osds`` carries ``index``/``s3Path``/``format``/
    ``sizeBytes``/``virtualSizeBytes``/``source`` (``"obc"`` or ``"central"``).
    No-ops when ``projectCephCapture`` itself is absent.
    """
    capture = topology.get(_PROJECT_CEPH_CAPTURE_KEY)
    if capture is None:
        return
    capture["restore"] = {"mon": mon, "osds": osds}


def validate_ceph_capture_disk_set(osd_count: int, devices: list[dict]) -> None:
    """Ensure discovered devices match expected mon + OSD counts."""
    mon_count = sum(1 for d in devices if d.get("kind") == CEPH_SOURCE_MON)
    osd_devices = [d for d in devices if d.get("kind") == CEPH_SOURCE_OSD]

    if mon_count != 1:
        raise ValueError(f"expected 1 ceph-mon device, found {mon_count}")

    if len(osd_devices) != osd_count:
        raise ValueError(
            f"expected {osd_count} ceph-osd device(s), found {len(osd_devices)}"
        )
