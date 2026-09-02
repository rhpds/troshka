import logging
import os
import time

import yaml

from app.services.providers.base import ProviderDriver

logger = logging.getLogger(__name__)

CRD_GROUP = "troshka.redhat.com"
CRD_VERSION = "v1alpha1"
CACHE_NAMESPACE = "troshka-cache"
_KUBEMACPOOL_VM_IGNORE_LABEL = "mutatevirtualmachines.kubemacpool.io"
_KUBEVIRT_API_GROUP = "kubevirt.io"
_QEMU_SESSION_URI = "qemu:///session"
_ROUTE_API = "route.openshift.io"


def patch_kubevirt_run_strategy(custom_api, namespace, kv_name, strategy: str):
    """Set KubeVirt runStrategy, clearing deprecated spec.running if present."""
    patch_ops = [{"op": "add", "path": "/spec/runStrategy", "value": strategy}]
    try:
        vm = custom_api.get_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_name,
        )
        if vm.get("spec", {}).get("running") is not None:
            patch_ops.insert(0, {"op": "remove", "path": "/spec/running"})
    except Exception:
        pass
    custom_api.patch_namespaced_custom_object(
        group=_KUBEVIRT_API_GROUP,
        version="v1",
        namespace=namespace,
        plural="virtualmachines",
        name=kv_name,
        body=patch_ops,
        _content_type="application/json-patch+json",
    )


OPERATOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "operator"
)


def _operator_ns(provider):
    return provider.get_credentials().get("namespace", "troshka-operator")


def _project_ns(provider, project_id):
    creds = provider.get_credentials()
    prefix = creds.get("project_prefix", "troshka-")
    return f"{prefix}{project_id[:8]}"


def _project_namespace_labels(project_id: str) -> dict[str, str]:
    return {
        "app": "troshka",
        "troshka-project": project_id[:8],
        _KUBEMACPOOL_VM_IGNORE_LABEL: "ignore",
    }


_GW_BLOCKED_POD_PORTS = {80: 1080, 443: 1443, 8080: 18080}


def gateway_pod_listen_port(ext_port: int) -> int:
    """TCP port the gateway pod listens on for a given external/LB port.

    OpenShift/OVN rejects inbound connections to some pod-network ports, so the
    gateway socat proxy listens on alternate ports while the LoadBalancer still
    exposes the original external port.
    """
    return _GW_BLOCKED_POD_PORTS.get(ext_port, ext_port)


def ensure_showroom_cluster_service(
    provider, project_id: str, showroom_node_id: str
) -> str:
    """ClusterIP Service for showroom nginx; returns in-namespace DNS name."""
    _, core_api, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    pid = project_id[:8]
    ctr_short = showroom_node_id[:8]
    svc_name = f"showroom-{pid}"
    svc_body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": svc_name,
            "namespace": namespace,
            "labels": {
                "app": "troshka-showroom",
                "troshka-project": pid,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"troshka-pod": ctr_short},
            "ports": [
                {
                    "name": "http",
                    "port": 80,
                    "targetPort": 80,
                    "protocol": "TCP",
                }
            ],
        },
    }
    try:
        core_api.create_namespaced_service(namespace=namespace, body=svc_body)
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise
    return svc_name


def _get_k8s_clients(provider):
    from kubernetes import client

    creds = provider.get_credentials()
    config = client.Configuration()
    config.host = creds["api_url"]
    config.api_key = {"authorization": f"Bearer {creds['token']}"}
    config.verify_ssl = creds.get("verify_ssl", False)
    api_client = client.ApiClient(config)
    return (
        client.CustomObjectsApi(api_client),
        client.CoreV1Api(api_client),
        client.ApiClient(config),
    )


def _ensure_s3_secret(
    provider,
    namespace,
    s3_config,
    secret_name="s3-credentials",  # pragma: allowlist secret
):
    """Create or update S3 credentials Secret in the given namespace."""
    from kubernetes import client as k8s_client

    _, core_api, _ = _get_k8s_clients(provider)
    secret_data = {
        # CDI source.s3 expects these keys
        "accessKeyId": s3_config.get("access_key_id", ""),
        "secretKey": s3_config.get("secret_access_key", ""),
        # AWS env vars for export jobs
        "AWS_ACCESS_KEY_ID": s3_config.get("access_key_id", ""),
        "AWS_SECRET_ACCESS_KEY": s3_config.get("secret_access_key", ""),
        "AWS_DEFAULT_REGION": s3_config.get("region", "us-east-1"),
    }
    endpoint = s3_config.get("endpoint_url", "")
    if endpoint:
        secret_data["AWS_ENDPOINT_URL"] = endpoint

    try:
        core_api.create_namespaced_secret(
            namespace=namespace,
            body=k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(name=secret_name),
                string_data=secret_data,
            ),
        )
    except Exception as e:
        if "AlreadyExists" in str(e):
            core_api.patch_namespaced_secret(
                name=secret_name,
                namespace=namespace,
                body=k8s_client.V1Secret(string_data=secret_data),
            )
        else:
            raise


def _apply_ops_pod_secret(core_api, namespace: str, secret: dict) -> None:
    """Create-or-replace the ops-pod config Secret (idempotent)."""
    name = secret["metadata"]["name"]
    try:
        core_api.create_namespaced_secret(namespace=namespace, body=secret)
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise
        core_api.replace_namespaced_secret(name=name, namespace=namespace, body=secret)


def _apply_ops_pod(core_api, namespace: str, pod: dict) -> None:
    """Create the ops Pod, replacing any pre-existing one (pods are immutable)."""
    name = pod["metadata"]["name"]
    try:
        core_api.create_namespaced_pod(namespace=namespace, body=pod)
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise
        core_api.delete_namespaced_pod(name=name, namespace=namespace)
        core_api.create_namespaced_pod(namespace=namespace, body=pod)


def create_ops_pod(provider, project_id: str, pod: dict, secret: dict) -> str:
    """[LIVE-ENV] Create the ops-pod Secret + Pod in the project namespace.

    The manifests are built by the pure
    :func:`app.services.ocp.ops_pod_scaffold.build_ops_pod_kubevirt_manifests`;
    this only issues the live k8s calls. The Secret (per-cluster install/agent
    configs + pull secret) is created first so its volume mount resolves when the
    Pod starts. Returns the project namespace.
    """
    _, core_api, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    _apply_ops_pod_secret(core_api, namespace, secret)
    _apply_ops_pod(core_api, namespace, pod)
    logger.info(
        "Ops pod %s: created Pod %s + Secret in %s",
        project_id[:8],
        pod["metadata"]["name"],
        namespace,
    )
    return namespace


def _apply_crds(ext_api, operator_dir):
    """Load CRD YAML files and create-or-update them on the cluster."""
    from kubernetes.client.exceptions import ApiException

    crd_files = [
        os.path.join(operator_dir, "crds", "troshkaproject.yaml"),
        os.path.join(operator_dir, "crds", "troshkanetwork.yaml"),
        os.path.join(operator_dir, "crds", "troshkavm.yaml"),
    ]
    for crd_path in crd_files:
        with open(crd_path) as f:
            crd_body = yaml.safe_load(f)
        try:
            ext_api.create_custom_resource_definition(body=crd_body)
            logger.info(f"Created CRD {crd_body['metadata']['name']}")
        except ApiException as e:
            if e.status == 409:
                ext_api.patch_custom_resource_definition(
                    name=crd_body["metadata"]["name"], body=crd_body
                )
                logger.info(f"Updated CRD {crd_body['metadata']['name']}")
            else:
                raise


def _ensure_operator_crds(provider):
    """Create or patch Troshka CRDs before deploy/operator updates."""
    from kubernetes import client

    _, _, api_client = _get_k8s_clients(provider)
    ext_api = client.ApiextensionsV1Api(api_client)
    _apply_crds(ext_api, os.path.normpath(OPERATOR_DIR))


