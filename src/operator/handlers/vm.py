import kopf
import logging
import time
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, golden_pvc_name, owner_ref, TOOLS_IMAGE
from helpers.kubevirt import (
    build_kubevirt_vm,
    build_cloudinit_secret,
    build_datavolume_from_s3,
    build_blank_pvc,
    build_clone_datavolume,
    CACHE_NAMESPACE,
)

logger = logging.getLogger(__name__)


def _get_s3_config_from_project(namespace):
    custom_api = client.CustomObjectsApi()
    projects = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkaprojects",
    )
    items = projects.get("items", [])
    if items:
        return items[0].get("spec", {}).get("s3Config", {})
    return {}


def _wait_for_datavolume(custom_api, name, namespace, timeout=600):
    for _ in range(timeout // 5):
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
        except Exception:
            pass
        time.sleep(5)
    return False


def _ensure_golden_pvc(custom_api, core_api, s3_path, size_gb, s3_config, presigned_url=""):
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
        pvc_name, CACHE_NAMESPACE, s3_path, size_gb, s3_config,
        presigned_url=presigned_url,
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

    if not _wait_for_datavolume(custom_api, pvc_name, CACHE_NAMESPACE):
        raise kopf.TemporaryError(
            f"Golden PVC {pvc_name} import failed", delay=30
        )

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

    for disk in spec.get("disks", []):
        disk_id = disk.get("id", "")[:8]
        pvc_name = f"{name}-disk-{disk_id}"

        s3_path = None
        presigned_url = ""
        if disk.get("libraryImage", {}).get("s3Path"):
            s3_path = disk["libraryImage"]["s3Path"]
            presigned_url = disk["libraryImage"].get("presignedUrl", "")
        elif disk.get("patternImage", {}).get("s3Path"):
            s3_path = disk["patternImage"]["s3Path"]
            presigned_url = disk["patternImage"].get("presignedUrl", "")

        if s3_path:
            size_gb = disk.get("sizeGb", 20)
            golden_name = _ensure_golden_pvc(
                custom_api, core_api, s3_path, size_gb, s3_config,
                presigned_url=presigned_url,
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
                if e.status != 409:
                    raise

            if not _wait_for_datavolume(custom_api, pvc_name, namespace):
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
        golden_name = _ensure_golden_pvc(
            custom_api, core_api, cdrom_s3, 10, s3_config
        )
        clone_dv = build_clone_datavolume(
            cdrom_pvc, namespace, golden_name, CACHE_NAMESPACE, 10
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
        _wait_for_datavolume(custom_api, cdrom_pvc, namespace)
        disk_pvcs["cdrom"] = cdrom_pvc

    cloudinit_secret_name = None
    ci_secret = build_cloudinit_secret(body)
    if ci_secret:
        ci_secret["metadata"]["ownerReferences"] = [owner_ref(body)]
        cloudinit_secret_name = ci_secret["metadata"]["name"]
        try:
            core_api.create_namespaced_secret(
                namespace=namespace, body=ci_secret
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    if spec.get("guestfishCommands"):
        gf_commands = spec["guestfishCommands"]
        root_disk_id = (
            spec["disks"][0]["id"] if spec.get("disks") else ""
        )
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
                                    "securityContext": {
                                        "privileged": True
                                    },
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "disk",
                                    "persistentVolumeClaim": {
                                        "claimName": root_pvc
                                    },
                                }
                            ],
                            "restartPolicy": "Never",
                        },
                    },
                },
            }
            batch_api = client.BatchV1Api()
            try:
                batch_api.create_namespaced_job(
                    namespace=namespace, body=job
                )
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
                        logger.error(
                            f"Guestfish job {gf_job_name} failed"
                        )
                        break
                except Exception:
                    pass
                time.sleep(5)

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
            nad_name = net.get("status", {}).get(
                "nadName", f"{net_name}-nad"
            )
            nad_refs[net_name] = nad_name
    except Exception:
        pass

    kv_vm = build_kubevirt_vm(
        body, disk_pvcs, nad_refs, cloudinit_secret_name
    )
    kv_vm["metadata"]["ownerReferences"] = [owner_ref(body)]

    try:
        custom_api.create_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            body=kv_vm,
        )
        logger.info(f"Created KubeVirt VM {kv_vm['metadata']['name']}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    if spec.get("bmcEnabled"):
        from helpers.bmc import build_bmc_pod

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
            project_label = namespace.replace("troshka-", "")
            bmc_vms = [
                {
                    "vmId": spec["vmId"],
                    "smbiosUuid": spec.get("smbiosUuid", ""),
                }
            ]

            existing_bmc = None
            try:
                existing_bmc = core_api.read_namespaced_pod(
                    name=f"bmc-{project_label}", namespace=namespace
                )
            except client.exceptions.ApiException:
                pass

            if not existing_bmc:
                bmc_pod = build_bmc_pod(
                    project_label, namespace, bmc_vms, bmc_nad, {}
                )
                try:
                    core_api.create_namespaced_pod(
                        namespace=namespace, body=bmc_pod
                    )
                    logger.info(f"Created BMC pod for {namespace}")
                except client.exceptions.ApiException as e:
                    if e.status != 409:
                        raise

    patch.status["state"] = (
        "Running" if spec.get("powerOnAtDeploy", True) else "Stopped"
    )
    patch.status["kubevirtVmName"] = kv_vm["metadata"]["name"]
    logger.info(f"TroshkaVM {name} reconciled")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkavms")
async def vm_delete(spec, meta, namespace, name, **_):
    logger.info(
        f"TroshkaVM {name} deleting — ownerReferences handle KubeVirt VM cascade"
    )
