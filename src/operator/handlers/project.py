import json
import kopf
import logging
import time
from kubernetes import client
from helpers.k8s import CRD_GROUP, CRD_VERSION, owner_ref, build_gateway_pod
from helpers.topology import (
    extract_networks,
    extract_vms,
    build_static_leases,
    resolve_vm_disks,
    resolve_nic_networks,
)

logger = logging.getLogger(__name__)

CAPTURE_ANNOTATION = "troshka.redhat.com/capture-request"


async def _handle_capture(capture_config, namespace, name, patch):
    """Handle pattern capture: stop VMs, snapshot disks, export to S3."""
    from helpers.patterns import (
        build_volume_snapshot,
        build_temp_pvc_from_snapshot,
        build_export_job,
    )

    s3_config = capture_config.get("s3Config", {})
    pattern_id = capture_config.get("patternId", name)
    disk_manifest = capture_config.get("disks", [])

    patch.status["phase"] = "Capturing"
    patch.status["captureProgress"] = "Stopping VMs"

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    # Stop all KubeVirt VMs
    vms = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP, version=CRD_VERSION,
        namespace=namespace, plural="troshkavms",
    )
    kv_names = []
    for vm_item in vms.get("items", []):
        kv_name = vm_item.get("status", {}).get(
            "kubevirtVmName", f"troshka-{vm_item['metadata']['name']}"
        )
        kv_names.append(kv_name)
        try:
            custom_api.patch_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=namespace, plural="virtualmachines",
                name=kv_name, body={"spec": {"running": False}},
            )
        except Exception as e:
            logger.warning(f"Failed to stop VM {kv_name}: {e}")

    # Wait for all VMIs to be gone (max 120s)
    for attempt in range(40):
        try:
            vmis = custom_api.list_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=namespace, plural="virtualmachineinstances",
            )
            if not vmis.get("items"):
                break
        except Exception:
            pass
        time.sleep(3)

    # Snapshot and export each disk
    captured_disks = []
    export_jobs = []

    for disk_info in disk_manifest:
        pvc_name = disk_info["pvcName"]
        disk_id = disk_info["diskId"][:8]
        vm_name = disk_info["vmName"]
        s3_key = disk_info["s3Key"]
        size_gb = disk_info.get("sizeGb", 50)

        snap_name = f"snap-{vm_name}-{disk_id}"
        temp_pvc_name = f"export-{vm_name}-{disk_id}"
        job_name = f"{vm_name}-{disk_id}"

        patch.status["captureProgress"] = f"Snapshotting {vm_name}/{disk_id}"
        logger.info(f"Capture {name}: snapshotting PVC {pvc_name}")

        snapshot = build_volume_snapshot(snap_name, namespace, pvc_name)
        try:
            custom_api.create_namespaced_custom_object(
                group="snapshot.storage.k8s.io", version="v1",
                namespace=namespace, plural="volumesnapshots",
                body=snapshot,
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        # Poll until snapshot is ready (max 5 min)
        for _ in range(60):
            try:
                vs = custom_api.get_namespaced_custom_object(
                    group="snapshot.storage.k8s.io", version="v1",
                    namespace=namespace, plural="volumesnapshots",
                    name=snap_name,
                )
                if vs.get("status", {}).get("readyToUse"):
                    break
            except Exception:
                pass
            time.sleep(5)

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
            job_name, namespace, temp_pvc_name, s3_key, s3_config,
        )
        try:
            batch_api.create_namespaced_job(
                namespace=namespace, body=export_job
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        export_jobs.append({
            "jobName": f"export-{job_name}",
            "snapName": snap_name,
            "tempPvcName": temp_pvc_name,
            "diskId": disk_info["diskId"],
            "vmId": disk_info.get("vmId", ""),
            "s3Key": s3_key,
            "format": disk_info.get("format", "qcow2"),
            "virtualSizeBytes": size_gb * 1073741824,
        })

    # Poll export Jobs until all complete (max 30 min)
    patch.status["captureProgress"] = f"Exporting {len(export_jobs)} disk(s) to S3"
    for attempt in range(180):
        all_done = True
        for ej in export_jobs:
            try:
                job = batch_api.read_namespaced_job(
                    name=ej["jobName"], namespace=namespace,
                )
                if job.status.succeeded and job.status.succeeded >= 1:
                    continue
                if job.status.failed and job.status.failed >= 3:
                    logger.error(f"Export job {ej['jobName']} failed")
                    patch.status["phase"] = "CaptureError"
                    patch.status["captureError"] = f"Export job {ej['jobName']} failed"
                    return
                all_done = False
            except Exception:
                all_done = False
        if all_done:
            break
        time.sleep(10)

    # Read Job pod logs to get actual file sizes
    for ej in export_jobs:
        try:
            pods = core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"job-name={ej['jobName']}",
            )
            if pods.items:
                logs = core_api.read_namespaced_pod_log(
                    name=pods.items[0].metadata.name,
                    namespace=namespace,
                )
                for line in logs.splitlines():
                    if line.startswith("DISK_SIZE_BYTES="):
                        ej["sizeBytes"] = int(line.split("=")[1])
        except Exception:
            pass

        captured_disks.append({
            "diskId": ej["diskId"],
            "vmId": ej["vmId"],
            "s3Key": ej["s3Key"],
            "format": ej["format"],
            "sizeBytes": ej.get("sizeBytes", 0),
            "virtualSizeBytes": ej["virtualSizeBytes"],
        })

    patch.status["capturedDisks"] = captured_disks
    patch.status["phase"] = "CaptureComplete"
    patch.status["captureProgress"] = "Done"
    logger.info(f"Pattern capture complete for {name}: {len(captured_disks)} disk(s)")

    # Cleanup temp PVCs and snapshots
    for ej in export_jobs:
        try:
            core_api.delete_namespaced_persistent_volume_claim(
                name=ej["tempPvcName"], namespace=namespace,
            )
        except Exception:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="snapshot.storage.k8s.io", version="v1",
                namespace=namespace, plural="volumesnapshots",
                name=ej["snapName"],
            )
        except Exception:
            pass
        try:
            batch_api.delete_namespaced_job(
                name=ej["jobName"], namespace=namespace,
                propagation_policy="Background",
            )
        except Exception:
            pass


