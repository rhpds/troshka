import asyncio
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


async def _ensure_deployment_gone(apps_api, namespace, dep_name, max_wait=30):
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

    try:
        async with asyncio.timeout(max_wait):
            while True:
                try:
                    apps_api.read_namespaced_deployment(
                        name=dep_name, namespace=namespace
                    )
                    await asyncio.sleep(1)
                except ApiException as e:
                    if e.status == 404:
                        return
                    raise
    except TimeoutError:
        logger.warning(f"Deployment {dep_name} not fully gone after {max_wait}s")


def _cleanup_recert_job(core_api, batch_api, namespace, job_name):
    try:
        pod_list = core_api.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={job_name}"
        )
        for pod in pod_list.items or []:
            try:
                core_api.delete_namespaced_pod(
                    name=pod.metadata.name, namespace=namespace
                )
            except Exception:
                pass
    except Exception:
        pass
    try:
        batch_api.delete_namespaced_job(
            name=job_name, namespace=namespace, propagation_policy="Background"
        )
    except Exception:
        pass


def _extract_kubeconfig_secret(
    core_api, namespace, job_name, project_name, vm_name=None
) -> str | None:
    """Read kubeconfig from recert Job logs and create Secret(s).

    Creates ocp-kubeconfig (default) and ocp-kubeconfig-{vm_name} if vm_name given.
    Returns None on success, or an error message string on failure.
    """
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
            return f"No pods found for recert job {job_name}"
        logs = core_api.read_namespaced_pod_log(
            name=pods[0].metadata.name,
            namespace=namespace,
            tail_lines=100,
        )
        logs_str = logs if isinstance(logs, str) else str(logs or "")
        logs_str = logs_str.replace("\\n", "\n")
        m = re.search(r"KUBECONFIG_B64_BEGIN\s+(\S+)\s+KUBECONFIG_B64_END", logs_str)
        if not m:
            return "No kubeconfig marker found in recert job logs"
        kc_data = m.group(1).strip()
        _b64.b64decode(kc_data)

        secret_names = ["ocp-kubeconfig"]
        if vm_name:
            secret_names.append(f"ocp-kubeconfig-{vm_name}")
        for secret_name in secret_names:
            secret_body = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": secret_name, "namespace": namespace},
                "data": {"config": kc_data},
            }
            try:
                core_api.create_namespaced_secret(namespace=namespace, body=secret_body)
                logger.info(
                    f"Created kubeconfig Secret {secret_name} for {project_name}"
                )
            except ApiException as e:
                if e.status == 409:
                    core_api.replace_namespaced_secret(
                        name=secret_name, namespace=namespace, body=secret_body
                    )
                else:
                    return f"Failed to create kubeconfig secret: {e}"
    except Exception as e:
        return f"Failed to extract kubeconfig: {e}"
    return None


CAPTURE_ANNOTATION = "troshka.redhat.com/capture-request"


