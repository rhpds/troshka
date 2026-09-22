"""Reconcile TroshkaCeph — project-scoped Ceph appliance (pods, no Rook)."""

import logging

import kopf
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from handlers.network import _modify_scc_users
from helpers.ceph_appliance import (
    APPLIANCE_SCC_NAME,
    APPLIANCE_SCC_SAS,
    CEPH_EXTERNAL_SECRET,
    CEPH_POOL_NAME,
    CEPH_SA,
    appliance_is_ready,
    apply_configmap,
    apply_deployment,
    apply_pvc,
    apply_secret_if_absent,
    build_ceph_rbac,
    build_conf_configmap,
    build_external_secret,
    build_mon_deployment,
    build_mon_pvc,
    build_osd_deployment,
    build_osd_pvcs,
    build_placeholder_identity_secrets,
    ceph_external_details_exported,
    delete_appliance,
    ensure_ceph_export_job,
    nested_mon_host_from_secret,
    read_or_create_fsid,
    service_account_ref,
)
from helpers.k8s import CRD_GROUP, CRD_VERSION
from helpers.rook_ceph import (
    discover_ceph_image,
    normalize_ceph_counts,
    normalize_restore_spec,
    validate_lab_ip,
)

logger = logging.getLogger(__name__)


def _ensure_service_account(api, namespace: str) -> None:
    try:
        api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=CEPH_SA),
            ),
        )
    except ApiException as e:
        if e.status != 409:
            raise


def _apply_rbac(rbac_api, namespace: str, role: dict, binding: dict) -> None:
    for body in (role, binding):
        try:
            if body["kind"] == "Role":
                rbac_api.create_namespaced_role(namespace=namespace, body=body)
            else:
                rbac_api.create_namespaced_role_binding(namespace=namespace, body=body)
        except ApiException as e:
            if e.status != 409:
                raise


def _log_restore_mode(restore: dict, spec: dict, patch, namespace: str) -> None:
    logger.info(
        "TroshkaCeph %s restoring in %s: mon PVC %s + %d OSD PVC(s)",
        spec.get("cephId", ""),
        namespace,
        restore["monPvc"],
        len(restore["osdPvcs"]),
    )
    patch.status["restoreMode"] = True


def _bind_scc(custom_api, namespace: str) -> None:
    for sa_name in APPLIANCE_SCC_SAS:
        try:
            _modify_scc_users(
                custom_api,
                APPLIANCE_SCC_NAME,
                service_account_ref(namespace, sa_name),
                "add",
            )
        except Exception as e:
            logger.warning(
                "Could not add %s to %s SCC: %s", sa_name, APPLIANCE_SCC_NAME, e
            )


def _unbind_scc(custom_api, namespace: str) -> None:
    for sa_name in APPLIANCE_SCC_SAS:
        try:
            _modify_scc_users(
                custom_api,
                APPLIANCE_SCC_NAME,
                service_account_ref(namespace, sa_name),
                "remove",
            )
        except Exception as e:
            logger.warning(
                "Could not remove %s from %s SCC: %s",
                sa_name,
                APPLIANCE_SCC_NAME,
                e,
            )