@kopf.on.create(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_create(spec, meta, namespace, name, body, patch, **_):
    action = spec.get("action", "deploy")
    logger.info(f"TroshkaProject {name} created with action={action}")

    if action == "capture":
        capture_config = {
            "patternId": spec.get("patternId", name),
            "s3Config": spec.get("s3Config", {}),
            "disks": spec.get("captureDisks", []),
        }
        await _handle_capture(capture_config, namespace, name, patch)
        return

    if action not in ("deploy",):
        logger.warning(f"Unknown action {action} for {name}")
        return

    patch.status["phase"] = "Deploying"
    patch.status["vmStates"] = {}
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

    # Create single gateway pod for all externalAccess networks
    gateway_nads = []
    gateway_ips = {}
    for net in networks:
        if net.get("externalAccess"):
            nad_name = f"net-{net['id'][:8]}-nad"
            gateway_nads.append(nad_name)
            if net.get("gateway"):
                gateway_ips[nad_name] = {
                    "ip": net["gateway"],
                    "cidr": net.get("cidr", "10.0.0.0/24"),
                }

    if gateway_nads:
        gw_pod = build_gateway_pod(body, gateway_nads, gateway_ips)
        try:
            api.create_namespaced_pod(namespace=namespace, body=gw_pod)
            logger.info(f"Created gateway pod for {name}")
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    vms = extract_vms(topology)
    vm_disks_map, vm_cdroms_map = resolve_vm_disks(topology)
    nic_network_map = resolve_nic_networks(topology)

    all_disks = []
    for vm in vms:
        all_disks.extend(vm_disks_map.get(vm["id"], []))
    for cdrom in vm_cdroms_map.values():
        if cdrom and cdrom.get("s3Path"):
            all_disks.append({"libraryImage": cdrom})

    if all_disks:
        from helpers.kubevirt import build_datavolume_from_s3, CACHE_NAMESPACE
        from helpers.k8s import golden_pvc_name

        s3_config = spec.get("s3Config", {})
        core_api = client.CoreV1Api()

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

        patch.status["deployProgress"] = {
            "percent": 30,
            "stage": "Downloading images",
            "detail": f"0/{len(all_disks)} disks",
        }

        for disk in all_disks:
            s3_path = None
            presigned_url = ""
            if disk.get("libraryImage", {}).get("s3Path"):
                s3_path = disk["libraryImage"]["s3Path"]
                presigned_url = disk["libraryImage"].get("presignedUrl", "")
            elif disk.get("patternImage", {}).get("s3Path"):
                s3_path = disk["patternImage"]["s3Path"]
                presigned_url = disk["patternImage"].get("presignedUrl", "")
            if not s3_path:
                continue

            pvc_name = golden_pvc_name(s3_path)
            try:
                core_api.read_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=CACHE_NAMESPACE
                )
                continue
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise

            size_gb = disk.get("sizeGb", 20)
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
                logger.info(f"Pre-created golden PVC {pvc_name} for parallel download")
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise

    patch.status["deployProgress"] = {
        "percent": 40,
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

    # Create VNC console proxy (pod + service + route)
    from helpers.vnc import build_vnc_proxy_deployment, build_vnc_service, build_vnc_route

    core_api = client.CoreV1Api()

    try:
        core_api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-vnc"),
            ),
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # Grant troshka-vnc SA access to KubeVirt VNC subresource
    rbac_api = client.RbacAuthorizationV1Api()
    role_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "troshka-vnc-access", "namespace": namespace},
        "rules": [
            {
                "apiGroups": ["kubevirt.io"],
                "resources": ["virtualmachineinstances"],
                "verbs": ["get"],
            },
            {
                "apiGroups": ["subresources.kubevirt.io"],
                "resources": ["virtualmachineinstances", "virtualmachineinstances/vnc"],
                "verbs": ["get"],
            },
        ],
    }
    try:
        rbac_api.create_namespaced_role(namespace=namespace, body=role_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    rb_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "troshka-vnc-access", "namespace": namespace},
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "troshka-vnc",
                "namespace": namespace,
            },
        ],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "troshka-vnc-access",
        },
    }
    try:
        rbac_api.create_namespaced_role_binding(namespace=namespace, body=rb_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    apps_api = client.AppsV1Api()
    vnc_dep = build_vnc_proxy_deployment(name, namespace, owner_body=body)
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=vnc_dep)
        logger.info(f"Created VNC proxy deployment for {name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    vnc_svc = build_vnc_service(name, namespace, owner_body=body)
    try:
        core_api.create_namespaced_service(namespace=namespace, body=vnc_svc)
        logger.info(f"Created VNC proxy service for {name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    vnc_route = build_vnc_route(name, namespace, owner_body=body)
    try:
        custom_api.create_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            body=vnc_route,
        )
        logger.info(f"Created VNC proxy route for {name}")
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # Read back the route to get the assigned hostname
    try:
        route = custom_api.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
            name=f"vnc-proxy-{name}",
        )
        console_host = route.get("spec", {}).get("host", "")
        if not console_host:
            console_host = (
                route.get("status", {})
                .get("ingress", [{}])[0]
                .get("host", "")
            )
        if console_host:
            patch.status["consoleRoute"] = console_host
            logger.info(f"Console route: {console_host}")
    except Exception as e:
        logger.warning(f"Could not read console route hostname: {e}")

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
    if phase not in ("Deploying", "Running"):
        return

    custom_api = client.CustomObjectsApi()

    # Get TroshkaVM CRs for VM ID mapping
    vms = custom_api.list_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=namespace,
        plural="troshkavms",
    )
    vm_items = vms.get("items", [])
    if not vm_items:
        return

    # Get actual KubeVirt VMI states (live truth)
    try:
        vmis = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachineinstances",
        )
        vmi_states = {}
        for vmi in vmis.get("items", []):
            vmi_name = vmi["metadata"]["name"]
            vmi_phase = vmi.get("status", {}).get("phase", "")
            vmi_states[vmi_name] = vmi_phase
    except Exception:
        vmi_states = {}

    vm_states = {}
    ready_count = 0
    for vm in vm_items:
        vm_id = vm.get("spec", {}).get("vmId", vm["metadata"]["name"])
        kv_name = vm.get("status", {}).get("kubevirtVmName", "")

        # Use live VMI state if available, fall back to TroshkaVM CR state
        if kv_name and kv_name in vmi_states:
            state = vmi_states[kv_name]
        else:
            state = vm.get("status", {}).get("state", "")
            if not state:
                state = "creating"
            # No VMI means VM is stopped (defined but not running)
            if kv_name and kv_name not in vmi_states and state == "Running":
                state = "Stopped"

        vm_states[vm_id] = state
        if state in ("Running", "Stopped"):
            ready_count += 1

    old_states = status.get("vmStates", {})
    if vm_states != old_states:
        patch.status["vmStates"] = vm_states

    if phase == "Deploying":
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
    logger.info(f"TroshkaProject {name} deleting — cleaning up all resources in {namespace}")
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    try:
        kv_vms = custom_api.list_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        for vm in kv_vms.get("items", []):
            vm_name = vm["metadata"]["name"]
            try:
                custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    name=vm_name,
                )
                logger.info(f"Deleted KubeVirt VM {vm_name}")
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete KubeVirt VM {vm_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list KubeVirt VMs in {namespace}: {e}")

    try:
        dvs = custom_api.list_namespaced_custom_object(
            group="cdi.kubevirt.io",
            version="v1beta1",
            namespace=namespace,
            plural="datavolumes",
        )
        for dv in dvs.get("items", []):
            dv_name = dv["metadata"]["name"]
            try:
                custom_api.delete_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                    name=dv_name,
                )
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete DataVolume {dv_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list DataVolumes in {namespace}: {e}")

    try:
        nads = custom_api.list_namespaced_custom_object(
            group="k8s.cni.cncf.io",
            version="v1",
            namespace=namespace,
            plural="network-attachment-definitions",
        )
        for nad in nads.get("items", []):
            nad_name = nad["metadata"]["name"]
            try:
                custom_api.delete_namespaced_custom_object(
                    group="k8s.cni.cncf.io",
                    version="v1",
                    namespace=namespace,
                    plural="network-attachment-definitions",
                    name=nad_name,
                )
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete NAD {nad_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list NADs in {namespace}: {e}")

    try:
        routes = custom_api.list_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=namespace,
            plural="routes",
        )
        for rt in routes.get("items", []):
            rt_name = rt["metadata"]["name"]
            try:
                custom_api.delete_namespaced_custom_object(
                    group="route.openshift.io",
                    version="v1",
                    namespace=namespace,
                    plural="routes",
                    name=rt_name,
                )
                logger.info(f"Deleted Route {rt_name}")
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete Route {rt_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list Routes in {namespace}: {e}")

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    try:
        scc = custom_api.get_cluster_custom_object(
            group="security.openshift.io",
            version="v1",
            plural="securitycontextconstraints",
            name="troshka-network-pods",
        )
        users = scc.get("users", []) or []
        if sa_ref in users:
            users.remove(sa_ref)
            custom_api.patch_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name="troshka-network-pods",
                body={"users": users},
            )
            logger.info(f"Removed {sa_ref} from SCC")
    except Exception as e:
        logger.warning(f"Could not clean SCC for {namespace}: {e}")

    logger.info(f"TroshkaProject {name} cleanup complete")


@kopf.on.update(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_update(spec, status, meta, namespace, name, body, patch, diff, **_):
    annotations = meta.get("annotations", {}) or {}
    capture_json = annotations.get(CAPTURE_ANNOTATION)
    if not capture_json:
        return

    phase = status.get("phase", "")
    if phase == "Capturing":
        return

    logger.info(f"Capture annotation detected on {name}, starting capture")
    try:
        capture_config = json.loads(capture_json)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Invalid capture annotation JSON on {name}: {e}")
        return

    await _handle_capture(capture_config, namespace, name, patch)
