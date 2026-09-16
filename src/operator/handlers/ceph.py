"""Reconcile TroshkaCeph — project-scoped Rook-Ceph in a box."""

import logging

import kopf
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from helpers.k8s import CRD_GROUP, CRD_VERSION
from helpers.rook_ceph import (
    CEPH_EXTERNAL_SECRET,
    MON_BRIDGE_NAME,
    build_ceph_block_pool,
    build_ceph_cluster,
    build_external_secret,
    build_mon_bridge_deployment,
    build_ceph_rbac,
    ceph_cluster_phase,
    is_ceph_ready,
    validate_lab_ip,
)

logger = logging.getLogger(__name__)


def _ensure_service_account(api, namespace: str) -> None:
    try:
        api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-ceph"),
            ),
        )
    except ApiException as e:
        if e.status != 409:
            raise


def _apply_object(api_fn, body: dict, namespace: str) -> None:
    kind = body.get("kind")
    name = body["metadata"]["name"]
    try:
        if kind == "Secret":
            api_fn(namespace=namespace, body=body)
        elif kind in ("Role", "RoleBinding"):
            api_fn(namespace=namespace, body=body)
        else:
            api_fn(namespace=namespace, body=body)
    except ApiException as e:
        if e.status != 409:
            raise
        logger.info("Already exists: %s/%s", kind, name)


def _apply_rbac(rbac_api, namespace: str, role: dict, binding: dict) -> None:
    for body in (role, binding):
        try:
            if body["kind"] == "Role":
                rbac_api.create_namespaced_role(namespace=namespace, body=body)
            else:
                rbac_api.create_namespaced_role_binding(
                    namespace=namespace, body=body
                )
        except ApiException as e:
            if e.status != 409:
                raise


def _apply_rook_cr(custom_api, body: dict, namespace: str, plural: str) -> None:
    try:
        custom_api.create_namespaced_custom_object(
            group="ceph.rook.io",
            version="v1",
            namespace=namespace,
            plural=plural,
            body=body,
        )
    except ApiException as e:
        if e.status != 409:
            raise


async def _reconcile_ceph(body, patch, namespace: str) -> None:
    spec = body.get("spec", {})
    lab_ip = spec.get("labIp", "")
    if not validate_lab_ip(lab_ip):
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Invalid labIp: {lab_ip!r}"
        return

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()
    rbac_api = client.RbacAuthorizationV1Api()

    _ensure_service_account(core_api, namespace)
    role, binding = build_ceph_rbac(body)
    _apply_rbac(rbac_api, namespace, role, binding)

    _apply_rook_cr(
        custom_api, build_ceph_cluster(body), namespace, "cephclusters"
    )
    _apply_rook_cr(
        custom_api, build_ceph_block_pool(body), namespace, "cephblockpools"
    )

    dep = build_mon_bridge_deployment(body)
    dep_name = dep["metadata"]["name"]
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=dep)
    except ApiException as e:
        if e.status != 409:
            raise
        apps_api.patch_namespaced_deployment(
            name=dep_name, namespace=namespace, body=dep
        )

    phase, fsid = ceph_cluster_phase(custom_api, namespace)
    secret_body = build_external_secret(body, fsid=fsid)
    try:
        core_api.create_namespaced_secret(namespace=namespace, body=secret_body)
    except ApiException as e:
        if e.status != 409:
            raise
        core_api.patch_namespaced_secret(
            name=CEPH_EXTERNAL_SECRET,
            namespace=namespace,
            body={"stringData": secret_body.get("stringData", {})},
        )

    osd_count = spec.get("osdCount", 3)
    replicate_size = spec.get("replicateSize", min(int(osd_count), 3))
    mon_endpoint = f"{lab_ip}:6789"

    patch.status["monEndpoint"] = mon_endpoint
    patch.status["secretName"] = CEPH_EXTERNAL_SECRET
    patch.status["storageClassName"] = spec.get(
        "storageClassName", "troshka-ceph-rbd"
    )
    patch.status["poolName"] = "troshka-ceph-pool"
    patch.status["osdCount"] = osd_count
    patch.status["replicateSize"] = replicate_size

    if is_ceph_ready(phase):
        patch.status["phase"] = "Ready"
        patch.status["message"] = "Ceph cluster ready"
        logger.info("TroshkaCeph ready in %s (mon=%s)", namespace, mon_endpoint)
        return

    patch.status["phase"] = "Progressing"
    patch.status["message"] = f"Waiting for rook CephCluster (phase={phase or 'unknown'})"


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkancephs")
async def ceph_create(body, patch, namespace, name, **_):
    logger.info("Creating TroshkaCeph %s in %s", name, namespace)
    patch.status["phase"] = "Pending"
    await _reconcile_ceph(body, patch, namespace)


@kopf.on.update(CRD_GROUP, CRD_VERSION, "troshkancephs", field="spec")
async def ceph_update(body, patch, namespace, name, **_):
    logger.info("Updating TroshkaCeph %s in %s", name, namespace)
    await _reconcile_ceph(body, patch, namespace)


@kopf.timer(CRD_GROUP, CRD_VERSION, "troshkancephs", interval=30.0, idle=15.0)
async def ceph_poll(body, patch, namespace, name, status, **_):
    current = (status or {}).get("phase", "")
    if current in ("Ready", "Failed"):
        return
    await _reconcile_ceph(body, patch, namespace)


def _delete_ceph_pvcs(core_api, namespace: str) -> None:
    """Remove Rook OSD and Troshka ceph backing PVCs after cluster teardown."""
    from helpers.rook_ceph import EXPORT_JOB_NAME, delete_ceph_storage_pvcs

    delete_ceph_storage_pvcs(core_api, namespace)

    batch_api = client.BatchV1Api()
    try:
        batch_api.delete_namespaced_job(
            name=EXPORT_JOB_NAME,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("Failed to delete ceph export job: %s", e)


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkancephs")
async def ceph_delete(namespace, name, **_):
    logger.info("Deleting TroshkaCeph %s in %s", name, namespace)
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    for dep_name in (MON_BRIDGE_NAME,):
        try:
            apps_api.delete_namespaced_deployment(
                name=dep_name, namespace=namespace
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete deployment %s: %s", dep_name, e)

    for plural, cr_name in (
        ("cephblockpools", "troshka-ceph-pool"),
        ("cephclusters", "troshka-ceph"),
    ):
        try:
            custom_api.delete_namespaced_custom_object(
                group="ceph.rook.io",
                version="v1",
                namespace=namespace,
                plural=plural,
                name=cr_name,
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete %s/%s: %s", plural, cr_name, e)

    _delete_ceph_pvcs(core_api, namespace)

    for secret_name in (CEPH_EXTERNAL_SECRET,):
        try:
            core_api.delete_namespaced_secret(
                name=secret_name, namespace=namespace
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete ceph secret: %s", e)

    for kind, delete_fn, obj_name in (
        ("RoleBinding", rbac_api.delete_namespaced_role_binding, "troshka-ceph"),
        ("Role", rbac_api.delete_namespaced_role, "troshka-ceph"),
    ):
        try:
            delete_fn(name=obj_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete ceph %s: %s", kind, e)

    try:
        core_api.delete_namespaced_service_account(
            name="troshka-ceph", namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            logger.warning("Failed to delete ceph service account: %s", e)