async def _stop_all_vms(custom_api, namespace):
    """Stop all KubeVirt VMs and wait for VMIs to be gone (max 120s)."""
    vms = cast(
        dict[str, Any],
        custom_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkavms",
        ),
    )
    for vm_item in vms.get("items", []):
        kv_name = vm_item.get("status", {}).get(
            "kubevirtVmName", f"troshka-{vm_item['metadata']['name']}"
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

    for _ in range(40):
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


async def _snapshot_and_export_disk(
    disk_info, s3_config, custom_api, core_api, batch_api, namespace, name, patch
):
    """Snapshot a single disk PVC, create temp PVC, and launch export job.

    Returns the export job metadata dict.
    """
    from helpers.patterns import (
        build_volume_snapshot,
        build_temp_pvc_from_snapshot,
        build_export_job,
    )

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

    return {
        "jobName": f"export-{job_name}",
        "snapName": snap_name,
        "tempPvcName": temp_pvc_name,
        "diskId": disk_info["diskId"],
        "vmId": disk_info.get("vmId", ""),
        "s3Key": s3_key,
        "format": disk_info.get("format", "qcow2"),
        "virtualSizeBytes": size_gb * 1073741824,
    }


def _check_export_job(batch_api, ej, namespace):
    """Check a single export Job status.

    Returns: "done" if succeeded, "failed" if permanently failed, "pending" otherwise.
    """
    try:
        job = batch_api.read_namespaced_job(
            name=ej["jobName"],
            namespace=namespace,
        )
        if job.status.succeeded and job.status.succeeded >= 1:  # type: ignore[union-attr]
            return "done"
        if job.status.failed and job.status.failed >= 3:  # type: ignore[union-attr]
            return "failed"
        return "pending"
    except Exception:
        return "pending"


async def _poll_export_jobs(batch_api, export_jobs, namespace, patch):
    """Poll export Jobs until all complete (max 30 min).

    Returns None on success, or sets CaptureError phase and returns error string.
    """
    patch.status["captureProgress"] = f"Exporting {len(export_jobs)} disk(s) to S3"
    for _ in range(180):
        all_done = True
        for ej in export_jobs:
            result = _check_export_job(batch_api, ej, namespace)
            if result == "done":
                continue
            if result == "failed":
                logger.error(f"Export job {ej['jobName']} failed")
                patch.status["phase"] = "CaptureError"
                patch.status["captureError"] = f"Export job {ej['jobName']} failed"
                return f"Export job {ej['jobName']} failed"
            all_done = False
        if all_done:
            break
        await asyncio.sleep(10)
    return None


def _read_export_sizes(core_api, export_jobs, namespace):
    """Read actual file sizes from export Job pod logs."""
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


def _cleanup_capture_resources(core_api, custom_api, batch_api, export_jobs, namespace):
    """Cleanup temp PVCs, snapshots, and export Jobs."""
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


async def _handle_capture(capture_config, namespace, name, patch):
    """Handle pattern capture: stop VMs, snapshot disks, export to S3."""
    s3_config = capture_config.get("s3Config", {})
    disk_manifest = capture_config.get("disks", [])

    patch.status["phase"] = "Capturing"
    patch.status["captureProgress"] = "Stopping VMs"

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    await _stop_all_vms(custom_api, namespace)

    # Snapshot and export each disk
    export_jobs = []
    for disk_info in disk_manifest:
        ej = await _snapshot_and_export_disk(
            disk_info,
            s3_config,
            custom_api,
            core_api,
            batch_api,
            namespace,
            name,
            patch,
        )
        export_jobs.append(ej)

    err = await _poll_export_jobs(batch_api, export_jobs, namespace, patch)
    if err:
        return

    _read_export_sizes(core_api, export_jobs, namespace)

    captured_disks = [
        {
            "diskId": ej["diskId"],
            "vmId": ej["vmId"],
            "s3Key": ej["s3Key"],
            "format": ej["format"],
            "sizeBytes": ej.get("sizeBytes", 0),
            "virtualSizeBytes": ej["virtualSizeBytes"],
        }
        for ej in export_jobs
    ]

    patch.status["capturedDisks"] = captured_disks
    patch.status["phase"] = "CaptureComplete"
    patch.status["captureProgress"] = "Done"
    logger.info(f"Pattern capture complete for {name}: {len(captured_disks)} disk(s)")

    _cleanup_capture_resources(core_api, custom_api, batch_api, export_jobs, namespace)


def _setup_recert_sa(core_api, custom_api, namespace):
    """Create recert SA and patch SCC for privileged Jobs."""
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


def _create_network_crs(
    custom_api, networks, static_leases, namespace, name, body, patch
):
    """Create TroshkaNetwork CRs for each network in the topology."""
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


async def _setup_gateway(core_api, apps_api, networks, namespace, name, body):
    """Create gateway deployment for externalAccess networks."""
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
        port_forwards = _extract_port_forwards(body)
        _cleanup_legacy_pod(core_api, namespace, f"gateway-{namespace}")
        await _ensure_deployment_gone(apps_api, namespace, f"gateway-{namespace}")
        gw_dep = build_gateway_deployment(
            body, gateway_nads, gateway_ips, port_forwards=port_forwards
        )
        apps_api.create_namespaced_deployment(namespace=namespace, body=gw_dep)
        logger.info(f"Created gateway deployment for {name}")


def _extract_port_forwards(project_cr):
    """Extract port forwards from the topology in the TroshkaProject CR."""
    topology = project_cr.get("spec", {}).get("topology", {})
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if data.get("subtype") == "gateway":
            return data.get("portForwards", [])
    return []


async def _setup_exec_pod(
    core_api, apps_api, spec, meta, networks, namespace, name, body
):
    """Create SSH key Secret and exec deployment attached to the first standard network."""
    cluster_nad = None
    cluster_cidr = "10.0.0.0/24"
    for net in networks:
        if net.get("networkType", "standard") != "bmc":
            cluster_nad = f"net-{net['id'][:8]}-nad"
            cluster_cidr = net.get("cidr", "10.0.0.0/24")
            break
    if not cluster_nad:
        return

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


def _ensure_cache_namespace_and_secrets(core_api, s3_config, central_s3_config):
    """Ensure cache namespace exists and S3 credential secrets are up to date."""
    from helpers.kubevirt import CACHE_NAMESPACE

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

    for secret_name, cfg in [
        ("s3-credentials", s3_config),
        ("s3-central-credentials", central_s3_config),
    ]:
        if not cfg.get("accessKeyId"):
            continue
        _upsert_s3_secret(core_api, CACHE_NAMESPACE, secret_name, cfg)


def _upsert_s3_secret(core_api, namespace, secret_name, cfg):
    """Create or update an S3 credential secret."""
    string_data = {
        "accessKeyId": cfg.get("accessKeyId", ""),
        "secretKey": cfg.get("secretKey", ""),
    }
    try:
        core_api.create_namespaced_secret(
            namespace=namespace,
            body=client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name),
                string_data=string_data,
            ),
        )
    except ApiException as e:
        if e.status == 409:
            core_api.patch_namespaced_secret(
                name=secret_name,
                namespace=namespace,
                body=client.V1Secret(string_data=string_data),
            )
        else:
            raise


