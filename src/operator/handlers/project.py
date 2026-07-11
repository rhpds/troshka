import kopf
import logging
import time
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, owner_ref
from helpers.topology import (
    extract_networks,
    extract_vms,
    build_static_leases,
    resolve_vm_disks,
    resolve_nic_networks,
)

logger = logging.getLogger(__name__)


async def _handle_capture(spec, namespace, name, body, patch):
    from helpers.patterns import (
        build_volume_snapshot,
        build_temp_pvc_from_snapshot,
        build_export_job,
    )

    patch.status["phase"] = "Capturing"
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    s3_config = spec.get("s3Config", {})
    pattern_id = spec.get("patternId", name)

    vms = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkavms",
    )

    for vm_item in vms.get("items", []):
        vm_name = vm_item["metadata"]["name"]
        kv_name = vm_item.get("status", {}).get(
            "kubevirtVmName", f"troshka-{vm_name}"
        )
        try:
            custom_api.patch_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
                name=kv_name,
                body={"spec": {"running": False}},
            )
        except Exception as e:
            logger.warning(f"Failed to stop VM {kv_name}: {e}")

    time.sleep(5)

    for vm_item in vms.get("items", []):
        vm_name = vm_item["metadata"]["name"]
        vm_spec = vm_item.get("spec", {})

        for disk in vm_spec.get("disks", []):
            disk_id = disk.get("id", "")[:8]
            pvc_name = f"{vm_name}-disk-{disk_id}"
            snap_name = f"snap-{vm_name}-{disk_id}"
            s3_path = f"patterns/{pattern_id}/{vm_name}-{disk_id}.qcow2"
            size_gb = disk.get("sizeGb", 20)

            snapshot = build_volume_snapshot(snap_name, namespace, pvc_name)
            try:
                custom_api.create_namespaced_custom_object(
                    group="snapshot.storage.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="volumesnapshots",
                    body=snapshot,
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            for _ in range(60):
                try:
                    vs = custom_api.get_namespaced_custom_object(
                        group="snapshot.storage.k8s.io",
                        version="v1",
                        namespace=namespace,
                        plural="volumesnapshots",
                        name=snap_name,
                    )
                    if vs.get("status", {}).get("readyToUse"):
                        break
                except Exception:
                    pass
                time.sleep(5)

            temp_pvc_name = f"export-{vm_name}-{disk_id}"
            temp_pvc = build_temp_pvc_from_snapshot(
                temp_pvc_name, namespace, snap_name, size_gb
            )
            try:
                core_api.create_namespaced_persistent_volume_claim(
                    namespace=namespace, body=temp_pvc
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

            export_job = build_export_job(
                f"{vm_name}-{disk_id}",
                namespace,
                snap_name,
                s3_path,
                s3_config,
                size_gb,
            )
            export_job["spec"]["template"]["spec"]["volumes"][0][
                "persistentVolumeClaim"
            ]["claimName"] = temp_pvc_name
            try:
                batch_api.create_namespaced_job(
                    namespace=namespace, body=export_job
                )
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

    patch.status["phase"] = "CaptureComplete"
    logger.info(f"Pattern capture initiated for {name}")


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_create(spec, meta, namespace, name, body, patch, **_):
    action = spec.get("action", "deploy")
    logger.info(f"TroshkaProject {name} created with action={action}")

    if action == "capture":
        await _handle_capture(spec, namespace, name, body, patch)
        return

    if action not in ("deploy",):
        logger.warning(f"Unknown action {action} for {name}")
        return

    patch.status["phase"] = "Deploying"
    patch.status["deployProgress"] = {
        "percent": 0,
        "stage": "Parsing topology",
        "detail": "",
    }

    topology = spec.get("topology", {})
    custom_api = client.CustomObjectsApi()

    networks = extract_networks(topology)
    static_leases = build_static_leases(topology)

    patch.status["deployProgress"] = {
        "percent": 10,
        "stage": "Creating networks",
        "detail": f"0/{len(networks)} networks",
    }

    for i, net in enumerate(networks):
        net_name = f"net-{net['id'][:8]}"

        net_spec = {
            "networkId": net["id"],
            "cidr": net["cidr"],
            "gateway": net.get("gateway", ""),
            "dhcpRange": net.get("dhcpRange", ""),
            "networkType": net.get("networkType", "standard"),
            "dnsForwarders": net.get("dnsForwarders", []),
            "externalAccess": net.get("externalAccess", False),
            "staticLeases": static_leases.get(net["id"], []),
        }
        if net.get("pxeConfig"):
            net_spec["pxeConfig"] = net["pxeConfig"]

        net_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaNetwork",
            "metadata": {
                "name": net_name,
                "namespace": namespace,
                "ownerReferences": [owner_ref(body)],
                "labels": {"troshka-project": name},
            },
            "spec": net_spec,
        }

        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkanetworks",
                body=net_cr,
            )
            logger.info(f"Created TroshkaNetwork {net_name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 10 + int(20 * (i + 1) / max(len(networks), 1)),
            "stage": "Creating networks",
            "detail": f"{i + 1}/{len(networks)} networks",
        }

    vms = extract_vms(topology)
    vm_disks_map, vm_cdroms_map = resolve_vm_disks(topology)
    nic_network_map = resolve_nic_networks(topology)

    patch.status["deployProgress"] = {
        "percent": 30,
        "stage": "Creating VMs",
        "detail": f"0/{len(vms)} VMs",
    }

    for i, vm in enumerate(vms):
        vm_name = f"vm-{vm['id'][:8]}"

        disk_specs = vm_disks_map.get(vm["id"], [])

        nic_specs = []
        for nic in vm.get("nics", []):
            nic_id = nic.get("id", "")
            nic_spec = {
                "id": nic_id,
                "mac": nic.get("mac", ""),
                "model": nic.get("model", "virtio"),
                "networkRef": nic_network_map.get(nic_id, ""),
            }
            nic_specs.append(nic_spec)

        vm_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaVM",
            "metadata": {
                "name": vm_name,
                "namespace": namespace,
                "ownerReferences": [owner_ref(body)],
                "labels": {"troshka-project": name},
            },
            "spec": {
                "vmId": vm["id"],
                "name": vm["name"],
                "cpus": vm["cpus"],
                "memory": vm["memory"],
                "firmware": vm.get("firmware", "bios"),
                "machineType": vm.get("machineType", "q35"),
                "smbiosUuid": vm.get("smbiosUuid", ""),
                "powerOnAtDeploy": vm.get("powerOnAtDeploy", True),
                "disks": disk_specs,
                "nics": nic_specs,
                "cloudInit": vm.get("cloudInit", {}),
                "bmcEnabled": vm.get("bmcEnabled", False),
                "bootOrder": vm.get("bootOrder", []),
            },
        }
        cdrom = vm_cdroms_map.get(vm["id"]) or vm.get("cdrom")
        if cdrom and cdrom.get("s3Path"):
            vm_cr["spec"]["cdrom"] = cdrom
        if vm.get("guestfishCommands"):
            vm_cr["spec"]["guestfishCommands"] = vm["guestfishCommands"]

        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkavms",
                body=vm_cr,
            )
            logger.info(f"Created TroshkaVM {vm_name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 30 + int(60 * (i + 1) / max(len(vms), 1)),
            "stage": "Creating VMs",
            "detail": f"{i + 1}/{len(vms)} VMs",
        }

    patch.status["phase"] = "Deploying"
    patch.status["deployProgress"] = {
        "percent": 90,
        "stage": "Waiting for VMs",
        "detail": f"0/{len(vms)} VMs ready",
    }
    logger.info(f"TroshkaProject {name} CRs created, waiting for VMs")