def _ensure_cache_s3_secrets(provider, s3_config, central_s3=None, obc_s3=None):
    """Mirror S3 credentials into troshka-cache for CDI golden-image imports."""
    from kubernetes import client as k8s_client

    _, core_api, _ = _get_k8s_clients(provider)
    try:
        core_api.create_namespace(
            body=k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(
                    name=CACHE_NAMESPACE,
                    labels={"app": "troshka-cache"},
                )
            )
        )
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise

    _ensure_s3_secret(provider, CACHE_NAMESPACE, s3_config, "s3-credentials")
    if central_s3:
        _ensure_s3_secret(
            provider, CACHE_NAMESPACE, central_s3, "s3-central-credentials"
        )
    if obc_s3:
        _ensure_s3_secret(provider, CACHE_NAMESPACE, obc_s3, "s3-obc-credentials")


def _try_existing_cluster_resource(kind, name, body, rbac_api):
    """Try to read/patch an existing cluster-scoped resource. Returns True if handled."""
    from kubernetes.client.exceptions import ApiException

    try:
        if kind == "ClusterRole":
            rbac_api.read_cluster_role(name=name)
            logger.info(f"ClusterRole {name} already exists, skipping")
            return True
        rbac_api.patch_cluster_role_binding(name=name, body=body)
        logger.info(f"ClusterRoleBinding {name} patched")
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _handle_create_conflict(kind, name, ns, body, apps_api):
    """Patch a Deployment on 409 Conflict during creation."""
    if kind == "Deployment":
        apps_api.patch_namespaced_deployment(name=name, namespace=ns, body=body)
    logger.info(f"Updated {kind} {name}")


def _apply_manifest(kind, name, ns, body, core_api, rbac_api, apps_api):
    """Create or patch a single Kubernetes manifest by kind."""
    from kubernetes.client.exceptions import ApiException

    if kind in ("ClusterRole", "ClusterRoleBinding"):
        if _try_existing_cluster_resource(kind, name, body, rbac_api):
            return

    try:
        if kind == "Namespace":
            core_api.create_namespace(body=body)
        elif kind == "ServiceAccount":
            core_api.create_namespaced_service_account(namespace=ns, body=body)
        elif kind == "ClusterRole":
            rbac_api.create_cluster_role(body=body)
        elif kind == "ClusterRoleBinding":
            rbac_api.create_cluster_role_binding(body=body)
        elif kind == "Deployment":
            apps_api.create_namespaced_deployment(namespace=ns, body=body)
        logger.info(f"Created {kind} {name}")
    except ApiException as e:
        if e.status == 409:
            _handle_create_conflict(kind, name, ns, body, apps_api)
        else:
            raise


def _deploy_operator(provider):
    from kubernetes import client

    _custom_api, core_api, api_client = _get_k8s_clients(provider)
    apps_api = client.AppsV1Api(api_client)
    rbac_api = client.RbacAuthorizationV1Api(api_client)
    ext_api = client.ApiextensionsV1Api(api_client)

    creds = provider.get_credentials()
    operator_ns = creds.get("namespace", "troshka-operator")

    operator_dir = os.path.normpath(OPERATOR_DIR)

    _apply_crds(ext_api, operator_dir)  # same helper as _ensure_operator_crds

    deploy_dir = os.path.join(operator_dir, "deploy")
    manifest_order = [
        "namespace.yaml",
        "serviceaccount.yaml",
        "clusterrole.yaml",
        "clusterrolebinding.yaml",
        "deployment.yaml",
    ]

    for filename in manifest_order:
        path = os.path.join(deploy_dir, filename)
        with open(path) as f:
            body = yaml.safe_load(f)

        kind = body["kind"]
        name = body["metadata"]["name"]
        ns = body["metadata"].get("namespace")

        if ns:
            body["metadata"]["namespace"] = operator_ns
            ns = operator_ns
        if kind == "Namespace":
            body["metadata"]["name"] = operator_ns
            name = operator_ns
        if kind == "ClusterRoleBinding":
            for subj in body.get("subjects", []):
                if subj.get("namespace"):
                    subj["namespace"] = operator_ns

        _apply_manifest(kind, name, ns, body, core_api, rbac_api, apps_api)

    logger.info("Operator deployed successfully")


def _stop_vms_gracefully(custom_api, namespace):
    """Halt all VMs so QEMU flushes I/O and releases RBD watchers."""
    try:
        vms = custom_api.list_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        for vm in dict(vms).get("items", []):  # type: ignore[call-overload]
            try:
                patch_kubevirt_run_strategy(
                    custom_api, namespace, vm["metadata"]["name"], "Halted"
                )
            except Exception:
                pass
    except Exception:
        pass


def _wait_for_vmis_terminated(custom_api, namespace):
    """Wait up to 90s for VMIs to terminate gracefully."""
    for _ in range(45):
        try:
            vmis = custom_api.list_namespaced_custom_object(
                group=_KUBEVIRT_API_GROUP,
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
            )
            if not dict(vmis).get("items", []):  # type: ignore[call-overload]
                break
        except Exception:
            break
        time.sleep(2)


def _force_delete_vmis(custom_api, namespace):
    """Force-delete any remaining VMIs that didn't stop gracefully."""
    try:
        vmis = custom_api.list_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachineinstances",
        )
        for vmi in dict(vmis).get("items", []):  # type: ignore[call-overload]
            try:
                custom_api.delete_namespaced_custom_object(
                    group=_KUBEVIRT_API_GROUP,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachineinstances",
                    name=vmi["metadata"]["name"],
                    grace_period_seconds=0,
                )
            except Exception:
                pass
    except Exception:
        pass


def _force_delete_virt_launcher_pods(core_api, namespace):
    """Force-delete virt-launcher pods directly."""
    try:
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector="kubevirt.io=virt-launcher",
        )
        for pod in getattr(pods, "items", []):
            try:
                core_api.delete_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )
            except Exception:
                pass
    except Exception:
        pass


def _wait_for_virt_launchers_gone(custom_api, core_api, namespace):
    """Wait for virt-launcher pods and VMIs to be fully gone."""
    for _ in range(30):
        try:
            pods = core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector="kubevirt.io=virt-launcher",
            )
            vmis = custom_api.list_namespaced_custom_object(
                group=_KUBEVIRT_API_GROUP,
                version="v1",
                namespace=namespace,
                plural="virtualmachineinstances",
            )
            if not getattr(pods, "items", []) and not dict(vmis).get("items", []):  # type: ignore[call-overload]
                break
        except Exception:
            break
        time.sleep(2)


def _collect_pv_names(core_api, namespace):
    """Collect PersistentVolume names bound to PVCs in the given namespace."""
    pvcs = core_api.list_namespaced_persistent_volume_claim(namespace=namespace)
    pv_names = set()
    for pvc in getattr(pvcs, "items", []):
        if pvc.spec.volume_name:
            pv_names.add(pvc.spec.volume_name)
    return pv_names


def _delete_detached_volume_attachments(storage_api, matching):
    """Delete VolumeAttachments that have completed CSI detach (attached=false)."""
    for va in matching:
        attached = getattr(getattr(va, "status", None), "attached", True)
        if not attached:
            try:
                storage_api.delete_volume_attachment(name=va.metadata.name)
            except Exception:
                pass


def _poll_and_cleanup_attachments(storage_api, pv_names):
    """Poll for VolumeAttachments matching the given PV names and clean up detached ones."""
    for _ in range(30):
        was = storage_api.list_volume_attachment()
        matching = [
            va
            for va in getattr(was, "items", [])
            if getattr(va.spec.source, "persistent_volume_name", None) in pv_names
        ]
        if not matching:
            break
        _delete_detached_volume_attachments(storage_api, matching)
        time.sleep(2)


def _cleanup_volume_attachments(core_api, namespace):
    """Clean up cluster-scoped VolumeAttachments that survive namespace deletion.

    Only deletes attachments whose CSI detach has completed (status.attached=false)
    to avoid confusing the attach-detach controller on RWO volumes.
    """
    try:
        from kubernetes import client as _kc

        storage_api = _kc.StorageV1Api(core_api.api_client)
        pv_names = _collect_pv_names(core_api, namespace)
        if pv_names:
            _poll_and_cleanup_attachments(storage_api, pv_names)
    except Exception:
        pass