def _create_golden_pvc_for_disk(
    custom_api, core_api, disk, s3_config, central_s3_config
):
    """Create a single golden PVC for a disk if it doesn't already exist."""
    from helpers.kubevirt import build_datavolume_from_s3, CACHE_NAMESPACE
    from helpers.k8s import golden_pvc_name

    s3_path, use_central = _resolve_disk_s3_path(disk)
    if not s3_path:
        return

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
        return
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


def _precreate_golden_pvcs(custom_api, core_api, spec, all_disks, patch):
    """Pre-create golden PVCs for parallel image downloads."""
    s3_config = spec.get("s3Config", {})
    central_s3_config = spec.get("centralS3Config", {})

    _ensure_cache_namespace_and_secrets(core_api, s3_config, central_s3_config)

    patch.status["deployProgress"] = {
        "percent": 30,
        "stage": "Downloading images",
        "detail": f"0/{len(all_disks)} disks",
    }

    for disk in all_disks:
        _create_golden_pvc_for_disk(
            custom_api, core_api, disk, s3_config, central_s3_config
        )


def _resolve_disk_s3_path(disk):
    """Extract S3 path and central flag from a disk spec.

    Returns (s3_path, use_central) tuple.
    """
    if disk.get("libraryImage", {}).get("s3Path"):
        return disk["libraryImage"]["s3Path"], disk["libraryImage"].get(
            "central", False
        )
    if disk.get("patternImage", {}).get("s3Path"):
        return disk["patternImage"]["s3Path"], disk["patternImage"].get(
            "central", False
        )
    return None, False


def _build_vm_cr(
    vm,
    vm_disks_map,
    vm_cdroms_map,
    nic_network_map,
    bastion_boot_pvc,
    namespace,
    name,
    body,
):
    """Build a TroshkaVM CR dict for a single VM."""
    vm_name = f"vm-{vm['id'][:8]}"
    disk_specs = vm_disks_map.get(vm["id"], [])

    nic_specs = []
    for nic in vm.get("nics", []):
        nic_id = nic.get("id", "")
        nic_specs.append(
            {
                "id": nic_id,
                "mac": nic.get("mac", ""),
                "model": nic.get("model", "virtio"),
                "networkRef": nic_network_map.get(nic_id, ""),
            }
        )

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
            "bmcIp": vm.get("bmcIp", ""),
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
    return vm_cr


