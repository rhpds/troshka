"""Materialize mon/OSD PVCs for project-Ceph pattern restore (Task 9).

Restore-mode deploy (pattern deploy whose captured topology carries a
``projectCephCapture.restore`` block — see
``docs/dev/project-ceph-pattern-restore.md``, Strategy A) must pre-create the
mon PVC (fixed name ``rook-ceph-mon-a``, Filesystem, tar.gz archive of the mon
rocksdb store) and OSD PVC(s) (Block, qcow2 disk image) from their captured S3
objects *before* the restore-mode ``TroshkaCeph`` CR is created, so Rook
adopts the pre-filled claims instead of provisioning empty new ones.

This reuses the existing CDI S3-import DataVolume mechanism (the same one
that pre-creates golden VM-disk PVCs in ``handlers/project.py``): the mon
device uses CDI's ``contentType: archive`` (untar onto a Filesystem claim)
and the OSD device(s) use the default ``contentType: kubevirt`` import onto a
``volumeMode: Block`` claim, labeled so Rook's ``ceph.rook.io/DeviceSet*``
discovery (``discover_ceph_device_pvcs`` in ``rook_ceph.py``) finds them.
"""

from __future__ import annotations

import asyncio
import logging
import math

from kubernetes.client.exceptions import ApiException

from helpers.kubevirt import s3_import_url
from helpers.rook_ceph import (
    CEPH_DEVICE_SET_NAME,
    CEPH_MON_PVC_NAME,
    CEPH_OSD_TEMPLATE_NAME,
    ROOK_LABEL_DEVICE_SET,
    ROOK_LABEL_DEVICE_SET_PVC_ID,
    ROOK_LABEL_SET_INDEX,
)

logger = logging.getLogger(__name__)

_DV_GROUP = "cdi.kubevirt.io"
_DV_VERSION = "v1beta1"
_DV_PLURAL = "datavolumes"
_GIB = 1073741824
_MIN_MON_RESTORE_GI = 10
_MIN_OSD_RESTORE_GI = 50


def osd_restore_pvc_name(index: int) -> str:
    """Deterministic restore-claim name for an OSD device.

    Rook adopts OSD PVCs by the ``ceph.rook.io/DeviceSet*`` labels, not by
    name (see the Task 0 spike), so this name never needs to match Rook's own
    ``generateName``-based ``osd-set-data-<random>`` convention.
    """
    return f"osd-restore-data-{int(index)}"


def _request_gi(size_bytes: int, minimum_gi: int) -> int:
    """Size the restore claim comfortably above the captured virtual size."""
    size_gi = max(1, -(-int(size_bytes or 0) // _GIB))
    size_gi = max(size_gi, minimum_gi)
    return max(size_gi + 2, math.ceil(size_gi * 1.15))


def device_s3_config(device: dict, s3_config: dict, central_s3_config: dict | None):
    """Return (s3_config, secret_name) for a resolved capture device.

    Mirrors ``handlers/project.py``'s ``_create_golden_pvc_for_disk``: OBC
    (local RGW)-sourced devices use ``s3_config``'s nested ``obcConfig``,
    central (S4)-sourced devices use ``central_s3_config``, and anything else
    falls back to the project's own ``s3Config`` secret.
    """
    source = device.get("source", "central")
    obc_config = s3_config.get("obcConfig") if s3_config else None
    if source == "obc" and obc_config:
        secret = obc_config.get(
            "credentialsSecret", "s3-obc-credentials"  # pragma: allowlist secret
        )
        return obc_config, secret
    if source == "central" and central_s3_config:
        return central_s3_config, "s3-central-credentials"  # pragma: allowlist secret
    return s3_config or {}, "s3-credentials"  # pragma: allowlist secret


def build_mon_restore_datavolume(
    namespace: str, device: dict, s3_config: dict, secret_name: str
) -> dict:
    """CDI DataVolume that untars the captured mon rocksdb archive onto the
    fixed ``rook-ceph-mon-a`` claim (Filesystem, ``contentType: archive``)."""
    s3_url = s3_import_url(device["s3Path"], s3_config)
    size_bytes = device.get("virtualSizeBytes") or device.get("sizeBytes") or 0
    request_gi = _request_gi(size_bytes, _MIN_MON_RESTORE_GI)
    return {
        "apiVersion": f"{_DV_GROUP}/{_DV_VERSION}",
        "kind": "DataVolume",
        "metadata": {
            "name": CEPH_MON_PVC_NAME,
            "namespace": namespace,
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-restore-mon"},
        },
        "spec": {
            "source": {"s3": {"url": s3_url, "secretRef": secret_name}},
            "contentType": "archive",
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{request_gi}Gi"}},
            },
        },
    }


def build_osd_restore_datavolume(
    namespace: str,
    device: dict,
    s3_config: dict,
    secret_name: str,
    storage_class: str,
) -> dict:
    """CDI DataVolume that imports the captured OSD qcow2 block image onto a
    ``volumeMode: Block`` claim labeled for Rook's ``osd-set`` DeviceSet
    discovery (``ceph.rook.io/DeviceSet``/``DeviceSetPVCId``/``setIndex``)."""
    index = int(device["index"])
    name = osd_restore_pvc_name(index)
    s3_url = s3_import_url(device["s3Path"], s3_config)
    size_bytes = device.get("virtualSizeBytes") or device.get("sizeBytes") or 0
    request_gi = _request_gi(size_bytes, _MIN_OSD_RESTORE_GI)
    pvc_id = f"{CEPH_DEVICE_SET_NAME}-{CEPH_OSD_TEMPLATE_NAME}-{index}"
    return {
        "apiVersion": f"{_DV_GROUP}/{_DV_VERSION}",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-ceph",
                "troshka-role": "ceph-restore-osd",
                ROOK_LABEL_DEVICE_SET: CEPH_DEVICE_SET_NAME,
                ROOK_LABEL_DEVICE_SET_PVC_ID: pvc_id,
                ROOK_LABEL_SET_INDEX: str(index),
            },
        },
        "spec": {
            "source": {"s3": {"url": s3_url, "secretRef": secret_name}},
            "pvc": {
                "accessModes": ["ReadWriteOnce"],
                "volumeMode": "Block",
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": f"{request_gi}Gi"}},
            },
        },
    }


