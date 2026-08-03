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
    except client.ApiException as e:
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
        return items[0].get("spec", {}).get("s3Config", {})  # type: ignore[union-attr]
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
        return items[0].get("spec", {}).get("centralS3Config", {})  # type: ignore[union-attr]
    return {}


async def _wait_for_datavolume(
    custom_api, name, namespace, *, owner_name=None, owner_namespace=None
):
    try:
        async with asyncio.timeout(3600):
            while True:
                if owner_name and owner_namespace:
                    try:
                        custom_api.get_namespaced_custom_object(
                            group=CRD_GROUP,
                            version=CRD_VERSION,
                            namespace=owner_namespace,
                            plural="troshkavms",
                            name=owner_name,
                        )
                    except client.ApiException as e:
                        if e.status == 404:
                            logger.warning(
                                f"Owner TroshkaVM {owner_name} deleted, "
                                f"aborting wait for {name}"
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
                except client.ApiException as e:
                    if e.status == 404:
                        logger.warning(
                            f"DataVolume {name} not found, may have been deleted"
                        )
                        return False
                except Exception:
                    pass
                await asyncio.sleep(5)
    except TimeoutError:
        logger.warning(f"DataVolume {name} timed out after 3600s")
        return False


async def _ensure_golden_pvc(
    custom_api,
    core_api,
    s3_path,
    size_gb,
    s3_config,
    secret_name: str | None = "s3-credentials",  # pragma: allowlist secret
):
    if not secret_name:
        secret_name = "s3-credentials"  # pragma: allowlist secret
    pvc_name = golden_pvc_name(s3_path)
    try:
        core_api.read_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=CACHE_NAMESPACE
        )
        logger.info(f"Golden PVC {pvc_name} already exists")
        return pvc_name
    except client.ApiException as e:
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
    except client.ApiException as e:
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
    except client.ApiException as e:
        if e.status != 409:
            raise

    if not await _wait_for_datavolume(custom_api, pvc_name, CACHE_NAMESPACE):
        raise kopf.TemporaryError(f"Golden PVC {pvc_name} import failed", delay=30)

    logger.info(f"Golden PVC {pvc_name} ready")
    return pvc_name


def _resolve_disk_s3(disk, s3_config, central_s3_config):
    """Return (s3_path, s3_config_dict, secret_name) for a disk, or (None, None, None)."""
    s3_path = None
    use_central = False
    if disk.get("libraryImage", {}).get("s3Path"):
        s3_path = disk["libraryImage"]["s3Path"]
        use_central = disk["libraryImage"].get("central", False)
    elif disk.get("patternImage", {}).get("s3Path"):
        s3_path = disk["patternImage"]["s3Path"]
        use_central = disk["patternImage"].get("central", False)
    if not s3_path:
        return None, None, None
    if use_central and central_s3_config:
        return (
            s3_path,
            central_s3_config,
            "s3-central-credentials",
        )  # pragma: allowlist secret  # NOSONAR
    return s3_path, s3_config, "s3-credentials"  # pragma: allowlist secret  # NOSONAR


def _create_clone_datavolume(custom_api, namespace, pvc_name, clone_dv):
    """Create a clone DataVolume, handling 409 conflict with retry logic."""
    try:
        custom_api.create_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            body=clone_dv,
        )
    except client.ApiException as e:
        if e.status != 409:
            raise
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
                logger.info(f"DataVolume {pvc_name} exists (phase={phase}), waiting")
        except client.ApiException as ge:
            if ge.status == 404:
                custom_api.create_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    body=clone_dv,
                )
                logger.info(f"Created DataVolume {pvc_name} (after 404)")
            else:
                raise


async def _provision_disk_pvcs(
    spec,
    name,
    namespace,
    body,
    core_api,
    custom_api,
    s3_config,
    central_s3_config,
    patch,
):
    """Provision all disk PVCs (cloned from S3 or blank). Returns disk_pvcs dict."""
    disk_pvcs = {}
    for disk in spec.get("disks", []):
        disk_id = disk.get("id", "")[:8]
        pvc_name = f"{name}-disk-{disk_id}"

        s3_path, disk_s3, secret = _resolve_disk_s3(disk, s3_config, central_s3_config)

        if s3_path:
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
            _create_clone_datavolume(custom_api, namespace, pvc_name, clone_dv)

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
            except client.ApiException as e:
                if e.status != 409:
                    raise

        disk_pvcs[disk.get("id", "")] = pvc_name
    return disk_pvcs