def _delete_vm_crs(custom_api, namespace):
    """Delete VirtualMachine custom resources."""
    try:
        vms = custom_api.list_namespaced_custom_object(
            group=_KUBEVIRT_API_GROUP,
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
        )
        for vm in dict(vms).get("items", []):  # type: ignore[call-overload]
            try:
                custom_api.delete_namespaced_custom_object(
                    group=_KUBEVIRT_API_GROUP,
                    version="v1",
                    namespace=namespace,
                    plural="virtualmachines",
                    name=vm["metadata"]["name"],
                    grace_period_seconds=0,
                )
            except Exception:
                pass
    except Exception:
        pass


def _delete_namespace_jobs(provider, namespace):
    """Delete all Jobs (recert, guestfish, export) in the namespace."""
    try:
        from kubernetes import client as _kc

        _, _, api_client = _get_k8s_clients(provider)
        batch_api = _kc.BatchV1Api(api_client)
        jobs = batch_api.list_namespaced_job(namespace=namespace)
        for job in getattr(jobs, "items", []):
            try:
                batch_api.delete_namespaced_job(
                    name=job.metadata.name,
                    namespace=namespace,
                    propagation_policy="Background",
                )
            except Exception:
                pass
    except Exception:
        pass


def _query_cluster_capacity(core_api):
    """Sum allocatable vCPUs and RAM across schedulable worker nodes.

    Returns ``(total_vcpus, total_ram_mb)`` or ``(256, 1048576)`` as fallback.
    """
    total_vcpus = 0
    total_ram_mb = 0
    try:
        nodes = core_api.list_node()
        for node in getattr(nodes, "items", []):
            labels = node.metadata.labels or {}
            taints = node.spec.taints or []

            is_worker = "node-role.kubernetes.io/worker" in labels
            is_unschedulable = node.spec.unschedulable or False
            has_noschedule = any(t.effect == "NoSchedule" for t in taints)
            if not is_worker or is_unschedulable or has_noschedule:
                continue

            alloc = node.status.allocatable or {}
            cpu_str = alloc.get("cpu", "0")
            mem_str = alloc.get("memory", "0")
            total_vcpus += int(cpu_str)
            if mem_str.endswith("Ki"):
                total_ram_mb += int(mem_str[:-2]) // 1024
            elif mem_str.endswith("Mi"):
                total_ram_mb += int(mem_str[:-2])
            elif mem_str.endswith("Gi"):
                total_ram_mb += int(mem_str[:-2]) * 1024
    except Exception as e:
        logger.warning(f"Failed to query cluster capacity: {e}")
        total_vcpus = 256
        total_ram_mb = 1024 * 1024
    return total_vcpus, total_ram_mb


def _query_ceph_storage_gb(core_api):
    """Query total Ceph storage via the rook-ceph-tools pod. Returns 0 on failure."""
    try:
        toolbox_pods = core_api.list_namespaced_pod(
            namespace="openshift-storage",
            label_selector="app=rook-ceph-tools",
        )
        if not getattr(toolbox_pods, "items", []):
            return 0

        from kubernetes.stream import stream as k8s_stream

        resp = k8s_stream(
            core_api.connect_get_namespaced_pod_exec,
            getattr(toolbox_pods, "items", [])[0].metadata.name,
            "openshift-storage",
            command=["ceph", "df", "-f", "json"],
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=False,
        )
        stdout = ""
        while resp.is_open():
            resp.update(timeout=10)
            if resp.peek_stdout():
                stdout += resp.read_stdout()
            if resp.peek_stderr():
                resp.read_stderr()
        resp.close()

        import json

        ceph_df = json.loads(stdout)
        stats = ceph_df.get("stats", {})
        total_bytes = stats.get("total_bytes", 0)
        return int(total_bytes / (1024**3))
    except Exception as e:
        logger.warning(f"Failed to query Ceph storage capacity: {e}")
        return 0


def _count_addresses(addr: str) -> int:
    """Count IPs in a single MetalLB address entry (CIDR, range, or single IP)."""
    import ipaddress

    addr = addr.strip()
    try:
        if "-" in addr:
            start_s, end_s = addr.split("-", 1)
            start = int(ipaddress.ip_address(start_s.strip()))
            end = int(ipaddress.ip_address(end_s.strip()))
            return max(0, end - start + 1)
        if "/" in addr:
            return ipaddress.ip_network(addr, strict=False).num_addresses
        ipaddress.ip_address(addr)  # validate single IP
        return 1
    except ValueError:
        return 0


def _query_metallb_capacity(custom_api):
    """Sum assignable external IPs across auto-assign MetalLB IPAddressPools.

    Troshka's EIP LoadBalancer services don't pin a pool, so only pools with
    ``autoAssign`` enabled (the default) are eligible. Returns the total number
    of external IPs MetalLB can hand out, or 0 if MetalLB isn't installed.
    """
    try:
        pools = custom_api.list_cluster_custom_object(
            group="metallb.io",
            version="v1beta1",
            plural="ipaddresspools",
        )
    except Exception as e:
        logger.warning(f"Failed to query MetalLB IPAddressPools: {e}")
        return 0

    total = 0
    for pool in pools.get("items", []):
        spec = pool.get("spec", {})
        if spec.get("autoAssign") is False:
            continue
        for addr in spec.get("addresses", []):
            total += _count_addresses(addr)
    return total


def _query_metallb_usage(core_api):
    """Count assigned external IPs across LoadBalancer Services cluster-wide.

    Returns ``(total_used, troshka_used)``:
      * ``total_used`` — every LoadBalancer Service currently holding an
        ingress IP (Troshka EIPs plus any other MetalLB consumer, e.g. the
        cluster ingress router).
      * ``troshka_used`` — the subset labeled ``app=troshka-eip``.

    The difference (external consumers) is what eats into the pool capacity
    available for new Troshka EIPs. Returns ``(0, 0)`` if the API can't be read.
    """
    try:
        svcs = core_api.list_service_for_all_namespaces()
    except Exception as e:
        logger.warning(f"Failed to list LoadBalancer services: {e}")
        return 0, 0

    total_used = 0
    troshka_used = 0
    for svc in svcs.items:
        spec = getattr(svc, "spec", None)
        if not spec or spec.type != "LoadBalancer":
            continue
        assigned = _count_assigned_ingress(svc)
        if assigned == 0:
            continue
        total_used += assigned
        meta = getattr(svc, "metadata", None)
        labels = (getattr(meta, "labels", None) or {}) if meta else {}
        if labels.get("app") == "troshka-eip":
            troshka_used += assigned
    return total_used, troshka_used


def _count_assigned_ingress(svc) -> int:
    """Count ingress entries on a Service that carry an assigned IP."""
    status = getattr(svc, "status", None)
    lb = getattr(status, "load_balancer", None) if status else None
    ingress = getattr(lb, "ingress", None) if lb else None
    return sum(1 for ing in (ingress or []) if getattr(ing, "ip", None))