def build_ceph_restore_datavolumes(
    namespace: str,
    restore_capture: dict | None,
    s3_config: dict,
    central_s3_config: dict | None = None,
    storage_class: str = "",
) -> tuple[dict | None, list[dict], str, list[str]]:
    """Build the mon + OSD restore DataVolume manifests for a resolved
    ``projectCephCapture.restore`` block.

    ``restore_capture`` is ``{"mon": {...} | None, "osds": [{...}, ...]}``
    (device dicts carrying ``s3Path``/``format``/``sizeBytes``/
    ``virtualSizeBytes``/``source``/``index``), stamped onto the deploy-time
    topology by the backend (PatternDisk ids resolved to S3 paths — the
    operator has no DB access to do this itself).

    Returns ``(mon_dv, osd_dvs, mon_pvc_name, osd_pvc_names)``. All four are
    empty/``None``/``""`` when ``restore_capture`` has no usable devices
    (a fresh-bootstrap deploy with no prior Ceph capture).
    """
    if not restore_capture:
        return None, [], "", []

    mon_device = restore_capture.get("mon")
    osd_devices = restore_capture.get("osds") or []

    mon_dv = None
    mon_name = ""
    if mon_device and mon_device.get("s3Path"):
        cfg, secret = device_s3_config(mon_device, s3_config, central_s3_config)
        mon_dv = build_mon_restore_datavolume(namespace, mon_device, cfg, secret)
        mon_name = CEPH_MON_PVC_NAME

    osd_dvs: list[dict] = []
    osd_names: list[str] = []
    for device in sorted(osd_devices, key=lambda d: int(d.get("index", 0))):
        if not device.get("s3Path"):
            continue
        cfg, secret = device_s3_config(device, s3_config, central_s3_config)
        dv = build_osd_restore_datavolume(namespace, device, cfg, secret, storage_class)
        osd_dvs.append(dv)
        osd_names.append(dv["metadata"]["name"])

    return mon_dv, osd_dvs, mon_name, osd_names


def _create_datavolume(custom_api, namespace: str, dv: dict) -> None:
    name = dv["metadata"]["name"]
    try:
        custom_api.create_namespaced_custom_object(
            group=_DV_GROUP,
            version=_DV_VERSION,
            namespace=namespace,
            plural=_DV_PLURAL,
            body=dv,
        )
        logger.info("Created ceph-restore DataVolume %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("ceph-restore DataVolume %s already exists in %s", name, namespace)


def materialize_ceph_restore_pvcs(
    custom_api,
    namespace: str,
    restore_capture: dict | None,
    s3_config: dict,
    central_s3_config: dict | None = None,
    storage_class: str = "",
) -> tuple[str, list[str]]:
    """Create (idempotently) the mon + OSD restore DataVolumes.

    Returns ``(monPvcName, osdPvcNames)`` — ``("", [])`` when
    ``restore_capture`` has no usable devices. Does not wait for CDI to
    finish importing — call ``wait_for_ceph_restore_datavolumes`` for that
    before creating the restore-mode ``TroshkaCeph`` CR.
    """
    mon_dv, osd_dvs, mon_name, osd_names = build_ceph_restore_datavolumes(
        namespace, restore_capture, s3_config, central_s3_config, storage_class
    )
    if mon_dv:
        _create_datavolume(custom_api, namespace, mon_dv)
    for dv in osd_dvs:
        _create_datavolume(custom_api, namespace, dv)
    return mon_name, osd_names


def _dv_phase(custom_api, namespace: str, name: str) -> str:
    try:
        dv = custom_api.get_namespaced_custom_object(
            group=_DV_GROUP,
            version=_DV_VERSION,
            namespace=namespace,
            plural=_DV_PLURAL,
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return ""
        raise
    return str((dv.get("status") or {}).get("phase") or "")


async def wait_for_ceph_restore_datavolumes(
    custom_api,
    namespace: str,
    names: list[str],
    max_wait_seconds: int = 1800,
    sleep_seconds: int = 5,
) -> None:
    """Poll each restore DataVolume until CDI reports ``Succeeded``.

    Raises ``RuntimeError`` on a terminal ``Failed`` phase or on timeout —
    callers must not create the restore-mode ``TroshkaCeph`` CR against an
    unfinished or failed import (Rook would adopt a not-yet-populated PVC).
    """
    pending = [n for n in names if n]
    if not pending:
        return
    iterations = max(1, max_wait_seconds // sleep_seconds)
    for _ in range(iterations):
        still_pending = []
        for name in pending:
            phase = _dv_phase(custom_api, namespace, name)
            if phase == "Succeeded":
                continue
            if phase == "Failed":
                raise RuntimeError(f"ceph-restore DataVolume {name} failed to import")
            still_pending.append(name)
        pending = still_pending
        if not pending:
            return
        await asyncio.sleep(sleep_seconds)
    raise RuntimeError(
        f"ceph-restore DataVolume(s) not Succeeded after {max_wait_seconds}s: {pending}"
    )
