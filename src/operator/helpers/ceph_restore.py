"""Materialize mon/OSD PVCs for project-Ceph pattern restore.

Restore-mode deploy (pattern deploy whose captured topology carries a
``projectCephCapture.restore`` block — see
``docs/dev/project-ceph-pattern-restore.md``) must pre-create the mon PVC
(fixed name ``troshka-ceph-mon``, Filesystem, tar.gz of the mon store) and
OSD PVC(s) (``troshka-ceph-osd-{i}``, Block, qcow2) from captured S3 objects
*before* the restore-mode ``TroshkaCeph`` CR is created, so the appliance
adopts the pre-filled claims instead of provisioning empty new ones.

OSD devices reuse CDI S3-import DataVolumes onto ``volumeMode: Block`` claims
labeled ``troshka-role=ceph-osd``.

The mon device is restored via an empty Filesystem PVC + rclone/tar Job
instead of CDI ``contentType: archive``. CDI's archive importer runs as
non-root and fails with ``unlinkat //data: permission denied`` when clearing
the RBD mount root (OpenShift CDI / ODF). The Job mirrors the capture-side
export path (``helpers/patterns.build_ceph_device_export_job``).
"""

from __future__ import annotations

import asyncio
import logging
import math

from kubernetes.client.exceptions import ApiException

from helpers.k8s import TOOLS_IMAGE
from helpers.kubevirt import s3_import_url
from helpers.rook_ceph import CEPH_MON_PVC_NAME

logger = logging.getLogger(__name__)

_DV_GROUP = "cdi.kubevirt.io"
_DV_VERSION = "v1beta1"
_DV_PLURAL = "datavolumes"
_GIB = 1073741824
_MIN_MON_RESTORE_GI = 10
_MIN_OSD_RESTORE_GI = 50
# Filesystem SC for mon rocksdb (OSD virtualization SC is Block-oriented).
DEFAULT_MON_RESTORE_STORAGE_CLASS = "ocs-storagecluster-ceph-rbd"
MON_RESTORE_JOB_NAME = f"restore-{CEPH_MON_PVC_NAME}"


def osd_restore_pvc_name(index: int) -> str:
    """Deterministic restore-claim name matching appliance OSD PVC names."""
    return f"troshka-ceph-osd-{int(index)}"


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


def build_mon_restore_pvc(
    namespace: str, device: dict, storage_class: str = ""
) -> dict:
    """Empty Filesystem PVC that the mon restore Job will populate."""
    size_bytes = device.get("virtualSizeBytes") or device.get("sizeBytes") or 0
    request_gi = _request_gi(size_bytes, _MIN_MON_RESTORE_GI)
    sc = storage_class or DEFAULT_MON_RESTORE_STORAGE_CLASS
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": CEPH_MON_PVC_NAME,
            "namespace": namespace,
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-restore-mon"},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": sc,
            "resources": {"requests": {"storage": f"{request_gi}Gi"}},
        },
    }