def _setup_vnc_proxy(custom_api, core_api, namespace, name, body, patch):
    """Create VNC console proxy SA, RBAC, deployment, service, and route."""
    from helpers.vnc import (
        build_vnc_proxy_deployment,
        build_vnc_service,
        build_vnc_route,
    )

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

    _create_vnc_rbac(namespace)

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


def _create_vnc_rbac(namespace):
    """Create VNC Role and RoleBinding for KubeVirt VNC subresource access."""
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


def _collect_recert_configs(vms, vm_disks_map, bastion_boot_pvc):
    """Collect recert configs for VMs that need certificate regeneration."""
    recert_configs = []
    for vm in vms:
        if not vm.get("recertEnabled"):
            continue
        vm_disks = vm_disks_map.get(vm["id"], [])
        if not any(d.get("patternImage") for d in vm_disks):
            continue
        if not vm_disks:
            continue
        rhcos_pvc = f"vm-{vm['id'][:8]}-disk-{vm_disks[0].get('id', '')[:8]}"
        recert_configs.append(
            {
                "rhcosPvc": rhcos_pvc,
                "bastionPvc": bastion_boot_pvc or "",
                "vmName": vm.get("name", vm["id"][:8]),
            }
        )
    return recert_configs


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

    _setup_recert_sa(core_api, custom_api, namespace)

    networks = extract_networks(topology)
    static_leases = build_static_leases(topology)

    patch.status["deployProgress"] = {
        "percent": 10,
        "stage": "Creating networks",
        "detail": f"0/{len(networks)} networks",
    }

    _create_network_crs(
        custom_api, networks, static_leases, namespace, name, body, patch
    )

    apps_api = client.AppsV1Api()
    await _setup_gateway(core_api, apps_api, networks, namespace, name, body)
    await _setup_exec_pod(
        core_api, apps_api, spec, meta, networks, namespace, name, body
    )

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
        _precreate_golden_pvcs(custom_api, core_api, spec, all_disks, patch)

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
        vm_cr = _build_vm_cr(
            vm,
            vm_disks_map,
            vm_cdroms_map,
            nic_network_map,
            bastion_boot_pvc,
            namespace,
            name,
            body,
        )
        try:
            custom_api.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkavms",
                body=vm_cr,
            )
            logger.info(f"Created TroshkaVM {vm_cr['metadata']['name']}")
        except ApiException as e:
            if e.status != 409:
                raise

        patch.status["deployProgress"] = {
            "percent": 30 + int(60 * (i + 1) / max(len(vms), 1)),
            "stage": "Creating VMs",
            "detail": f"{i + 1}/{len(vms)} VMs",
        }

    _setup_vnc_proxy(custom_api, core_api, namespace, name, body, patch)

    recert_configs = _collect_recert_configs(vms, vm_disks_map, bastion_boot_pvc)
    if recert_configs:
        patch.status["recertConfig"] = recert_configs
        patch.status["recertIndex"] = 0
        logger.info(
            f"Recert pending for {len(recert_configs)} VM(s): "
            + ", ".join(c["vmName"] for c in recert_configs)
        )

    patch.status["phase"] = "Deploying"
    patch.status["deployProgress"] = {
        "percent": 90,
        "stage": "Waiting for VMs",
        "detail": f"0/{len(vms)} VMs ready",
    }
    logger.info(f"TroshkaProject {name} CRs created, waiting for VMs")


def _resolve_vm_state(vm, vmi_states):
    """Resolve a single VM's state from VMI states and CR status."""
    kv_name = vm.get("status", {}).get("kubevirtVmName", "")
    if kv_name and kv_name in vmi_states:
        return vmi_states[kv_name]
    state = vm.get("status", {}).get("state", "")
    if not state:
        return "creating"
    if kv_name and kv_name not in vmi_states and state == "Running":
        return "Stopped"
    return state


