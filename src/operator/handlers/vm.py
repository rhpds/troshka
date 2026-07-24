import asyncio
import kopf
import logging
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, golden_pvc_name, owner_ref, TOOLS_IMAGE
from helpers.kubevirt import (
    build_kubevirt_vm,
    build_cloudinit_secret,
    build_datavolume_from_s3,
    build_blank_pvc,
    build_clone_datavolume,
    build_recert_job,
    CACHE_NAMESPACE,
)

logger = logging.getLogger(__name__)


def _cleanup_legacy_pod(core_api, namespace, pod_name):
    """Delete a standalone Pod if it exists (migration from Pod to Deployment)."""
    try:
        pod = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
        owners = getattr(pod.metadata, "owner_references", None) or []
        if not any(o.kind == "ReplicaSet" for o in owners):
            core_api.delete_namespaced_pod(name=pod_name, namespace=namespace)
            logger.info(f"Deleted legacy standalone Pod {pod_name}")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def _get_s3_config_from_project(namespace):
    custom_api = client.CustomObjectsApi()
    projects = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkaprojects",
    )
    items = projects.get("items", [])  # type: ignore[union-attr]
    if items:
        return items[0].get("spec", {}).get("s3Config", {})
    return {}


def _get_central_s3_config_from_project(namespace):
    custom_api = client.CustomObjectsApi()
    projects = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkaprojects",
    )
    items = projects.get("items", [])  # type: ignore[union-attr]
    if items:
        return items[0].get("spec", {}).get("centralS3Config", {})
    return {}


async def _wait_for_datavolume(
    custom_api, name, namespace, timeout=3600, owner_name=None, owner_namespace=None
):
    for _ in range(timeout // 5):
        if owner_name and owner_namespace:
            try:
                custom_api.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=owner_namespace,
                    plural="troshkavms",
                    name=owner_name,
                )
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    logger.warning(
                        f"Owner TroshkaVM {owner_name} deleted, aborting wait for {name}"
                    )
                    return False
            except Exception:
                pass
        try:
            dv = custom_api.get_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
                name=name,
            )
            phase = dv.get("status", {}).get("phase", "")
            if phase == "Succeeded":
                return True
            if phase in ("Failed", "Error"):
                logger.error(
                    f"DataVolume {name} failed: "
                    f"{dv.get('status', {}).get('conditions', [])}"
                )
                return False
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.warning(f"DataVolume {name} not found, may have been deleted")
                return False
        except Exception:
            pass
        await asyncio.sleep(5)
    logger.warning(f"DataVolume {name} timed out after {timeout}s")
    return False