@kopf.timer(CRD_GROUP, CRD_VERSION, "troshkaprojects", interval=10, idle=10)
async def project_status_check(spec, status, namespace, name, patch, **_):
    phase = status.get("phase", "")
    if phase != "Deploying":
        return

    custom_api = client.CustomObjectsApi()
    vms = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkavms",
    )
    vm_items = vms.get("items", [])
    if not vm_items:
        return

    vm_states = {}
    ready_count = 0
    for vm in vm_items:
        vm_name = vm.get("spec", {}).get("name", vm["metadata"]["name"])
        state = vm.get("status", {}).get("state", "")
        vm_states[vm.get("spec", {}).get("vmId", vm["metadata"]["name"])] = state or "creating"
        if state in ("Running", "Stopped"):
            ready_count += 1

    patch.status["vmStates"] = vm_states
    patch.status["deployProgress"] = {
        "percent": 90 + int(10 * ready_count / max(len(vm_items), 1)),
        "stage": "Waiting for VMs",
        "detail": f"{ready_count}/{len(vm_items)} VMs ready",
    }

    if ready_count == len(vm_items):
        patch.status["phase"] = "Running"
        patch.status["deployProgress"] = {
            "percent": 100,
            "stage": "Done",
            "detail": "",
        }
        logger.info(f"TroshkaProject {name} all VMs ready — phase: Running")


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_delete(spec, meta, namespace, name, **_):
    logger.info(
        f"TroshkaProject {name} deleting — ownerReferences handle child cascade"
    )