def _detect_scheduling_error(core_api, namespace, kv_name):
    """Check if a Scheduling VM has unschedulable pods or volume attach failures.

    Returns error message string or None.
    """
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"kubevirt.io/domain={kv_name}",
        )
        for pod in pods.items or []:  # type: ignore[union-attr]
            for cond in pod.status.conditions or []:  # type: ignore[union-attr]
                if cond.reason == "Unschedulable":
                    return cond.message or "Unschedulable"
            ev_list = core_api.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod.metadata.name},reason=FailedAttachVolume",  # type: ignore[union-attr]
            )
            for ev in ev_list.items or []:  # type: ignore[union-attr]
                return ev.message or "Volume attach failed"
    except Exception:
        pass
    return None


def _collect_vm_states(vm_items, vmi_states, core_api, namespace):
    """Collect VM states and scheduling errors from VMI states and pod conditions.

    Returns (vm_states dict, ready_count, scheduling_errors dict).
    """
    vm_states = {}
    ready_count = 0
    scheduling_errors = {}

    for vm in vm_items:
        vm_id = vm.get("spec", {}).get("vmId", vm["metadata"]["name"])
        kv_name = vm.get("status", {}).get("kubevirtVmName", "")
        state = _resolve_vm_state(vm, vmi_states)

        if state == "Scheduling" and kv_name:
            err = _detect_scheduling_error(core_api, namespace, kv_name)
            if err:
                scheduling_errors[vm_id] = err
                state = "error"

        vm_states[vm_id] = state
        if state in ("Running", "Stopped"):
            ready_count += 1

    return vm_states, ready_count, scheduling_errors


def _recert_job_name_from_cfg(cfg):
    """Derive the recert job name from a recert config entry."""
    rhcos_pvc = cfg.get("rhcosPvc", "")
    vm_part = rhcos_pvc.split("-disk-")[0] if "-disk-" in rhcos_pvc else "vm"
    return f"recert-{vm_part}", vm_part, cfg.get("vmName", "vm")


def _check_recert_pvcs_ready(core_api, recert_cfgs, namespace):
    """Check if all recert PVCs are Bound. Returns True if all ready."""
    for cfg in recert_cfgs:
        pvc_name = cfg.get("rhcosPvc", "")
        if not pvc_name:
            continue
        try:
            pvc = core_api.read_namespaced_persistent_volume_claim(
                name=pvc_name, namespace=namespace
            )
            if pvc.status.phase != "Bound":  # type: ignore[union-attr]
                return False
        except Exception:
            return False
    return True


def _create_recert_jobs(batch_api, recert_cfgs, namespace):
    """Create recert Jobs for all configs. Returns error string or None."""
    from helpers.kubevirt import build_recert_job

    for cfg in recert_cfgs:
        job_name, vm_part, vm_label = _recert_job_name_from_cfg(cfg)
        rhcos_pvc = cfg.get("rhcosPvc", "")
        try:
            batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                job = build_recert_job(
                    vm_part,
                    namespace,
                    rhcos_pvc,
                    vm_name=vm_label,
                )
                try:
                    batch_api.create_namespaced_job(namespace=namespace, body=job)
                    logger.info(f"Created recert job {job_name}")
                except Exception as ce:
                    logger.error(f"Recert job creation failed for {vm_label}: {ce}")
                    return f"Failed to create recert job: {ce}"
    return None


def _poll_recert_jobs(batch_api, recert_cfgs, namespace, status, patch):
    """Poll recert Jobs. Returns (all_done, should_return).

    should_return=True means the caller should return immediately (retry or error).
    """
    done_count = 0
    running_names = []
    for i, cfg in enumerate(recert_cfgs):
        job_name, _, vm_label = _recert_job_name_from_cfg(cfg)
        try:
            js = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
            if js.status.succeeded:  # type: ignore[union-attr]
                done_count += 1
            elif js.status.failed:  # type: ignore[union-attr]
                attempts_key = f"recertAttempts_{i}"
                attempts = status.get(attempts_key, 0) + 1
                if attempts < 3:
                    logger.warning(
                        f"Recert failed for {vm_label} (attempt {attempts}/3)"
                    )
                    patch.status[attempts_key] = attempts
                    try:
                        batch_api.delete_namespaced_job(
                            name=job_name,
                            namespace=namespace,
                            propagation_policy="Background",
                        )
                    except Exception:
                        pass
                    return False, True
                patch.status["phase"] = "Error"
                patch.status["error"] = (
                    f"Certificate regeneration failed for {vm_label} after 3 attempts"
                )
                return False, True
            else:
                running_names.append(vm_label)
        except ApiException:
            running_names.append(vm_label)

    if done_count < len(recert_cfgs):
        patch.status["deployProgress"] = {
            "percent": 70 + int(10 * done_count / len(recert_cfgs)),
            "stage": "Regenerating certificates",
            "detail": f"recert {done_count}/{len(recert_cfgs)} ({', '.join(running_names)})",
        }
        return False, True

    return True, False