class KubeVirtDriver(ProviderDriver):
    def provision_host(
        self, provider, host_id, instance_type, storage_size_gb, **kwargs
    ):
        _deploy_operator(provider)

        custom_api, core_api, _ = _get_k8s_clients(provider)
        creds = provider.get_credentials()
        api_url = creds["api_url"]

        total_vcpus, total_ram_mb = _query_cluster_capacity(core_api)
        storage_gb = _query_ceph_storage_gb(core_api)
        max_eips = _query_metallb_capacity(custom_api)

        return {
            "host_id": host_id,
            "instance_id": api_url,
            "instance_type": "kubevirt-cluster",
            "public_ip": api_url.replace("https://", "").split(":")[0],  # NOSONAR
            "private_ip": api_url.replace("https://", "").split(":")[0],
            "total_vcpus": total_vcpus,
            "total_ram_mb": total_ram_mb,
            "private_key": "",
            "key_pair_name": "",
            "storage_size_gb": storage_gb or storage_size_gb or 0,
            "max_eips": max_eips,
        }

    def terminate_host(self, provider, instance_id):
        # No-op: KubeVirt virtual host represents the cluster, not a provisionable instance
        pass

    def get_external_ip_capacity(self, provider):
        """Discover MetalLB external-IP pool size and live usage for the cluster.

        Returns ``{total, used, troshka_used, external_used, available}`` or
        ``None`` if the cluster can't be reached. ``external_used`` is capacity
        consumed by non-Troshka LoadBalancer services; the effective ceiling
        Troshka should honor for placement is ``total - external_used``.
        """
        try:
            custom_api, core_api, _ = _get_k8s_clients(provider)
        except Exception as e:
            logger.warning(f"Failed to reach cluster for EIP capacity: {e}")
            return None
        total = _query_metallb_capacity(custom_api)
        used, troshka_used = _query_metallb_usage(core_api)
        external_used = max(0, used - troshka_used)
        return {
            "total": total,
            "used": used,
            "troshka_used": troshka_used,
            "external_used": external_used,
            "available": max(0, total - used),
        }

    def get_host_status(self, provider, instance_id):
        try:
            _, core_api, _ = _get_k8s_clients(provider)
            creds = provider.get_credentials()
            op_ns = creds.get("namespace", "troshka-operator")
            core_api.read_namespace(name=op_ns)
            return {
                "instance_id": instance_id,
                "state": "running",
                "public_ip": instance_id.replace("https://", "").split(":")[0],
                "private_ip": instance_id.replace("https://", "").split(":")[0],
            }
        except Exception:
            return None

    def resize_host(self, provider, instance_id, new_instance_type):
        return {}

    def extend_host_storage(self, provider, host, db, increment_gb=None):
        return {}

    def get_host_powerstate(self, provider, instance_id):
        return "running"

    def start_host(self, provider, instance_id):
        # No-op: KubeVirt virtual host is always running (cluster-level)
        pass

    def stop_host(self, provider, instance_id):
        # No-op: KubeVirt virtual host is always running (cluster-level)
        pass

    def setup_console(self, provider, base_domain):
        return {
            "console_base_domain": base_domain,
            "console_zone_id": "",
            "console_nameservers": [],
        }

    def create_console_record(self, provider, host, hostname, ip_address):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _operator_ns(provider)

        svc_name = f"vnc-{hostname}"
        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {"app": "troshka-vnc", "troshka-host": hostname},
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [{"port": 8080, "targetPort": 8080, "protocol": "TCP"}],
                "selector": {"app": f"vnc-proxy-{hostname}"},
            },
        }
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc_body)
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        route_body = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": f"console-{hostname}",
                "namespace": namespace,
                "labels": {"app": "troshka-vnc", "troshka-host": hostname},
                "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
            },
            "spec": {
                "host": hostname,
                "to": {"kind": "Service", "name": svc_name},
                "port": {"targetPort": 8080},
                "tls": {
                    "termination": "edge",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        }
        try:
            custom_api.create_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route_body,
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

    def delete_console_record(self, provider, host, hostname, ip_address):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _operator_ns(provider)
        try:
            core_api.delete_namespaced_service(
                name=f"vnc-{hostname}", namespace=namespace
            )
        except Exception:
            pass
        try:
            custom_api.delete_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                name=f"console-{hostname}",
            )
        except Exception:
            pass

    def delete_console(self, provider):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _operator_ns(provider)
        try:
            svcs = core_api.list_namespaced_service(
                namespace=namespace, label_selector="app=troshka-vnc"
            )
            for svc in getattr(svcs, "items", []):
                core_api.delete_namespaced_service(
                    name=svc.metadata.name, namespace=namespace
                )
        except Exception:
            pass
        try:
            routes = custom_api.list_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                label_selector="app=troshka-vnc",
            )
            for route in dict(routes).get("items", []):  # type: ignore[call-overload]
                custom_api.delete_namespaced_custom_object(
                    group=_ROUTE_API,
                    version="v1",
                    namespace=namespace,
                    plural="routes",
                    name=route["metadata"]["name"],
                )
        except Exception:
            pass

    def allocate_eip(self, provider, host, eip_id, project_id=None):
        _, core_api, _ = _get_k8s_clients(provider)
        namespace = (
            _project_ns(provider, project_id) if project_id else _operator_ns(provider)
        )
        project_short = project_id[:8] if project_id else eip_id[:8]

        svc_name = f"troshka-eip-{eip_id[:8]}"
        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-eip",
                    "troshka-eip-id": eip_id[:8],
                },
            },
            "spec": {
                "type": "LoadBalancer",
                "ports": [{"port": 443, "targetPort": 443, "protocol": "TCP"}],
                "selector": {"app": f"troshka-gateway-{project_short}"},
            },
        }
        core_api.create_namespaced_service(namespace=namespace, body=svc_body)

        for _ in range(60):
            svc = core_api.read_namespaced_service(name=svc_name, namespace=namespace)
            ingress = svc.status.load_balancer.ingress  # type: ignore[union-attr]
            if ingress and ingress[0].ip:
                return {"public_ip": ingress[0].ip, "allocation_id": svc_name}
            time.sleep(2)

        raise TimeoutError(f"MetalLB did not assign IP to {svc_name} within 120s")

    def associate_eip(self, provider, host, allocation_id):
        return {}

    def release_eip(self, provider, allocation_id, namespace=None):
        _, core_api, _ = _get_k8s_clients(provider)
        ns = namespace or _operator_ns(provider)
        try:
            core_api.delete_namespaced_service(name=allocation_id, namespace=ns)
        except Exception:
            pass

    def update_eip_ports(self, provider, host, allocation_id, ports, namespace=None):
        _, core_api, _ = _get_k8s_clients(provider)
        ns = namespace or _operator_ns(provider)
        svc_ports = []
        for p in ports:
            ext_port = int(p["port"])
            target_port = p.get("target_port", ext_port)
            if target_port == ext_port:
                target_port = gateway_pod_listen_port(ext_port)
            svc_ports.append(
                {
                    "name": p.get("name", f"port-{ext_port}"),
                    "port": ext_port,
                    "targetPort": target_port,
                    "protocol": p.get("protocol", "TCP"),
                }
            )
        core_api.patch_namespaced_service(
            name=allocation_id,
            namespace=ns,
            body={"spec": {"ports": svc_ports}},
            _content_type="application/merge-patch+json",
        )

    def create_route_access(
        self, provider, host, project_id, vm_name, int_ip, port, target_port=None
    ):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        ext_port = int(port)
        pod_port = gateway_pod_listen_port(ext_port)
        if target_port is not None:
            pod_port = int(target_port)

        svc_name = f"rt-{vm_name}-{port}"[:63]
        route_name = svc_name

        svc_body = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": svc_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-route-access",
                    "troshka-project": project_id[:8],
                },
            },
            "spec": {
                "type": "ClusterIP",
                "ports": [
                    {"port": pod_port, "targetPort": pod_port, "protocol": "TCP"}
                ],
                "selector": {"app": f"troshka-gateway-{project_id[:8]}"},
            },
        }
        try:
            core_api.create_namespaced_service(namespace=namespace, body=svc_body)
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        # Edge termination uses the cluster wildcard cert on the router. Only the
        # API server (6443) needs passthrough to preserve client TLS.
        passthrough = ext_port == 6443
        route_body = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": route_name,
                "namespace": namespace,
                "labels": {
                    "app": "troshka-route-access",
                    "troshka-project": project_id[:8],
                },
                "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
            },
            "spec": {
                "to": {"kind": "Service", "name": svc_name},
                "port": {"targetPort": pod_port},
                "tls": (
                    {"termination": "passthrough"}
                    if passthrough
                    else {
                        "termination": "edge",
                        "insecureEdgeTerminationPolicy": "Redirect",
                    }
                ),
            },
        }
        try:
            result = custom_api.create_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                body=route_body,
            )
            hostname = dict(result).get("spec", {}).get("host", "")  # type: ignore[call-overload]
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise
            existing = custom_api.get_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=namespace,
                plural="routes",
                name=route_name,
            )
            hostname = dict(existing).get("spec", {}).get("host", "")  # type: ignore[call-overload]

        return {
            "hostname": hostname,
            "route_name": route_name,
            "service_name": svc_name,
        }

    def create_app_proxy_route(
        self, provider, project_id, public_host, source_route_name
    ):
        """Clone the showroom route under an explicit public host so an
        OAuth-protected app (console/oauth) is reachable at a deterministic
        hostname the showroom's app-proxy nginx can Host-route. Returns the host."""
        custom_api, _core_api, _ = _get_k8s_clients(provider)
        ns = _project_ns(provider, project_id)
        try:
            raw = custom_api.get_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=ns,
                plural="routes",
                name=source_route_name,
            )
        except Exception:
            logger.warning(
                "App-proxy route: source route %s not found", source_route_name
            )
            return ""
        src_spec: dict = {}
        if isinstance(raw, dict):
            _s = raw.get("spec")
            if isinstance(_s, dict):
                src_spec = _s
        name = public_host.split(".")[0][:63]
        route_body = {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": {
                    "app": "troshka-route-access",
                    "troshka-project": project_id[:8],
                },
                "annotations": {"haproxy.router.openshift.io/timeout": "3600s"},
            },
            "spec": {
                "host": public_host,
                "to": src_spec.get("to"),
                "port": src_spec.get("port"),
                "tls": src_spec.get("tls"),
            },
        }
        try:
            custom_api.create_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=ns,
                plural="routes",
                body=route_body,
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise
        return public_host

    def delete_route_access(self, provider, project_id, namespace=None):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        ns = namespace or _project_ns(provider, project_id)
        label = f"troshka-project={project_id[:8]}"
        try:
            svcs = core_api.list_namespaced_service(namespace=ns, label_selector=label)
            for svc in getattr(svcs, "items", []):
                core_api.delete_namespaced_service(name=svc.metadata.name, namespace=ns)
        except Exception:
            pass
        try:
            routes = custom_api.list_namespaced_custom_object(
                group=_ROUTE_API,
                version="v1",
                namespace=ns,
                plural="routes",
                label_selector=label,
            )
            for route in dict(routes).get("items", []):  # type: ignore[call-overload]
                custom_api.delete_namespaced_custom_object(
                    group=_ROUTE_API,
                    version="v1",
                    namespace=ns,
                    plural="routes",
                    name=route["metadata"]["name"],
                )
        except Exception:
            pass

    def deploy_project(self, provider, project_id, topology, s3_config, **kwargs):
        _ensure_operator_crds(provider)

        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)

        from kubernetes import client as k8s_client

        try:
            core_api.create_namespace(
                body=k8s_client.V1Namespace(
                    metadata=k8s_client.V1ObjectMeta(
                        name=namespace,
                        labels=_project_namespace_labels(project_id),
                    )
                )
            )
        except Exception as e:
            if "AlreadyExists" not in str(e):
                raise

        # Use OBC config for local RGW when available
        from app.services.s3_storage import get_cluster_s3_config

        db = kwargs.get("db")
        cluster_s3 = get_cluster_s3_config(db, provider.id) if db else None
        obc_s3 = None
        if cluster_s3:
            obc_s3 = {
                "access_key_id": cluster_s3.get("access_key_id", ""),
                "secret_access_key": cluster_s3.get("secret_access_key", ""),
                "region": cluster_s3.get("region", "us-east-1"),
                "endpoint_url": cluster_s3.get("endpoint", ""),
                "bucket": cluster_s3.get("bucket", ""),
            }
            _ensure_s3_secret(provider, namespace, obc_s3, "s3-obc-credentials")

        _ensure_s3_secret(provider, namespace, s3_config, "s3-credentials")

        central_s3 = kwargs.get("central_s3_config")
        if central_s3:
            _ensure_s3_secret(provider, namespace, central_s3, "s3-central-credentials")

        _ensure_cache_s3_secrets(provider, s3_config, central_s3, obc_s3)

        s3_cr_config = {
            "bucket": s3_config.get("bucket", ""),
            "endpoint": s3_config.get("endpoint_url", "")
            or s3_config.get("endpoint", ""),
            "region": s3_config.get("region", ""),
            "credentialsSecret": "s3-credentials",  # pragma: allowlist secret
            "accessKeyId": s3_config.get("access_key_id", ""),
            "secretKey": s3_config.get("secret_access_key", ""),
        }
        if cluster_s3:
            s3_cr_config["obcConfig"] = {
                "bucket": cluster_s3.get("bucket", ""),
                "endpoint": cluster_s3.get("endpoint", ""),
                "region": cluster_s3.get("region", "us-east-1"),
                "credentialsSecret": "s3-obc-credentials",  # pragma: allowlist secret
            }

        project_cr = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "TroshkaProject",
            "metadata": {
                "name": f"project-{project_id[:8]}",
                "namespace": namespace,
            },
            "spec": {
                "projectId": project_id,
                "topology": topology,
                "s3Config": s3_cr_config,
                "action": "deploy",
            },
        }
        if central_s3:
            project_cr["spec"]["centralS3Config"] = {
                "bucket": central_s3.get("bucket", ""),
                "endpoint": central_s3.get("endpoint_url", "")
                or central_s3.get("endpoint", ""),
                "region": central_s3.get("region", ""),
                "credentialsSecret": "s3-central-credentials",  # pragma: allowlist secret
                "accessKeyId": central_s3.get("access_key_id", ""),
                "secretKey": central_s3.get("secret_access_key", ""),
            }
        if kwargs.get("common_password"):
            project_cr["spec"]["commonPassword"] = kwargs["common_password"]
        if kwargs.get("registry_credentials"):
            project_cr["spec"]["registryCredentials"] = kwargs["registry_credentials"]
        if kwargs.get("exec_ssh_key"):
            project_cr["spec"]["execSshKey"] = kwargs["exec_ssh_key"]
            logger.info(
                "deploy_project: execSshKey set, length=%d",
                len(kwargs["exec_ssh_key"]),
            )

        custom_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=namespace,
            plural="troshkaprojects",
            body=project_cr,
        )
        return f"project-{project_id[:8]}"

    def destroy_project(self, provider, project_id):
        custom_api, core_api, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)

        _stop_vms_gracefully(custom_api, namespace)
        _wait_for_vmis_terminated(custom_api, namespace)
        _force_delete_vmis(custom_api, namespace)
        _force_delete_virt_launcher_pods(core_api, namespace)
        _wait_for_virt_launchers_gone(custom_api, core_api, namespace)
        _cleanup_volume_attachments(core_api, namespace)
        _delete_vm_crs(custom_api, namespace)
        _delete_namespace_jobs(provider, namespace)

        # Delete TroshkaProject CR and wait for finalizers
        cr_name = f"project-{project_id[:8]}"
        try:
            custom_api.delete_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=cr_name,
            )
        except Exception:
            pass

        for _ in range(30):
            try:
                custom_api.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=namespace,
                    plural="troshkaprojects",
                    name=cr_name,
                )
                time.sleep(2)
            except Exception:
                break

        try:
            core_api.delete_namespace(name=namespace)
        except Exception:
            pass

    def get_project_status(self, provider, project_id):
        custom_api, _, _ = _get_k8s_clients(provider)
        namespace = _project_ns(provider, project_id)
        try:
            cr = custom_api.get_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural="troshkaprojects",
                name=f"project-{project_id[:8]}",
            )
            s = dict(cr).get("status", {})  # type: ignore[call-overload]
            if not isinstance(s, dict):
                s = {}
            # Include DataVolumes from the project namespace
            dvs = []
            try:
                project_dvs = custom_api.list_namespaced_custom_object(
                    group="cdi.kubevirt.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="datavolumes",
                )
                dvs.extend(dict(project_dvs).get("items", []))  # type: ignore[call-overload]
            except Exception:
                pass
            # Include cache DVs only for golden PVCs this project references
            try:
                # Build set of golden names from project's clone DVs
                golden_refs = set()
                for dv in dvs:
                    source = dv.get("spec", {}).get("source", {})
                    pvc_src = source.get("pvc", {})
                    if pvc_src.get("namespace") == "troshka-cache":
                        golden_refs.add(pvc_src.get("name", ""))
                if golden_refs:
                    cache_dvs = custom_api.list_namespaced_custom_object(
                        group="cdi.kubevirt.io",
                        version="v1beta1",
                        namespace="troshka-cache",
                        plural="datavolumes",
                    )
                    for cdv in dict(cache_dvs).get("items", []):  # type: ignore[call-overload]
                        if cdv.get("metadata", {}).get("name") in golden_refs:
                            dvs.append(cdv)
            except Exception:
                pass
            s["dataVolumes"] = dvs
            return s
        except Exception:
            return {}

    def get_vm_states(self, provider, project_id):
        status = self.get_project_status(provider, project_id)
        return status.get("vmStates", {})


