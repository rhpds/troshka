import asyncio
import datetime
import json
import kopf
import logging
from typing import Any, cast
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from helpers.k8s import (
    CRD_GROUP,
    CRD_VERSION,
    owner_ref,
    build_exec_deployment,
    build_gateway_deployment,
)
from helpers.topology import (
    extract_networks,
    extract_vms,
    build_static_leases,
    resolve_vm_disks,
    resolve_nic_networks,
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
    except ApiException as e:
        if e.status != 404:
            raise


async def _ensure_deployment_gone(apps_api, namespace, dep_name, timeout=30):
    """Force-delete a deployment and wait for it to be fully gone."""
    try:
        apps_api.delete_namespaced_deployment(
            name=dep_name,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise

    for _ in range(timeout):
        try:
            apps_api.read_namespaced_deployment(name=dep_name, namespace=namespace)
            await asyncio.sleep(1)
        except ApiException as e:
            if e.status == 404:
                return
            raise


def _extract_kubeconfig_secret(core_api, namespace, job_name, project_name):
    """Read kubeconfig from recert Job logs and create a Secret for the exec pod."""
    import base64 as _b64
    import re

    try:
        pods = getattr(
            core_api.list_namespaced_pod(
                namespace, label_selector=f"job-name={job_name}"
            ),
            "items",
            [],
        )
        if not pods:
            return
        logs = core_api.read_namespaced_pod_log(
            name=pods[0].metadata.name,
            namespace=namespace,
            tail_lines=100,
        )
        logs_str = logs if isinstance(logs, str) else str(logs or "")
        logs_str = logs_str.replace("\\n", "\n")
        m = re.search(r"KUBECONFIG_B64_BEGIN\s+(\S+)\s+KUBECONFIG_B64_END", logs_str)
        if not m:
            idx = logs_str.find("KUBECONFIG_B64_BEGIN")
            end_idx = logs_str.find("KUBECONFIG_B64_END")
            context = ""
            if idx >= 0:
                context = repr(logs_str[idx + 19 : idx + 30])
            logger.warning(
                f"No kubeconfig found in recert job logs "
                f"(type={type(logs).__name__}, len={len(logs_str)}, "
                f"has_marker={'KUBECONFIG_B64' in logs_str}, "
                f"begin_idx={idx}, end_idx={end_idx}, "
                f"after_begin={context})"
            )
            return
        kc_data = m.group(1).strip()
        _b64.b64decode(kc_data)

        secret_name = "ocp-kubeconfig"  # pragma: allowlist secret
        secret_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "data": {"config": kc_data},
        }
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=secret_body)
            logger.info(f"Created kubeconfig Secret for {project_name}")
        except ApiException as e:
            if e.status == 409:
                core_api.replace_namespaced_secret(
                    name=secret_name, namespace=namespace, body=secret_body
                )
                logger.info(f"Replaced kubeconfig Secret for {project_name}")
            else:
                raise
    except Exception as e:
        logger.warning(f"Failed to extract kubeconfig from recert logs: {e}")


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
    vms = cast(
        dict[str, Any],
        custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkavms",
        ),
    )
    kv_names = []
    for vm_item in vms.get("items", []):
        kv_name = vm_item.get("status", {}).get(
            "kubevirtVmName", f"troshka-{vm_item['metadata']['name']}"
        )
        kv_names.append(kv_name)
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

    # Wait for all VMIs to be gone (max 120s)
    for attempt in range(40):
        try:
            vmis = cast(
                dict[str, Any],
                custom_api.list_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                ),
            )
            if not vmis.get("items"):
                break
        except Exception:
            pass
        await asyncio.sleep(3)

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
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                body=snapshot,
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Poll until snapshot is ready (max 5 min)
        for _ in range(60):
            try:
                vs = cast(
                    dict[str, Any],
                    custom_api.get_namespaced_custom_object(
                        group="snapshot.storage.k8s.io",
                        version="v1",
                        namespace=namespace,
                        plural="volumesnapshots",
                        name=snap_name,
                    ),
                )
                if vs.get("status", {}).get("readyToUse"):
                    break
            except Exception:
                pass
            await asyncio.sleep(5)

        temp_pvc = build_temp_pvc_from_snapshot(
            temp_pvc_name, namespace, snap_name, size_gb
        )
        try:
            core_api.create_namespaced_persistent_volume_claim(
                namespace=namespace, body=temp_pvc
            )
        except ApiException as e:
            if e.status != 409:
                raise

        export_job = build_export_job(
            job_name,
            namespace,
            temp_pvc_name,
            s3_key,
            s3_config,
        )
        try:
            batch_api.create_namespaced_job(namespace=namespace, body=export_job)
        except ApiException as e:
            if e.status != 409:
                raise

        export_jobs.append(
            {
                "jobName": f"export-{job_name}",
                "snapName": snap_name,
                "tempPvcName": temp_pvc_name,
                "diskId": disk_info["diskId"],
                "vmId": disk_info.get("vmId", ""),
                "s3Key": s3_key,
                "format": disk_info.get("format", "qcow2"),
                "virtualSizeBytes": size_gb * 1073741824,
            }
        )

    # Poll export Jobs until all complete (max 30 min)
    patch.status["captureProgress"] = f"Exporting {len(export_jobs)} disk(s) to S3"
    for attempt in range(180):
        all_done = True
        for ej in export_jobs:
            try:
                job = batch_api.read_namespaced_job(
                    name=ej["jobName"],
                    namespace=namespace,
                )
                if job.status.succeeded and job.status.succeeded >= 1:  # type: ignore[union-attr]
                    continue
                if job.status.failed and job.status.failed >= 3:  # type: ignore[union-attr]
                    logger.error(f"Export job {ej['jobName']} failed")
                    patch.status["phase"] = "CaptureError"
                    patch.status["captureError"] = f"Export job {ej['jobName']} failed"
                    return
                all_done = False
            except Exception:
                all_done = False
        if all_done:
            break
        await asyncio.sleep(10)

    # Read Job pod logs to get actual file sizes
    for ej in export_jobs:
        try:
            pod_items: list = getattr(
                core_api.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=f"job-name={ej['jobName']}",
                ),
                "items",
                [],
            )
            if pod_items:
                logs = str(
                    core_api.read_namespaced_pod_log(
                        name=pod_items[0].metadata.name,
                        namespace=namespace,
                    )
                )
                for line in logs.splitlines():
                    if line.startswith("DISK_SIZE_BYTES="):
                        ej["sizeBytes"] = int(line.split("=")[1])
        except Exception:
            pass

        captured_disks.append(
            {
                "diskId": ej["diskId"],
                "vmId": ej["vmId"],
                "s3Key": ej["s3Key"],
                "format": ej["format"],
                "sizeBytes": ej.get("sizeBytes", 0),
                "virtualSizeBytes": ej["virtualSizeBytes"],
            }
        )

    patch.status["capturedDisks"] = captured_disks
    patch.status["phase"] = "CaptureComplete"
    patch.status["captureProgress"] = "Done"
    logger.info(f"Pattern capture complete for {name}: {len(captured_disks)} disk(s)")

    # Cleanup temp PVCs and snapshots
    for ej in export_jobs:
        try:
            core_api.delete_namespaced_persistent_volume_claim(
                name=ej["tempPvcName"],
                namespace=namespace,
            )
        except Exception:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=ej["snapName"],
            )
        except Exception:
            pass
        try:
            batch_api.delete_namespaced_job(
                name=ej["jobName"],
                namespace=namespace,
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
    core_api = client.CoreV1Api()

    # Create recert SA for privileged Jobs (cert regeneration on RHCOS disks)
    try:
        core_api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-recert"),
            ),
        )
    except ApiException as e:
        if e.status != 409:
            raise
    try:
        scc = cast(
            dict[str, Any],
            custom_api.get_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name="troshka-privileged-jobs",
            ),
        )
        sa_ref = f"system:serviceaccount:{namespace}:troshka-recert"
        users = scc.get("users", []) or []
        if sa_ref not in users:
            users.append(sa_ref)
            custom_api.patch_cluster_custom_object(
                group="security.openshift.io",
                version="v1",
                plural="securitycontextconstraints",
                name="troshka-privileged-jobs",
                body={"users": users},
            )
    except Exception as e:
        logger.warning(f"Could not patch SCC for recert SA in {namespace}: {e}")

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
            "dnsRecords": net.get("dnsRecords", []),
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
        except ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 10 + int(20 * (i + 1) / max(len(networks), 1)),
            "stage": "Creating networks",
            "detail": f"{i + 1}/{len(networks)} networks",
        }

    # Create single gateway deployment for all externalAccess networks
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

    apps_api = client.AppsV1Api()
    if gateway_nads:
        _cleanup_legacy_pod(core_api, namespace, f"gateway-{namespace}")
        await _ensure_deployment_gone(apps_api, namespace, f"gateway-{namespace}")
        gw_dep = build_gateway_deployment(body, gateway_nads, gateway_ips)
        apps_api.create_namespaced_deployment(namespace=namespace, body=gw_dep)
        logger.info(f"Created gateway deployment for {name}")

    # Create SSH key Secret + exec pod attached to the first standard network
    cluster_nad = None
    cluster_cidr = "10.0.0.0/24"
    for net in networks:
        if net.get("networkType", "standard") != "bmc":
            cluster_nad = f"net-{net['id'][:8]}-nad"
            cluster_cidr = net.get("cidr", "10.0.0.0/24")
            break
    if cluster_nad:
        exec_ssh_key = spec.get("execSshKey", "")
        if exec_ssh_key:
            import base64 as _b64

            secret_body = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name="exec-ssh-key",
                    namespace=namespace,
                    owner_references=[
                        client.V1OwnerReference(
                            api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                            kind="TroshkaProject",
                            name=name,
                            uid=meta["uid"],
                            controller=True,
                        )
                    ],
                ),
                data={
                    "id_ed25519": _b64.b64encode(exec_ssh_key.encode()).decode(),
                },
            )
            try:
                core_api.create_namespaced_secret(namespace=namespace, body=secret_body)
                logger.info(f"Created exec SSH key secret for {name}")
            except ApiException as e:
                if e.status == 409:
                    core_api.replace_namespaced_secret(
                        name="exec-ssh-key",
                        namespace=namespace,
                        body=secret_body,
                    )
                    logger.info(f"Replaced exec SSH key secret for {name}")
                else:
                    raise

        exec_project_id = spec.get("projectId", namespace)[:8]
        _cleanup_legacy_pod(core_api, namespace, f"exec-{exec_project_id}")
        await _ensure_deployment_gone(apps_api, namespace, f"exec-{exec_project_id}")
        exec_dep = build_exec_deployment(
            body,
            cluster_nad,
            cidr=cluster_cidr,
            ssh_key_secret=(
                "exec-ssh-key" if exec_ssh_key else None
            ),  # pragma: allowlist secret
        )
        apps_api.create_namespaced_deployment(namespace=namespace, body=exec_dep)
        logger.info(f"Created exec deployment for {name}")

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
        central_s3_config = spec.get("centralS3Config", {})
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
        except ApiException as e:
            if e.status != 409:
                raise

        # Create S3 credential secrets in cache namespace for CDI source.s3
        for secret_name, cfg in [
            ("s3-credentials", s3_config),
            ("s3-central-credentials", central_s3_config),
        ]:
            if not cfg.get("accessKeyId"):
                continue
            try:
                core_api.create_namespaced_secret(
                    namespace=CACHE_NAMESPACE,
                    body=client.V1Secret(
                        metadata=client.V1ObjectMeta(name=secret_name),
                        string_data={
                            "accessKeyId": cfg.get("accessKeyId", ""),
                            "secretKey": cfg.get("secretKey", ""),
                        },
                    ),
                )
            except ApiException as e:
                if e.status == 409:
                    core_api.patch_namespaced_secret(
                        name=secret_name,
                        namespace=CACHE_NAMESPACE,
                        body=client.V1Secret(
                            string_data={
                                "accessKeyId": cfg.get("accessKeyId", ""),
                                "secretKey": cfg.get("secretKey", ""),
                            },
                        ),
                    )
                else:
                    raise

        patch.status["deployProgress"] = {
            "percent": 30,
            "stage": "Downloading images",
            "detail": f"0/{len(all_disks)} disks",
        }

        for disk in all_disks:
            s3_path = None
            use_central = False
            if disk.get("libraryImage", {}).get("s3Path"):
                s3_path = disk["libraryImage"]["s3Path"]
                use_central = disk["libraryImage"].get("central", False)
            elif disk.get("patternImage", {}).get("s3Path"):
                s3_path = disk["patternImage"]["s3Path"]
                use_central = disk["patternImage"].get("central", False)
            if not s3_path:
                continue

            if use_central and central_s3_config:
                disk_s3_config = central_s3_config
                secret_name = "s3-central-credentials"  # pragma: allowlist secret
            else:
                disk_s3_config = s3_config
                secret_name = "s3-credentials"  # pragma: allowlist secret

            pvc_name = golden_pvc_name(s3_path)
            try:
                core_api.read_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=CACHE_NAMESPACE
                )
                continue
            except ApiException as e:
                if e.status != 404:
                    raise

            size_gb = disk.get("sizeGb", 20)
            dv = build_datavolume_from_s3(
                pvc_name,
                CACHE_NAMESPACE,
                s3_path,
                size_gb,
                disk_s3_config,
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
                logger.info(f"Pre-created golden PVC {pvc_name} for parallel download")
            except ApiException as e:
                if e.status != 409:
                    raise

    patch.status["deployProgress"] = {
        "percent": 40,
        "stage": "Preparing disks",
        "detail": f"cloning {len(vms)} VMs",
    }

    # Find bastion boot disk PVC name for recert kubeconfig injection
    bastion_boot_pvc = None
    for vm in vms:
        if vm.get("name") == "bastion" and vm.get("os") != "rhcos":
            bastion_disks = vm_disks_map.get(vm["id"], [])
            if bastion_disks:
                bastion_boot_pvc = (
                    f"vm-{vm['id'][:8]}-disk-{bastion_disks[0].get('id', '')[:8]}"
                )

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
                "os": vm.get("os", ""),
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
        if vm.get("os") == "rhcos" and bastion_boot_pvc:
            vm_cr["spec"]["bastionPvc"] = bastion_boot_pvc

        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkavms",
                body=vm_cr,
            )
            logger.info(f"Created TroshkaVM {vm_name}")
        except ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 30 + int(60 * (i + 1) / max(len(vms), 1)),
            "stage": "Creating VMs",
            "detail": f"{i + 1}/{len(vms)} VMs",
        }

    # Create VNC console proxy (pod + service + route)
    from helpers.vnc import (
        build_vnc_proxy_deployment,
        build_vnc_service,
        build_vnc_route,
    )

    core_api = client.CoreV1Api()

    try:
        core_api.create_namespaced_service_account(
            namespace=namespace,
            body=client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name="troshka-vnc"),
            ),
        )
    except ApiException as e:
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
    except ApiException as e:
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
    except ApiException as e:
        if e.status != 409:
            raise

    apps_api = client.AppsV1Api()
    vnc_dep = build_vnc_proxy_deployment(name, namespace, owner_body=body)
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=vnc_dep)
        logger.info(f"Created VNC proxy deployment for {name}")
    except ApiException as e:
        if e.status != 409:
            raise

    vnc_svc = build_vnc_service(name, namespace, owner_body=body)
    try:
        core_api.create_namespaced_service(namespace=namespace, body=vnc_svc)
        logger.info(f"Created VNC proxy service for {name}")
    except ApiException as e:
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
    except ApiException as e:
        if e.status != 409:
            raise

    # Read back the route to get the assigned hostname
    try:
        route = cast(
            dict[str, Any],
            custom_api.get_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"vnc-proxy-{name}",
            ),
        )
        console_host = route.get("spec", {}).get("host", "")
        if not console_host:
            console_host = (
                route.get("status", {}).get("ingress", [{}])[0].get("host", "")
            )
        if console_host:
            patch.status["consoleRoute"] = console_host
            logger.info(f"Console route: {console_host}")
    except Exception as e:
        logger.warning(f"Could not read console route hostname: {e}")

    # Store recert config in CR status for the timer to pick up
    rhcos_vm = next((v for v in vms if v.get("os") == "rhcos"), None)
    is_pattern = rhcos_vm and any(
        d.get("patternImage") for d in vm_disks_map.get(rhcos_vm["id"], [])
    )
    if rhcos_vm and is_pattern:
        rhcos_disks = vm_disks_map.get(rhcos_vm["id"], [])
        rhcos_pvc = (
            f"vm-{rhcos_vm['id'][:8]}-disk-{rhcos_disks[0].get('id', '')[:8]}"
            if rhcos_disks
            else None
        )
        if rhcos_pvc:
            patch.status["recertConfig"] = {
                "rhcosPvc": rhcos_pvc,
                "bastionPvc": bastion_boot_pvc or "",
                "vmName": rhcos_vm.get("name", "cp-0"),
            }
            logger.info(f"Recert pending for {rhcos_pvc} (bastion: {bastion_boot_pvc})")

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
    vms = cast(
        dict[str, Any],
        custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkavms",
        ),
    )
    vm_items = vms.get("items", [])
    if not vm_items:
        return

    # Get actual KubeVirt VMI states (live truth)
    try:
        vmis = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
            ),
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
        # Phase 1: Recert (if needed) — runs while VMs are defined but stopped
        recert_cfg = status.get("recertConfig")
        if recert_cfg and not status.get("recertDone"):
            rhcos_pvc = recert_cfg.get("rhcosPvc", "")
            bastion_pvc_name = recert_cfg.get("bastionPvc", "")
            core_api = client.CoreV1Api()

            pvcs_ready = True
            for pvc_name in [rhcos_pvc, bastion_pvc_name]:
                if not pvc_name:
                    continue
                try:
                    pvc = core_api.read_namespaced_persistent_volume_claim(
                        name=pvc_name, namespace=namespace
                    )
                    if pvc.status.phase != "Bound":  # type: ignore[union-attr]
                        pvcs_ready = False
                except Exception:
                    pvcs_ready = False

            if not pvcs_ready:
                patch.status["deployProgress"] = {
                    "percent": 60,
                    "stage": "Preparing disks",
                    "detail": "waiting for disk clones",
                }
                return

            # Check if recert Job already exists
            from kubernetes import client as _kc

            batch_api = _kc.BatchV1Api()
            vm_part = rhcos_pvc.split("-disk-")[0] if "-disk-" in rhcos_pvc else "vm"
            recert_job_name = f"recert-{vm_part}"

            try:
                js = batch_api.read_namespaced_job(
                    name=recert_job_name, namespace=namespace
                )
                if js.status.succeeded:  # type: ignore[union-attr]
                    logger.info(f"Recert completed: {recert_job_name}")
                    _extract_kubeconfig_secret(
                        core_api, namespace, recert_job_name, name
                    )
                    patch.status["recertDone"] = True
                elif js.status.failed:  # type: ignore[union-attr]
                    logger.warning(f"Recert failed: {recert_job_name}")
                    patch.status["recertDone"] = True
                else:
                    patch.status["deployProgress"] = {
                        "percent": 75,
                        "stage": "Regenerating certificates",
                        "detail": f"recert on {recert_cfg.get('vmName', 'cp-0')}",
                    }
                return
            except ApiException as e:
                if e.status != 404:
                    return
            except Exception:
                return

            # Job doesn't exist — create it
            patch.status["deployProgress"] = {
                "percent": 70,
                "stage": "Regenerating certificates",
                "detail": f"starting recert on {recert_cfg.get('vmName', 'cp-0')}",
            }
            from helpers.kubevirt import build_recert_job

            recert_job = build_recert_job(
                vm_part,
                namespace,
                rhcos_pvc,
                bastion_pvc=bastion_pvc_name or None,
            )
            try:
                batch_api.create_namespaced_job(namespace=namespace, body=recert_job)
                logger.info(f"Created recert job {recert_job_name}")
            except Exception as e:
                logger.warning(f"Recert job creation failed: {e}")
                patch.status["recertDone"] = True
            return

        # Phase 2: Start VMs — patch running=true on all KubeVirt VMs
        if not status.get("vmsStarted"):
            started = 0
            for vm in vm_items:
                kv_name = vm.get("status", {}).get("kubevirtVmName", "")
                if not kv_name:
                    continue
                power_on = vm.get("spec", {}).get("powerOnAtDeploy", True)
                if not power_on:
                    started += 1
                    continue
                try:
                    custom_api.patch_namespaced_custom_object(
                        group="kubevirt.io",
                        version="v1",
                        namespace=namespace,
                        plural="virtualmachines",
                        name=kv_name,
                        body={"spec": {"running": True}},
                    )
                    started += 1
                except Exception:
                    pass
            if started == len(vm_items):
                patch.status["vmsStarted"] = True
                logger.info(f"TroshkaProject {name}: started {started} VMs")
            else:
                patch.status["deployProgress"] = {
                    "percent": 80,
                    "stage": "Starting VMs",
                    "detail": f"{started}/{len(vm_items)} started",
                }
                return

        # Phase 3: Wait for all VMs to be running
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
        f"TroshkaProject {name} deleting — cleaning up all resources in {namespace}"
    )
    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    # Force-delete VMIs first (immediate, no graceful shutdown wait)
    try:
        vmis = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
            ),
        )
        for vmi in vmis.get("items", []):
            try:
                custom_api.delete_namespaced_custom_object(
                    group="kubevirt.io",
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=vmi["metadata"]["name"],
                    grace_period_seconds=0,
                )
            except ApiException:
                pass
    except Exception:
        pass

    try:
        kv_vms = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="kubevirt.io",
                version="v1",
                namespace=namespace,
                plural="virtualmachines",
            ),
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
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete KubeVirt VM {vm_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list KubeVirt VMs in {namespace}: {e}")

    try:
        dvs = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="cdi.kubevirt.io",
                version="v1beta1",
                namespace=namespace,
                plural="datavolumes",
            ),
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
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete DataVolume {dv_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list DataVolumes in {namespace}: {e}")

    try:
        nads = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="k8s.cni.cncf.io",
                version="v1",
                namespace=namespace,
                plural="network-attachment-definitions",
            ),
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
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete NAD {nad_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list NADs in {namespace}: {e}")

    try:
        routes = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group="route.openshift.io",
                version="v1",
                namespace=namespace,
                plural="routes",
            ),
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
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete Route {rt_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to list Routes in {namespace}: {e}")

    sa_ref = f"system:serviceaccount:{namespace}:troshka-network"
    for scc_name in ("troshka-network-pods", "troshka-gateway"):
        try:
            scc = cast(
                dict[str, Any],
                custom_api.get_cluster_custom_object(
                    group="security.openshift.io",
                    version="v1",
                    plural="securitycontextconstraints",
                    name=scc_name,
                ),
            )
            users = scc.get("users", []) or []
            if sa_ref in users:
                users.remove(sa_ref)
                custom_api.patch_cluster_custom_object(
                    group="security.openshift.io",
                    version="v1",
                    plural="securitycontextconstraints",
                    name=scc_name,
                    body={"users": users},
                )
                logger.info(f"Removed {sa_ref} from {scc_name} SCC")
        except Exception as e:
            logger.warning(f"Could not clean SCC {scc_name} for {namespace}: {e}")

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