def build_mon_restore_job(
    namespace: str, device: dict, s3_config: dict, secret_name: str
) -> dict:
    """Job that rclone-downloads the mon tar.gz and untars onto ``rook-ceph-mon-a``.

    Excludes ``lost+found`` (root-owned on a fresh ext4; present in the capture
    tarball but irrelevant to Rook's mon store).
    """
    s3_path = device["s3Path"]
    s3_bucket = s3_config.get("bucket", "")
    s3_endpoint = s3_config.get("endpoint") or "https://s3.amazonaws.com"
    restore_cmd = (
        "set -e; export HOME=/scratch; "
        "export RCLONE_CONFIG=/scratch/rclone.conf; "
        "cat > $RCLONE_CONFIG <<REOF\n"
        "[target]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $AWS_ACCESS_KEY_ID\n"
        "secret_access_key = $AWS_SECRET_ACCESS_KEY\n"
        f"endpoint = {s3_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "REOF\n"
        f"rclone copyto target:{s3_bucket}/{s3_path} /scratch/mon.tar.gz; "
        # Non-root cannot chmod/utime the PVC mount root (`.`); GNU tar still
        # exits non-zero after a successful extract. Accept success when the
        # mon store directory is present.
        "tar -C /disk -xzf /scratch/mon.tar.gz --exclude=lost+found "
        "--no-same-owner --no-same-permissions -m "
        "|| test -d /disk/ceph-a || test -d /disk/data/store.db; "
        'echo "mon restore complete"'
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": MON_RESTORE_JOB_NAME,
            "namespace": namespace,
            "labels": {"app": "troshka-ceph", "troshka-role": "ceph-restore-mon"},
        },
        "spec": {
            "backoffLimit": 3,
            "activeDeadlineSeconds": 1800,
            "template": {
                "spec": {
                    "serviceAccountName": "troshka-export",
                    "securityContext": {
                        "runAsUser": 107,
                        "runAsGroup": 107,
                        "fsGroup": 107,
                    },
                    "containers": [
                        {
                            "name": "restore",
                            "image": TOOLS_IMAGE,
                            "imagePullPolicy": "Always",
                            "command": ["sh", "-c", restore_cmd],
                            "envFrom": [{"secretRef": {"name": secret_name}}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            "volumeMounts": [
                                {"name": "disk", "mountPath": "/disk"},
                                {"name": "scratch", "mountPath": "/scratch"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "disk",
                            "persistentVolumeClaim": {"claimName": CEPH_MON_PVC_NAME},
                        },
                        {"name": "scratch", "emptyDir": {}},
                    ],
                    "restartPolicy": "Never",
                }
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
    """CDI DataVolume that imports the captured OSD qcow2 onto a Block claim
    named/labeled for the Troshka Ceph appliance OSD Deployment."""
    index = int(device["index"])
    name = osd_restore_pvc_name(index)
    s3_url = s3_import_url(device["s3Path"], s3_config)
    size_bytes = device.get("virtualSizeBytes") or device.get("sizeBytes") or 0
    request_gi = _request_gi(size_bytes, _MIN_OSD_RESTORE_GI)
    return {
        "apiVersion": f"{_DV_GROUP}/{_DV_VERSION}",
        "kind": "DataVolume",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-ceph",
                "troshka-role": "ceph-osd",
                "troshka-ceph-osd-index": str(index),
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


def build_ceph_restore_resources(
    namespace: str,
    restore_capture: dict | None,
    s3_config: dict,
    central_s3_config: dict | None = None,
    storage_class: str = "",
    mon_storage_class: str = "",
) -> tuple[dict | None, dict | None, list[dict], str, list[str]]:
    """Build mon PVC+Job and OSD restore DataVolume manifests.

    Returns ``(mon_pvc, mon_job, osd_dvs, mon_pvc_name, osd_pvc_names)``.
    All empty when ``restore_capture`` has no usable devices.
    """
    if not restore_capture:
        return None, None, [], "", []

    mon_device = restore_capture.get("mon")
    osd_devices = restore_capture.get("osds") or []

    mon_pvc = None
    mon_job = None
    mon_name = ""
    if mon_device and mon_device.get("s3Path"):
        cfg, secret = device_s3_config(mon_device, s3_config, central_s3_config)
        mon_pvc = build_mon_restore_pvc(namespace, mon_device, mon_storage_class)
        mon_job = build_mon_restore_job(namespace, mon_device, cfg, secret)
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

    return mon_pvc, mon_job, osd_dvs, mon_name, osd_names


# Back-compat alias used by older tests / call sites expecting DV-shaped mon.
def build_ceph_restore_datavolumes(
    namespace: str,
    restore_capture: dict | None,
    s3_config: dict,
    central_s3_config: dict | None = None,
    storage_class: str = "",
) -> tuple[dict | None, list[dict], str, list[str]]:
    """Deprecated shape: returns ``(mon_job_or_none, osd_dvs, mon_name, osd_names)``.

    Prefer ``build_ceph_restore_resources``. The first element is the mon Job
    (not a DataVolume) when a mon device is present.
    """
    _mon_pvc, mon_job, osd_dvs, mon_name, osd_names = build_ceph_restore_resources(
        namespace, restore_capture, s3_config, central_s3_config, storage_class
    )
    return mon_job, osd_dvs, mon_name, osd_names


def build_identity_object_manifests(
    namespace: str, identity_objects: list[dict] | None
) -> list[dict]:
    """Build orphan-safe Secret/ConfigMap manifests for the captured identity
    objects (fsid, mon/admin/csi cephx keys, mon-endpoints — see the
    identity-object capture matrix in
    ``docs/dev/project-ceph-pattern-restore.md``).

    Deliberately **no** ``ownerReferences`` — the restore-mode
    ``TroshkaCeph``/``CephCluster`` does not exist yet when these are
    created, matching the spike's orphan-then-recreate pattern. Secret
    ``data`` is used as-is (already base64, captured straight off the wire);
    ConfigMap values were base64-encoded at capture time for uniform JSON
    storage and are decoded back to plain strings here. Returns ``[]`` when
    there is nothing captured (a fresh-bootstrap deploy with no prior Ceph
    capture).
    """
    import base64

    manifests = []
    for obj in identity_objects or []:
        kind = obj.get("kind")
        name = obj.get("name")
        data = obj.get("data") or {}
        if kind not in ("Secret", "ConfigMap") or not name:
            continue
        manifest = {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-ceph",
                    "troshka-role": "ceph-restore-identity",
                },
            },
        }
        if kind == "Secret":
            manifest["data"] = dict(data)
            manifest["type"] = obj.get("type") or "Opaque"
        else:
            manifest["data"] = {
                k: base64.b64decode(v).decode() for k, v in data.items()
            }
        manifests.append(manifest)
    return manifests


def restore_identity_objects(
    core_api, namespace: str, identity_objects: list[dict] | None
) -> None:
    """Idempotently (re)create the captured identity Secrets/ConfigMap in
    the project namespace, before the restore-mode ``TroshkaCeph`` CR is
    created (Strategy A — see docs/dev/project-ceph-pattern-restore.md).
    No-op when ``identity_objects`` is empty (fresh-bootstrap deploy).

    If a Secret already exists with the wrong type (e.g. Opaque from an
    earlier restore bug), delete and recreate so Rook can own
    ``kubernetes.io/rook`` Secrets.
    """
    for manifest in build_identity_object_manifests(namespace, identity_objects):
        kind = manifest["kind"]
        name = manifest["metadata"]["name"]
        try:
            if kind == "Secret":
                _ensure_identity_secret(core_api, namespace, manifest)
            else:
                core_api.create_namespaced_config_map(
                    namespace=namespace, body=manifest
                )
            logger.info("Restored ceph identity %s %s in %s", kind, name, namespace)
        except ApiException as e:
            if e.status != 409:
                raise
            logger.info(
                "ceph identity %s %s already exists in %s", kind, name, namespace
            )


def _ensure_identity_secret(core_api, namespace: str, manifest: dict) -> None:
    """Create identity Secret; replace if present with wrong type."""
    name = manifest["metadata"]["name"]
    want_type = manifest.get("type") or "kubernetes.io/rook"
    try:
        existing = core_api.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        core_api.create_namespaced_secret(namespace=namespace, body=manifest)
        return
    if existing.type == want_type:
        return
    logger.info(
        "Replacing ceph identity Secret %s in %s (type %s -> %s)",
        name,
        namespace,
        existing.type,
        want_type,
    )
    core_api.delete_namespaced_secret(name=name, namespace=namespace)
    core_api.create_namespaced_secret(namespace=namespace, body=manifest)


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


def _delete_stale_mon_datavolume(custom_api, core_api, namespace: str) -> None:
    """Remove a prior CDI archive DV for the mon claim (pre-Job migration).

    Only deletes the PVC when it still looks like a CDI-managed claim (has a
    DataVolume annotation). A Job-populated Bound PVC must be left alone so
    retries do not wipe a finished mon restore.
    """
    try:
        custom_api.delete_namespaced_custom_object(
            group=_DV_GROUP,
            version=_DV_VERSION,
            namespace=namespace,
            plural=_DV_PLURAL,
            name=CEPH_MON_PVC_NAME,
        )
        logger.info(
            "Deleted stale ceph-restore mon DataVolume %s in %s",
            CEPH_MON_PVC_NAME,
            namespace,
        )
    except ApiException as e:
        if e.status != 404:
            raise

    try:
        pvc = core_api.read_namespaced_persistent_volume_claim(
            name=CEPH_MON_PVC_NAME, namespace=namespace
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise
    annotations = (pvc.metadata.annotations or {}) if pvc.metadata else {}
    # CDI-created claims carry this annotation; Job-populated empties do not.
    if "cdi.kubevirt.io/storage.contentType" not in annotations and (
        "cdi.kubevirt.io/createdForDataVolume" not in annotations
    ):
        return
    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=CEPH_MON_PVC_NAME, namespace=namespace
        )
        logger.info(
            "Deleted stale CDI mon PVC %s in %s",
            CEPH_MON_PVC_NAME,
            namespace,
        )
    except ApiException as e:
        if e.status != 404:
            raise


def _create_namespaced_pvc(core_api, namespace: str, pvc: dict) -> None:
    name = pvc["metadata"]["name"]
    try:
        core_api.create_namespaced_persistent_volume_claim(
            namespace=namespace, body=pvc
        )
        logger.info("Created ceph-restore mon PVC %s in %s", name, namespace)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("ceph-restore mon PVC %s already exists in %s", name, namespace)


def _create_namespaced_job(batch_api, namespace: str, job: dict) -> None:
    name = job["metadata"]["name"]
    try:
        batch_api.create_namespaced_job(namespace=namespace, body=job)
        logger.info("Created ceph-restore mon Job %s in %s", name, namespace)
        return
    except ApiException as e:
        if e.status != 409:
            raise
    # Existing Job: leave succeeded Jobs alone; recreate failed/incomplete ones
    # so command fixes (tar soft-fail, endpoint) take effect on retry.
    try:
        existing = batch_api.read_namespaced_job(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            batch_api.create_namespaced_job(namespace=namespace, body=job)
            return
        raise
    if existing.status.succeeded and existing.status.succeeded >= 1:  # type: ignore[union-attr]
        logger.info("ceph-restore mon Job %s already succeeded in %s", name, namespace)
        return
    logger.info("Recreating incomplete ceph-restore mon Job %s in %s", name, namespace)
    try:
        batch_api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body={"propagationPolicy": "Background"},
        )
    except ApiException as del_err:
        if del_err.status != 404:
            raise
    # Brief settle so the name can be reused (Jobs are immutable).
    import time

    for _ in range(20):
        try:
            batch_api.read_namespaced_job(name=name, namespace=namespace)
            time.sleep(0.5)
        except ApiException as e:
            if e.status == 404:
                break
            raise
    batch_api.create_namespaced_job(namespace=namespace, body=job)
    logger.info("Recreated ceph-restore mon Job %s in %s", name, namespace)


def materialize_ceph_restore_pvcs(
    custom_api,
    namespace: str,
    restore_capture: dict | None,
    s3_config: dict,
    central_s3_config: dict | None = None,
    storage_class: str = "",
    core_api=None,
    batch_api=None,
    mon_storage_class: str = "",
) -> tuple[str, list[str]]:
    """Create (idempotently) the mon PVC+Job and OSD restore DataVolumes.

    Returns ``(monPvcName, osdPvcNames)`` — ``("", [])`` when
    ``restore_capture`` has no usable devices. Does not wait for completion —
    call ``wait_for_ceph_restore`` before creating the restore-mode
    ``TroshkaCeph`` CR.

    ``core_api`` / ``batch_api`` are required when a mon device is present
    (PVC + Job path). OSD-only restores only need ``custom_api``.
    """
    mon_pvc, mon_job, osd_dvs, mon_name, osd_names = build_ceph_restore_resources(
        namespace,
        restore_capture,
        s3_config,
        central_s3_config,
        storage_class,
        mon_storage_class,
    )
    if mon_pvc and mon_job:
        if core_api is None or batch_api is None:
            raise ValueError(
                "core_api and batch_api required to materialize mon restore PVC+Job"
            )
        # Drop the pre-migration CDI archive DV if still present so the empty
        # PVC can bind and the Job can populate it.
        _delete_stale_mon_datavolume(custom_api, core_api, namespace)
        _create_namespaced_pvc(core_api, namespace, mon_pvc)
        _create_namespaced_job(batch_api, namespace, mon_job)
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


def _dv_progress_line(custom_api, namespace: str, name: str) -> str:
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
            return f"{name}: waiting"
        raise
    status = dv.get("status") or {}
    phase = str(status.get("phase") or "Pending")
    if phase == "Succeeded":
        return f"{name}: done"
    if phase == "Failed":
        return f"{name}: failed"
    progress = status.get("progress") or ""
    if progress and progress != "N/A":
        return f"{name}: {progress}"
    return f"{name}: {phase}"


def _mon_job_status(batch_api, namespace: str) -> str:
    """Return done / failed / pending for the mon restore Job."""
    try:
        job = batch_api.read_namespaced_job(
            name=MON_RESTORE_JOB_NAME, namespace=namespace
        )
    except ApiException as e:
        if e.status == 404:
            return "pending"
        raise
    if job.status.succeeded and job.status.succeeded >= 1:  # type: ignore[union-attr]
        return "done"
    conditions = getattr(job.status, "conditions", None) or []
    for c in conditions:
        if c.type == "Failed" and c.status == "True":
            return "failed"
    failed = getattr(job.status, "failed", None)
    if failed is not None and failed >= 3:
        return "failed"
    return "pending"


def _ceph_restore_progress_detail(
    custom_api, batch_api, namespace: str, mon_name: str, osd_names: list[str]
) -> str:
    lines: list[str] = []
    if mon_name and batch_api is not None:
        st = _mon_job_status(batch_api, namespace)
        lines.append(f"ceph-mon: {st}")
    for name in osd_names:
        lines.append(_dv_progress_line(custom_api, namespace, name))
    return "\n".join(lines)


async def wait_for_ceph_restore(
    custom_api,
    namespace: str,
    mon_name: str,
    osd_names: list[str],
    batch_api=None,
    max_wait_seconds: int = 1800,
    sleep_seconds: int = 5,
    on_progress=None,
) -> None:
    """Poll mon Job + OSD DataVolumes until all succeed.

    Raises ``RuntimeError`` on a terminal failure or timeout — callers must
    not create the restore-mode ``TroshkaCeph`` CR against unfinished imports.
    """
    pending_osds = [n for n in osd_names if n]
    need_mon = bool(mon_name)
    if not pending_osds and not need_mon:
        return
    if need_mon and batch_api is None:
        raise ValueError("batch_api required to wait for mon restore Job")

    iterations = max(1, max_wait_seconds // sleep_seconds)
    for _ in range(iterations):
        if on_progress:
            try:
                on_progress(
                    _ceph_restore_progress_detail(
                        custom_api, batch_api, namespace, mon_name, osd_names
                    )
                )
            except Exception as e:
                logger.warning("ceph restore progress callback failed: %s", e)

        mon_done = True
        if need_mon:
            mon_st = _mon_job_status(batch_api, namespace)
            if mon_st == "failed":
                raise RuntimeError(
                    f"ceph-restore mon Job {MON_RESTORE_JOB_NAME} failed"
                )
            mon_done = mon_st == "done"

        still_pending = []
        for name in pending_osds:
            phase = _dv_phase(custom_api, namespace, name)
            if phase == "Succeeded":
                continue
            if phase == "Failed":
                raise RuntimeError(f"ceph-restore DataVolume {name} failed to import")
            still_pending.append(name)
        pending_osds = still_pending

        if mon_done and not pending_osds:
            return
        await asyncio.sleep(sleep_seconds)

    pending_desc = list(pending_osds)
    if need_mon and _mon_job_status(batch_api, namespace) != "done":
        pending_desc.insert(0, MON_RESTORE_JOB_NAME)
    raise RuntimeError(
        f"ceph-restore not complete after {max_wait_seconds}s: {pending_desc}"
    )


# Back-compat name used by existing tests / wiring.
async def wait_for_ceph_restore_datavolumes(
    custom_api,
    namespace: str,
    names: list[str],
    max_wait_seconds: int = 1800,
    sleep_seconds: int = 5,
    batch_api=None,
    on_progress=None,
) -> None:
    """Wait for restore resources. ``names`` may include the mon PVC name.

    When the mon PVC name is present, ``batch_api`` must be provided so the
    mon Job can be polled; remaining names are treated as OSD DataVolumes.
    """
    mon_name = ""
    osd_names: list[str] = []
    for n in names:
        if not n:
            continue
        if n == CEPH_MON_PVC_NAME:
            mon_name = n
        else:
            osd_names.append(n)
    await wait_for_ceph_restore(
        custom_api,
        namespace,
        mon_name,
        osd_names,
        batch_api=batch_api,
        max_wait_seconds=max_wait_seconds,
        sleep_seconds=sleep_seconds,
        on_progress=on_progress,
    )
