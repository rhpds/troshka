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
) -> None:
    """Stamp pattern topology with captured mon/OSD PatternDisk ids."""
    topology[_PROJECT_CEPH_CAPTURE_KEY] = {
        "monDiskId": mon_disk_id,
        "osdDiskIds": list(osd_disk_ids),
    }


def get_project_ceph_capture(topology: dict) -> dict | None:
    """Return projectCephCapture block or None when absent."""
    capture = topology.get(_PROJECT_CEPH_CAPTURE_KEY)
    if not capture:
        return None
    return {
        "monDiskId": capture["monDiskId"],
        "osdDiskIds": list(capture["osdDiskIds"]),
    }


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