async def _reconcile_ceph(body, patch, namespace: str) -> None:
    spec = body.get("spec", {})
    lab_ip = spec.get("labIp", "")
    if not validate_lab_ip(lab_ip):
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Invalid labIp: {lab_ip!r}"
        return

    restore = normalize_restore_spec(spec)
    if restore["enabled"]:
        _log_restore_mode(restore, spec, patch, namespace)

    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()
    rbac_api = client.RbacAuthorizationV1Api()

    _bind_scc(custom_api, namespace)
    _ensure_service_account(core_api, namespace)
    role, binding = build_ceph_rbac(body)
    _apply_rbac(rbac_api, namespace, role, binding)

    fsid = read_or_create_fsid(core_api, namespace, body)
    for secret_body in build_placeholder_identity_secrets(body, fsid):
        apply_secret_if_absent(core_api, namespace, secret_body)

    apply_configmap(core_api, namespace, build_conf_configmap(body, fsid))

    restore_enabled = restore["enabled"]
    if not restore_enabled:
        apply_pvc(core_api, namespace, build_mon_pvc(body))
        for pvc in build_osd_pvcs(body):
            apply_pvc(core_api, namespace, pvc)

    ceph_image = discover_ceph_image(custom_api)
    apply_deployment(apps_api, namespace, build_mon_deployment(body, ceph_image))

    osd_count, replicate_size, _ = normalize_ceph_counts(spec)
    for i in range(osd_count):
        apply_deployment(apps_api, namespace, build_osd_deployment(body, ceph_image, i))

    secret_body = build_external_secret(body, fsid=fsid)
    try:
        core_api.create_namespaced_secret(namespace=namespace, body=secret_body)
    except ApiException as e:
        if e.status != 409:
            raise
        if not ceph_external_details_exported(core_api, namespace):
            try:
                existing = core_api.read_namespaced_secret(
                    name=CEPH_EXTERNAL_SECRET, namespace=namespace
                )
                data = existing.data or {}
                if not data.get("fsid") and fsid:
                    core_api.patch_namespaced_secret(
                        name=CEPH_EXTERNAL_SECRET,
                        namespace=namespace,
                        body={
                            "stringData": {"fsid": fsid, "mon-host": f"{lab_ip}:3300"}
                        },
                    )
            except ApiException:
                pass

    lab_mon_endpoint = f"{lab_ip}:3300"
    nested_mon = nested_mon_host_from_secret(core_api, namespace)
    mon_endpoint = nested_mon or lab_mon_endpoint

    patch.status["monEndpoint"] = mon_endpoint
    patch.status["labMonEndpoint"] = lab_mon_endpoint
    patch.status["secretName"] = CEPH_EXTERNAL_SECRET
    patch.status["storageClassName"] = spec.get("storageClassName", "troshka-ceph-rbd")
    patch.status["poolName"] = CEPH_POOL_NAME
    patch.status["osdCount"] = osd_count
    patch.status["replicateSize"] = replicate_size

    if appliance_is_ready(apps_api, namespace, osd_count):
        batch_api = client.BatchV1Api()
        ensure_ceph_export_job(batch_api, body, namespace)
        if ceph_external_details_exported(core_api, namespace):
            nested_mon = nested_mon_host_from_secret(core_api, namespace)
            if nested_mon:
                patch.status["monEndpoint"] = nested_mon
            patch.status["phase"] = "Ready"
            patch.status["message"] = "Ceph appliance ready"
            logger.info(
                "TroshkaCeph ready in %s (mon=%s)",
                namespace,
                patch.status["monEndpoint"],
            )
            return
        patch.status["phase"] = "Progressing"
        patch.status["message"] = "Exporting ODF external cluster details"
        return

    patch.status["phase"] = "Progressing"
    patch.status["message"] = "Waiting for Ceph mon/OSD pods"


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


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkancephs")
async def ceph_delete(namespace, name, body=None, **_):
    logger.info("Deleting TroshkaCeph %s in %s", name, namespace)
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    rbac_api = client.RbacAuthorizationV1Api()
    batch_api = client.BatchV1Api()

    _unbind_scc(custom_api, namespace)

    osd_count = 3
    if body:
        osd_count, _, _ = normalize_ceph_counts(body.get("spec") or {})

    delete_appliance(apps_api, core_api, batch_api, namespace, osd_count)

    for kind, delete_fn, obj_name in (
        ("RoleBinding", rbac_api.delete_namespaced_role_binding, CEPH_SA),
        ("Role", rbac_api.delete_namespaced_role, CEPH_SA),
    ):
        try:
            delete_fn(name=obj_name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning("Failed to delete ceph %s: %s", kind, e)

    try:
        core_api.delete_namespaced_service_account(name=CEPH_SA, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            logger.warning("Failed to delete ceph service account: %s", e)