async def _ensure_golden_pvc(
    custom_api,
    core_api,
    s3_path,
    size_gb,
    s3_config,
    secret_name="s3-credentials",  # pragma: allowlist secret
):
    pvc_name = golden_pvc_name(s3_path)
    try:
        core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=CACHE_NAMESPACE
        )
        logger.info(f"Golden PVC {pvc_name} already exists")
        return pvc_name
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        core_api.create_namespace(
            body=client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name=CACHE_NAMESPACE,
                    labels={"app": "troshka-cache"},
                )
            )
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    dv = build_datavolume_from_s3(
        pvc_name,
        CACHE_NAMESPACE,
        s3_path,
        size_gb,
        s3_config,
        secret_name=secret_name,
    )
    try:
        custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=CACHE_NAMESPACE,
            plural="datavolumes",
            body=dv,
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    if not await _wait_for_datavolume(custom_api, pvc_name, CACHE_NAMESPACE):
        raise kopf.TemporaryError(f"Golden PVC {pvc_name} import failed", delay=30)

    logger.info(f"Golden PVC {pvc_name} ready")
    return pvc_name


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating VM {name} in {namespace}")
    patch.status["state"] = "Creating"

    core_api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    s3_config = _get_s3_config_from_project(namespace)

    disk_pvcs = {}

    central_s3_config = _get_central_s3_config_from_project(namespace)

    for disk in spec.get("disks", []):
        disk_id = disk.get("id", "")[:8]
        pvc_name = f"{name}-disk-{disk_id}"

        s3_path = None
        use_central = False
        if disk.get("libraryImage", {}).get("s3Path"):
            s3_path = disk["libraryImage"]["s3Path"]
            use_central = disk["libraryImage"].get("central", False)
        elif disk.get("patternImage", {}).get("s3Path"):
            s3_path = disk["patternImage"]["s3Path"]
            use_central = disk["patternImage"].get("central", False)

        if s3_path:
            if use_central and central_s3_config:
                disk_s3 = central_s3_config
                secret = "s3-central-credentials"  # pragma: allowlist secret
            else:
                disk_s3 = s3_config
                secret = "s3-credentials"  # pragma: allowlist secret
            size_gb = disk.get("sizeGb", 20)
            golden_name = await _ensure_golden_pvc(
                custom_api,
                core_api,
                s3_path,
                size_gb,
                disk_s3,
                secret_name=secret,
            )

            clone_dv = build_clone_datavolume(
                pvc_name, namespace, golden_name, CACHE_NAMESPACE, size_gb
            )
            clone_dv["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                custom_api.create_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    body=clone_dv,
                )
            except client.exceptions.ApiException as e:
                if e.status == 409:
                    try:
                        existing_dv = custom_api.get_namespaced_custom_object(
                            group="cdi.kubevirt.io",
                            version="v1beta1",
                            namespace=namespace,
                            plural="datavolumes",
                            name=pvc_name,
                        )
                        phase = existing_dv.get("status", {}).get("phase", "")
                        if phase == "Succeeded":
                            logger.info(
                                f"DataVolume {pvc_name} already exists and succeeded, skipping"
                            )
                        else:
                            logger.info(
                                f"DataVolume {pvc_name} exists (phase={phase}), waiting"
                            )
                    except client.exceptions.ApiException as ge:
                        if ge.status == 404:
                            custom_api.create_namespaced_custom_object(
                                group="cdi.kubevirt.io",
                                version="v1beta1",
                                namespace=namespace,
                                plural="datavolumes",
                                body=clone_dv,
                            )
                            logger.info(
                                f"Created DataVolume {pvc_name} (after 404)"
                            )
                        else:
                            raise
                else:
                    raise

            if not await _wait_for_datavolume(
                custom_api,
                pvc_name,
                namespace,
                owner_name=name,
                owner_namespace=namespace,
            ):
                patch.status["state"] = "Error"
                patch.status["message"] = f"Disk clone failed for {disk_id}"
                raise kopf.PermanentError(f"Disk clone {pvc_name} failed")

        elif disk.get("blank"):
            size_gb = disk.get("sizeGb", 20)
            pvc = build_blank_pvc(pvc_name, namespace, size_gb)
            pvc["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace, body=pvc
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

        disk_pvcs[disk.get("id", "")] = pvc_name

    if spec.get("cdrom", {}).get("s3Path"):
        cdrom_pvc = f"{name}-cdrom"
        cdrom_s3 = spec["cdrom"]["s3Path"]
        try:
            golden_name = await _ensure_golden_pvc(
                custom_api, core_api, cdrom_s3, 10, s3_config
            )
            cdrom_size = 10
            try:
                golden_pvc = core_api.read_namespaced_persistent_volume_claim(
                    name=golden_name, namespace=CACHE_NAMESPACE
                )
                golden_storage = golden_pvc.spec.resources.requests.get(
                    "storage", "10Gi"
                )
                cdrom_size = max(cdrom_size, int(golden_storage.rstrip("Gi")))
            except Exception:
                pass
            clone_dv = build_clone_datavolume(
                cdrom_pvc, namespace, golden_name, CACHE_NAMESPACE, cdrom_size
            )
            clone_dv["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                custom_api.create_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    body=clone_dv,
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise
            await _wait_for_datavolume(
                custom_api,
                cdrom_pvc,
                namespace,
                owner_name=name,
                owner_namespace=namespace,
            )
            disk_pvcs["cdrom"] = cdrom_pvc
        except Exception as e:
            logger.warning(
                f"CDROM setup failed for {name} (non-fatal, VM will boot without ISO): {e}"
            )

    # Recert is handled by the project handler before VMs are created

    cloudinit_secret_name = None
    ci_secret = build_cloudinit_secret(body)
    if ci_secret:
        ci_secret["metadata"]["ownerReferences"] = [owner_ref(body)]
        cloudinit_secret_name = ci_secret["metadata"]["name"]
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=ci_secret)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    if spec.get("guestfishCommands"):
        gf_commands = spec["guestfishCommands"]
        root_disk_id = spec["disks"][0]["id"] if spec.get("disks") else ""
        root_pvc = disk_pvcs.get(root_disk_id)
        if root_pvc and gf_commands:
            gf_job_name = f"guestfish-{name}"
            gf_cmd = "; ".join(gf_commands)

            job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": gf_job_name,
                    "namespace": namespace,
                    "ownerReferences": [owner_ref(body)],
                },
                "spec": {
                    "backoffLimit": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "guestfish",
                                    "image": TOOLS_IMAGE,
                                    "command": [
                                        "sh",
                                        "-c",
                                        f"guestfish --rw -a /disk/disk.img -i {gf_cmd}",
                                    ],
                                    "volumeMounts": [
                                        {
                                            "name": "disk",
                                            "mountPath": "/disk",
                                        }
                                    ],
                                    "securityContext": {"privileged": True},
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "disk",
                                    "persistentVolumeClaim": {"claimName": root_pvc},
                                }
                            ],
                            "restartPolicy": "Never",
                        },
                    },
                },
            }
            batch_api = client.BatchV1Api()
            try:
                batch_api.create_namespaced_job(namespace=namespace, body=job)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            for _ in range(120):
                try:
                    j = batch_api.read_namespaced_job(
                        name=gf_job_name, namespace=namespace
                    )
                    if j.status.succeeded:
                        break
                    if j.status.failed:
                        logger.error(f"Guestfish job {gf_job_name} failed")
                        break
                except Exception:
                    pass
                await asyncio.sleep(5)

    nad_refs = {}
    try:
        networks = custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkanetworks",
        )
        for net in networks.get("items", []):
            net_name = net["metadata"]["name"]
            nad_name = net.get("status", {}).get("nadName", f"{net_name}-nad")
            nad_refs[net_name] = nad_name
    except Exception:
        pass

    kv_vm = build_kubevirt_vm(body, disk_pvcs, nad_refs, cloudinit_secret_name)
    kv_vm["metadata"]["ownerReferences"] = [owner_ref(body)]

    kv_vm_name = kv_vm["metadata"]["name"]
    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Created KubeVirt VM {kv_vm_name}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            logger.info(
                f"KubeVirt VM {kv_vm_name} exists (stale), waiting for deletion"
            )
            for _ in range(30):
                try:
                    custom_api.get_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachines",
                        name=kv_vm_name,
                    )
                    await asyncio.sleep(2)
                except client.exceptions.ApiException as ge:
                    if ge.status == 404:
                        break
                    raise
            custom_api.create_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                body=kv_vm,
            )
            logger.info(f"Created KubeVirt VM {kv_vm_name} (after stale cleanup)")
        else:
            raise

    if spec.get("bmcEnabled"):
        from helpers.bmc import build_bmc_deployment

        try:
            core_api.create_namespaced_service_account(
                namespace=namespace,
                body=client.V1ServiceAccount(
                    metadata=client.V1ObjectMeta(name="troshka-bmc"),
                ),
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        bmc_nad = None
        try:
            nets = custom_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkanetworks",
            )
            for net in nets.get("items", []):
                if net.get("spec", {}).get("networkType") == "bmc":
                    bmc_nad = net.get("status", {}).get(
                        "nadName", f"{net['metadata']['name']}-nad"
                    )
                    break
        except Exception:
            pass

        if bmc_nad:
            apps_api = client.AppsV1Api()
            project_label = namespace.replace("troshka-", "")
            bmc_vms = [
                {
                    "vmId": spec["vmId"],
                    "smbiosUuid": spec.get("smbiosUuid", ""),
                }
            ]

            existing_bmc = None
            try:
                existing_bmc = apps_api.read_namespaced_deployment(
                    name=f"bmc-{project_label}", namespace=namespace
                )
            except client.exceptions.ApiException:
                pass

            if not existing_bmc:
                _cleanup_legacy_pod(core_api, namespace, f"bmc-{project_label}")
                bmc_dep = build_bmc_deployment(
                    project_label, namespace, bmc_vms, bmc_nad, {}
                )
                try:
                    apps_api.create_namespaced_deployment(
                        namespace=namespace, body=bmc_dep
                    )
                    logger.info(f"Created BMC deployment for {namespace}")
                except client.exceptions.ApiException as e:
                    if e.status != 409:
                        raise

    patch.status["state"] = (
        "Running" if spec.get("powerOnAtDeploy", True) else "Stopped"
    )
    patch.status["kubevirtVmName"] = kv_vm["metadata"]["name"]
    logger.info(f"TroshkaVM {name} reconciled")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_delete(spec, status, meta, namespace, name, **_):
    logger.info(f"TroshkaVM {name} deleting — cleaning up KubeVirt resources")
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    kv_name = status.get("kubevirtVmName", f"troshka-{name}")
    try:
        custom_api.delete_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_name,
        )
        logger.info(f"Deleted KubeVirt VM {kv_name}")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete KubeVirt VM {kv_name}: {e}")

    for disk in spec.get("disks", []):
        disk_id = disk.get("id", "")[:8]
        pvc_name = f"{name}-disk-{disk_id}"
        for resource_type in ("datavolumes", "persistentvolumeclaims"):
            try:
                if resource_type == "datavolumes":
                    custom_api.delete_namespaced_custom_object(
                        group="cdi.kubevirt.io",
                        version="v1beta1",
                        namespace=namespace,
                        plural=resource_type,
                        name=pvc_name,
                    )
                else:
                    core_api.delete_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=namespace
                    )
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete {resource_type}/{pvc_name}: {e}")

    if spec.get("cdrom", {}).get("s3Path"):
        cdrom_pvc = f"{name}-cdrom"
        try:
            core_api.delete_namespaced_persistent_volume_claim(
                name=cdrom_pvc, namespace=namespace
            )
        except client.exceptions.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete cdrom PVC {cdrom_pvc}: {e}")

    ci_secret_name = f"cloudinit-{name}"
    try:
        core_api.delete_namespaced_secret(name=ci_secret_name, namespace=namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete cloud-init secret {ci_secret_name}: {e}")