async def _provision_cdrom(
    spec, name, namespace, body, core_api, custom_api, s3_config
):
    """Provision CDROM PVC if spec has a cdrom s3Path. Returns pvc_name or None."""
    if not spec.get("cdrom", {}).get("s3Path"):
        return None
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
            golden_storage = golden_pvc.spec.resources.requests.get("storage", "10Gi")
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
        except client.ApiException as e:
            if e.status != 409:
                raise
        await _wait_for_datavolume(
            custom_api,
            cdrom_pvc,
            namespace,
            owner_name=name,
            owner_namespace=namespace,
        )
        return cdrom_pvc
    except Exception as e:
        logger.warning(
            f"CDROM setup failed for {name} (non-fatal, VM will boot without ISO): {e}"
        )
        return None


async def _run_guestfish_job(spec, name, namespace, body, disk_pvcs):
    """Run guestfish commands against the root disk if specified."""
    if not spec.get("guestfishCommands"):
        return
    gf_commands = spec["guestfishCommands"]
    root_disk_id = spec["disks"][0]["id"] if spec.get("disks") else ""
    root_pvc = disk_pvcs.get(root_disk_id)
    if not root_pvc or not gf_commands:
        return
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
                            "volumeMounts": [{"name": "disk", "mountPath": "/disk"}],
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
    except client.ApiException as e:
        if e.status != 409:
            raise
    for _ in range(120):
        try:
            j = batch_api.read_namespaced_job(name=gf_job_name, namespace=namespace)
            if j.status.succeeded:  # type: ignore[union-attr]
                break
            if j.status.failed:  # type: ignore[union-attr]
                logger.error(f"Guestfish job {gf_job_name} failed")
                break
        except Exception:
            pass
        await asyncio.sleep(5)


async def _delete_and_wait_for_kubevirt_vm(custom_api, namespace, kv_vm_name):
    """Delete a KubeVirt VM and wait for it to be fully removed."""
    try:
        custom_api.delete_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_vm_name,
        )
    except Exception:
        pass
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
        except client.ApiException as ge:
            if ge.status == 404:
                break
            raise


async def _recreate_kubevirt_vm(custom_api, namespace, kv_vm, kv_vm_name):
    """Delete an existing KubeVirt VM and recreate it."""
    logger.info(f"KubeVirt VM {kv_vm_name} already exists, deleting and recreating")
    await _delete_and_wait_for_kubevirt_vm(custom_api, namespace, kv_vm_name)
    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Created KubeVirt VM {kv_vm_name} (after cleanup)")
    except client.ApiException as ce:
        if ce.status == 409:
            logger.info(f"KubeVirt VM {kv_vm_name} still exists, adopting")
        else:
            raise


async def _create_or_adopt_kubevirt_vm(
    custom_api, namespace, kv_vm, kv_vm_name, existing_kv_name
):
    """Create a KubeVirt VM, handling 409 conflict with delete-and-recreate."""
    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Created KubeVirt VM {kv_vm_name}")
    except client.ApiException as e:
        if e.status != 409:
            raise
        if existing_kv_name:
            logger.info(
                f"KubeVirt VM {kv_vm_name} already exists (previously created), adopting"
            )
            return
        await _recreate_kubevirt_vm(custom_api, namespace, kv_vm, kv_vm_name)


def _ensure_bmc_sa_and_rbac(namespace, core_api, custom_api):
    """Create BMC service account, patch SCC, and create RBAC role/binding."""
    try:
        core_api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-bmc"),
            ),
        )
    except client.ApiException as e:
        if e.status != 409:
            raise

    sa_ref = f"system:serviceaccount:{namespace}:troshka-bmc"
    try:
        scc = custom_api.get_cluster_custom_object(
            group="security.openshift.io",
            version="v1",
            plural="securitycontextconstraints",
            name="troshka-gateway",
        )
        users = scc.get("users", []) or []
        if sa_ref not in users:
            users.append(sa_ref)
            custom_api.patch_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name="troshka-gateway",
                body={"users": users},
            )
            logger.info(f"Added {sa_ref} to troshka-gateway SCC")
    except Exception as e:
        logger.warning(f"Could not patch SCC for BMC in {namespace}: {e}")

    rbac_api = client.RbacAuthorizationV1Api()
    try:
        rbac_api.create_namespaced_role(
            namespace=namespace,
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "troshka-bmc", "namespace": namespace},
                "rules": [
                    {
                        "apiGroups": ["kubevirt.io"],
                        "resources": [
                            "virtualmachines",
                            "virtualmachineinstances",
                        ],
                        "verbs": ["get", "list", "patch"],
                    },
                    {
                        "apiGroups": ["cdi.kubevirt.io"],
                        "resources": ["datavolumes"],
                        "verbs": ["create", "get", "list", "delete"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["persistentvolumeclaims"],
                        "verbs": ["get", "list"],
                    },
                ],
            },
        )
    except client.ApiException as e:
        if e.status != 409:
            raise
    try:
        rbac_api.create_namespaced_role_binding(
            namespace=namespace,
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "troshka-bmc", "namespace": namespace},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "troshka-bmc",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "troshka-bmc",
                        "namespace": namespace,
                    },
                ],
            },
        )
    except client.ApiException as e:
        if e.status != 409:
            raise