def kubevirt_vm_is_headless(provider, project_id, vm_id, vm_node_data=None) -> bool:
    """True when the VM has no graphical display (serial-only console)."""
    namespace = _project_ns(provider, project_id)
    kv_name = f"troshka-vm-{vm_id[:8]}"
    try:
        custom_api, _, _ = _get_k8s_clients(provider)
        vm = custom_api.get_namespaced_custom_object(
            group="kubevirt.io",
            version="v1",
            namespace=namespace,
            plural="virtualmachines",
            name=kv_name,
        )
        vm_body = dict(vm) if isinstance(vm, dict) else {}
        spec = vm_body.get("spec") or {}
        template = spec.get("template") if isinstance(spec, dict) else {}
        template = template if isinstance(template, dict) else {}
        template_spec = template.get("spec") if isinstance(template, dict) else {}
        template_spec = template_spec if isinstance(template_spec, dict) else {}
        domain = template_spec.get("domain") if isinstance(template_spec, dict) else {}
        domain = domain if isinstance(domain, dict) else {}
        devices = domain.get("devices") if isinstance(domain, dict) else {}
        devices = devices if isinstance(devices, dict) else {}
        graphics = devices.get("autoattachGraphicsDevice")
        if graphics is False:
            return True
        return False
    except Exception:
        pass
    from app.services.headless import serial_exec_needs_headless

    return serial_exec_needs_headless(
        headless=(vm_node_data or {}).get("headless"),
        serial_exec_type=(vm_node_data or {}).get("serialExecType") or "",
    )