def _finalize_recert(core_api, batch_api, recert_cfgs, namespace, name):
    """Extract kubeconfigs and clean up recert Jobs."""
    logger.info(f"All {len(recert_cfgs)} recert jobs done for {name}")
    for cfg in recert_cfgs:
        job_name, _, vm_label = _recert_job_name_from_cfg(cfg)
        err = _extract_kubeconfig_secret(
            core_api,
            namespace,
            job_name,
            name,
            vm_name=vm_label,
        )
        if err:
            logger.warning(f"Kubeconfig extraction failed for {vm_label}: {err}")
        _cleanup_recert_job(core_api, batch_api, namespace, job_name)


def _find_stale_volume_attachments(storage_api, core_api, namespace):
    """Find VolumeAttachments in the namespace that are not used by any running pod.

    Returns list of stale VolumeAttachment names.
    """
    pvc_list = core_api.list_namespaced_persistent_volume_claim(namespace=namespace)
    pv_names = {}
    for pvc in pvc_list.items or []:  # type: ignore[union-attr]
        vol = pvc.spec.volume_name  # type: ignore[union-attr]
        if vol:
            pv_names[vol] = pvc.metadata.name  # type: ignore[union-attr]
    if not pv_names:
        return []

    attachments = storage_api.list_volume_attachment()
    stale = []
    for va in attachments.items or []:  # type: ignore[union-attr]
        pv = va.spec.source.persistent_volume_name  # type: ignore[union-attr]
        if pv not in pv_names:
            continue
        node = va.spec.node_name  # type: ignore[union-attr]
        if not _pod_uses_pvc_on_node(core_api, namespace, node, pv_names[pv]):
            stale.append(va.metadata.name)  # type: ignore[union-attr]
    return stale


def _pod_uses_pvc_on_node(core_api, namespace, node, pvc_name):
    """Check if any non-terminating pod on the given node uses the PVC."""
    try:
        node_pods = core_api.list_namespaced_pod(
            namespace=namespace,
            field_selector=f"spec.nodeName={node}",
        )
        for p in node_pods.items or []:  # type: ignore[union-attr]
            if p.metadata.deletion_timestamp:  # type: ignore[union-attr]
                continue
            for v in p.spec.volumes or []:  # type: ignore[union-attr]
                claim = getattr(v, "persistent_volume_claim", None)
                if claim and claim.claim_name == pvc_name:
                    return True
    except Exception:
        pass
    return False


def _start_kubevirt_vms(custom_api, vm_items, namespace):
    """Patch running=true on all KubeVirt VMs. Returns count of started VMs."""
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
    return started