def _find_bmc_nad(namespace, custom_api):
    """Find the NAD name for the BMC network in the namespace. Returns None if not found."""
    try:
        nets = custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkanetworks",
        )
        for net in nets.get("items", []):
            if net.get("spec", {}).get("networkType") == "bmc":
                return net.get("status", {}).get(
                    "nadName", f"{net['metadata']['name']}-nad"
                )
    except Exception:
        pass
    return None


def _setup_bmc(spec, namespace, core_api, custom_api, domain_uuid=""):
    """Set up BMC service account, RBAC, SCC, and deployment if bmcEnabled."""
    if not spec.get("bmcEnabled"):
        return
    from helpers.bmc import build_bmc_deployment

    _ensure_bmc_sa_and_rbac(namespace, core_api, custom_api)

    bmc_nad = _find_bmc_nad(namespace, custom_api)
    if not bmc_nad:
        return

    apps_api = client.AppsV1Api()
    project_label = namespace.replace("troshka-", "")
    bmc_vms = [
        {
            "vmId": spec["vmId"],
            "smbiosUuid": spec.get("smbiosUuid", ""),
            "bmcIp": spec.get("bmcIp", ""),
            "domainUuid": domain_uuid,
        }
    ]
    existing_bmc = None
    try:
        existing_bmc = apps_api.read_namespaced_deployment(
            name=f"bmc-{project_label}", namespace=namespace
        )
    except client.ApiException:
        pass
    if not existing_bmc:
        _cleanup_legacy_pod(core_api, namespace, f"bmc-{project_label}")
        from handlers.project import _get_bmc_credentials

        credentials = _get_bmc_credentials(custom_api, namespace)
        bmc_dep = build_bmc_deployment(
            project_label, namespace, bmc_vms, bmc_nad, credentials
        )
        try:
            apps_api.create_namespaced_deployment(namespace=namespace, body=bmc_dep)
            logger.info(f"Created BMC deployment for {namespace}")
        except client.ApiException as e:
            if e.status != 409:
                raise


def _resolve_nad_refs(custom_api, namespace):
    """Build a dict mapping TroshkaNetwork names to their NAD names."""
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
    return nad_refs


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_create(spec, meta, namespace, name, body, patch, **_):
    logger.info(f"Creating VM {name} in {namespace}")
    patch.status["state"] = "Creating"

    core_api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    s3_config = _get_s3_config_from_project(namespace)
    central_s3_config = _get_central_s3_config_from_project(namespace)

    disk_pvcs = await _provision_disk_pvcs(
        spec,
        name,
        namespace,
        body,
        core_api,
        custom_api,
        s3_config,
        central_s3_config,
        patch,
    )

    cdrom_pvc = await _provision_cdrom(
        spec,
        name,
        namespace,
        body,
        core_api,
        custom_api,
        s3_config,
    )
    if cdrom_pvc:
        disk_pvcs["cdrom"] = cdrom_pvc

    # Recert is handled by the project handler before VMs are created

    cloudinit_secret_name = None
    ci_secret = build_cloudinit_secret(body)
    if ci_secret:
        ci_secret["metadata"]["ownerReferences"] = [owner_ref(body)]
        cloudinit_secret_name = ci_secret["metadata"]["name"]
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=ci_secret)
        except client.ApiException as e:
            if e.status != 409:
                raise

    await _run_guestfish_job(spec, name, namespace, body, disk_pvcs)

    nad_refs = _resolve_nad_refs(custom_api, namespace)

    kv_vm = build_kubevirt_vm(body, disk_pvcs, nad_refs, cloudinit_secret_name)
    kv_vm["metadata"]["ownerReferences"] = [owner_ref(body)]

    kv_vm_name = kv_vm["metadata"]["name"]
    existing_kv_name = body.get("status", {}).get("kubevirtVmName", "")
    await _create_or_adopt_kubevirt_vm(
        custom_api,
        namespace,
        kv_vm,
        kv_vm_name,
        existing_kv_name,
    )

    # Read back the KubeVirt VM's UID as the domain UUID (same pattern as
    # troshkad reading domain_uuid from virsh define)
    try:
        created_vm = custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_vm_name,
        )
        domain_uuid = created_vm["metadata"]["uid"]
    except Exception:
        domain_uuid = ""
    patch.status["domainUuid"] = domain_uuid

    _setup_bmc(spec, namespace, core_api, custom_api, domain_uuid=domain_uuid)

    patch.status["state"] = (
        "Running" if spec.get("powerOnAtDeploy", True) else "Stopped"
    )
    patch.status["kubevirtVmName"] = kv_vm["metadata"]["name"]
    logger.info(f"TroshkaVM {name} reconciled")