def _find_virt_launcher(core_v1, namespace, vm_name):
    """Find the running virt-launcher pod for a VM, or raise RuntimeError."""
    pod_list: list = getattr(
        core_v1.list_namespaced_pod(
            namespace, label_selector=f"vm.kubevirt.io/name={vm_name}"
        ),
        "items",
        [],
    )
    for p in pod_list:
        if p.metadata.name.startswith("virt-launcher-") and p.status.phase == "Running":
            return p
    raise RuntimeError(f"No running virt-launcher pod for {vm_name}")


def _poll_guest_exec(pod_exec_fn, domain, pid, timeout):
    """Poll guest-exec-status until the command completes or times out."""
    import base64
    import json

    status_payload = json.dumps(
        {
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        }
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = pod_exec_fn(
            ["virsh", "qemu-agent-command", domain, status_payload, "--timeout", "10"],
        )
        status = json.loads(sr).get("return", {})
        if status.get("exited"):
            stdout = ""
            stderr = ""
            if status.get("out-data"):
                stdout = base64.b64decode(status["out-data"]).decode(
                    "utf-8", errors="replace"
                )
            if status.get("err-data"):
                stderr = base64.b64decode(status["err-data"]).decode(
                    "utf-8", errors="replace"
                )
            return {
                "output": stdout,
                "error": stderr,
                "exit_code": status.get("exitcode", -1),
                "method": "guest-agent",
            }
        time.sleep(0.5)

    raise RuntimeError(f"guest-exec timed out after {timeout}s (pid={pid})")


def kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout=600):
    """Execute command via qemu-guest-agent inside the virt-launcher pod."""
    import json

    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    launcher = _find_virt_launcher(core_v1, namespace, vm_name)

    from kubernetes.stream import stream as k8s_stream

    def _pod_exec_raw(pod_name, ns, cmd, req_timeout=30):
        """Exec in pod and return raw stdout (not Python-parsed)."""
        ws = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            ns,
            container="compute",
            command=cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=False,
            _request_timeout=req_timeout,
        )
        out = ""
        while ws.is_open():
            ws.update(timeout=req_timeout)
            if ws.peek_stdout():
                out += ws.read_stdout()
            if ws.peek_stderr():
                ws.read_stderr()
        ws.close()
        return out

    # Discover the libvirt domain name inside the pod
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=["virsh", "list", "--name"],
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=True,
        _request_timeout=30,
    )
    domain = resp.strip().split("\n")[0].strip()
    if not domain:
        raise RuntimeError("No libvirt domain found in virt-launcher pod")

    # Check guest agent availability
    check_resp = _pod_exec_raw(
        launcher.metadata.name,
        namespace,
        [
            "virsh",
            "qemu-agent-command",
            domain,
            '{"execute":"guest-info"}',
            "--timeout",
            "10",
        ],
    )
    if "error" in check_resp.lower() and "guest agent" in check_resp.lower():
        raise RuntimeError(f"Guest agent not available: {check_resp}")

    try:
        info = json.loads(check_resp)
        cmds = info.get("return", {}).get("supported_commands", [])
        exec_cmd = next((c for c in cmds if c.get("name") == "guest-exec"), None)
        if exec_cmd and not exec_cmd.get("enabled", False):
            raise RuntimeError("guest-exec is disabled (blocked by guest agent config)")
    except (json.JSONDecodeError, StopIteration):
        pass

    # Execute command
    exec_payload = json.dumps(
        {
            "execute": "guest-exec",
            "arguments": {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            },
        }
    )
    exec_resp = _pod_exec_raw(
        launcher.metadata.name,
        namespace,
        ["virsh", "qemu-agent-command", domain, exec_payload, "--timeout", "10"],
    )
    parsed = json.loads(exec_resp)
    pid = parsed.get("return", {}).get("pid")
    if pid is None:
        raise RuntimeError(f"No PID in guest-exec response: {exec_resp}")

    # Poll for completion via extracted helper
    def _bound_exec(cmd):
        return _pod_exec_raw(launcher.metadata.name, namespace, cmd)

    return _poll_guest_exec(_bound_exec, domain, pid, timeout)


def _find_exec_pod(core_v1, namespace, project_id):
    """Find the exec pod for a project, falling back to dnsmasq pod."""
    project_name = f"project-{project_id[:8]}"
    exec_pods: list = getattr(
        core_v1.list_namespaced_pod(
            namespace,
            label_selector=f"app=troshka-exec,troshka-project={project_name}",
        ),
        "items",
        [],
    )
    for p in exec_pods:
        if p.status.phase == "Running":
            return p
    dns_pods: list = getattr(
        core_v1.list_namespaced_pod(namespace, label_selector="app=troshka-dnsmasq"),
        "items",
        [],
    )
    for p in dns_pods:
        if p.status.phase == "Running":
            return p
    return None


def kubevirt_exec_ssh(
    provider, project_id, _vm_id, vm_ip, username, password, command, timeout=600
):
    """Execute command via SSH from the exec pod (or dnsmasq pod fallback)."""
    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)

    exec_pod = _find_exec_pod(core_v1, namespace, project_id)
    if not exec_pod:
        raise RuntimeError("No running exec pod found")

    if not vm_ip:
        raise RuntimeError("No VM IP for SSH exec")
    if not password:
        raise RuntimeError(
            "No password for SSH exec (key auth not supported on KubeVirt)"
        )

    ssh_cmd = [
        "sshpass",
        "-p",
        password,
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={min(timeout, 10)}",
        f"{username}@{vm_ip}",
        command,
    ]
    resp = _kubevirt_ws_pod_exec(
        core_v1, exec_pod.metadata.name, namespace, ssh_cmd, timeout
    )
    return {
        "output": resp,
        "error": "",
        "exit_code": 0,
        "method": "ssh",
    }


def _kubevirt_ws_pod_exec(core_v1, pod_name, namespace, command, timeout, attempts=3):
    """Run a pod-exec websocket stream, tolerating transient empty responses.

    k8s_stream(_preload_content=True) raises AttributeError("'NoneType' object has
    no attribute 'decode'") when the websocket yields no body — e.g. the API
    server closed the stream or the exec outlived its idle timeout (seen when a
    long-running SSH wait_for holds the exec open). Retry briefly, then surface a
    clean error instead of the confusing NoneType crash. Returns "" if the stream
    legitimately produced no output.
    """
    import time

    from kubernetes.stream import stream as k8s_stream

    last_err = None
    for attempt in range(attempts):
        try:
            resp = k8s_stream(
                core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=command,
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False,
                _preload_content=True,
                _request_timeout=timeout + 10,
            )
            return resp or ""
        except AttributeError as e:
            # 'NoneType' object has no attribute 'decode' — empty/closed stream.
            last_err = e
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(
        "pod exec stream returned no data (websocket closed or idle timeout)"
    ) from last_err