@kopf.timer(CRD_GROUP, CRD_VERSION, "troshkaprojects", interval=10, idle=10)
async def project_status_check(status, namespace, name, patch, **_):
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

    core_api_ev = client.CoreV1Api()
    vm_states, ready_count, scheduling_errors = _collect_vm_states(
        vm_items, vmi_states, core_api_ev, namespace
    )

    old_states = status.get("vmStates", {})
    if vm_states != old_states:
        patch.status["vmStates"] = vm_states
    if scheduling_errors:
        old_errors = status.get("schedulingErrors", {})
        if scheduling_errors != old_errors:
            patch.status["schedulingErrors"] = scheduling_errors
            for vm_id, msg in scheduling_errors.items():
                logger.warning(f"VM {vm_id} scheduling error: {msg}")

    if phase == "Running":
        _ensure_bmc_deployment(vm_items, namespace)

    if phase == "Deploying":
        # Phase 1: Recert (if needed) — all Jobs run in parallel
        recert_cfgs = status.get("recertConfig")
        if isinstance(recert_cfgs, list) and not status.get("recertDone"):
            core_api = client.CoreV1Api()
            batch_api = client.BatchV1Api()

            if not _check_recert_pvcs_ready(core_api, recert_cfgs, namespace):
                patch.status["deployProgress"] = {
                    "percent": 60,
                    "stage": "Preparing disks",
                    "detail": "waiting for disk clones",
                }
                return

            err = _create_recert_jobs(batch_api, recert_cfgs, namespace)
            if err:
                patch.status["phase"] = "Error"
                patch.status["error"] = err
                return

            all_done, should_return = _poll_recert_jobs(
                batch_api, recert_cfgs, namespace, status, patch
            )
            if should_return:
                return

            if all_done:
                _finalize_recert(core_api, batch_api, recert_cfgs, namespace, name)
                patch.status["recertDone"] = True

        # Phase 1.5: Wait for final recert cleanup (legacy compat)
        if status.get("recertDone") and not status.get("recertCleaned"):
            patch.status["recertCleaned"] = True

        # Phase 2: Start VMs — patch running=true on all KubeVirt VMs
        if status.get("recertConfig") and not status.get("recertCleaned"):
            return
        if not status.get("vmsStarted"):
            # Clean up stale VolumeAttachments left by CDI clones before starting
            storage_api = client.StorageV1Api()
            core_api_pvc = client.CoreV1Api()
            try:
                stale = _find_stale_volume_attachments(
                    storage_api, core_api_pvc, namespace
                )
                for va_name in stale:
                    try:
                        storage_api.delete_volume_attachment(name=va_name)
                        logger.info(f"Deleted stale VolumeAttachment {va_name}")
                    except Exception:
                        pass
                if stale:
                    patch.status["deployProgress"] = {
                        "percent": 79,
                        "stage": "Releasing disks",
                        "detail": f"detached {len(stale)} stale volume(s)",
                    }
                    return
            except Exception as e:
                logger.warning(f"PVC release check failed for {name}: {e}")

            started = _start_kubevirt_vms(custom_api, vm_items, namespace)
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


def _ensure_bmc_deployment(vm_items, namespace):
    """Verify BMC deployment exists if any VM has bmcEnabled. Recreate if missing."""
    bmc_vms = []
    for vm in vm_items:
        spec = vm.get("spec", {})
        if spec.get("bmcEnabled"):
            bmc_vms.append(
                {
                    "vmId": spec.get("vmId", ""),
                    "smbiosUuid": spec.get("smbiosUuid", ""),
                    "bmcIp": spec.get("bmcIp", ""),
                }
            )
    if not bmc_vms:
        return

    apps_api = client.AppsV1Api()
    project_label = namespace.replace("troshka-", "")
    dep_name = f"bmc-{project_label}"
    try:
        apps_api.read_namespaced_deployment(name=dep_name, namespace=namespace)
        return
    except ApiException as e:
        if e.status != 404:
            return

    from handlers.vm import _ensure_bmc_sa_and_rbac, _find_bmc_nad
    from helpers.bmc import build_bmc_deployment

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    # Enrich bmcIp from topology when VM CRs have empty values
    if any(not v["bmcIp"] for v in bmc_vms):
        topo_ips = _get_bmc_ips_from_topology(custom_api, namespace)
        for v in bmc_vms:
            if not v["bmcIp"] and v["vmId"] in topo_ips:
                v["bmcIp"] = topo_ips[v["vmId"]]

    _ensure_bmc_sa_and_rbac(namespace, core_api, custom_api)

    bmc_nad = _find_bmc_nad(namespace, custom_api)
    if not bmc_nad:
        return

    credentials = _get_bmc_credentials(custom_api, namespace)
    bmc_dep = build_bmc_deployment(
        project_label, namespace, bmc_vms, bmc_nad, credentials
    )
    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=bmc_dep)
        logger.info(f"Recreated missing BMC deployment for {namespace}")
    except ApiException as e:
        if e.status != 409:
            logger.warning(f"Failed to recreate BMC deployment for {namespace}: {e}")