async def _clone_s3_disk(
    disk_id,
    pvc_name,
    disk,
    name,
    namespace,
    body,
    core_api,
    custom_api,
    s3_config,
    central_s3_config,
    patch,
):
    """Clone a single S3-backed disk to a PVC. Raises on failure."""
    s3_path, disk_s3, secret = _resolve_disk_s3(disk, s3_config, central_s3_config)
    if not s3_path:
        return False

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
    except client.ApiException as e:
        if e.status != 409:
            raise
    if not await _wait_for_datavolume(
        custom_api,
        pvc_name,
        namespace,
        owner_name=name,
        owner_namespace=namespace,
    ):
        patch.status["state"] = "Error"
        patch.status["message"] = f"Disk clone failed for {disk_id[:8]}"
        raise kopf.PermanentError(f"Disk clone {pvc_name} failed")
    return True


async def _provision_new_disks(
    new_disks,
    old_disks,
    name,
    namespace,
    body,
    core_api,
    custom_api,
    s3_config,
    central_s3_config,
    patch,
):
    """Provision PVCs for newly added disks (skipping disks that already existed)."""
    disk_pvcs = {}
    for disk_id, disk in new_disks.items():
        pvc_name = f"{name}-disk-{disk_id[:8]}"
        if disk_id in old_disks:
            disk_pvcs[disk_id] = pvc_name
            continue

        cloned = await _clone_s3_disk(
            disk_id,
            pvc_name,
            disk,
            name,
            namespace,
            body,
            core_api,
            custom_api,
            s3_config,
            central_s3_config,
            patch,
        )

        if not cloned and disk.get("blank"):
            size_gb = disk.get("sizeGb", 20)
            pvc = build_blank_pvc(pvc_name, namespace, size_gb)
            pvc["metadata"]["ownerReferences"] = [owner_ref(body)]
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace, body=pvc
                )
            except client.ApiException as e:
                if e.status != 409:
                    raise

        disk_pvcs[disk_id] = pvc_name
    return disk_pvcs


def _try_delete_datavolume(custom_api, namespace, pvc_name):
    """Attempt to delete a DataVolume, ignoring 404."""
    try:
        custom_api.delete_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
            name=pvc_name,
        )
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete datavolumes/{pvc_name}: {e}")


def _try_delete_pvc(core_api, namespace, pvc_name):
    """Attempt to delete a PVC, ignoring 404."""
    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=pvc_name, namespace=namespace
        )
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete persistentvolumeclaims/{pvc_name}: {e}")


def _delete_removed_disks(old_disks, new_disks, name, namespace, core_api, custom_api):
    """Delete PVCs and DataVolumes for disks that were removed."""
    removed = set(old_disks) - set(new_disks)
    for disk_id in removed:
        old_pvc = f"{name}-disk-{disk_id[:8]}"
        _try_delete_datavolume(custom_api, namespace, old_pvc)
        _try_delete_pvc(core_api, namespace, old_pvc)


async def _stop_kubevirt_vm(custom_api, namespace, kv_name):
    """Stop a KubeVirt VM and wait for the VMI to terminate."""
    try:
        custom_api.patch_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_name,
            body={"spec": {"running": False}},
        )
    except Exception:
        pass
    for _ in range(60):
        try:
            custom_api.get_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
                name=kv_name,
            )
            await asyncio.sleep(2)
        except client.ApiException as e:
            if e.status == 404:
                break
            raise
    logger.info(f"VM {kv_name} stopped for reconfigure")