def kubevirt_upload_to_vm(
    provider,
    project_id,
    vm_ip,
    username,
    password,
    file_bytes,
    remote_path,
    mode="0644",
    timeout=600,
):
    """Upload a file to a VM via SSH from the project exec pod."""
    import base64
    import shlex
    import uuid

    if not password:
        raise RuntimeError("No password for KubeVirt file upload")

    tmp = f"/tmp/troshka-up-{uuid.uuid4().hex}"
    tmp_q = shlex.quote(tmp)
    dest_q = shlex.quote(remote_path)
    parent_q = shlex.quote(os.path.dirname(remote_path) or ".")

    payload = base64.b64encode(file_bytes).decode("ascii")
    chunk_size = 24000
    for idx in range(0, len(payload), chunk_size):
        chunk = payload[idx : idx + chunk_size]
        redir = ">" if idx == 0 else ">>"
        cmd = f"printf '%s' {shlex.quote(chunk)} | base64 -d {redir} {tmp_q}"
        kubevirt_exec_ssh(
            provider,
            project_id,
            None,
            vm_ip,
            username,
            password,
            cmd,
            timeout=timeout,
        )

    finalize = f"mkdir -p {parent_q} && mv {tmp_q} {dest_q} && chmod {mode} {dest_q}"
    return kubevirt_exec_ssh(
        provider,
        project_id,
        None,
        vm_ip,
        username,
        password,
        finalize,
        timeout=timeout,
    )


def kubevirt_download_from_vm(
    provider, project_id, vm_ip, username, password, remote_path, timeout=600
):
    """Download a file from a VM via SSH from the project exec pod."""
    import base64
    import shlex

    if not password:
        raise RuntimeError("No password for KubeVirt file download")

    cmd = f"base64 -w0 {shlex.quote(remote_path)}"
    result = kubevirt_exec_ssh(
        provider,
        project_id,
        None,
        vm_ip,
        username,
        password,
        cmd,
        timeout=timeout,
    )
    raw = (result.get("output") or "").strip()
    return base64.b64decode(raw)


_CHAR_TO_KEYS = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c.upper()}"]
    _CHAR_TO_KEYS[_c.upper()] = ["KEY_LEFTSHIFT", f"KEY_{_c.upper()}"]
for _c in "1234567890":
    _CHAR_TO_KEYS[_c] = [f"KEY_{_c}"]
_CHAR_TO_KEYS.update(
    {
        "!": ["KEY_LEFTSHIFT", "KEY_1"],
        "@": ["KEY_LEFTSHIFT", "KEY_2"],
        "#": ["KEY_LEFTSHIFT", "KEY_3"],
        "$": ["KEY_LEFTSHIFT", "KEY_4"],
        "%": ["KEY_LEFTSHIFT", "KEY_5"],
        "^": ["KEY_LEFTSHIFT", "KEY_6"],
        "&": ["KEY_LEFTSHIFT", "KEY_7"],
        "*": ["KEY_LEFTSHIFT", "KEY_8"],
        "(": ["KEY_LEFTSHIFT", "KEY_9"],
        ")": ["KEY_LEFTSHIFT", "KEY_0"],
        " ": ["KEY_SPACE"],
        "\n": ["KEY_ENTER"],
        "\t": ["KEY_TAB"],
        "-": ["KEY_MINUS"],
        "=": ["KEY_EQUAL"],
        "[": ["KEY_LEFTBRACE"],
        "]": ["KEY_RIGHTBRACE"],
        "\\": ["KEY_BACKSLASH"],
        ";": ["KEY_SEMICOLON"],
        "'": ["KEY_APOSTROPHE"],
        "`": ["KEY_GRAVE"],
        ",": ["KEY_COMMA"],
        ".": ["KEY_DOT"],
        "/": ["KEY_SLASH"],
        "_": ["KEY_LEFTSHIFT", "KEY_MINUS"],
        "+": ["KEY_LEFTSHIFT", "KEY_EQUAL"],
        "{": ["KEY_LEFTSHIFT", "KEY_LEFTBRACE"],
        "}": ["KEY_LEFTSHIFT", "KEY_RIGHTBRACE"],
        "|": ["KEY_LEFTSHIFT", "KEY_BACKSLASH"],
        ":": ["KEY_LEFTSHIFT", "KEY_SEMICOLON"],
        '"': ["KEY_LEFTSHIFT", "KEY_APOSTROPHE"],
        "~": ["KEY_LEFTSHIFT", "KEY_GRAVE"],
        "<": ["KEY_LEFTSHIFT", "KEY_COMMA"],
        ">": ["KEY_LEFTSHIFT", "KEY_DOT"],
        "?": ["KEY_LEFTSHIFT", "KEY_SLASH"],
    }
)


def _vnc_login(
    send_keys_fn, send_text_fn, screenshot_ocr_fn, detect_state_fn, username, password
):
    """Handle the VNC login loop. Returns True if shell prompt is reached."""
    for _ in range(4):
        ocr = screenshot_ocr_fn()
        state = detect_state_fn(ocr)

        if state == "shell":
            return True
        if state == "unknown":
            send_keys_fn("KEY_ENTER")
            time.sleep(1)
            continue
        if state == "login":
            send_text_fn(username + "\n")
            time.sleep(2)
            continue
        if state == "password":
            send_text_fn(password + "\n")
            time.sleep(3)
    return False


def _find_vnc_pods(core_v1, namespace, vm_name):
    """Find the virt-launcher and exec pods for VNC operations."""
    all_pods: list = getattr(core_v1.list_namespaced_pod(namespace), "items", [])
    launcher = None
    exec_pod = None
    for p in all_pods:
        if not p.status or p.status.phase != "Running":
            continue
        if p.metadata.name.startswith("virt-launcher-") and vm_name in p.metadata.name:
            launcher = p
        if p.metadata.name.startswith("exec-"):
            exec_pod = p
    if not launcher:
        raise RuntimeError(f"No running virt-launcher pod for {vm_name}")
    if not exec_pod:
        raise RuntimeError("No running exec pod for VNC OCR")
    return launcher, exec_pod


def _make_pod_exec_fn(core_v1, pod_name, namespace, container):
    """Create a callable that executes commands in a specific pod container."""
    from kubernetes.stream import stream as k8s_stream

    def _exec(cmd, req_timeout=15):
        ws = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container=container,
            command=cmd,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=True,
            _request_timeout=req_timeout,
        )
        return ws.strip() if isinstance(ws, str) else ""

    return _exec


def _detect_vnc_state(ocr_text):
    """Detect console state from OCR text: login, password, shell, or unknown."""
    import re

    text = ocr_text.strip()
    if not text or len(text) < 3:
        return "unknown"
    last_lines = "\n".join(text.split("\n")[-5:])
    if re.search(r"login\s*:?\s*$", last_lines, re.IGNORECASE | re.MULTILINE):
        return "login"
    if re.search(r"[Pp]ass[wvu]ord\s*:?\s*$", last_lines, re.MULTILINE):
        return "password"
    if re.search(r"[\]$#~]\s*$", last_lines, re.MULTILINE):
        return "shell"
    return "unknown"


def _vnc_screenshot_ocr(launcher_exec_fn, tools_exec_fn, domain):
    """Take a screenshot via virsh and OCR it via the tools pod."""
    img_path = "/tmp/troshka-screen.ppm"
    launcher_exec_fn(["virsh", "-c", _QEMU_SESSION_URI, "screenshot", domain, img_path])
    b64 = launcher_exec_fn(["base64", "-w0", img_path], req_timeout=10)
    launcher_exec_fn(["rm", "-f", img_path])
    if not b64:
        return ""
    tools_exec_fn(
        ["bash", "-c", f"cat > /tmp/screen.b64 << 'ENDOFB64'\n{b64}\nENDOFB64"],
        req_timeout=10,
    )
    result = tools_exec_fn(
        [
            "bash",
            "-c",
            "base64 -d /tmp/screen.b64 | tesseract stdin stdout 2>/dev/null;"
            " rm -f /tmp/screen.b64",
        ],
        req_timeout=15,
    )
    return result