def _get_bmc_credentials(custom_api, namespace):
    """Extract BMC credentials from the TroshkaProject CR topology."""
    try:
        projects = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
            ),
        )
        for proj in projects.get("items", []):
            topology = proj.get("spec", {}).get("topology", {})
            for node in topology.get("nodes", []):
                data = node.get("data", {})
                if data.get("networkType") == "bmc":
                    return {
                        "username": data.get("bmcUsername", ""),
                        "password": data.get("bmcPassword", ""),
                    }
    except Exception:
        pass
    return {}


def _get_bmc_ips_from_topology(custom_api, namespace):
    """Extract per-VM bmcIp from the TroshkaProject CR topology."""
    try:
        projects = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
            ),
        )
        for proj in projects.get("items", []):
            topology = proj.get("spec", {}).get("topology", {})
            ips = {}
            for node in topology.get("nodes", []):
                data = node.get("data", {})
                if node.get("type") == "vmNode" and data.get("bmcIp"):
                    vm_id = data.get("id", node.get("id", ""))
                    ips[vm_id] = data["bmcIp"]
            if ips:
                return ips
    except Exception:
        pass
    return {}


def _delete_custom_resources(
    custom_api,
    group,
    version,
    plural,
    namespace,
    resource_label="resource",
    grace_period=None,
):
    """List and delete all custom resources of a given type in a namespace."""
    try:
        resources = cast(
            dict[str, Any],
            custom_api.list_namespaced_custom_object(
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
            ),
        )
        for item in resources.get("items", []):
            item_name = item["metadata"]["name"]
            try:
                kwargs: dict[str, Any] = {
                    "group": group,
                    "version": version,
                    "namespace": namespace,
                    "plural": plural,
                    "name": item_name,
                }
                if grace_period is not None:
                    kwargs["grace_period_seconds"] = grace_period
                custom_api.delete_namespaced_custom_object(**kwargs)
                logger.info(f"Deleted {resource_label} {item_name}")
            except ApiException as e:
                if e.status != 404:
                    logger.warning(
                        f"Failed to delete {resource_label} {item_name}: {e}"
                    )
    except Exception as e:
        logger.warning(f"Failed to list {resource_label}s in {namespace}: {e}")


def _remove_sa_from_sccs(custom_api, namespace, sa_name, scc_names):
    """Remove a service account reference from the specified SCCs."""
    sa_ref = f"system:serviceaccount:{namespace}:{sa_name}"
    for scc_name in scc_names:
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


@kopf.on.delete(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_delete(namespace, name, **_):
    logger.info(
        f"TroshkaProject {name} deleting — cleaning up all resources in {namespace}"
    )
    custom_api = client.CustomObjectsApi()

    # Force-delete VMIs first (immediate, no graceful shutdown wait)
    _delete_custom_resources(
        custom_api,
        "kubevirt.io",
        "v1",
        "virtualmachineinstances",
        namespace,
        "VMI",
        grace_period=0,
    )
    _delete_custom_resources(
        custom_api,
        "kubevirt.io",
        "v1",
        "virtualmachines",
        namespace,
        "KubeVirt VM",
    )
    _delete_custom_resources(
        custom_api,
        "cdi.kubevirt.io",
        "v1beta1",
        "datavolumes",
        namespace,
        "DataVolume",
    )
    _delete_custom_resources(
        custom_api,
        "k8s.cni.cncf.io",
        "v1",
        "network-attachment-definitions",
        namespace,
        "NAD",
    )
    _delete_custom_resources(
        custom_api,
        "route.openshift.io",
        "v1",
        "routes",
        namespace,
        "Route",
    )

    _remove_sa_from_sccs(
        custom_api,
        namespace,
        "troshka-network",
        ("troshka-network-pods", "troshka-gateway"),
    )

    logger.info(f"TroshkaProject {name} cleanup complete")


@kopf.on.update(CRD_GROUP, CRD_VERSION, "troshkaprojects")
async def project_update(status, meta, namespace, name, patch, **_):
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