def _upsert_cloudinit_secret(body, namespace, core_api):
    """Create or replace the cloud-init secret. Returns the secret name or None."""
    ci_secret = build_cloudinit_secret(body)
    if not ci_secret:
        return None
    ci_secret["metadata"]["ownerReferences"] = [owner_ref(body)]
    secret_name = ci_secret["metadata"]["name"]
    try:
        core_api.replace_namespaced_secret(
            name=secret_name, namespace=namespace, body=ci_secret
        )
    except client.ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_secret(namespace=namespace, body=ci_secret)
        elif e.status != 409:
            raise
    return secret_name


async def _reconcile_disks(
    old_spec, new_spec, name, namespace, body, core_api, custom_api, patch
):
    """Handle disk provisioning and removal for a VM update. Returns disk_pvcs dict."""
    old_disks = {d.get("id", ""): d for d in old_spec.get("disks", [])}
    new_disks = {d.get("id", ""): d for d in new_spec.get("disks", [])}

    s3_config = _get_s3_config_from_project(namespace)
    central_s3_config = _get_central_s3_config_from_project(namespace)

    disk_pvcs = await _provision_new_disks(
        new_disks,
        old_disks,
        name,
        namespace,
        body,
        core_api,
        custom_api,
        s3_config,
        central_s3_config,
        patch,
    )
    _delete_removed_disks(old_disks, new_disks, name, namespace, core_api, custom_api)
    return disk_pvcs


@kopf.on.update(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_update(
    spec, old, new, diff, status, meta, namespace, name, body, patch, **_
):
    """Reconcile TroshkaVM spec changes by rebuilding the KubeVirt VM."""
    old_spec = (old or {}).get("spec", {})
    new_spec = (new or {}).get("spec", {})
    if old_spec == new_spec:
        return

    kv_name = status.get("kubevirtVmName", f"troshka-{name}")
    logger.info(f"TroshkaVM {name} updated — reconciling KubeVirt VM {kv_name}")
    patch.status["state"] = "Reconfiguring"

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    await _stop_kubevirt_vm(custom_api, namespace, kv_name)

    disk_pvcs = await _reconcile_disks(
        old_spec,
        new_spec,
        name,
        namespace,
        body,
        core_api,
        custom_api,
        patch,
    )

    await _delete_and_wait_for_kubevirt_vm(custom_api, namespace, kv_name)

    nad_refs = _resolve_nad_refs(custom_api, namespace)
    cloudinit_secret_name = _upsert_cloudinit_secret(body, namespace, core_api)

    # Rebuild and create KubeVirt VM with new spec
    kv_vm = build_kubevirt_vm(body, disk_pvcs, nad_refs, cloudinit_secret_name)
    kv_vm["metadata"]["ownerReferences"] = [owner_ref(body)]
    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Recreated KubeVirt VM {kv_name} with updated spec")
    except client.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"KubeVirt VM {kv_name} already exists after reconfigure")

    try:
        recreated_vm = custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_name,
        )
        domain_uuid = recreated_vm["metadata"]["uid"]
    except Exception:
        domain_uuid = status.get("domainUuid", "")
    patch.status["domainUuid"] = domain_uuid

    _setup_bmc(new_spec, namespace, core_api, custom_api, domain_uuid=domain_uuid)

    power_on = new_spec.get("powerOnAtDeploy", True)
    patch.status["state"] = "Running" if power_on else "Stopped"
    patch.status["kubevirtVmName"] = kv_vm["metadata"]["name"]
    logger.info(f"TroshkaVM {name} reconfigure complete")


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
    except client.ApiException as e:
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
            except client.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete {resource_type}/{pvc_name}: {e}")

    if spec.get("cdrom", {}).get("s3Path"):
        cdrom_pvc = f"{name}-cdrom"
        try:
            core_api.delete_namespaced_persistent_volume_claim(
                name=cdrom_pvc, namespace=namespace
            )
        except client.ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete cdrom PVC {cdrom_pvc}: {e}")

    ci_secret_name = f"cloudinit-{name}"
    try:
        core_api.delete_namespaced_secret(name=ci_secret_name, namespace=namespace)
    except client.ApiException as e:
        if e.status != 404:
            logger.warning(f"Failed to delete cloud-init secret {ci_secret_name}: {e}")