def _vnc_send_text(send_keys_fn, text):
    """Send text character-by-character via virsh send-key."""
    for ch in text:
        keys = _CHAR_TO_KEYS.get(ch)
        if keys:
            send_keys_fn(*keys)


def _parse_vnc_markers(text):
    """Extract output and exit code from VNC OCR text between markers."""
    import re

    m = re.search(
        r"TROSHKA_BEGIN[^\S\n]*\n(.*?)TROSHKA_EXIT[^\S\n]*(\d+)?", text, re.DOTALL
    )
    if m:
        output = m.group(1).strip()
        exit_code = int(m.group(2)) if m.group(2) else None
    else:
        output = text.strip()
        exit_code = None
    return output, exit_code


def kubevirt_exec_vnc(
    provider, project_id, vm_id, username, password, command, timeout=600
):
    """Execute command via VNC console: virsh send-key + screenshot + OCR.

    Screenshot taken in virt-launcher pod, OCR runs in the exec/tools pod
    (which has tesseract installed).
    """
    import time

    if not password:
        raise RuntimeError("Password required for VNC console exec")

    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    launcher, exec_pod = _find_vnc_pods(core_v1, namespace, vm_name)

    _launcher_exec = _make_pod_exec_fn(
        core_v1, launcher.metadata.name, namespace, "compute"
    )
    _tools_exec = _make_pod_exec_fn(core_v1, exec_pod.metadata.name, namespace, "exec")

    resp = _launcher_exec(["virsh", "-c", _QEMU_SESSION_URI, "list", "--name"])
    domain = resp.split("\n")[0].strip()
    if not domain:
        raise RuntimeError("No libvirt domain found in virt-launcher pod")

    def _send_keys(*keys):
        _launcher_exec(
            ["virsh", "-c", _QEMU_SESSION_URI, "send-key", domain] + list(keys)
        )

    def send_text_fn(text):
        return _vnc_send_text(_send_keys, text)

    def screenshot_fn():
        return _vnc_screenshot_ocr(_launcher_exec, _tools_exec, domain)

    _send_keys("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F3")
    time.sleep(2)

    logged_in = _vnc_login(
        _send_keys, send_text_fn, screenshot_fn, _detect_vnc_state, username, password
    )

    if not logged_in:
        return {
            "output": "",
            "error": "Could not reach shell prompt via VNC console",
            "exit_code": None,
            "method": "vnc",
        }

    send_text_fn("clear\n")
    time.sleep(0.5)
    wrapped = f"echo TROSHKA_BEGIN; {command} 2>&1; echo TROSHKA_EXIT $?"
    send_text_fn(wrapped + "\n")

    ocr = ""
    deadline = time.time() + min(timeout, 60)
    while time.time() < deadline:
        time.sleep(2)
        ocr = screenshot_fn()
        if "TROSHKA_EXIT" in ocr:
            break

    output, exit_code = _parse_vnc_markers(ocr)

    _send_keys("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F1")

    return {
        "output": output,
        "error": "",
        "exit_code": exit_code,
        "method": "vnc",
    }


class _VirtLauncherSerialConnection:
    """Bidirectional serial stream via virt-launcher pod exec + socat."""

    def __init__(self, stream):
        self._stream = stream
        self._timeout = 0.5

    def settimeout(self, timeout):
        self._timeout = timeout

    def send(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        self._stream.write_stdin(data)

    def recv(self):
        import websocket

        deadline = time.time() + self._timeout
        while time.time() < deadline:
            if not self._stream.is_open():
                raise websocket.WebSocketConnectionClosedException(
                    "serial stream closed"
                )
            remaining = max(0.1, deadline - time.time())
            self._stream.update(timeout=remaining)
            if self._stream.peek_stdout():
                chunk = self._stream.read_stdout()
                if isinstance(chunk, bytes):
                    return chunk.decode("utf-8", errors="replace")
                return chunk
        raise websocket.WebSocketTimeoutException("serial read timed out")

    def close(self):
        try:
            self._stream.close()
        except Exception:
            pass


def _serial_socket_path(custom_api, namespace, vm_name):
    """Return the virt-serial0 unix socket path inside the virt-launcher pod."""
    vmi = custom_api.get_namespaced_custom_object(
        group="kubevirt.io",
        version="v1",
        namespace=namespace,
        plural="virtualmachineinstances",
        name=vm_name,
    )
    uid = (vmi.get("metadata") or {}).get("uid")
    if not uid:
        raise RuntimeError(f"VMI {vm_name} has no UID")
    return f"/var/run/kubevirt-private/{uid}/virt-serial0"


def _open_virt_launcher_serial_stream(provider, namespace, vm_name, timeout):
    """Open a raw exec stream to virt-serial0 inside the virt-launcher pod."""
    from kubernetes.stream import stream as k8s_stream

    custom_api, core_v1, _ = _get_k8s_clients(provider)
    launcher = _find_virt_launcher(core_v1, namespace, vm_name)
    socket_path = _serial_socket_path(custom_api, namespace, vm_name)
    req_timeout = min(timeout, 30)
    return k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=["socat", "-", f"UNIX-CONNECT:{socket_path}"],
        stderr=True,
        stdout=True,
        stdin=True,
        tty=False,
        _preload_content=False,
        _request_timeout=req_timeout,
    )


def _create_console_ws(provider, namespace, vm_name, timeout):
    """Open a serial console stream to a KubeVirt VMI.

    The KubeVirt API WebSocket console subresource does not reliably deliver
    guest output through the apiserver proxy (virtctl works via client-go's
    wrapped dialer).  Connect directly to virt-serial0 in the virt-launcher
    pod instead — same socket virt-handler proxies for the API console.
    """
    return _VirtLauncherSerialConnection(
        _open_virt_launcher_serial_stream(provider, namespace, vm_name, timeout)
    )


def _console_ws_read(ws, secs):
    """Read all available data from WebSocket within timeout."""
    import time

    import websocket

    buf = ""
    deadline = time.time() + secs
    ws.settimeout(0.5)
    while time.time() < deadline:
        try:
            data = ws.recv()
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            buf += data
        except websocket.WebSocketTimeoutException:
            if buf:
                break
    return buf


def _console_ws_send(ws, text):
    """Send text data over WebSocket."""
    ws.send(text.encode("utf-8") if isinstance(text, str) else text)


def _console_handle_login(ws, combined, username, password):
    """Detect and handle login/password prompts on the serial console."""
    if "login:" in combined.lower():
        _console_ws_send(ws, f"{username}\n")
        _console_ws_read(ws, 2)
        _console_ws_send(ws, f"{password}\n")
        login_resp = _console_ws_read(ws, 3)
        if "login incorrect" in login_resp.lower():
            raise RuntimeError("Console login failed")
    elif "password:" in combined.lower():
        _console_ws_send(ws, f"{password}\n")
        login_resp = _console_ws_read(ws, 3)
        if "login incorrect" in login_resp.lower():
            raise RuntimeError("Console login failed")


def _parse_console_output(raw_output):
    """Strip ANSI codes and extract output between TROSHKA markers."""
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[4]
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from troshka_serial.linux import parse_marker_output

    return parse_marker_output(raw_output)


def kubevirt_exec_console(
    provider, project_id, vm_id, username, password, command, timeout=600
):
    """Execute command via KubeVirt serial console (WebSocket-based)."""
    import time

    _, _core_v1, _api_client = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    if not password:
        raise RuntimeError("Password required for console exec")

    ws = None
    try:
        ws = _create_console_ws(provider, namespace, vm_name, timeout)

        initial = _console_ws_read(ws, 3)
        _console_ws_send(ws, "\n")
        prompt_check = _console_ws_read(ws, 3)
        combined = initial + prompt_check

        _console_handle_login(ws, combined, username, password)

        _console_ws_send(ws, "echo TROSHKA_BEGIN\n")
        _console_ws_read(ws, 1)
        _console_ws_send(ws, f"({command}) 2>&1; echo TROSHKA_END $?\n")

        output = ""
        deadline = time.time() + min(timeout, 300)
        while time.time() < deadline:
            chunk = _console_ws_read(ws, 2)
            output += chunk
            if "TROSHKA_END" in output:
                break

        body, exit_code = _parse_console_output(output)

        return {
            "output": body,
            "error": "",
            "exit_code": exit_code,
            "method": "console",
        }
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
